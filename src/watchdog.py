import os
import asyncio
from datetime import datetime
import requests

# Why watchdogs?
# Websocket connections to the UniFi Protect NVR can drop or the callback may be
# invoked from non-event-loop threads after reconnects. Without supervision,
# camera event processing can silently stop while the process remains alive.
# The watchdog keeps a heartbeat of messages and re-subscribes when the stream
# goes stale. We also keep a separate twice-daily status task to verify the app
# is still online even when no events occur.

WEBSOCKET_STALE_SECONDS = int(os.getenv("WEBSOCKET_STALE_SECONDS", "300"))
WEBSOCKET_INITIAL_TIMEOUT = int(os.getenv("WEBSOCKET_INITIAL_TIMEOUT", "60"))  # Time to wait for first message
HEALTHCHECK_URL = os.getenv("HEALTHCHECK_URL")
HEALTHCHECK_INTERVAL_SECONDS = int(os.getenv("HEALTHCHECK_INTERVAL_SECONDS", "300"))

_last_ws_message_at = None
_connection_start_time = None

def update_ws_heartbeat(now: datetime):
    global _last_ws_message_at, _connection_start_time
    _last_ws_message_at = now
    # Initialize connection start time on first heartbeat
    if _connection_start_time is None:
        _connection_start_time = now

def reset_connection_start_time(now: datetime):
    """Reset connection start time when reconnecting."""
    global _connection_start_time
    _connection_start_time = now

def make_sync_callback(loop, async_callback, logger=None):
    def sync_callback(msg):
        try:
            loop.call_soon_threadsafe(asyncio.create_task, async_callback(msg))
        except Exception as e:
            if logger:
                logger.error(f"Failed to schedule callback on event loop: {e}")
    return sync_callback

async def ping_url(url, logger=None):
    try:
        await asyncio.to_thread(requests.get, url, timeout=10)
    except Exception as e:
        if logger:
            logger.warning(f"Healthcheck ping failed: {e}")

async def run_healthcheck_pings(logger, tz):
    """Ping HEALTHCHECK_URL periodically while the websocket is healthy.

    Pings stop if the process dies, hangs, or loses its websocket, which lets
    the external monitor raise an alert.
    """
    if not HEALTHCHECK_URL:
        return
    while True:
        now = datetime.now(tz) if tz is not None else datetime.now()
        if _last_ws_message_at is not None and (now - _last_ws_message_at).total_seconds() <= WEBSOCKET_STALE_SECONDS:
            await ping_url(HEALTHCHECK_URL, logger)
        elif logger:
            logger.warning("Skipping healthcheck ping: websocket is stale")
        await asyncio.sleep(HEALTHCHECK_INTERVAL_SECONDS)

class WebsocketWatchdog:
    def __init__(self, protect, unsubscribe_fn, loop, logger, tz, callback):
        self.protect = protect
        self.unsubscribe_fn = unsubscribe_fn
        self.callback = callback
        self.loop = loop
        self.logger = logger
        self.tz = tz
        self.reconnect_failure_count = 0
        self.max_reconnect_failures = 3

    async def run(self):
        check_interval = max(10, min(60, WEBSOCKET_STALE_SECONDS // 3 if WEBSOCKET_STALE_SECONDS > 0 else 30))
        while True:
            try:
                await asyncio.sleep(check_interval)
                now = datetime.now(self.tz) if self.tz is not None else datetime.now()
                
                # Check if connection is stale
                is_stale = False
                if _last_ws_message_at is None:
                    # No heartbeat yet - check connection age
                    if _connection_start_time is not None:
                        connection_age = (now - _connection_start_time).total_seconds()
                        if connection_age > WEBSOCKET_INITIAL_TIMEOUT:
                            is_stale = True
                            if self.logger:
                                self.logger.warning(f"Websocket never received messages after {int(connection_age)}s. Re-subscribing...")
                    else:
                        # Connection start time not set yet, skip check
                        continue
                else:
                    # Check if heartbeat is stale
                    elapsed = (now - _last_ws_message_at).total_seconds()
                    if elapsed > WEBSOCKET_STALE_SECONDS:
                        is_stale = True
                        if self.logger:
                            self.logger.warning(f"Websocket appears stale (no messages for {int(elapsed)}s). Re-subscribing...")
                
                if is_stale:
                    try:
                        if self.unsubscribe_fn:
                            self.unsubscribe_fn()
                    except Exception as e_unsub:
                        if self.logger:
                            self.logger.warning(f"Error during websocket unsubscribe: {e_unsub}")
                    try:
                        await self.protect.update()
                    except Exception as e_upd:
                        if self.logger:
                            self.logger.warning(f"Error during protect.update() before resubscribe: {e_upd}")
                    try:
                        cb = make_sync_callback(self.loop, self.callback, self.logger)
                        self.unsubscribe_fn = self.protect.subscribe_websocket(cb)
                        reset_connection_start_time(now)
                        update_ws_heartbeat(now)
                        self.reconnect_failure_count = 0  # Reset on successful reconnect
                        if self.logger:
                            self.logger.info("Websocket re-subscribed successfully")
                    except Exception as e_sub:
                        self.reconnect_failure_count += 1
                        if self.logger:
                            self.logger.error(f"Failed to re-subscribe websocket (attempt {self.reconnect_failure_count}/{self.max_reconnect_failures}): {e_sub}")
                        if self.reconnect_failure_count >= self.max_reconnect_failures:
                            if self.logger:
                                self.logger.error(f"Websocket reconnection failed {self.max_reconnect_failures} times consecutively. Exiting to trigger container restart.")
                            # Exit the application to trigger container restart
                            import sys
                            sys.exit(1)
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Watchdog encountered an error: {e}")

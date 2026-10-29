import asyncio
import os
import argparse
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta
import pytz
from uiprotect import ProtectApiClient
from events import display_event_history
from images import save_camera_image
from alerts import decide
from uiprotect.data.websocket import WSAction, WSSubscriptionMessage
from uiprotect.data.devices import Camera
from pushover import send_notification
from watchdog import (
    WebsocketWatchdog,
    make_sync_callback,
    update_ws_heartbeat,
    run_healthcheck_pings,
)
from motion_monitor import MotionMonitor

logger = None
args = None

camera_filter = None
test_mode = False  # Set to True to analyze all images
protect = None

# Protect only sends a camera update when motion starts, so sample frames ourselves while it lasts.
FRAME_INTERVAL_SECONDS = 2
MAX_WATCH_SECONDS = 120
_watching = set()  # camera ids currently being sampled
motion_monitor = None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
UNIFI_USERNAME = os.getenv("UNIFI_USERNAME")
UNIFI_PASSWORD = os.getenv("UNIFI_PASSWORD")
UNIFI_HOST = os.getenv("UNIFI_HOST", "192.168.1.1")  # Default value if not set
UNIFI_PORT = int(os.getenv("UNIFI_PORT", "443"))  # Default value if not set
CAMERA_FILTER = os.getenv("CAMERA_FILTER")  # Optional camera filter
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN")
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY")
 

# Timezone configuration (optional). If not set or invalid, system local time is used.
TIMEZONE = os.getenv("TIMEZONE", "").strip("'\"")
try:
    TZ = pytz.timezone(TIMEZONE) if TIMEZONE else None
except pytz.exceptions.UnknownTimeZoneError:
    TZ = None

# Set up logging
def setup_logging(quiet=False):
    # Create logs directory if it doesn't exist
    os.makedirs('log', exist_ok=True)
    
    # Configure logging
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Create custom formatter that uses configured timezone for asctime
    class TimezoneFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            dt = datetime.fromtimestamp(record.created)
            if TZ is not None:
                dt = dt.astimezone(TZ)
            if datefmt:
                return dt.strftime(datefmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")

    formatter = TimezoneFormatter('%(asctime)s | %(message)s', datefmt='%H:%M:%S')
    
    # Console handler (only if not quiet)
    if not quiet:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
    
    # File handler with daily rotation
    file_handler = TimedRotatingFileHandler(
        'log/camera_app.log',
        when='midnight',
        interval=1,
        backupCount=0  # Keep all logs indefinitely
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    return logger

def check_openai_key():
    """Check if OpenAI API key is set."""
    if not OPENAI_API_KEY:
        logger.error("OPENAI_API_KEY not set")
        exit(1)

def check_credentials(notify_enabled=False):
    """Check if all required credentials are set."""
    missing_vars = []
    if not OPENAI_API_KEY:
        missing_vars.append("OPENAI_API_KEY")
    if not UNIFI_USERNAME:
        missing_vars.append("UNIFI_USERNAME")
    if not UNIFI_PASSWORD:
        missing_vars.append("UNIFI_PASSWORD")
    
    # Only check Pushover credentials if notifications are enabled
    if notify_enabled:
        if not PUSHOVER_API_TOKEN:
            missing_vars.append("PUSHOVER_API_TOKEN")
        if not PUSHOVER_USER_KEY:
            missing_vars.append("PUSHOVER_USER_KEY")
    
    if missing_vars:
        logger.error(f"The following environment variables are not set: {', '.join(missing_vars)}")
        logger.error("Please set them in your environment or .env file")
        exit(1)

# --- Scheduled exit task ------------------------------------------------------
async def run_scheduled_exit():
    """Exit the application daily at 8pm to allow container restart.
    
    Uses configured TZ if provided; falls back to local system time.
    """
    while True:
        now = datetime.now(TZ) if TZ is not None else datetime.now()
        # Target exit time is 8pm (20:00)
        exit_time = now.replace(hour=20, minute=0, second=0, microsecond=0)
        
        # If it's already past 8pm today, schedule for tomorrow
        if now >= exit_time:
            exit_time = exit_time + timedelta(days=1)
        
        sleep_seconds = max(0, (exit_time - now).total_seconds())
        logger.info(f"Scheduled exit at {exit_time.strftime('%Y-%m-%d %H:%M:%S')} (in {int(sleep_seconds)} seconds)")
        
        try:
            await asyncio.sleep(sleep_seconds)
        except Exception:
            # If sleep is interrupted, continue loop to recompute
            continue
        
        logger.info("Scheduled exit triggered - exiting gracefully")
        # Exit the application to trigger container restart
        import sys
        sys.exit(0)

async def motion_active(camera):
    """Ask Protect whether the camera currently reports motion."""
    # uiprotect's private Camera model no longer exposes isMotionDetected, so read the raw camera JSON.
    try:
        raw = await protect.api_request_obj(f"cameras/{camera.id}")
    except Exception as e:
        logger.warning(f"Could not read motion state for camera {camera.name}: {e}")
        return False
    return bool(raw.get("isMotionDetected") or raw.get("isSmartDetected"))

async def process_frame(camera):
    """Capture one frame, classify it and send a notification if needed."""
    now = datetime.now(TZ) if TZ is not None else datetime.now()
    try:
        image_path = await save_camera_image(protect, camera, timestamp=now)
        if not image_path:
            return
        decision = await decide(image_path, OPENAI_API_KEY, now)
        motion_monitor.record_motion()

        probabilities = " ".join(f"{k}={v:.2f}" for k, v in decision.probabilities.items())
        notification = decision.notification
        if notification is None:
            logger.info(f"{camera.name}: {decision.subject} ({probabilities}), no notification")
            return
        logger.info(f"{camera.name}: {decision.subject} ({probabilities}), priority {notification.priority}: {notification.message}")
        if args.notify:
            send_notification(
                f"{camera.name}: {notification.message}",
                PUSHOVER_API_TOKEN,
                PUSHOVER_USER_KEY,
                priority=notification.priority,
                title=notification.title,
                attachment=image_path
            )
    except Exception as e:
        timestamp = now.strftime("%m-%d %H:%M:%S")
        with open('log/error.log', 'a') as error_log:
            error_log.write(f"{timestamp} - Error processing image from {camera.name}: {e}\n")
        logger.error(f"Error processing image from {camera.name}: {e}")

async def watch_camera(camera):
    """Keep sampling frames while Protect reports motion, so a misread first frame gets a second look."""
    start = datetime.now()
    frames = 0
    while True:
        await process_frame(camera)
        frames += 1
        elapsed = (datetime.now() - start).total_seconds()
        if test_mode or elapsed > MAX_WATCH_SECONDS or not await motion_active(camera):
            break
        await asyncio.sleep(FRAME_INTERVAL_SECONDS)
    logger.info(f"{camera.name}: motion ended after {frames} frame(s) in {elapsed:.0f}s")

# subscribe to Websocket for updates to UFPs
async def callback(msg: WSSubscriptionMessage):
    update_ws_heartbeat(datetime.now(TZ) if TZ is not None else datetime.now())

    if msg.action != WSAction.UPDATE:
        return
    camera = getattr(msg, "new_obj", None)
    if not isinstance(camera, Camera):
        return
    if camera_filter is not None and camera.name != camera_filter:
        return
    if camera.id in _watching:
        return

    _watching.add(camera.id)
    try:
        if test_mode or await motion_active(camera):
            await watch_camera(camera)
    finally:
        _watching.discard(camera.id)

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description='UniFi Camera Security Monitor')
    parser.add_argument('--test', action='store_true',
                      help='Enable test mode to analyze all images')
    parser.add_argument('--quiet', action='store_true',
                      help='Disable console output (logs will still be written to file)')
    parser.add_argument('--notify', action='store_true',
                      help='Enable Pushover notifications for alarms')
    parser.add_argument('--testalarm', action='store_true',
                      help='Send a test alarm notification and exit')
    parser.add_argument('--scheduled-exit', action='store_true',
                      help='Exit daily at 8pm to allow container restart')
    return parser.parse_args()

# --- Main function ------------------------------------------------------------

async def main():
    global camera_filter
    global protect
    global test_mode
    global logger
    global args
    global motion_monitor

    # Parse command line arguments
    args = parse_args()
    test_mode = args.test

    # Set up logging
    logger = setup_logging(quiet=args.quiet)
    logger.info("Starting camera app")

    # Check credentials
    check_credentials(notify_enabled=args.notify)

    # Timezone is not used; using local system time for all operations.

    if args.testalarm:
        logger.info("Sending test alarm notification...")
        send_notification(
            "Manual test of the alarm function",
            PUSHOVER_API_TOKEN,
            PUSHOVER_USER_KEY,
            priority=1,
            title="ALARM: This is just a test"
        )
        logger.info("Test alarm sent successfully")
        return

    if test_mode:
        logger.warning("Test mode enabled - sending all images to OpenAI for analysis, this can get expensive!")

    camera_filter = CAMERA_FILTER

    if args.notify:
        logger.info("Pushover notifications enabled")

    def notify(message, title, priority):
        if args.notify:
            send_notification(message, PUSHOVER_API_TOKEN, PUSHOVER_USER_KEY, priority=priority, title=title)

    motion_monitor = MotionMonitor(notify)

    # Start scheduled exit task if enabled
    if args.scheduled_exit:
        logger.info("Scheduled exit enabled - will exit daily at 8pm")
        asyncio.create_task(run_scheduled_exit())

    protect = ProtectApiClient(
        UNIFI_HOST,
        UNIFI_PORT,
        UNIFI_USERNAME,
        UNIFI_PASSWORD,
        verify_ssl=False
    )

    await protect.update()

    cam_string = ""
    for camera in protect.bootstrap.cameras.values():
        cam_string += camera.name + ", "

    # get names of your cameras
    if camera_filter:
        logger.info("Monitoring camera:   " + camera_filter)
    else:
        logger.info("Monitoring cameras: "+cam_string)
    
    # Capture event loop and create thread-safe callback
    loop = asyncio.get_running_loop()
    cb = make_sync_callback(loop, callback, logger)
    ws_unsubscribe = protect.subscribe_websocket(cb)
    # Initialize heartbeat timestamp when websocket is subscribed
    now_for_init = datetime.now(TZ) if TZ is not None else datetime.now()
    update_ws_heartbeat(now_for_init)
    logger.info("Websocket subscribed, heartbeat initialized")
    # Start websocket watchdog in background
    asyncio.create_task(WebsocketWatchdog(protect, ws_unsubscribe, loop, logger, TZ, callback).run())
    asyncio.create_task(run_healthcheck_pings(logger, TZ))
    asyncio.create_task(motion_monitor.run())
    
    while True:
        await asyncio.sleep(1)

    # Close the websocket connection
    if ws_unsubscribe:
        ws_unsubscribe()

if __name__ == "__main__":
    asyncio.run(main()) 
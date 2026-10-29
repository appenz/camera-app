import asyncio
import json
import logging
import os
import time

logger = logging.getLogger()

NO_MOTION_ALERT_HOURS = int(os.getenv("NO_MOTION_ALERT_HOURS", "12"))
CHECK_INTERVAL_SECONDS = 600
# Kept in log/ so it survives the daily 8pm restart; a redeploy starts the clock fresh.
STATE_FILE = "log/motion_state.json"


class MotionMonitor:
    """Warns via Pushover when no motion event has been processed for NO_MOTION_ALERT_HOURS.

    Catches the case where the app is up (so the Healthchecks ping keeps arriving)
    but camera events are no longer received or can't be classified.
    """

    def __init__(self, notify):
        self.notify = notify
        state = self._load()
        self.last_motion = state.get("last_motion", time.time())
        self.last_warning = state.get("last_warning")
        self._save()

    def record_motion(self):
        self.last_motion = time.time()
        if self.last_warning is not None:
            self.last_warning = None
            logger.info("Motion events received again after no-motion warning")
            self.notify("Motion events are being received again.", "Camera app recovered", -1)
        self._save()

    async def run(self):
        limit = NO_MOTION_ALERT_HOURS * 3600
        while True:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            now = time.time()
            if now - self.last_motion < limit:
                continue
            if self.last_warning is not None and now - self.last_warning < limit:
                continue
            hours = (now - self.last_motion) / 3600
            logger.warning(f"No motion events for {hours:.1f} hours")
            self.notify(
                f"No motion events processed for {hours:.0f} hours. The app is running but may not be "
                "receiving camera events, or image classification is failing.",
                "Camera app: no motion",
                0,
            )
            self.last_warning = now
            self._save()

    def _load(self):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self):
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump({"last_motion": self.last_motion, "last_warning": self.last_warning}, f)

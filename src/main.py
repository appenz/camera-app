import asyncio
import os
import argparse
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta
import pytz
from uiprotect import ProtectApiClient
from events import display_event_history
from images import save_camera_image, analyze_image, process_camera_image, compare_description
from uiprotect.data.websocket import WSAction, WSSubscriptionMessage
from uiprotect.data.devices import Camera
from pushover import send_notification
from watchdog import WebsocketWatchdog, make_sync_callback, run_twice_daily_status, update_ws_heartbeat

logger = None
args = None

camera_filter = None
test_mode = False  # Set to True to analyze all images
protect = None
custom_instructions = None

# Track last notification times for backoff logic
last_notification_time = None
last_alarm_time = None

# Track last person notification (alarm or observation) per camera for dedupe
last_person_notification_by_camera = {}

# Dedupe window in minutes for person notifications
PERSON_DEDUPE_MINUTES = 10


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

# Nighttime window for people high priority alarms (interpreted in TIMEZONE if set, else local system time)
NIGHT_START = (0, 0)    # 00:00
NIGHT_END = (4, 30)     # 04:30

def is_night(now=None):
    """Return True if the given time is within the night window.
    If now is None, uses current local time. Supports windows that span midnight.
    """
    if now is None:
        now = datetime.now(TZ) if TZ is not None else datetime.now()
    minutes = now.hour * 60 + now.minute
    start_minutes = NIGHT_START[0] * 60 + NIGHT_START[1]
    end_minutes = NIGHT_END[0] * 60 + NIGHT_END[1]

    # Normal window (e.g., 00:00-05:30)
    if start_minutes <= end_minutes:
        return start_minutes <= minutes < end_minutes
    # Wrap-around window (e.g., 23:00-01:00)
    return minutes >= start_minutes or minutes < end_minutes

def is_person_event(first_line: str) -> bool:
    fl = first_line.upper()
    return fl.startswith("ALARM PERSON") or fl.startswith("OBSERVATION PERSON")

def load_instructions():
    """Load instructions from file, ignoring comment lines."""
    instructions = []
    try:
        with open('instructions.txt', 'r') as f:
            for line in f:
                if not line.startswith('#'):
                    instructions.append(line)
    except FileNotFoundError:
        logger.warning("No instructions.txt file found. Using default instructions.")
        return None
    return ''.join(instructions)

# Base prompt with general instructions

base_prompt = """
You are a security agent verifying a camera feed. Your job is to detect specific events and report them.
Your output should always be exactly two lines:
1. First line: One of these in CAPS:
   - ALARM <type> for urgent situations
   - OBSERVATION <type> for non-urgent observations
   - NOTHING TO REPORT when everything is normal
2. Second line: A brief description of what you see

The current time is {time}.

{instructions}
"""

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

# --- Twice-daily status notification -----------------------------------------
async def run_twice_daily_status():
    """Send a low-priority "System online" notification at 08:00 and 20:00.

    Uses configured TZ if provided; falls back to local system time.
    """
    while True:
        now = datetime.now(TZ) if TZ is not None else datetime.now()
        morning = now.replace(hour=8, minute=0, second=0, microsecond=0)
        evening = now.replace(hour=20, minute=0, second=0, microsecond=0)

        if now < morning:
            next_run = morning
        elif now < evening:
            next_run = evening
        else:
            next_run = morning + timedelta(days=1)

        sleep_seconds = max(0, (next_run - now).total_seconds())
        logger.info(f"Next status check scheduled for {next_run.strftime('%Y-%m-%d %H:%M:%S')}")
        try:
            await asyncio.sleep(sleep_seconds)
        except Exception:
            # If sleep is interrupted for any reason, continue loop to recompute
            continue

        # Send the status notification only if notifications are enabled
        try:
            if args and getattr(args, 'notify', False):
                send_notification(
                    "System online",
                    PUSHOVER_API_TOKEN,
                    PUSHOVER_USER_KEY,
                    priority=-1
                )
                logger.info("Sent twice-daily status notification: System online")
        except Exception as e:
            logger.error(f"Failed to send status notification: {e}")

# --- Scheduled exit task ------------------------------------------------------
async def run_scheduled_exit():
    """Exit the application daily at 8pm to allow container restart.
    
    This task waits for the 8pm status notification to complete, then exits.
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
        
        # Wait a bit for the 8pm status notification to complete (if enabled)
        # The status notification runs at 8pm, so we wait 5 seconds after 8pm
        await asyncio.sleep(5)
        
        logger.info("Scheduled exit triggered - exiting gracefully")
        # Exit the application to trigger container restart
        import sys
        sys.exit(0)

# subscribe to Websocket for updates to UFPs
async def callback(msg: WSSubscriptionMessage):
    global filters
    global protect
    global prompt
    global args
    global last_notification_time
    global last_alarm_time
    global custom_instructions
    global last_observation_by_camera

    now_for_display = datetime.now(TZ) if TZ is not None else datetime.now()
    # Update websocket heartbeat
    update_ws_heartbeat(now_for_display)
    timestamp = now_for_display.strftime("%m-%d %H:%M:%S")
    current_time = now_for_display.strftime("%A %H:%M")  # %A gives full weekday name
    formatted_prompt = base_prompt.format(time=current_time, instructions=custom_instructions if custom_instructions else "")

    # Handle Initialization
    if msg.action == WSAction.ADD:
        return

    # Handle Updates, i.e. camera and NVR events
    elif msg.action == WSAction.UPDATE:
        if not hasattr(msg, 'new_obj'):
            logger.debug(f"update: {msg.id} - No new_obj found")
            return

        # Handle Camera Events
        if isinstance(msg.new_obj, Camera):
            camera = msg.new_obj
            camera_name = camera.name

            if camera_filter is None or camera_name == camera_filter:
                # Skip if not in test mode and no motion detected
                if not test_mode and not camera.is_motion_detected and not camera.is_smart_detected:
                    return
                
                # Process the image
                try:
                    analysis, image_path = await process_camera_image(protect, camera, formatted_prompt, OPENAI_API_KEY, test_mode)
                    if not analysis:
                        logger.error(f"no analysis for image from {camera.name}.")
                        return
                    else:
                        # Log the analysis
                        single_line_analysis = analysis.strip().replace('\n', ' ')
                        logger.info(f"{camera.name}: {single_line_analysis}")
                        
                        # Send notification if it's an alarm or observation and notifications are enabled
                        if args.notify and not analysis.startswith("NOTHING TO REPORT"):
                            current_time = datetime.now(TZ) if TZ is not None else datetime.now()
                            lines = analysis.strip().split('\n')
                            first_line = lines[0].strip()
                            message = lines[1] if len(lines) > 1 else ""
                            
                            # Determine notification priority and apply backoff logic
                            if analysis.startswith("ALARM"):
                                # If an alarm was sent in the last minute, downgrade to normal priority
                                if last_alarm_time and (current_time - last_alarm_time) < timedelta(minutes=1):
                                    priority = 0
                                    logger.info(f"Skipping notification (alarm) within backoff period")
                                else:
                                    # For person, hard limit to only send a priority 1 notification at night
                                    if first_line.upper().startswith("ALARM PERSON"):
                                        if not is_night(current_time):
                                            logger.info(f"Downgrading alarm, t={current_time}")
                                            priority = 0
                                        else:
                                            logger.info(f"Triggering alarm, t={current_time}")
                                            priority = 1
                                    else:
                                        priority = 1
                                    last_alarm_time = current_time
                            elif analysis.startswith("OBSERVATION"):
                                # Skip non-alarm notifications if any notification was sent in the last 10 seconds
                                if last_notification_time and (current_time - last_notification_time) < timedelta(seconds=10):
                                    logger.info(f"Skipping notification (observation) within backoff period")
                                    return
                                priority = -2
                            else:
                                priority = -2

                            # Unified person dedupe (applies to both alarm and observation)
                            if is_person_event(first_line):
                                prev = last_person_notification_by_camera.get(camera_name)
                                if prev and (current_time - prev["timestamp"]) < timedelta(minutes=PERSON_DEDUPE_MINUTES):
                                    if message and compare_description(prev["description"], message, OPENAI_API_KEY):
                                        logger.info(f"Skipping notification (person dedupe) within {PERSON_DEDUPE_MINUTES} minutes for {camera_name}")
                                        return

                            # Only proceed if we're not skipping due to backoff
                            if priority is not None:
                                lines = analysis.strip().split('\n')
                                title = lines[0].lower().capitalize()
                                message = lines[1] if len(lines) > 1 else ""
                                
                                send_notification(
                                    f"{camera.name}: {message}", 
                                    PUSHOVER_API_TOKEN, 
                                    PUSHOVER_USER_KEY,
                                    priority=priority,
                                    title=title,
                                    attachment=image_path
                                )
                                last_notification_time = current_time
                                # Store last person description for dedupe across window
                                if is_person_event(first_line):
                                    last_person_notification_by_camera[camera_name] = {"description": message, "timestamp": current_time}
                except Exception as e:
                    error_message = f"{timestamp} - Error processing image from {camera.name}: {str(e)}\n"
                    with open('log/error.log', 'a') as error_log:
                        error_log.write(error_message)
                    logger.error(f"Error processing image from {camera.name}: {str(e)}")

    else:
        # Not sure what this is...
        logger.debug(f"{msg.action}")

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
    global prompt
    global custom_instructions

    # Parse command line arguments
    args = parse_args()
    test_mode = args.test

    # Set up logging
    logger = setup_logging(quiet=args.quiet)
    logger.info("Starting camera app")

    # Load custom instructions if available
    custom_instructions = load_instructions()

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
    default_camera = None
    if camera_filter:
        protect = ProtectApiClient(
            UNIFI_HOST,
            UNIFI_PORT,
            UNIFI_USERNAME,
            UNIFI_PASSWORD,
            verify_ssl=False
        )
        await protect.update()
        default_camera = next((cam for cam in protect.bootstrap.cameras.values() if cam.name == camera_filter), None)

    if args.notify:
        logger.info("Pushover notifications enabled")
        # Send a startup notification
        message = "Camera app started and monitoring for events"
        image_path = None
        
        # Try to get an image if we have a default camera
        if default_camera:
            image_path = await save_camera_image(protect, default_camera, test_mode=True)
            if not image_path:
                message += " (no image available)"
        
        send_notification(
            message,
            PUSHOVER_API_TOKEN,
            PUSHOVER_USER_KEY,
            priority=-1,
            title="Camera App Started",
            attachment=image_path
        )

    # Start twice-daily status notifications in background
    asyncio.create_task(run_twice_daily_status())
    
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
    asyncio.create_task(WebsocketWatchdog(protect, ws_unsubscribe, loop, logger, TZ).run())
    
    while True:
        await asyncio.sleep(1)

    # Close the websocket connection
    if ws_unsubscribe:
        ws_unsubscribe()

if __name__ == "__main__":
    asyncio.run(main()) 
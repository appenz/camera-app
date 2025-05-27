import asyncio
import os
import argparse
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime, timedelta
import pytz
from uiprotect import ProtectApiClient
from events import display_event_history
from images import save_camera_image, analyze_image, process_camera_image
from uiprotect.data.websocket import WSAction, WSSubscriptionMessage
from uiprotect.data.devices import Camera
from pushover import send_notification

logger = None
args = None

camera_filter = None
test_mode = False  # Set to True to analyze all images
protect = None

# Track last notification times for backoff logic
last_notification_time = None
last_alarm_time = None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
UNIFI_USERNAME = os.getenv("UNIFI_USERNAME")
UNIFI_PASSWORD = os.getenv("UNIFI_PASSWORD")
UNIFI_HOST = os.getenv("UNIFI_HOST", "192.168.1.1")  # Default value if not set
UNIFI_PORT = int(os.getenv("UNIFI_PORT", "443"))  # Default value if not set
CAMERA_FILTER = os.getenv("CAMERA_FILTER")  # Optional camera filter
PUSHOVER_API_TOKEN = os.getenv("PUSHOVER_API_TOKEN")
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY")
TIMEZONE = os.getenv("TIMEZONE", "").strip("'\"")

def load_instructions():
    """Load instructions from file, ignoring comment lines."""
    instructions = []
    try:
        with open('instructions.txt', 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    instructions.append(line)
    except FileNotFoundError:
        logger.warning("No instructions.txt file found. Using default instructions.")
        return None
    return '\n'.join(instructions)

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

# Load custom instructions if available
custom_instructions = load_instructions()
prompt = base_prompt.format(instructions=custom_instructions if custom_instructions else "")

# Set up logging
def setup_logging(quiet=False):
    # Create logs directory if it doesn't exist
    os.makedirs('log', exist_ok=True)
    
    # Configure logging
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Create custom formatter that uses camera timezone
    class TimezoneFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):
            # Use record.created which is the timestamp when the log record was created
            dt = datetime.fromtimestamp(record.created)
            if TIMEZONE:
                try:
                    tz = pytz.timezone(TIMEZONE)
                    dt = dt.astimezone(tz)
                except pytz.exceptions.UnknownTimeZoneError:
                    pass
            if datefmt:
                return dt.strftime(datefmt)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    
    # Create formatters
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

def get_camera_time():
    """Get current time in camera's timezone."""
    if TIMEZONE:
        try:
            tz = pytz.timezone(TIMEZONE)
            return datetime.now(tz)
        except pytz.exceptions.UnknownTimeZoneError as e:
            logger.error(f"Invalid timezone: {TIMEZONE}. Error: {str(e)}. Using server's local time.")
            return datetime.now()
    return datetime.now()

# subscribe to Websocket for updates to UFPs
async def callback(msg: WSSubscriptionMessage):
    global filters
    global protect
    global prompt
    global args

    timestamp = get_camera_time().strftime("%m-%d %H:%M:%S")
    current_time = get_camera_time().strftime("%H:%M")
    formatted_prompt = prompt.format(time=current_time)

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
                    analysis = await process_camera_image(protect, camera, formatted_prompt, OPENAI_API_KEY, test_mode)
                    if not analysis:
                        logger.error(f"no analysis for image from {camera.name}.")
                        return
                    else:
                        # Log the analysis
                        single_line_analysis = analysis.strip().replace('\n', ' ')
                        logger.info(f"{camera.name}: {single_line_analysis}")
                        
                        # Send notification if it's an alarm or observation and notifications are enabled
                        if args.notify and not analysis.startswith("NOTHING TO REPORT"):
                            current_time = datetime.now()
                            
                            # Determine notification priority and apply backoff logic
                            if analysis.startswith("ALARM"):
                                # If an alarm was sent in the last minute, downgrade to normal priority
                                if last_alarm_time and (current_time - last_alarm_time) < timedelta(minutes=1):
                                    priority = 0
                                else:
                                    priority = 1
                                    last_alarm_time = current_time
                            elif analysis.startswith("OBSERVATION"):
                                # Skip non-alarm notifications if any notification was sent in the last 10 seconds
                                if last_notification_time and (current_time - last_notification_time) < timedelta(seconds=10):
                                    return
                                priority = -1
                            else:
                                priority = -2

                            # Only proceed if we're not skipping due to backoff
                            if priority is not None:
                                lines = analysis.strip().split('\n')
                                title = lines[0].lower().capitalize()
                                message = lines[1]
                                
                                send_notification(
                                    f"{camera.name}: {message}", 
                                    PUSHOVER_API_TOKEN, 
                                    PUSHOVER_USER_KEY,
                                    priority=priority,
                                    title=title
                                )
                                last_notification_time = current_time
                except Exception as e:
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
    return parser.parse_args()

# --- Main function ------------------------------------------------------------

async def main():
    global camera_filter
    global protect
    global test_mode
    global logger
    global args

    # Parse command line arguments
    args = parse_args()
    test_mode = args.test

    # Set up logging
    logger = setup_logging(quiet=args.quiet)
    logger.info("Starting camera app")

    # Check credentials
    check_credentials(notify_enabled=args.notify)

    # Check timezone
    if not TIMEZONE:
        logger.warning("No timezone set in environment. Using server's local time. Consider setting TIMEZONE in .env")
    elif TIMEZONE.upper() == 'UTC':
        logger.warning("Timezone is set to UTC. Consider setting a more specific timezone in .env")

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

    if args.notify:
        logger.info("Pushover notifications enabled")

    camera_filter = CAMERA_FILTER

    protect = ProtectApiClient(
        UNIFI_HOST,
        UNIFI_PORT,
        UNIFI_USERNAME,
        UNIFI_PASSWORD,
        verify_ssl=False
    )

    await protect.update() # this will initialize the protect .bootstrap and open a Websocket connection for updates

    cam_string = ""
    for camera in protect.bootstrap.cameras.values():
        cam_string += camera.name + ", "

    # get names of your cameras
    if camera_filter:
        logger.info("Monitoring camera:   " + camera_filter)
    else:
        logger.info("Monitoring cameras: "+cam_string)
    
    # Create a wrapper function that will await our async callback
    def sync_callback(msg):
        asyncio.create_task(callback(msg))
    
    unsub = protect.subscribe_websocket(sync_callback)
    
    while True:
        await asyncio.sleep(1)

    # Close the websocket connection
    unsub()

if __name__ == "__main__":
    asyncio.run(main()) 
import os
import time
import asyncio
import base64
import logging
from datetime import datetime, timedelta
import pytz
TIMEZONE = os.getenv("TIMEZONE", "").strip("'\"")
try:
    TZ = pytz.timezone(TIMEZONE) if TIMEZONE else None
except pytz.exceptions.UnknownTimeZoneError:
    TZ = None
from pathlib import Path
from openai import OpenAI

# Get the logger
logger = logging.getLogger()

OPENAI_MODEL = "gpt-6-luna"
OPENAI_REASONING_EFFORT = "low"

UNIFI_HOST = os.getenv("UNIFI_HOST", "192.168.1.1")
RTSPS_PORT = int(os.getenv("UNIFI_RTSPS_PORT", "7441"))
RTSP_TIMEOUT_SECONDS = 10
IMAGE_RETENTION_DAYS = 7
_last_prune = None

def get_image_filename(camera_name, timestamp):
    """Generate filename for camera images."""
    timestamp_str = timestamp.strftime("%Y-%m-%d_%H-%M-%S")
    return f"{camera_name}_{timestamp_str}.jpg"

async def get_rtsps_url(protect, camera):
    """RTSPS URL of the camera's highest-resolution stream, or None if no stream is shared."""
    # Read the raw camera JSON: uiprotect's channel model has changed across versions.
    data = await protect.api_request_obj(f"cameras/{camera.id}")
    channels = [c for c in data.get("channels", []) if c.get("isRtspEnabled") and c.get("rtspAlias")]
    if not channels:
        return None
    channel = max(channels, key=lambda c: c.get("width", 0))
    return f"rtsps://{UNIFI_HOST}:{RTSPS_PORT}/{channel['rtspAlias']}"

async def save_rtsp_frame(protect, camera, filepath):
    """Grab one full-resolution frame from the camera's RTSPS stream. Returns True on success."""
    try:
        url = await get_rtsps_url(protect, camera)
    except Exception as e:
        logger.warning(f"Could not look up RTSP stream for camera {camera.name}: {e}")
        return False
    if url is None:
        logger.warning(f"No RTSP-enabled channel on camera {camera.name}")
        return False
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-v", "error", "-rtsp_transport", "tcp", "-i", url,
        "-frames:v", "1", "-q:v", "4", str(filepath),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), RTSP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning(f"RTSP frame grab timed out for camera {camera.name}")
        return False
    if proc.returncode != 0 or not filepath.exists():
        # The URL contains the stream's access token; keep it out of the log.
        error = stderr.decode(errors="replace").replace(url, "<rtsp url>").strip()[-200:]
        logger.warning(f"RTSP frame grab failed for camera {camera.name}: {error}")
        return False
    return True

async def get_snapshot(camera):
    """Get a snapshot from Protect. Protect always returns 640x360, whatever size is requested."""
    try:
        image = await camera.get_snapshot()
    except Exception as e:
        logger.error(f"Snapshot request failed for camera {camera.name}: {e}")
        image = None

    if image is None:
        timestamp = (datetime.now(TZ) if TZ is not None else datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
        error_message = f"{timestamp} - No image received from camera {camera.name}\n"
        with open('log/error.log', 'a') as error_log:
            error_log.write(error_message)
        logger.error(f"No image received from camera {camera.name}")

    return image

def prune_images(images_dir, now):
    """Delete saved camera images older than IMAGE_RETENTION_DAYS, at most once an hour."""
    global _last_prune
    if _last_prune is not None and now - _last_prune < timedelta(hours=1):
        return
    _last_prune = now
    cutoff = time.time() - IMAGE_RETENTION_DAYS * 86400
    for path in images_dir.glob("*.jpg"):
        if not path.name.startswith("test_") and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)

async def save_camera_image(protect, camera, timestamp=None, test_mode=False):
    """Save camera image with timestamp-based filename."""
    images_dir = Path("images")
    images_dir.mkdir(exist_ok=True)

    if timestamp is None:
        timestamp = datetime.now(TZ) if TZ is not None else datetime.now()
    prune_images(images_dir, timestamp)

    filepath = images_dir / get_image_filename(camera.name, timestamp)
    try:
        if await save_rtsp_frame(protect, camera, filepath):
            return str(filepath)
    except Exception as e:
        logger.warning(f"RTSP frame grab failed for camera {camera.name}: {e}")

    logger.warning(f"Falling back to 640x360 snapshot for camera {camera.name}")
    image = await get_snapshot(camera)
    if image is None:
        return None
    with open(filepath, "wb") as f:
        f.write(image)
    return str(filepath)

async def analyze_image(image_path, prompt, api_key):
    """Analyze image using OpenAI SDK."""
    try:
        # Encode image to base64 data URL
        with open(image_path, "rb") as f:
            base64_image = base64.b64encode(f.read()).decode("utf-8")

        client = OpenAI(api_key=api_key, timeout=20.0)

        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            reasoning_effort=OPENAI_REASONING_EFFORT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                        },
                    ],
                }
            ],
            max_completion_tokens=2000,
        )

        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"analyze_image exception: {e}")
        return None

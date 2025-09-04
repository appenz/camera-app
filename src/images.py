import os
import time
import base64
import requests
import traceback
import logging
from datetime import datetime
import pytz
from pathlib import Path
from typing import Any
from openai import OpenAI

# Get the logger
logger = logging.getLogger()

# Get timezone from environment
TIMEZONE = os.getenv("TIMEZONE")

def get_camera_time():
    """Get current time in camera's timezone."""
    if TIMEZONE:
        try:
            tz = pytz.timezone(TIMEZONE)
            return datetime.now(tz)
        except pytz.exceptions.UnknownTimeZoneError:
            logger.error(f"Invalid timezone: {TIMEZONE}. Using server's local time.")
            return datetime.now()
    return datetime.now()

def get_image_filename(camera_name, timestamp):
    """Generate filename for camera images."""
    timestamp_str = timestamp.strftime("%Y-%m-%d_%H-%M-%S")
    return f"{camera_name}_{timestamp_str}.jpg"

async def get_high_quality_snapshot(protect, camera):
    """Get high quality snapshot from camera."""
    camera_id = camera.id
    path = "snapshot"
    params: dict[str, Any] = {}
    params["ts"] = int(time.time() * 1000)
    params["force"] = "true"
    params["highQuality"] = "true"
    image = await protect.api_request_raw(
            f"cameras/{camera_id}/{path}",
            params=params,
            raise_exception=False,
        )
    
    if image is None:
        timestamp = get_camera_time().strftime("%Y-%m-%d %H:%M:%S")
        error_message = f"{timestamp} - No image received from camera {camera.name}\n"
        with open('log/error.log', 'a') as error_log:
            error_log.write(error_message)
        logger.error(f"No image received from camera {camera.name}")
    
    return image

async def save_camera_image(protect, camera, timestamp=None, test_mode=False):
    """Save camera image with timestamp-based filename."""
    # Create images directory
    images_dir = Path("images")
    images_dir.mkdir(exist_ok=True)
    
    # Get timestamp
    if timestamp is None:
        timestamp = get_camera_time()
    
    # Save image
    filename = get_image_filename(camera.name, timestamp)
    filepath = images_dir / filename
    
    # Get image at highest quality resolution
    image = await get_high_quality_snapshot(protect, camera)

    if image is None:
        return None

    with open(filepath, "wb") as f:
        f.write(image)
    
    return str(filepath)

async def analyze_image(image_path, prompt, api_key):
    """Analyze image using OpenAI API."""
    # Encode image
    with open(image_path, "rb") as f:
        base64_image = base64.b64encode(f.read()).decode('utf-8')
    
    # Call OpenAI API
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}"
    }
    
    payload = {
        "model": "gpt-5-mini",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{base64_image}"
                    }
                }
            ]
        }],
        "max_tokens": 1000
    }
    
    # Get response
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers=headers,
        json=payload
    )
    
    if response.status_code == 200:
        return response.json()['choices'][0]['message']['content']
    else:
        print(f"Error: {response.status_code}")
        return None

async def process_camera_image(protect, camera, prompt, api_key, test_mode=False):
    """Save and analyze a camera image.
    
    Args:
        protect: ProtectApiClient instance
        camera: Camera instance
        prompt: Formatted prompt for analysis
        api_key: OpenAI API key
        test_mode: Whether to force analysis regardless of motion detection
    
    Returns:
        tuple: (analysis_result, image_path) or (None, None) if image couldn't be saved
    """
    try:
        image_path = await save_camera_image(protect, camera, test_mode=test_mode)
        if not image_path:
            return None, None
        analysis = await analyze_image(image_path, prompt, api_key)
        return analysis, image_path
    except Exception as e:        
        # Get current timestamp
        timestamp = get_camera_time().strftime("%Y-%m-%d %H:%M:%S")
        
        # Format error message with timestamp and full traceback
        error_msg = f"\n[{timestamp}] Error processing image from camera {camera.name}:\n"
        error_msg += f"Exception: {str(e)}\n"
        error_msg += "Traceback:\n"
        error_msg += traceback.format_exc()
        error_msg += "\n" + "-"*80 + "\n"
        
        # Append to error log file
        with open('log/error.log', 'a') as f:
            f.write(error_msg)
        
        return None, None

def compare_description(desc_a: str, desc_b: str, api_key: str) -> bool:
    """Compare two person descriptions using OpenAI SDK to decide if they are the same person.
    Returns True if they likely refer to the same person, False otherwise.
    """
    try:
        client = OpenAI(api_key=api_key, timeout=10)

        system_prompt = (
            "You compare two short surveillance descriptions and answer with exactly one word: "
            "SAME if both clearly refer to the same person, otherwise DIFFERENT. "
        )

        user_prompt = (
            f"Description A: {desc_a}\n"
            f"Description B: {desc_b}\n\n"
            "Answer with exactly one word: SAME or DIFFERENT"
        )

        completion = client.chat.completions.create(
            model="gpt-5-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=5,
            temperature=0,
            n=1,
        )

        content = completion.choices[0].message.content.strip().upper()
        return "SAME" in content
    except Exception as e:
        logger.error(f"compare_description exception: {e}")
        return False


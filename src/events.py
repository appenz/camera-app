import os
from datetime import datetime, timedelta

def get_event_filename(protect, event):
    """Generate path for an event video."""
    # Get camera name
    camera_name = protect.bootstrap.cameras[event.camera_id].name
    
    # Format: YYYY-MM-DD-HHMMSS-CameraName.mp4
    local_time = event.start.astimezone(datetime.now().astimezone().tzinfo)
    filename = f"{local_time.strftime('%Y-%m-%d-%H%M%S')}-{camera_name}.mp4"
    return os.path.join("events", filename)

async def download_event_video(protect, event):
    """Download and save event video if not already exists."""
    # Skip motion events that are linked to smart detect events
    if event.type == "motion" and event.smart_detect_event_ids:
        print(f"Skipping motion event {event.id} (linked to smart detect)")
        return None
        
    filepath = get_event_filename(protect, event)
    
    # Skip if exists
    if os.path.exists(filepath):
        return filepath
    
    # Download video
    print(event)
    video_data = await event.get_video()
    
    if video_data:
        with open(filepath, "wb") as f:
            f.write(video_data)
        return filepath
    else:
        print(f"No video data available for {os.path.basename(filepath)}")
        return None

async def display_event_history(protect, camera_filter=None):
    """Display event history and download videos."""
    # Get events from the last 24 hours
    end = datetime.now()
    start = end - timedelta(days=1)
    
    events = await protect.get_events(start=start, end=end)
    print(f"Found {len(events)} events in the last 24 hours")
    
    # Print header
    print(f"{'Camera':<10} {'Time':<15} {'Event Type':<15} {'Length':<4}")
    print("-" * 60)
    
    # Get local timezone
    local_tz = datetime.now().astimezone().tzinfo
    
    for event in events:
        camera_name = protect.bootstrap.cameras[event.camera_id].name
        if camera_filter and camera_name != camera_filter:
            continue
        # Convert UTC to local time
        local_time = event.start.astimezone(local_tz)
        start_time = local_time.strftime("%m/%d %H:%M:%S")
        type = str(event.type)[10:].lower()
        if event.end is not None:
            length = int((event.end - event.start).total_seconds())
        else:
            length = 0

        # Print in aligned columns, and download each event video
        print(f"{camera_name:<10} {start_time:<15} {type:<15} {length:>3}s")
        await download_event_video(protect, event) 
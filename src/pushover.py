import requests
import os

def send_notification(message, api_token, user_key, priority=0, title=None, attachment=None):
    """Send a notification via Pushover.
    
    Args:
        message (str): The message to send
        api_token (str): Pushover application token
        user_key (str): Pushover user key
        priority (int): Message priority (-2 to 2)
            -2: Lowest priority
            -1: Low priority
             0: Normal priority (default)
             1: High priority
             2: Emergency priority
        title (str): Message title (optional)
        attachment (str): Path to image file (optional)
        
    Returns:
        bool: True if notification was sent successfully, False otherwise
    """
    if not api_token or not user_key:
        return False
        
    # Send notification
    try:
        data = {
            "token": api_token,
            "user": user_key,
            "message": message,
            "priority": priority
        }
        
        if title:
            data["title"] = title
            
        files = None
        if attachment:
            if not os.path.isfile(attachment):
                return False
            files = {
                "attachment": (os.path.basename(attachment), open(attachment, "rb"))
            }
            
        response = requests.post(
            "https://api.pushover.net/1/messages.json",
            data=data,
            files=files
        )
        
        # Close file if it was opened
        if files and "attachment" in files:
            files["attachment"][1].close()
            
        return response.status_code == 200
    except Exception:
        return False 
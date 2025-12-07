# AI Unifi Camera Security Monitor

A Python application that monitors UniFi Protect security cameras and uses OpenAI's GPT-4o Vision LLM to detect specific events. Thanks to the LLM, the rules for events can be very complex, i.e. you can monitor parking spots, look for Racoons or check the weather. If GPT-4o understands it, it will work.

The system analyzes camera feeds in real-time and can send notifications with images via Pushover when events are detected. It is written in python, runs on a host or in a Docker container, is open source (Apache 2.0) and relatively cheap to operate (for me about ~$0.25/day).

## Features

The application offers real-time monitoring of Unifi Protect security cameras. In a nutshell it:
- Monitors the cameras to see if an event is in progress
- If yes, it will take an image every 10 seconds and send it to an OpenAI image model for analysis
- What to look for in the image can be steered via a prompt (e.g. suspicious people, racoons, a parking spot being empty etc.)
- It supports monitoring multiple cameras or only a single one

The notifications you receive include images and look like this.

<img src="/doc/IMG_3725.jpg" alt="Unifi Camera LLM App Example" width="450">

## Setup

The application uses the `uv` package manager, which simplifies the setup process by handling most of the work, including installing dependencies. This means you don't have to manually manage Python packages or virtual environments, as `uv` takes care of it for you. Follow the steps below to get started:

1. Install the `uv` package manager if you haven't already. You can find the installation instructions on the [uv GitHub repository](https://github.com/astral-sh/uv).

2. Clone the repository and navigate into the project directory.

3. Install the dependencies:
   ```bash
   uv sync
   ```

4. Set up your environment variables as described in the `env.example` file. You can copy this file to `.env` and fill in your actual credentials. For details see below.

5. Do a test run of the app. This will print descriptions of any observations.
   ```bash
   uv run --env-file .env src/main.py --test
   ```

   **Note:** The `--env-file .env` flag tells `uv` to load environment variables from your `.env` file. This is the recommended way to run the application as it keeps your credentials secure and separate from your code.

## Custom Instructions

To tailor the event detection in the camera feed to your specific requirements, you can add custom instructions in the `instructions.txt` file. This file allows you to specify what events to monitor. Note that any line beginning with `#` is considered a comment and will be ignored. For guidance, refer to the example provided in `instructions.txt.example`.

## Notifications

The application can optionally use [Pushover](https://pushover.net/) for sending notifications. Pushover is a simple service that delivers notifications to your mobile devices and desktop. The system sends different types of notifications with varying priorities:

- **ALARM** (Priority 1): Urgent situations that require immediate attention
  - These notifications will make your device make a sound and vibrate

- **OBSERVATION** (Priority -2): Non-urgent observations
  - These notifications are delivered quietly without disturbing you

To avoid spam, the system uses backoff logic:
- **Non-Urgent (OBSERVATION)**: Skips notifications if sent in the last 10 seconds.
- **Urgent (ALARM)**: First ALARM per minute is high priority; subsequent ones are normal priority.

## Environment Variables

The application requires several environment variables to be set. You can set these either in a `.env` file or directly in your environment. Here's a complete list of all variables:

### Required Variables

- `OPENAI_API_KEY`: Your OpenAI API key for image analysis
- `UNIFI_USERNAME`: Your UniFi Protect username
- `UNIFI_PASSWORD`: Your UniFi Protect password

### Optional Variables

- `UNIFI_HOST`: UniFi Protect host address (default: "192.168.1.1")
- `UNIFI_PORT`: UniFi Protect port (default: 443)
- `CAMERA_FILTER`: Name of a specific camera to monitor (if not set, all cameras will be monitored)
- `TIMEZONE`: Timezone for camera location (e.g., 'America/Los_Angeles', 'Europe/London', 'Asia/Tokyo')
  - If not set, the server's local time will be used
  - UTC is not recommended; use a more specific timezone instead
  - Wrong time zone can confuse the LLM (e.g. image shows daytime, but time is night time)

### Pushover Notification Variables (Required if using `--notify`)

- `PUSHOVER_API_TOKEN`: Your Pushover application token
- `PUSHOVER_USER_KEY`: Your Pushover user key

Example `.env` file:
```bash
# OpenAI API key for image analysis
OPENAI_API_KEY=sk-your-openai-api-key

# UniFi Protect credentials
UNIFI_USERNAME=your-unifi-username
UNIFI_PASSWORD=your-unifi-password
UNIFI_HOST=192.168.1.1
UNIFI_PORT=443

# Pushover credentials for notifications
PUSHOVER_API_TOKEN=your-pushover-app-token
PUSHOVER_USER_KEY=your-pushover-user-key

# Optional: Filter to monitor only a specific camera
CAMERA_FILTER=FrontDoor

# Timezone for camera location
TIMEZONE=America/Los_Angeles
```

## Command Line Arguments

The application supports the following command line arguments:

- `--test`: Enable test mode to analyze all images (not just when motion is detected). Note: This can get expensive as it sends all images to OpenAI for analysis.
- `--quiet`: Disable console output (logs will still be written to file)
- `--notify`: Enable Pushover notifications for alarms and observations (default is off)
- `--testalarm`: Send a test alarm notification and exit (useful for testing notification setup)

**Note:** When using `uv run`, you can use the `--env-file .env` flag to automatically load environment variables from your `.env` file.

Example usage:
```bash
# Run with notifications enabled
uv run --env-file .env src/main.py --notify

# Run in test mode with notifications
uv run --env-file .env src/main.py --test --notify

```

## Setting Up Pushover

1. Create a Pushover account at [pushover.net](https://pushover.net/)
2. Install the Pushover app on your mobile device
3. Create a new application in your Pushover dashboard
4. Note down your:
   - User Key (found in your Pushover dashboard)
   - API Token (from your new application)
5. Add these to your `.env` file:
   ```bash
   PUSHOVER_API_TOKEN=your-app-token
   PUSHOVER_USER_KEY=your-user-key
   ```

To enable notifications, run the application with the `--notify` flag:
```bash
uv run --env-file .env src/main.py --notify
```

You can test your notification setup using:
```bash
uv run --env-file .env src/main.py --testalarm
```

## Docker Container

The application can be run in a Docker container, which provides an isolated environment with all dependencies pre-installed. You need outbound connectivity from the container to the Ubiqiti system and OpenAI.

1. Build the container:
   ```bash
   docker build -t camera-app .
   ```

2. Run the container with your environment variables:
   ```bash
   docker run -it --env-file .env camera-app
   ```

The container will automatically start the application with notifications enabled.

## License

This project is licensed under the Apache License, Version 2.0. See the LICENSE file for more details.


import asyncio
import logging
from dataclasses import dataclass
from datetime import time, timedelta

from classify import classify_probabilities, pick_subject
from images import analyze_image

logger = logging.getLogger()

# Pushover priorities: -2 lowest (no alert on the phone), 0 normal, 1 high (bypasses quiet hours)
RACCOON_PRIORITY = 1
RACCOON_REPEAT_PRIORITY = 0
RACCOON_REPEAT_WINDOW = timedelta(minutes=1)
# The prompt model can never trigger priority 1.
PRIORITY_BY_ACTION = {"OBSERVATION": -2, "ALERT": 0}

# People are only reported between these times (window spans midnight).
PERSON_HOURS_START = time(23, 0)
PERSON_HOURS_END = time(6, 0)
PERSON_REPEAT_WINDOW = timedelta(minutes=2)

PROMPT_FILES = {"animal": "animals.prompt", "person": "person.prompt"}

OUTPUT_FORMAT = """Respond with exactly two lines:
1. One of NOACTION, OBSERVATION or ALERT
2. A short one-line description for the notification"""

_last_raccoon_alert = None
_last_observation = {}  # subject -> (datetime, text)
# Motion events arrive as concurrent tasks; serialize so each sees the previous observation.
_lock = asyncio.Lock()


@dataclass
class Notification:
    priority: int
    title: str
    message: str


@dataclass
class Decision:
    subject: str
    probabilities: dict
    notification: Notification | None


def in_person_hours(now):
    t = now.time()
    return t >= PERSON_HOURS_START or t < PERSON_HOURS_END


def build_prompt(subject, now):
    with open(PROMPT_FILES[subject]) as f:
        rules = "".join(line for line in f if not line.startswith("#"))
    last = _last_observation.get(subject)
    last_text = f"{last[0]:%A %H:%M} - {last[1]}" if last else "none"
    return (
        f"{rules.strip()}\n\n"
        f"Current time: {now:%A %H:%M}\n"
        f"Last observation: {last_text}\n\n"
        f"{OUTPUT_FORMAT}"
    )


def parse_response(text):
    """Return (action, message); action is None if the response doesn't follow the format."""
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    if not lines:
        return None, ""
    first = lines[0].upper()
    action = next((a for a in ("NOACTION", "OBSERVATION", "ALERT") if first.startswith(a)), None)
    return action, " ".join(lines[1:])


async def classify(image_path, api_key):
    """Return (subject, probabilities) for the image."""
    probabilities = await asyncio.to_thread(classify_probabilities, image_path, api_key)
    return pick_subject(probabilities), probabilities


async def decide(image_path, api_key, now, probabilities=None):
    """Classify the image (unless probabilities are given) and decide what (if anything) to send."""
    async with _lock:
        return await _decide(image_path, api_key, now, probabilities)


async def _decide(image_path, api_key, now, probabilities):
    global _last_raccoon_alert

    if probabilities is None:
        probabilities = await asyncio.to_thread(classify_probabilities, image_path, api_key)
    subject = pick_subject(probabilities)

    if subject == "none":
        return Decision(subject, probabilities, None)

    if subject == "raccoon":
        repeat = _last_raccoon_alert is not None and now - _last_raccoon_alert < RACCOON_REPEAT_WINDOW
        priority = RACCOON_REPEAT_PRIORITY if repeat else RACCOON_PRIORITY
        if not repeat:
            _last_raccoon_alert = now
        return Decision(subject, probabilities, Notification(priority, "Alarm raccoon", "Raccoon at the pond"))

    if subject == "person":
        if not in_person_hours(now):
            return Decision(subject, probabilities, None)
        last = _last_observation.get("person")
        if last is not None and now - last[0] < PERSON_REPEAT_WINDOW:
            return Decision(subject, probabilities, None)

    response = await analyze_image(image_path, build_prompt(subject, now), api_key)
    action, message = parse_response(response)
    if action is None:
        logger.warning(f"Unexpected {subject} prompt response, sending as observation: {response!r}")
        action, message = "OBSERVATION", (response or "").strip().replace("\n", " ") or f"Unidentified {subject}"
    if action == "NOACTION":
        logger.info(f"{subject}: NOACTION {message}")
        return Decision(subject, probabilities, None)

    _last_observation[subject] = (now, message)
    title = f"{action.capitalize()} {subject}"
    return Decision(subject, probabilities, Notification(PRIORITY_BY_ACTION[action], title, message))

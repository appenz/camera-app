import base64
import logging

import requests

logger = logging.getLogger()

DECISIONS_URL = "https://api.openai.com/v1/decisions"
DECISIONS_MODEL = "gpt-6-luna"

SCENE = (
    "Security camera image of a garden near the front door of a house, with a rectangular "
    "pond surrounded by stone paving and plants. Night images are black-and-white infrared."
)

SUBJECTS = [
    {"value": "none", "description": "No person and no animal is visible. The pond has fish in it; fish don't count."},
    {"value": "person", "description": "A person (a human being) is visible, even partially or at the edge of the frame."},
    {"value": "raccoon", "description": "An animal that is clearly identifiable as a raccoon."},
    {"value": "animal", "description": "An animal other than fish that is not positively identifiable as a raccoon, such as a cat, squirrel, bird, opossum, coyote, or an unclear or unknown animal."},
]

# Raccoon is checked first so a raccoon still alerts when a person is also in frame.
RACCOON_THRESHOLD = 0.3
PERSON_THRESHOLD = 0.5
ANIMAL_THRESHOLD = 0.45


def classify_probabilities(image_path, api_key):
    """Ask the Decisions API what is in the image. Returns {subject: probability}."""
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")
    response = requests.post(
        DECISIONS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": DECISIONS_MODEL,
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": SCENE},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{image_b64}"},
                ],
            }],
            "questions": [{
                "type": "choice",
                "name": "subject",
                "instructions": "Is there one of the following in the image?",
                "choices": SUBJECTS,
            }],
        },
        timeout=20,
    )
    response.raise_for_status()
    answer = response.json()["answers"][0]
    if answer["type"] != "choice":
        raise RuntimeError(f"Unexpected decision answer: {answer}")
    return {p["value"]: p["probability"] for p in answer["probabilities"]}


def pick_subject(probabilities):
    """Map probabilities to one of: raccoon, person, animal, none."""
    if probabilities.get("raccoon", 0) >= RACCOON_THRESHOLD:
        return "raccoon"
    if probabilities.get("person", 0) >= PERSON_THRESHOLD:
        return "person"
    if probabilities.get("animal", 0) + probabilities.get("raccoon", 0) >= ANIMAL_THRESHOLD:
        return "animal"
    return "none"

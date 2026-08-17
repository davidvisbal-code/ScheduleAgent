"""
Run this once a day (e.g. 7:00 AM) on a separate schedule from main.py.
Collects everything main.py queued overnight/during the day and sends it as
ONE WhatsApp message instead of many separate pings.

Sends via the APPROVED TEMPLATE (not free-form text) since this message is
unprompted (business-initiated) -- WhatsApp blocks free-form text outside a
24-hour reply window, which a daily proactive digest will always be outside
of. Templates are exempt from that restriction once approved by Meta.
"""

import os
import json
import requests
from pathlib import Path

WHATSAPP_PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"].strip()
WHATSAPP_TO_NUMBER = os.environ["WHATSAPP_TO_NUMBER"].strip()
WHATSAPP_ACCESS_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"].strip()  # System User permanent token

# Must match EXACTLY what you named/approved in WhatsApp Manager -> Message
# Templates. Check there if this ever fails with a "template not found" error.
TEMPLATE_NAME = os.environ.get("WHATSAPP_TEMPLATE_NAME", "").strip() or "daily_digest"
TEMPLATE_LANGUAGE = os.environ.get("WHATSAPP_TEMPLATE_LANGUAGE", "").strip() or "en_US"

BASE_DIR = Path(__file__).parent
DIGEST_QUEUE_FILE = BASE_DIR / "digest_queue.json"


def build_digest_text():
    if not DIGEST_QUEUE_FILE.exists():
        queue = []
    else:
        queue = json.loads(DIGEST_QUEUE_FILE.read_text())

    if not queue:
        return queue, "Nothing new overnight -- your schedule's unchanged."

    urgent = [item["text"] for item in queue if item["text"].startswith("⚠️")]
    routine = [item["text"] for item in queue if not item["text"].startswith("⚠️")]
    ordered = urgent + routine
    return queue, "\n\n".join(ordered)


def main():
    queue, body_text = build_digest_text()

    # The template's {{1}} variable gets filled in here. WhatsApp templates
    # have their own length limits (shorter than free-form messages), so
    # this trims more aggressively than the old free-text version did.
    response = requests.post(
        f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"},
        json={
            "messaging_product": "whatsapp",
            "to": WHATSAPP_TO_NUMBER,
            "type": "template",
            "template": {
                "name": TEMPLATE_NAME,
                "language": {"code": TEMPLATE_LANGUAGE},
                "components": [
                    {
                        "type": "body",
                        "parameters": [{"type": "text", "text": body_text[:1000]}],
                    }
                ],
            },
        },
        timeout=30,
    )

    if response.status_code != 200:
        print(f"[digest] FAILED ({response.status_code}): {response.text}")
        raise SystemExit(1)

    print(f"[digest] sent via template '{TEMPLATE_NAME}': {response.json()}")
    DIGEST_QUEUE_FILE.write_text("[]")
    print(f"[digest] {len(queue)} item(s) cleared.")


if __name__ == "__main__":
    main()

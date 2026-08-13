"""
Run this once a day (e.g. 7:00 AM) on a separate schedule from main.py.
Collects everything main.py queued overnight/during the day and sends it as
ONE WhatsApp message instead of many separate pings.
"""

import os
import json
from pathlib import Path
from composio import Composio

COMPOSIO_API_KEY = os.environ["COMPOSIO_API_KEY"].strip()
WHATSAPP_PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"].strip()
WHATSAPP_TO_NUMBER = os.environ["WHATSAPP_TO_NUMBER"].strip()
WHATSAPP_ACCOUNT_ID = os.environ.get("WHATSAPP_ACCOUNT_ID", "").strip() or None

BASE_DIR = Path(__file__).parent
DIGEST_QUEUE_FILE = BASE_DIR / "digest_queue.json"

composio = Composio(api_key=COMPOSIO_API_KEY)


def main():
    if not DIGEST_QUEUE_FILE.exists():
        queue = []
    else:
        queue = json.loads(DIGEST_QUEUE_FILE.read_text())

    if not queue:
        text = "Good morning! Nothing new overnight — your schedule's unchanged."
    else:
        # Conflicts (start with the warning emoji) go first so you see the
        # things that actually need a decision before routine updates.
        urgent = [item["text"] for item in queue if item["text"].startswith("⚠️")]
        routine = [item["text"] for item in queue if not item["text"].startswith("⚠️")]
        ordered = urgent + routine
        text = "Good morning! Here's what happened:\n\n" + "\n\n".join(ordered)

    composio.tools.execute(
        "WHATSAPP_SEND_MESSAGE",
        arguments={
            "phone_number_id": WHATSAPP_PHONE_NUMBER_ID,
            "to_number": WHATSAPP_TO_NUMBER,
            "text": text[:4000],
        },
        connected_account_id=WHATSAPP_ACCOUNT_ID,
    )

    DIGEST_QUEUE_FILE.write_text("[]")
    print(f"[digest] sent, {len(queue)} item(s) cleared.")


if __name__ == "__main__":
    main()

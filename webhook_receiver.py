"""
Minimal webhook receiver for WhatsApp Cloud API.

Purpose right now: pass Meta's Callback URL verification handshake, and log
incoming messages so you can confirm the whole pipeline is live. It does NOT
yet forward messages into Claude/Composio for a reply -- that's the next
phase, once this basic handshake is confirmed working.

Run locally + expose via ngrok for testing (see instructions below).
Later: this same script moves onto your VPS for a permanent URL.
"""

import os
from flask import Flask, request

app = Flask(__name__)

# Must match EXACTLY what you type into Meta's "Verify token" field.
VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "david-marymount-2026")


@app.route("/webhook", methods=["GET"])
def verify():
    """Meta calls this once, when you click 'Verify and save'."""
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        print("[webhook] Verified successfully by Meta.")
        return challenge, 200
    print("[webhook] Verification FAILED -- token mismatch.")
    return "Forbidden", 403


@app.route("/webhook", methods=["POST"])
def receive():
    """Meta calls this every time a message is sent to your bot number."""
    data = request.get_json()
    print("[webhook] Incoming payload:")
    print(data)

    # Uncomment once you're ready to see just the message text:
    # try:
    #     msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
    #     print(f"From {msg['from']}: {msg['text']['body']}")
    # except (KeyError, IndexError):
    #     pass  # status update or non-text message, not a plain text message

    return "", 200  # Meta requires a fast 200 response, always


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))  # Render assigns this automatically
    app.run(host="0.0.0.0", port=port)

"""
WhatsApp webhook receiver -- now with actual replies.

Flow per incoming message:
1. Meta POSTs the message here.
2. We check it's really from YOU (security -- ignore anyone else).
3. We ask Claude to answer, giving it live access to your Calendar/Classroom/
   Gmail via the same Composio MCP connection you already set up in Claude.ai.
4. Claude's answer gets texted back to you via WhatsApp.
5. The last few exchanges are remembered so it feels like a conversation.

Meta requires a fast response, so we reply "" , 200 immediately and do the
real work in a background thread -- otherwise Meta may think the webhook
failed and retry, causing duplicate replies.
"""

import os
import json
import threading
import requests
from pathlib import Path
from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "david-marymount-2026")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
COMPOSIO_MCP_URL = os.environ["COMPOSIO_MCP_URL"]  # the combined MCP server URL from Composio
WHATSAPP_PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
WHATSAPP_ACCESS_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"]  # the System User permanent token
AUTHORIZED_NUMBER = os.environ["WHATSAPP_AUTHORIZED_NUMBER"]  # YOUR number, digits only, no +

BASE_DIR = Path(__file__).parent
HISTORY_FILE = BASE_DIR / "conversation_history.json"
SEEN_IDS_FILE = BASE_DIR / "seen_message_ids.json"
RULES_FILE = BASE_DIR / "rules.json"

MAX_HISTORY_TURNS = 10  # how many past exchanges to remember


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def already_processed(message_id):
    """Meta sometimes retries a webhook delivery -- don't reply twice."""
    seen = load_json(SEEN_IDS_FILE, [])
    if message_id in seen:
        return True
    seen.append(message_id)
    save_json(SEEN_IDS_FILE, seen[-200:])  # keep the list from growing forever
    return False


def build_system_prompt():
    rules = load_json(RULES_FILE, {})
    return f"""You are David's personal WhatsApp assistant. He's texting you like a
normal conversation, so keep replies short and conversational -- this is a
text message, not a report. A sentence or two is usually enough unless he
asks for a list or details.

You have live access to his Gmail, Google Calendar, and Google Classroom
(school and personal, where connected) through the tools available to you --
use them whenever a question needs real data instead of guessing.

His scheduling rules, for context if relevant:
{json.dumps(rules.get("weekly_planner", {}), indent=2)}
{json.dumps(rules.get("immovable_keywords", []), indent=2)}

Never take any action that changes his calendar, sends an email, or modifies
anything -- you're read-only here. If he asks you to change or send
something, tell him you'll need to do that a different way for now, don't
attempt it."""


def ask_claude(user_message, history):
    messages = history + [{"role": "user", "content": user_message}]

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "mcp-client-2025-04-04",  # MCP connector is a beta feature
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 600,
            "system": build_system_prompt(),
            "messages": messages,
            "mcp_servers": [
                {
                    "type": "url",
                    "url": COMPOSIO_MCP_URL,
                    "name": "composio-school-tools",
                }
            ],
        },
        timeout=60,
    )
    data = response.json()

    if "content" not in data:
        print(f"[claude] Unexpected response: {data}")
        return "Sorry, something went wrong on my end -- try again in a bit."

    # Claude's reply may include tool-use blocks alongside text; only the
    # text blocks are what we actually send back over WhatsApp.
    text_parts = [block["text"] for block in data["content"] if block.get("type") == "text"]
    return "\n".join(text_parts).strip() or "I looked into it but don't have a clear answer -- want to rephrase?"


def send_whatsapp_reply(to_number, text):
    requests.post(
        f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages",
        headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"},
        json={
            "messaging_product": "whatsapp",
            "to": to_number,
            "text": {"body": text[:4000]},
        },
        timeout=30,
    )


def handle_message_async(from_number, message_text):
    history = load_json(HISTORY_FILE, [])

    reply = ask_claude(message_text, history)
    send_whatsapp_reply(from_number, reply)

    history.append({"role": "user", "content": message_text})
    history.append({"role": "assistant", "content": reply})
    save_json(HISTORY_FILE, history[-(MAX_HISTORY_TURNS * 2):])

    print(f"[webhook] Replied to {from_number}: {reply[:100]}")


@app.route("/webhook", methods=["GET"])
def verify():
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
    data = request.get_json()

    try:
        msg = data["entry"][0]["changes"][0]["value"]["messages"][0]
    except (KeyError, IndexError, TypeError):
        return "", 200  # status update or non-message event, nothing to do

    if msg.get("type") != "text":
        return "", 200  # ignore images/audio/etc. for now

    from_number = msg["from"]
    message_id = msg["id"]
    message_text = msg["text"]["body"]

    if from_number != AUTHORIZED_NUMBER:
        print(f"[webhook] Ignored message from unauthorized number: {from_number}")
        return "", 200  # silently ignore anyone who isn't David

    if already_processed(message_id):
        return "", 200  # Meta retried a delivery, don't reply twice

    # Reply in a background thread so Meta gets its fast 200 immediately.
    threading.Thread(target=handle_message_async, args=(from_number, message_text)).start()

    return "", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

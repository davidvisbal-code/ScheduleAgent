"""
Run this once a day (e.g. 7:00 AM) on a separate schedule from main.py.
Collects everything main.py queued overnight/during the day, has Claude
organize and prioritize it into ONE clean summary (dropping anything
trivial, grouping by category), and sends that via the approved WhatsApp
template instead of many separate pings.

Sends via the APPROVED TEMPLATE (not free-form text) since this message is
unprompted (business-initiated) -- WhatsApp blocks free-form text outside a
24-hour reply window, which a daily proactive digest will always be outside
of. Templates are exempt from that restriction once approved by Meta.
"""

import os
import json
import datetime
import requests
from pathlib import Path

COMPOSIO_API_KEY = os.environ["COMPOSIO_API_KEY"].strip()
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()
COMPOSIO_MCP_URL = os.environ["COMPOSIO_MCP_URL"].strip()  # same combined MCP URL the chat bot uses
# NOT the same key as COMPOSIO_API_KEY above -- that one is for the raw SDK
# (composio.tools.execute), this one is the original key MCP calls need,
# same value that's still sitting correctly in Render for the chat bot.
COMPOSIO_MCP_AUTH_TOKEN = os.environ["COMPOSIO_MCP_AUTH_TOKEN"].strip()
WHATSAPP_PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"].strip()
WHATSAPP_TO_NUMBER = os.environ["WHATSAPP_TO_NUMBER"].strip()
WHATSAPP_ACCESS_TOKEN = os.environ["WHATSAPP_ACCESS_TOKEN"].strip()  # System User permanent token

# Must match EXACTLY what you named/approved in WhatsApp Manager -> Message
# Templates. Check there if this ever fails with a "template not found" error.
TEMPLATE_NAME = os.environ.get("WHATSAPP_TEMPLATE_NAME", "").strip() or "daily_digest"
TEMPLATE_LANGUAGE = os.environ.get("WHATSAPP_TEMPLATE_LANGUAGE", "").strip() or "en_US"

BASE_DIR = Path(__file__).parent
DIGEST_QUEUE_FILE = BASE_DIR / "digest_queue.json"


def sanitize_for_template(text):
    """WhatsApp template parameters can't contain newlines/tabs or 4+
    consecutive spaces (allowed in free-form text, not templates)."""
    text = text.replace("\n\n", " • ").replace("\n", " • ").replace("\t", " ")
    while "    " in text:  # collapse any run of 4+ spaces
        text = text.replace("    ", " ")
    return text


def organize_with_claude(raw_items):
    """Take the raw queued lines (already tagged ⚠️/📅/📚/📧) and have Claude
    turn them into ONE organized, prioritized recap. Claude has LIVE access
    to Gmail/Calendar/Classroom/Drive here -- for anything that needs more
    context than the title/subject alone (a full email body, a Classroom
    assignment's actual instructions, a linked Doc/Slide/Sheet), it should
    actually open and read it before writing the summary, not guess from
    the title."""
    prompt = f"""Here are the raw events detected overnight/today, already
tagged by type (⚠️ = urgent conflict, 📅 = calendar, 📚 = classroom
coursework, 📧 = email):

{json.dumps(raw_items, indent=2)}

You have live access to David's Gmail, Google Calendar, Google Classroom,
and Google Drive through the tools available to you. USE THEM: for any item
above where the title/subject alone isn't enough to actually inform David
(a classroom assignment whose real instructions matter, an email whose body
has the actual details, a linked Doc/Slide/Sheet with content he needs),
look it up and read it before writing the summary. Don't just repeat the
raw title back to him -- tell him what it actually says.

Write David a short morning debrief covering all of this. Rules:
- Group into EXPLICIT LABELED SECTIONS in this order, and skip any section
  that has nothing in it (don't write "None" for empty ones, just omit
  them): "⚠️ CONFLICTS:", "📚 DUE SOON:", "📅 CALENDAR:", "📧 EMAILS:".
  Start each section with its bold-style label, then the items for that
  section, then move to the next label. This must read as clearly separated
  blocks, not one flowing paragraph -- that's the #1 complaint so far.
- Within "📚 DUE SOON", be concrete and specific per item: course, what it
  actually is, and the real due date/time if you found one -- e.g. "Math:
  Quiz Tue 8/25 (found in linked Sheet, not Classroom's own due date)."
  Missing or vague due-date info here is the single worst failure mode.
- For emails, only mention ones that seem genuinely worth knowing about
  (school administrative notices, teacher messages, anything actionable) --
  skip marketing, generic notifications, anything unimportant. For the ones
  you do mention, summarize what the email actually says, not just the
  subject line.
- CRITICAL: any email from a teacher or the school domain must be FULLY
  read (open the actual message, not just the subject/snippet) -- teachers
  regularly communicate real quiz/assignment dates directly in email
  threads, not just through Classroom. A subject line alone is not enough
  to judge whether an email matters.
- If a Classroom post or email links to a Doc/Slide/Sheet with real
  instructions or content, open it and pull out what's actually relevant.
- IMPORTANT: several of David's teachers (especially Math and English) keep
  the real schedule of quizzes, workshops, and activity due dates inside a
  Google Sheet or Doc, or only mention it in an email conversation with the
  teacher -- NOT always attached directly to the Classroom post. Don't just
  check what's linked on the coursework item itself: actively SEARCH Drive
  for a spreadsheet/doc related to the course, and SEARCH Gmail for recent
  messages from that teacher, before concluding there's no real date. This
  matters more than anything else in this digest -- a missed quiz date is
  the worst possible failure here.
- For anything due soon that David hasn't indicated he's finished, phrase
  it as a direct question inviting a reply, not a passive FYI -- e.g.
  "Have you started the Physics problem set due Friday? Tell me and I can
  block time for it" instead of just "Physics: problem set due Friday."
  This matters -- a reminder he can't act on isn't useful to him.
- Keep it conversational and brief, like a text from a friend catching you
  up, not a bulleted report.
- CRITICAL: do NOT narrate your own investigation ("Let me search...",
  "I'll dig into...", "Now let me check..."). Do all your searching/tool
  use silently, then write ONLY the final debrief -- none of your process
  should ever appear in the message David actually receives.
- CRITICAL: your entire response must be ONE continuous block with NO line
  breaks of any kind (WhatsApp template limitation) -- use " • " between
  distinct points instead of new lines. Keep it under 900 characters total.
- If, after filtering, nothing is actually worth mentioning, just say so in
  one short sentence."""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "mcp-client-2025-04-04",  # MCP connector is a beta feature
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 1500,
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": prompt}],
                "mcp_servers": [
                    {
                        "type": "url",
                        "url": COMPOSIO_MCP_URL,
                        "name": "composio-school-tools",
                        "authorization_token": COMPOSIO_MCP_AUTH_TOKEN,
                    }
                ],
            },
            timeout=170,  # tool calls take longer than a plain text response
        )
    except requests.exceptions.RequestException as e:
        print(f"[digest] Claude organize call raised an exception, falling back to raw list: {e}")
        return sanitize_for_template(" • ".join(raw_items))

    data = response.json()
    if "content" not in data:
        print(f"[digest] Claude organize call failed ({response.status_code}), falling back to raw list: {data}")
        return sanitize_for_template(" • ".join(raw_items))

    text_parts = [block["text"] for block in data["content"] if block.get("type") == "text"]
    text = "".join(text_parts).strip()
    return sanitize_for_template(text) if text else sanitize_for_template(" • ".join(raw_items))


def build_digest_text():
    if not DIGEST_QUEUE_FILE.exists():
        queue = []
    else:
        queue = json.loads(DIGEST_QUEUE_FILE.read_text())

    if not queue:
        return queue, "Nothing new overnight -- your schedule's unchanged"

    # Drop anything older than 48h (stale leftovers from a past failed
    # send that never cleared the queue) and remove exact duplicates,
    # which is what caused the same summary repeating 3x in one message.
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=48)).isoformat()
    fresh = [item for item in queue if item.get("added", "") >= cutoff]
    seen = set()
    deduped = []
    for item in fresh:
        if item["text"] not in seen:
            seen.add(item["text"])
            deduped.append(item)

    if not deduped:
        return queue, "Nothing new overnight -- your schedule's unchanged"

    raw_items = [item["text"] for item in deduped]
    return queue, organize_with_claude(raw_items)


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

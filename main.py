"""
David's Schedule Agent
-----------------------
Checks Gmail, Google Calendar, and Google Classroom (personal + school accounts)
for anything new since the last run. Flags conflicts against "immovable" items
(school coursework, accepted meetings, and anything matching immovable_keywords
in rules.json) using Claude to reason about the best resolution, then sends a
WhatsApp message summarizing what's new and any conflicts. Never edits your
calendar or replies to anything on its own -- it only notifies you.

Run this on a loop (every 1-2 min on a small VPS) or on a schedule (GitHub
Actions, every 15 min minimum reliably). See README.md for setup.
"""

import os
import json
import datetime
import html
from pathlib import Path

from composio import Composio
import anthropic

# ---------- CONFIG (fill these in via environment variables, see .env.example) ----------

COMPOSIO_API_KEY = os.environ["COMPOSIO_API_KEY"].strip()
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()

WHATSAPP_PHONE_NUMBER_ID = os.environ["WHATSAPP_PHONE_NUMBER_ID"].strip()  # from WHATSAPP_GET_PHONE_NUMBERS
WHATSAPP_TO_NUMBER = os.environ["WHATSAPP_TO_NUMBER"].strip()  # your number, digits only, no +

# Connected account IDs from your Composio dashboard (Connected Accounts section).
# Leave a value as None (or unset the env var) to skip that account entirely --
# e.g. if you never connect Gmail personal, just don't set GMAIL_PERSONAL_ACCOUNT_ID.
ACCOUNTS = {
    "gmail_school": os.environ.get("GMAIL_SCHOOL_ACCOUNT_ID", "").strip() or None,
    "gmail_personal": os.environ.get("GMAIL_PERSONAL_ACCOUNT_ID", "").strip() or None,
    "calendar_school": os.environ.get("CALENDAR_SCHOOL_ACCOUNT_ID", "").strip() or None,
    "calendar_personal": os.environ.get("CALENDAR_PERSONAL_ACCOUNT_ID", "").strip() or None,
    "classroom": os.environ.get("CLASSROOM_ACCOUNT_ID", "").strip() or None,
}

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
RULES_FILE = BASE_DIR / "rules.json"
DIGEST_QUEUE_FILE = BASE_DIR / "digest_queue.json"

LOOKAHEAD_DAYS = 14  # how far ahead to check calendars for events

# ---------- CLIENTS ----------

composio = Composio(api_key=COMPOSIO_API_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------- HELPERS ----------

def load_json(path, default):
    if path.exists():
        return json.loads(path.read_text())
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def run_tool(tool_slug, arguments, account_key):
    """Execute a Composio tool using a specific connected account, if configured."""
    account_id = ACCOUNTS.get(account_key)
    if not account_id:
        return None  # account not connected, skip silently

    # Ask Composio directly which user_id owns this connected account,
    # instead of guessing a value -- this was failing before.
    try:
        account_info = composio.connected_accounts.get(account_id)
        real_user_id = account_info.user_id
    except Exception as e:
        print(f"[warn] Couldn't look up user_id for {account_key} ({account_id}): {e}")
        real_user_id = "default"

    result = composio.tools.execute(
        tool_slug,
        arguments=arguments,
        connected_account_id=account_id,
        user_id=real_user_id,
        dangerously_skip_version_check=True,
    )
    if not result.get("successful", False):
        print(f"[warn] {tool_slug} on {account_key} failed: {result.get('error')}")
        return None
    return result.get("data", {})


def append_to_digest(text):
    """Queue a message for the next morning digest instead of texting immediately.
    Urgent conflicts (immovable overlaps) still get flagged in the queue with a
    marker so send_digest.py can put them first, but nothing goes out live --
    that's the whole point of the digest model instead of 30 pings a day."""
    queue = load_json(DIGEST_QUEUE_FILE, [])
    queue.append({"text": text, "added": datetime.datetime.utcnow().isoformat()})
    save_json(DIGEST_QUEUE_FILE, queue)
    print(f"[queued] {text[:80]}...")


def is_immovable(title, rules):
    title_lower = (title or "").lower()
    return any(kw in title_lower for kw in rules["immovable_keywords"])


def is_movable(title, rules):
    title_lower = (title or "").lower()
    return any(kw in title_lower for kw in rules["movable_keywords"])


def overlaps(start_a, end_a, start_b, end_b):
    return start_a < end_b and start_b < end_a


def parse_event_times(event):
    start = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
    end = event.get("end", {}).get("dateTime") or event.get("end", {}).get("date")
    if not start or not end:
        return None, None
    try:
        return datetime.datetime.fromisoformat(start.replace("Z", "+00:00")), \
               datetime.datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None, None


# ---------- FETCHERS ----------

def fetch_calendar_events(account_key):
    now = datetime.datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + datetime.timedelta(days=LOOKAHEAD_DAYS)).isoformat() + "Z"
    data = run_tool(
        "GOOGLECALENDAR_EVENTS_LIST",
        {
            "calendarId": "primary",
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 250,
        },
        account_key,
    )
    if not data:
        return []
    return data.get("items", [])


def fetch_classroom_coursework():
    if not ACCOUNTS.get("classroom"):
        return []
    courses_data = run_tool(
        "GOOGLE_CLASSROOM_COURSES_LIST",
        {"studentId": "me", "courseStates": ["ACTIVE"], "pageSize": 50},
        "classroom",
    )
    if not courses_data:
        return []
    all_work = []
    for course in courses_data.get("courses", []):
        work_data = run_tool(
            "GOOGLE_CLASSROOM_COURSE_WORK_LIST",
            {"courseId": course["id"], "courseWorkStates": ["PUBLISHED"], "pageSize": 50},
            "classroom",
        )
        if not work_data:
            continue
        for item in work_data.get("courseWork", []):
            item["_courseName"] = course.get("name", "")
            all_work.append(item)
    return all_work


def fetch_recent_emails(account_key):
    data = run_tool(
        "GMAIL_SEARCH_THREADS" if False else "GMAIL_FETCH_EMAILS",
        {"max_results": 15, "user_id": "me"},
        account_key,
    )
    if not data:
        return []
    return data.get("messages", []) or data.get("threads", [])


# ---------- CONFLICT REASONING ----------

def ask_claude_for_resolution(new_event, conflicting_events):
    prompt = f"""A new calendar event was just added:
{json.dumps(new_event, indent=2)}

It conflicts (time overlap) with these existing, immovable commitments:
{json.dumps(conflicting_events, indent=2)}

The immovable items cannot be moved. Suggest the single best resolution for
the new event (decline it, ask to reschedule it, or note that it's short
enough to skip part of it) in 2-3 sentences, written directly to David as if
you're texting him a heads-up. Do not take any action, just recommend."""

    response = claude.messages.create(
        model="claude-sonnet-5",
        max_tokens=300,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# ---------- MAIN ----------

def main():
    state = load_json(STATE_FILE, {"calendar_ids": [], "coursework_ids": [], "email_ids": []})
    rules = load_json(RULES_FILE, {"immovable_keywords": [], "movable_keywords": []})

    messages = []

    # --- Calendar (school + personal) ---
    all_events = []
    for key in ("calendar_school", "calendar_personal"):
        all_events.extend(fetch_calendar_events(key))

    new_calendar_ids = []
    new_events = []
    for ev in all_events:
        eid = ev.get("id")
        if not eid:
            continue
        new_calendar_ids.append(eid)
        if eid not in state["calendar_ids"]:
            new_events.append(ev)

    # Immovable events = anything already known + tagged immovable, or accepted invites
    immovable_existing = [
        ev for ev in all_events
        if ev.get("id") not in [e.get("id") for e in new_events]
        and (is_immovable(ev.get("summary", ""), rules) or
             ev.get("attendees") and any(
                 a.get("self") and a.get("responseStatus") == "accepted"
                 for a in ev.get("attendees", [])
             ))
    ]

    for ev in new_events:
        title = ev.get("summary", "(untitled event)")
        start, end = parse_event_times(ev)
        if start and end:
            conflicts = []
            for existing in immovable_existing:
                ex_start, ex_end = parse_event_times(existing)
                if ex_start and ex_end and overlaps(start, end, ex_start, ex_end):
                    conflicts.append(existing)
            if conflicts:
                resolution = ask_claude_for_resolution(ev, conflicts)
                messages.append(f"⚠️ New event \"{title}\" conflicts with immovable commitments.\n{resolution}")
            else:
                messages.append(f"📅 New event added: \"{title}\" ({start.strftime('%a %I:%M %p')})")

    # --- Classroom coursework (always immovable, just report new items) ---
    coursework = fetch_classroom_coursework()
    new_coursework_ids = []
    for item in coursework:
        cid = item.get("id")
        if not cid:
            continue
        new_coursework_ids.append(cid)
        if cid not in state["coursework_ids"]:
            due = item.get("dueDate")
            due_str = f"{due.get('month')}/{due.get('day')}/{due.get('year')}" if due else "no due date"
            messages.append(f"📚 New coursework in {item.get('_courseName')}: \"{item.get('title')}\" (due {due_str})")

    # --- Gmail (school + personal): filter spam/noise, dedupe, add context ---
    new_email_ids = []
    seen_subjects_this_run = set()
    spam_terms = rules.get("spam_filters", {}).get("sender_or_subject_contains", [])
    for key in ("gmail_school", "gmail_personal"):
        emails = fetch_recent_emails(key)
        for msg in emails:
            mid = msg.get("id") or msg.get("threadId")
            if not mid:
                continue
            new_email_ids.append(mid)
            if mid in state["email_ids"]:
                continue

            subject = msg.get("subject", "(no subject)")
            sender = (msg.get("sender") or msg.get("from") or "").lower()
            subject_lower = subject.lower()

            if any(term in sender or term in subject_lower for term in spam_terms):
                continue  # filtered out as marketing/notification noise
            if subject in seen_subjects_this_run:
                continue  # duplicate subject in the same run (e.g. repeated alerts)
            seen_subjects_this_run.add(subject)

            snippet = html.unescape((msg.get("preview", {}) or {}).get("body", "")[:120].replace("\n", " ").strip())
            sender_name = sender.split("<")[0].strip() or sender
            detail = f' -- from {sender_name}: "{snippet}..."' if snippet else f" -- from {sender_name}"
            messages.append(f"📧 New email ({key.replace('gmail_', '')}): {subject}{detail}")

    # --- Queue for next morning digest + persist ---
    for m in messages:
        append_to_digest(m)

    save_json(STATE_FILE, {
        "calendar_ids": new_calendar_ids,
        "coursework_ids": new_coursework_ids,
        "email_ids": new_email_ids,
    })

    print(f"[done] {len(messages)} update(s) sent.")


if __name__ == "__main__":
    main()

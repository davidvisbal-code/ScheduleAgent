"""
Run this once a week, on Wednesdays (a fresh run before the day starts, e.g.
6:00 AM). Builds a full plan from THIS Wednesday through next Tuesday,
working around fixed commitments (classes, accepted meetings, Classroom
due dates) and filling open time according to rules.json's priority order.
Creates events directly on your calendar (auto_create_events: true in
rules.json) tagged with the "🗓️ Auto:" prefix so they're easy to find,
edit, or bulk-delete if a week goes sideways.

Holidays/weekends and Fridays (your work day) get different treatment --
see build_day_plan() below.
"""

import os
import json
import datetime
from pathlib import Path

from composio import Composio
import anthropic

COMPOSIO_API_KEY = os.environ["COMPOSIO_API_KEY"].strip()
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"].strip()

ACCOUNTS = {
    "gmail_personal": os.environ.get("GMAIL_PERSONAL_ACCOUNT_ID", "").strip() or None,
    "calendar_personal": os.environ.get("CALENDAR_PERSONAL_ACCOUNT_ID", "").strip() or None,
    "calendar_school": os.environ.get("CALENDAR_SCHOOL_ACCOUNT_ID", "").strip() or None,
    "classroom": os.environ.get("CLASSROOM_ACCOUNT_ID", "").strip() or None,
}
# Everything gets written to ONE calendar (your personal one), even though
# source data (classes, school invites) comes from the school account --
# matches "everything should be managed on one account."
WRITE_TARGET_ACCOUNT = "calendar_personal"

BASE_DIR = Path(__file__).parent
RULES_FILE = BASE_DIR / "rules.json"
DIGEST_QUEUE_FILE = BASE_DIR / "digest_queue.json"

composio = Composio(api_key=COMPOSIO_API_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def load_json(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def queue_digest(text):
    queue = load_json(DIGEST_QUEUE_FILE, [])
    queue.append({"text": text, "added": datetime.datetime.utcnow().isoformat()})
    save_json(DIGEST_QUEUE_FILE, queue)


def run_tool(tool_slug, arguments, account_key):
    account_id = ACCOUNTS.get(account_key)
    if not account_id:
        return None
    result = composio.tools.execute(
        tool_slug, arguments=arguments, connected_account_id=account_id,
        dangerously_skip_version_check=True,
    )
    if not result.get("successful", False):
        print(f"[warn] {tool_slug} on {account_key} failed: {result.get('error')}")
        return None
    return result.get("data", {})


def get_week_range():
    """This Wednesday through next Tuesday (inclusive), based on today's date."""
    today = datetime.date.today()
    days_since_wed = (today.weekday() - 2) % 7  # Monday=0 ... Wednesday=2
    wed = today - datetime.timedelta(days=days_since_wed)
    tue = wed + datetime.timedelta(days=6)
    return wed, tue


def fetch_holidays(rules, start, end):
    calendar_id = rules["weekly_planner"]["colombia_holiday_calendar_id"]
    data = run_tool(
        "GOOGLECALENDAR_EVENTS_LIST",
        {
            "calendarId": calendar_id,
            "timeMin": f"{start.isoformat()}T00:00:00Z",
            "timeMax": f"{end.isoformat()}T23:59:59Z",
            "singleEvents": True,
        },
        "calendar_personal",  # any connected account can read a public calendar
    )
    if not data:
        return set()
    holiday_dates = set()
    for ev in data.get("items", []):
        d = ev.get("start", {}).get("date")
        if d:
            holiday_dates.add(datetime.date.fromisoformat(d))
    return holiday_dates


def fetch_existing_events(start, end):
    events = []
    for key in ("calendar_school", "calendar_personal"):
        data = run_tool(
            "GOOGLECALENDAR_EVENTS_LIST",
            {
                "calendarId": "primary",
                "timeMin": f"{start.isoformat()}T00:00:00Z",
                "timeMax": f"{end.isoformat()}T23:59:59Z",
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 250,
            },
            key,
        )
        if data:
            events.extend(data.get("items", []))
    return events


def fetch_due_dates(start, end):
    if not ACCOUNTS.get("classroom"):
        return []
    courses_data = run_tool(
        "GOOGLE_CLASSROOM_COURSES_LIST",
        {"studentId": "me", "courseStates": ["ACTIVE"], "pageSize": 50},
        "classroom",
    )
    due_items = []
    if not courses_data:
        return due_items
    for course in courses_data.get("courses", []):
        work_data = run_tool(
            "GOOGLE_CLASSROOM_COURSE_WORK_LIST",
            {"courseId": course["id"], "courseWorkStates": ["PUBLISHED"], "pageSize": 50},
            "classroom",
        )
        if not work_data:
            continue
        for item in work_data.get("courseWork", []):
            due = item.get("dueDate")
            if not due:
                continue
            due_date = datetime.date(due["year"], due["month"], due["day"])
            if start <= due_date <= end:
                due_items.append({
                    "course": course.get("name", ""),
                    "title": item.get("title", ""),
                    "due_date": due_date,
                })
    return due_items


def fetch_friday_work_tasks(rules):
    """Read (never reply to) personal Gmail for work-related tasks, filtering spam."""
    data = run_tool(
        "GMAIL_FETCH_EMAILS",
        {"max_results": 25, "user_id": "me"},
        rules["weekly_planner"]["friday_work_task_source"],
    )
    if not data:
        return []
    spam_terms = rules["spam_filters"]["sender_or_subject_contains"]
    tasks = []
    for msg in data.get("messages", []):
        sender = (msg.get("sender") or msg.get("from") or "").lower()
        subject = (msg.get("subject") or "").lower()
        if any(term in sender or term in subject for term in spam_terms):
            continue  # filtered out as marketing/university spam
        tasks.append({"sender": sender, "subject": msg.get("subject", "")})
    return tasks


def build_day_plan(day, rules, holidays, existing_events, due_items, friday_tasks):
    """Ask Claude to propose blocks for a single day, given everything fixed."""
    is_holiday = day in holidays
    is_weekend = day.weekday() >= 5  # Sat=5, Sun=6
    is_friday = day.weekday() == 4

    day_events = [
        e for e in existing_events
        if e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")).startswith(day.isoformat())
    ]
    day_due = [d for d in due_items if d["due_date"] == day]
    days_until = {d["title"]: (d["due_date"] - datetime.date.today()).days for d in due_items}

    context = {
        "date": day.isoformat(),
        "weekday": day.strftime("%A"),
        "is_colombian_holiday": is_holiday,
        "is_weekend": is_weekend,
        "is_friday_work_day": is_friday and rules["weekly_planner"]["friday_is_work_day"],
        "already_on_calendar": [
            {"title": e.get("summary"), "start": e.get("start"), "end": e.get("end")}
            for e in day_events
        ],
        "due_today_or_soon": [
            {"title": d["title"], "course": d["course"], "days_until_due": days_until.get(d["title"], None)}
            for d in due_items
        ],
        "friday_work_emails": friday_tasks if is_friday else [],
        "priority_order": rules["weekly_planner"]["priority_order"],
        "immediate_due_date_threshold_days": rules["weekly_planner"]["immediate_due_date_threshold_days"],
        "school_end_time": rules["weekly_planner"]["school_end_time"],
        "post_school_rest_buffer_minutes": rules["weekly_planner"]["post_school_rest_buffer_minutes"],
        "bedtime": rules["weekly_planner"]["bedtime"],
        "wake_time": rules["weekly_planner"]["wake_time"],
    }

    prompt = f"""You're planning ONE day of David's schedule. Here's everything
fixed and relevant for this day:

{json.dumps(context, indent=2)}

Rules:
- Never touch or move anything already on the calendar (immovable classes,
  accepted meetings). Only propose NEW blocks for open time.
- Rest and downtime come first in priority. Only bump rest for a due date
  that's within {context['immediate_due_date_threshold_days']} days.
- Leave real room for social interaction -- don't schedule every open minute.
- If it's a Colombian holiday or weekend, keep it light: at most 1-2 optional
  blocks (e.g. a study block only if something is due Monday), otherwise
  leave the day open.
- If it's Friday and a work day, shape the evening/day around the work email
  subjects listed, not around school routine.
- Respect the wake/bedtime window -- nothing before wake_time or after bedtime.

Respond ONLY with a JSON array (no other text) of proposed blocks, each:
{{"title": str, "start_time": "HH:MM", "end_time": "HH:MM", "reason": str}}
An empty array is a valid answer if nothing needs to be added today."""

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        print(f"[warn] Couldn't parse Claude's plan for {day}: {raw[:200]}")
        return []


def create_event(rules, day, block):
    prefix = rules["weekly_planner"]["auto_planned_event_prefix"]
    run_tool(
        "GOOGLECALENDAR_CREATE_EVENT",
        {
            "calendarId": "primary",
            "summary": f"{prefix} {block['title']}",
            "description": block.get("reason", ""),
            "start": {"dateTime": f"{day.isoformat()}T{block['start_time']}:00"},
            "end": {"dateTime": f"{day.isoformat()}T{block['end_time']}:00"},
        },
        WRITE_TARGET_ACCOUNT,
    )


def main():
    rules = load_json(RULES_FILE, {})
    start, end = get_week_range()

    holidays = fetch_holidays(rules, start, end)
    existing_events = fetch_existing_events(start, end)
    due_items = fetch_due_dates(start, end)
    friday_tasks = fetch_friday_work_tasks(rules)

    summary_lines = [f"📆 Weekly plan built: {start.strftime('%a %b %d')} → {end.strftime('%a %b %d')}"]

    day = start
    while day <= end:
        blocks = build_day_plan(rules=rules, day=day, holidays=holidays,
                                 existing_events=existing_events, due_items=due_items,
                                 friday_tasks=friday_tasks)
        if rules["weekly_planner"]["auto_create_events"]:
            for block in blocks:
                create_event(rules, day, block)
        if blocks:
            block_summary = ", ".join(f"{b['title']} ({b['start_time']}-{b['end_time']})" for b in blocks)
            summary_lines.append(f"{day.strftime('%a %m/%d')}: {block_summary}")
        else:
            summary_lines.append(f"{day.strftime('%a %m/%d')}: nothing added, day left as-is")
        day += datetime.timedelta(days=1)

    # Ask about progress on anything due soon -- this SURFACES the question;
    # actually adjusting the plan based on your reply needs inbound WhatsApp
    # handling, which isn't built yet (see README "Not built yet" section).
    soon_due = [d for d in due_items if (d["due_date"] - datetime.date.today()).days <= 2]
    if soon_due:
        titles = ", ".join(d["title"] for d in soon_due)
        summary_lines.append(f"\nHow's progress on: {titles}? Reply and I'll factor it into next week.")

    queue_digest("\n".join(summary_lines))
    print("[done] Weekly plan created and queued for next digest.")


if __name__ == "__main__":
    main()

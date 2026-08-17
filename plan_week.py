"""
Run this EVERY MORNING (e.g. 6:00 AM). Builds a rolling 7-day plan --
today through 6 days out -- working around fixed commitments (classes,
accepted meetings, Classroom due dates) and filling open time according to
rules.json's priority order. Since this runs daily, it first clears out
YESTERDAY's auto-added events for the coming days and rebuilds fresh, so
the plan always reflects the current state rather than piling up duplicates.

Everything is written to your SCHOOL calendar (not personal), with a
10-minute-before reminder set on every auto-created event.

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
# Everything gets written to your SCHOOL calendar so reminders/notifications
# (10 min before) show up the same place your other school events do.
WRITE_TARGET_ACCOUNT = "calendar_school"

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

    try:
        account_info = composio.connected_accounts.get(account_id)
        real_user_id = account_info.user_id
    except Exception as e:
        print(f"[warn] Couldn't look up user_id for {account_key} ({account_id}): {e}")
        real_user_id = "default"

    result = composio.tools.execute(
        tool_slug, arguments=arguments, connected_account_id=account_id,
        user_id=real_user_id,
        dangerously_skip_version_check=True,
    )
    if not result.get("successful", False):
        print(f"[warn] {tool_slug} on {account_key} failed: {result.get('error')}")
        return None
    return result.get("data", {})


def get_week_range():
    """Rolling 7-day window: today through 6 days from now."""
    today = datetime.date.today()
    end = today + datetime.timedelta(days=6)
    return today, end


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
    # Look back to the most recent Monday too, not just forward -- otherwise
    # by Wednesday the planner can't see whether Monday's gym actually
    # happened, and "readjust if missed" has nothing to check against.
    days_since_monday = start.weekday()  # Monday=0
    lookback_start = start - datetime.timedelta(days=days_since_monday)

    events = []
    for key in ("calendar_school", "calendar_personal"):
        data = run_tool(
            "GOOGLECALENDAR_EVENTS_LIST",
            {
                "calendarId": "primary",
                "timeMin": f"{lookback_start.isoformat()}T00:00:00Z",
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

    # Count gym sessions already on the calendar THIS WEEK (Mon through
    # today), so Claude can tell if a makeup session is actually needed --
    # this only works now that fetch_existing_events looks back to Monday.
    week_start = day - datetime.timedelta(days=day.weekday())
    gym_this_week = sum(
        1 for e in existing_events
        if "gym" in e.get("summary", "").lower()
        and week_start.isoformat() <= e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")) <= day.isoformat()
    )
    gym_done_today = any(
        "gym" in e.get("summary", "").lower()
        and e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")).startswith(day.isoformat())
        for e in existing_events
    )

    day_events = [
        e for e in existing_events
        if e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")).startswith(day.isoformat())
    ]
    day_due = [d for d in due_items if d["due_date"] == day]
    days_until = {d["title"]: (d["due_date"] - datetime.date.today()).days for d in due_items}

    weekday_name = day.strftime("%A")
    tomorrow_name = (day + datetime.timedelta(days=1)).strftime("%A")
    schedule = rules["weekly_planner"]["daily_schedule"]
    today_facts = schedule.get(weekday_name, {})
    tomorrow_facts = schedule.get(tomorrow_name, {})
    sleep_rules = rules["weekly_planner"]["sleep_rules"]

    # Tonight's bedtime target depends on TOMORROW's wake time, not today's.
    bedtime_target = None
    if tomorrow_facts.get("wake_time"):
        wake_dt = datetime.datetime.strptime(tomorrow_facts["wake_time"], "%H:%M")
        min_before_sleep = datetime.timedelta(
            hours=sleep_rules["minimum_hours"],
            minutes=sleep_rules["cushion_before_minutes"],
        )
        max_before_sleep = datetime.timedelta(
            hours=sleep_rules["minimum_hours"],
            minutes=-sleep_rules["cushion_after_minutes"],
        )
        earliest = (wake_dt - min_before_sleep).strftime("%H:%M")
        latest = (wake_dt - max_before_sleep).strftime("%H:%M")
        bedtime_target = f"between {earliest} and {latest}"

    context = {
        "date": day.isoformat(),
        "weekday": weekday_name,
        "is_colombian_holiday": is_holiday,
        "is_weekend": is_weekend,
        "is_friday_work_day": is_friday and rules["weekly_planner"]["friday_is_work_day"],
        "wake_time_today": today_facts.get("wake_time"),
        "gym_before_school_today": today_facts.get("gym_before_school", False),
        "school_end_time_today": today_facts.get("school_end_time"),
        "extracurricular_today": today_facts.get("extracurricular"),
        "lunch_at_school_today": today_facts.get("lunch_at_school", False),
        "day_notes": today_facts.get("notes"),
        "commute_minutes": rules["weekly_planner"]["commute_minutes"],
        "gym_target_sessions_per_week": rules["weekly_planner"]["gym_target_sessions_per_week"],
        "gym_sessions_already_this_week": gym_this_week,
        "gym_done_today_already": gym_done_today,
        "gym_makeup_priority": rules["weekly_planner"]["gym_makeup_priority"],
        "weekend_open_ratio": rules["weekly_planner"]["weekend_planning"]["open_ratio"] if is_weekend else None,
        "counselor_meeting_notes": rules["weekly_planner"]["counselor_meetings"]["notes"] if day.weekday() >= 5 else None,
        "weekday_block_style": rules["weekly_planner"]["weekday_block_style"] if not is_weekend and not is_holiday else None,
        "tonight_bedtime_target": bedtime_target,
        "tomorrow_weekday": tomorrow_name,
        "tomorrow_wake_time": tomorrow_facts.get("wake_time"),
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
    }

    static_instructions = f"""You're planning ONE day of David's schedule at a time.

Rules:
- Never touch or move anything already on the calendar (immovable classes,
  accepted meetings). Only propose NEW blocks for open time.
- Rest and downtime come first in priority. Only bump rest for a due date
  that's within {rules['weekly_planner']['immediate_due_date_threshold_days']} days.
- Leave real room for social interaction -- don't schedule every open minute.
- If it's a Colombian holiday or weekend, keep it light: at most 1-2 optional
  blocks (e.g. a study block only if something is due Monday), otherwise
  leave the day open. Saturday is also a backup gym day if the week's
  Mon/Wed sessions didn't happen -- check already_on_calendar for gym this
  week before deciding whether to add one.
- If it's Friday and a work day, shape the evening/day around the work email
  subjects listed, not around school routine.
- gym_before_school_today means an early gym+sauna session already happened
  before wake_time_today's normal hour -- don't schedule anything else that
  early, and note the person is already up.
- School gets out at school_end_time_today, but free time doesn't start
  then -- add commute_minutes before the person is actually home. If
  extracurricular_today is set, use ITS end_time instead, plus commute.
- lunch_at_school_today means no need to schedule a lunch/cooking block at
  home; otherwise assume lunch happens shortly after arriving home.
- Track toward gym_target_sessions_per_week (3) across the week using
  gym_sessions_already_this_week. If today is Monday or Wednesday and
  gym_before_school_today is true but gym_done_today_already is false,
  that morning session was skipped -- follow gym_makeup_priority exactly
  (same-day afternoon first, then Fri/Sat/Sun, lighter session on the
  weekend days).
- On weekends, respect weekend_open_ratio -- leave roughly that fraction of
  the day completely open for friends/parties/downtime. Don't over-fill it.
- If weekday_block_style is present (a school day, not weekend/holiday),
  this day should be FULLY blocked -- fill essentially every hour from
  wake_time to tonight_bedtime_target with labeled blocks, no meaningful
  gaps besides real transitions already accounted for. Prefix every new
  block's title with one of weekday_block_style's category_labels (e.g.
  "Golden Hour: ...", "Movement: ...", "Study: ..."). This is the opposite
  of the weekend approach -- don't leave open time on school days.
- If counselor_meeting_notes is present (today is Sat/Sun), don't schedule
  something else into the likely counselor slot unless one is already on
  the calendar (check already_on_calendar first).
- Respect tonight_bedtime_target for when this day's schedule should wind
  down -- nothing pushed past that window. It already accounts for
  tomorrow's wake time, which may be earlier (gym day) than usual.

Respond ONLY with a JSON array (no other text) of proposed blocks, each:
{{"title": str, "start_time": "HH:MM", "end_time": "HH:MM", "reason": str}}
An empty array is a valid answer if nothing needs to be added today."""

    prompt = f"""Here's everything fixed and relevant for this specific day:

{json.dumps(context, indent=2)}"""

    response = claude.messages.create(
        model="claude-sonnet-5",
        max_tokens=800,
        system=[
            {
                "type": "text",
                "text": static_instructions,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": prompt}],
    )
    text_blocks = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    raw = "".join(text_blocks).strip()
    # Be tolerant of any stray text around the actual JSON array, instead of
    # requiring the whole response to be pure JSON (which kept failing).
    start_idx = raw.find("[")
    end_idx = raw.rfind("]")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        raw = raw[start_idx:end_idx + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        block_types = [getattr(b, "type", "?") for b in response.content]
        print(f"[warn] Couldn't parse Claude's plan for {day}. "
              f"stop_reason={getattr(response, 'stop_reason', '?')}, "
              f"block_types={block_types}, raw_length={len(raw)}, "
              f"raw_preview={raw[:200]!r}")
        return []


def delete_previous_auto_events(rules, start, end):
    """Since this runs daily, clear out auto-created events still in the
    upcoming window before rebuilding -- keeps the plan 'ready to modify'
    instead of piling up duplicates every morning."""
    prefix = rules["weekly_planner"]["auto_planned_event_prefix"]
    data = run_tool(
        "GOOGLECALENDAR_EVENTS_LIST",
        {
            "calendarId": "primary",
            "timeMin": f"{start.isoformat()}T00:00:00Z",
            "timeMax": f"{end.isoformat()}T23:59:59Z",
            "q": prefix,
            "singleEvents": True,
        },
        WRITE_TARGET_ACCOUNT,
    )
    if not data:
        return
    for ev in data.get("items", []):
        if ev.get("summary", "").startswith(prefix):
            run_tool(
                "GOOGLECALENDAR_DELETE_EVENT",
                {"calendarId": "primary", "eventId": ev["id"]},
                WRITE_TARGET_ACCOUNT,
            )


def create_event(rules, day, block):
    prefix = rules["weekly_planner"]["auto_planned_event_prefix"]

    start_h, start_m = (int(x) for x in block["start_time"].split(":"))
    end_h, end_m = (int(x) for x in block["end_time"].split(":"))
    total_minutes = (end_h * 60 + end_m) - (start_h * 60 + start_m)
    if total_minutes <= 0:
        total_minutes += 24 * 60  # handles a block that crosses midnight

    run_tool(
        "GOOGLECALENDAR_CREATE_EVENT",
        {
            "calendar_id": "primary",
            "summary": f"{prefix} {block['title']}",
            "description": block.get("reason", ""),
            "start_datetime": f"{day.isoformat()}T{block['start_time']}:00",
            "timezone": "America/Bogota",
            "event_duration_hour": total_minutes // 60,
            "event_duration_minutes": total_minutes % 60,
            "create_meeting_room": False,
        },
        WRITE_TARGET_ACCOUNT,
    )


def main():
    rules = load_json(RULES_FILE, {})
    start, end = get_week_range()

    delete_previous_auto_events(rules, start, end)

    holidays = fetch_holidays(rules, start, end)
    existing_events = fetch_existing_events(start, end)
    due_items = fetch_due_dates(start, end)
    friday_tasks = fetch_friday_work_tasks(rules)

    summary_lines = [f"📆 7-day plan refreshed: {start.strftime('%a %b %d')} → {end.strftime('%a %b %d')}"]

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

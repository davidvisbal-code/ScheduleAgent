# Schedule Agent

Checks Gmail, Google Calendar, and Google Classroom (school + personal accounts,
where connected) for anything new. Flags conflicts against immovable
commitments using Claude, then sends you a WhatsApp summary. Never edits
anything on its own -- read-only checks, one outbound WhatsApp message.

## What counts as immovable vs movable

Edit `rules.json`:
- `immovable_keywords`: event/course titles containing these words are treated
  as unmovable (currently: math, physics, chemistry).
- `movable_keywords`: titles containing these are flagged as flexible
  (currently: gym, counselor, guidance, reading, errand, buy, shopping,
  personal).
- All Classroom coursework is always immovable, regardless of keywords.
- A calendar invite you've marked "accepted" is treated as immovable too --
  but you still get alerted if something new conflicts with it, so you can
  change your mind.
- Anything that doesn't match a keyword defaults to movable.

To change this, either edit the JSON file directly, or tell the agent in a
Claude chat what to add ("mark Guidance as immovable too") and copy its
answer into `immovable_keywords` / `movable_keywords`.

## One-time setup

1. **Get a Composio API key**: composio.dev dashboard → Settings → API Keys.
2. **Get an Anthropic API key**: console.anthropic.com → API Keys. (This is
   separate from your Claude.ai subscription -- it's billed by usage, but
   this script makes very few calls, so cost should be a few cents a month.)
3. **Get your WhatsApp phone_number_id**: this comes from the WhatsApp
   Business setup you already did in Composio -- ask Claude in chat to run
   `WHATSAPP_GET_PHONE_NUMBERS` for you and give you the `id` field.
4. **Get your connected account IDs**: Composio dashboard → each toolkit
   (Gmail, Google Calendar, Google Classroom) → Connected Accounts → copy the
   account ID shown there (the random-word ID, e.g. "ofter-darer") for each
   account (school and/or personal).
5. Copy `.env.example` to `.env` and fill in all the values from steps 1-4.

## Running it

### Option A: small VPS (recommended if you want near-instant updates)

1. Rent a small VPS (DigitalOcean, Hetzner, etc. -- ~$5/month, smallest tier
   is fine).
2. Install Python 3.11+, then:
   ```
   pip install -r requirements.txt
   ```
3. Instead of running `main.py` once, wrap it in a loop so it checks
   continuously. Simplest way, from the VPS terminal:
   ```
   while true; do python main.py; sleep 90; done
   ```
   This checks every 90 seconds. Run this inside `tmux` or `screen` (or set
   it up as a systemd service) so it keeps running after you disconnect.
4. Load your `.env` file before running (e.g. `export $(cat .env | xargs)`
   or use a tool like `python-dotenv`).

### Option B: GitHub Actions (free, but checks every ~15-20 min, not instant)

1. Push this whole folder to a **private** GitHub repository.
2. Go to the repo's Settings → Secrets and variables → Actions, and add each
   value from your `.env` file as a separate secret (same names).
3. The workflow in `.github/workflows/check_schedule.yml` will run
   automatically every 15 minutes. You can also trigger it manually from the
   Actions tab to test it immediately.

## Testing before you trust it

Run it manually once (`python main.py` locally, or "Run workflow" in GitHub
Actions) and confirm you get a WhatsApp message back, even if it just says
"0 updates sent" in the terminal with nothing new to report. Then add a test
event to your calendar and run it again to confirm it gets flagged correctly.

## Limits to know about

- Gmail checking here is intentionally simple (new message = notify) -- it
  doesn't try to judge which emails matter. Refine `fetch_recent_emails()` in
  `main.py` if you want it to filter by sender or keyword.
- WhatsApp won't deliver a free-form message if you haven't messaged the bot
  number within the last 24 hours -- message it occasionally to keep that
  window open, or set up an approved message template in Meta Business Suite.
- Conflict detection only compares event *times* -- it can't tell if two
  things are actually incompatible beyond overlapping on the clock (e.g. it
  won't know a 5-minute walk between buildings isn't enough).

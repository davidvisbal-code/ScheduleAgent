"""
Connects ONE account at a time to this Composio Developer Platform project.
Run once per account -- pick which one via the ACCOUNT_TO_CONNECT input in
the GitHub Actions workflow dropdown.

For Gmail/Calendar, school and personal use the SAME toolkit but need to be
connected as two separate accounts under the same user_id -- the script
handles that automatically (allow_multiple=True on the second one).
"""

import os
import time
from composio import Composio

composio = Composio(api_key=os.environ["COMPOSIO_API_KEY"].strip())

# Which account to connect this run -- set via workflow input.
TARGET = os.environ["ACCOUNT_TO_CONNECT"].strip()

# Maps each target to (toolkit slug, user_id, whether this is a 2nd account
# on the same toolkit that needs allow_multiple=True)
CONFIG = {
    "gmail_school":      {"toolkit": "gmail",           "user_id": "david", "allow_multiple": False},
    "gmail_personal":    {"toolkit": "gmail",           "user_id": "david", "allow_multiple": True},
    "calendar_school":   {"toolkit": "googlecalendar",  "user_id": "david", "allow_multiple": False},
    "calendar_personal": {"toolkit": "googlecalendar",  "user_id": "david", "allow_multiple": True},
    "classroom":         {"toolkit": "googleclassroom", "user_id": "david", "allow_multiple": False},
    "whatsapp":          {"toolkit": "whatsapp",        "user_id": "david", "allow_multiple": False},
}

if TARGET not in CONFIG:
    raise SystemExit(f"Unknown target '{TARGET}'. Must be one of: {list(CONFIG.keys())}")

cfg = CONFIG[TARGET]
toolkit = cfg["toolkit"]
user_id = cfg["user_id"]

print(f"\n=== Connecting: {TARGET} (toolkit: {toolkit}) ===\n")

# If you made a specific auth config yourself (e.g. one with proper
# Classroom scopes via Composio's dashboard), set an env var like
# GYM_AUTH_CONFIG_ID_OVERRIDE / CLASSROOM_AUTH_CONFIG_ID_OVERRIDE to force
# using it, instead of auto-detecting/creating one that might be too narrow.
override_id = os.environ.get(f"{TARGET.upper()}_AUTH_CONFIG_ID_OVERRIDE", "").strip()
if override_id:
    auth_config_id = override_id
    print(f"Using manually specified auth config: {auth_config_id}")
else:
    auth_configs = composio.auth_configs.list(toolkit_slug=toolkit)
    if auth_configs.items:
        auth_config_id = auth_configs.items[0].id
        print(f"Using existing auth config: {auth_config_id}")
    else:
        new_config = composio.auth_configs.create(
            toolkit=toolkit,
            options={"type": "use_composio_managed_auth"},
        )
        auth_config_id = new_config.id
        print(f"Created new auth config: {auth_config_id}")

kwargs = {"user_id": user_id, "auth_config_id": auth_config_id}

# Clean up any incomplete/failed leftover connections from earlier attempts
# tonight, so we end up with exactly one real account, not duplicates.
existing = composio.connected_accounts.list(user_ids=[user_id], auth_config_ids=[auth_config_id])
for acc in existing.items:
    if acc.status != "ACTIVE":
        print(f"Removing incomplete leftover connection: {acc.id} (status: {acc.status})")
        composio.connected_accounts.delete(acc.id)
    elif not cfg["allow_multiple"]:
        print(f"Found an already-ACTIVE connection ({acc.id}) and this toolkit doesn't allow multiple.")
        print(f"If you actually want to replace it, delete it manually in Composio first, then re-run this.")
        raise SystemExit(1)

if cfg["allow_multiple"]:
    connection_request = composio.connected_accounts.link(user_id, auth_config_id, allow_multiple=True)
else:
    connection_request = composio.connected_accounts.link(user_id, auth_config_id)

print("\n" + "=" * 70)
print("OPEN THIS URL IN YOUR BROWSER AND LOG IN / APPROVE ACCESS:")
print(connection_request.redirect_url)
print("=" * 70 + "\n")
print("Waiting up to 5 minutes for you to complete it...\n")

connected = connection_request.wait_for_connection(timeout=300)

print("\n=== SUCCESS ===")
print(f"Connected account ID for {TARGET}:  {connected.id}")
print(f"\nCopy this ID into your GitHub secret: "
      f"{TARGET.upper()}_ACCOUNT_ID = {connected.id}")

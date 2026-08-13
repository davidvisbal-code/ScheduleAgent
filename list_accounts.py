"""
Run this ONCE to see the truth about your connected accounts -- their real
IDs and user_ids, side by side. Not part of the regular system; just a
diagnostic to stop guessing.
"""
import os
from composio import Composio

composio = Composio(api_key=os.environ["COMPOSIO_API_KEY"].strip())

accounts = composio.connected_accounts.list()

print(f"Found {len(accounts.items)} connected account(s):\n")
for acc in accounts.items:
    print(f"  id: {acc.id}")
    print(f"  user_id: {acc.user_id}")
    print(f"  toolkit: {getattr(acc, 'toolkit', getattr(acc, 'app_name', '?'))}")
    print(f"  status: {acc.status}")
    print("  ---")

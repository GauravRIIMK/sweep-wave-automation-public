# -*- coding: utf-8 -*-
"""One-time helper: mint a Gmail refresh token for the GitHub Action.

Run it ONCE on your laptop, sign in as rathore.gaurav1007@gmail.com, and paste the
printed refresh token into the repo secret GMAIL_REFRESH_TOKEN.

    pip install google-auth-oauthlib
    python tools/get_refresh_token.py path/to/client_secret.json
"""
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",   # create drafts
    "https://www.googleapis.com/auth/gmail.modify",    # trash a draft when a reply lands
]

if len(sys.argv) < 2:
    sys.exit("usage: python tools/get_refresh_token.py <client_secret.json>")

flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

print("\n" + "=" * 70)
print("GMAIL_CLIENT_ID     =", creds.client_id)
print("GMAIL_CLIENT_SECRET =", creds.client_secret)
print("GMAIL_REFRESH_TOKEN =", creds.refresh_token)
print("=" * 70)
print("\nAdd these three as repository secrets, then delete this output.")
if not creds.refresh_token:
    print("\nNo refresh token returned. Revoke the app at "
          "https://myaccount.google.com/permissions and run this again.")

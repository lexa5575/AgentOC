"""
Google Sheets OAuth Setup
-------------------------

One-time script to get Sheets API refresh token.
Run locally (NOT in Docker) — opens browser for Google auth.

IMPORTANT: When the browser opens, log in with the email account
that HAS ACCESS to the stock spreadsheet (not necessarily the Gmail account).

Prerequisites:
    1. Go to Google Cloud Console → APIs & Services → Credentials
    2. Create OAuth 2.0 Client ID (type: Desktop App)
    3. Download JSON → save as sheets_credentials.json next to this script

Usage:
    python scripts/sheets_auth.py

Output:
    Prints SHEETS_CLIENT_ID, SHEETS_CLIENT_SECRET, SHEETS_REFRESH_TOKEN
    for your .env file.
"""

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

CREDENTIALS_FILE = Path(__file__).parent / "sheets_credentials.json"


def main():
    if not CREDENTIALS_FILE.exists():
        print(f"ERROR: {CREDENTIALS_FILE} not found!")
        print()
        print("Steps:")
        print("  1. Go to https://console.cloud.google.com/apis/credentials")
        print("  2. Create OAuth 2.0 Client ID (Desktop App)")
        print("  3. Download JSON and save as:")
        print(f"     {CREDENTIALS_FILE}")
        sys.exit(1)

    flow = InstalledAppFlow.from_client_secrets_file(
        str(CREDENTIALS_FILE),
        scopes=SCOPES,
    )

    print()
    print("=" * 60)
    print("IMPORTANT: Log in with the email that has ACCESS")
    print("to the stock spreadsheet!")
    print("=" * 60)
    print()

    # Don't auto-open browser — print URL so user can open in the right browser/profile
    creds = flow.run_local_server(
        port=8086,
        access_type="offline",
        prompt="consent",
        open_browser=False,
    )

    # Read client_id and client_secret from credentials.json
    with open(CREDENTIALS_FILE) as f:
        data = json.load(f)
    client_config = data.get("installed", data.get("web", {}))

    print()
    print("=" * 60)
    print("SUCCESS! Add these to your .env file:")
    print("=" * 60)
    print()
    print(f"SHEETS_CLIENT_ID={client_config['client_id']}")
    print(f"SHEETS_CLIENT_SECRET={client_config['client_secret']}")
    print(f"SHEETS_REFRESH_TOKEN={creds.refresh_token}")
    print()
    print("=" * 60)


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""Standalone script to get a Google Calendar refresh token."""

import os

from google_auth_oauthlib.flow import InstalledAppFlow

# --- Instructions ---
# 1. Make sure you have a `credentials.json` file from Google Cloud Console
#    in the same directory as this script, or set the GOOGLE_CALENDAR_CLIENT_ID
#    and GOOGLE_CALENDAR_CLIENT_SECRET environment variables.
# 2. Run this script from your terminal: `python scripts/get_google_calendar_token.py`
# 3. It will open a browser window for you to authorize the application.
# 4. After authorization, it will print a refresh token.
# 5. Copy this token and add it as `GOOGLE_CALENDAR_REFRESH_TOKEN` in your
#    Railway project's environment variables.
# --- /Instructions ---

CLIENT_ID = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID", "71845778050-vgd7adv8s9i7ej0elnd7d17mpv3kh7jk.apps.googleusercontent.com")
CLIENT_SECRET = os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET", "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")
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly"
]


def main():
    """Runs the OAuth flow and prints the refresh token."""
    client_config = {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    print("\n--- Google Calendar Refresh Token ---")
    print("Copy this token and add it to your Railway environment variables as GOOGLE_CALENDAR_REFRESH_TOKEN:")
    print(f"\n{creds.refresh_token}\n")


if __name__ == "__main__":
    main()

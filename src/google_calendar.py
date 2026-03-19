# -*- coding: utf-8 -*-
"""Google Calendar integration for Family Brain."""

import datetime
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google.oauth2.credentials import Credentials

from . import brain

logger = brain.logger

# --- Environment Variables ---
CLIENT_ID = os.environ.get("GOOGLE_CALENDAR_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GOOGLE_CALENDAR_CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("GOOGLE_CALENDAR_REFRESH_TOKEN")
CALENDAR_ID = os.environ.get(
    "GOOGLE_CALENDAR_ID",
    "primary",
)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def _get_access_token() -> Optional[str]:
    """Get a fresh access token using the refresh token via direct HTTP."""
    if not all([CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN]):
        logger.warning(
            "Google Calendar credentials (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN) not fully configured."
        )
        return None

    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request(
        TOKEN_URI,
        data=data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
            access_token = result.get("access_token")
            if access_token:
                logger.info("Google Calendar: access token obtained successfully.")
                return access_token
            logger.error("Google Calendar: no access_token in response: %s", result)
            return None
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        logger.error("Google Calendar token refresh failed (%s): %s", e.code, body)
        return None
    except Exception as e:
        logger.error("Google Calendar token refresh error: %s", e)
        return None


def _get_credentials() -> Optional[Credentials]:
    """Get Google Calendar credentials using direct token refresh."""
    access_token = _get_access_token()
    if not access_token:
        return None

    creds = Credentials(
        token=access_token,
        refresh_token=REFRESH_TOKEN,
        token_uri=TOKEN_URI,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        scopes=SCOPES,
    )
    return creds


def create_event(
    event_name: str,
    event_date: str,
    event_time: Optional[str] = None,
    location: Optional[str] = None,
    description: Optional[str] = None,
    family_member: Optional[str] = None,
) -> Optional[str]:
    """Creates an event on Google Calendar.

    Args:
        event_name: The name of the event.
        event_date: The date of the event in YYYY-MM-DD format.
        event_time: The time of the event in HH:MM format (optional).
        location: The location of the event (optional).
        description: A description for the event (optional).
        family_member: The family member associated with the event (optional).

    Returns:
        The Google Calendar event ID if successful, otherwise None.
    """
    creds = _get_credentials()
    if not creds:
        logger.warning("Skipping Google Calendar event creation due to missing credentials.")
        return None

    try:
        service = build("calendar", "v3", credentials=creds)

        event_body: dict[str, Any] = {
            "summary": event_name,
            "location": location,
            "description": description,
        }

        if event_time:
            # Event with a specific time (1-hour duration)
            start_dt = datetime.datetime.fromisoformat(f"{event_date}T{event_time}")
            end_dt = start_dt + datetime.timedelta(hours=1)
            event_body["start"] = {
                "dateTime": start_dt.isoformat(),
                "timeZone": "Europe/London",
            }
            event_body["end"] = {
                "dateTime": end_dt.isoformat(),
                "timeZone": "Europe/London",
            }
        else:
            # All-day event
            event_body["start"] = {"date": event_date}
            event_body["end"] = {"date": event_date}

        if family_member:
            event_body["summary"] = f"{event_name} ({family_member})"

        event = (
            service.events()
            .insert(calendarId=CALENDAR_ID, body=event_body)
            .execute()
        )
        logger.info(f"Event created: {event.get('htmlLink')}")
        return event.get("id")

    except HttpError as error:
        logger.error(f"An error occurred with Google Calendar API: {error}")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        return None

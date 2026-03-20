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
    # Debug: log credential lengths to verify they're loaded correctly
    logger.info(
        "[GCAL DEBUG] CLIENT_ID len=%d, CLIENT_SECRET len=%d, REFRESH_TOKEN len=%d",
        len(CLIENT_ID), len(CLIENT_SECRET), len(REFRESH_TOKEN)
    )

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
    end_time: Optional[str] = None,
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

        # Build description — append return time for all-day events
        full_description = description or ""
        if end_time and not event_time:
            # All-day event with a return/end time
            full_description = f"Back by {end_time}" + (f"\n{description}" if description else "")

        event_body: dict[str, Any] = {
            "summary": event_name,
            "location": location,
            "description": full_description or None,
        }

        if event_time:
            # Event with a specific start time
            start_dt = datetime.datetime.fromisoformat(f"{event_date}T{event_time}")
            if end_time:
                end_dt = datetime.datetime.fromisoformat(f"{event_date}T{end_time}")
            else:
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
            # All-day event — next day is the exclusive end date for Google Calendar
            start_date = datetime.date.fromisoformat(event_date)
            end_date = start_date + datetime.timedelta(days=1)
            event_body["start"] = {"date": event_date}
            event_body["end"] = {"date": end_date.isoformat()}

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

def get_events(
    time_min: str,
    time_max: str,
    max_results: int = 50,
) -> list[dict]:
    """Fetch events from Google Calendar within a time range.

    Args:
        time_min: Start of range in RFC3339 format (e.g. '2026-03-20T00:00:00Z')
        time_max: End of range in RFC3339 format (e.g. '2026-03-27T23:59:59Z')
        max_results: Maximum number of events to return (default 50)

    Returns:
        List of event dicts with keys: id, summary, start, end, description, location
        Returns empty list on error or if calendar not configured.
    """
    creds = _get_credentials()
    if not creds:
        logger.warning("Skipping Google Calendar read due to missing credentials.")
        return []
    try:
        service = build("calendar", "v3", credentials=creds)
        events_result = (
            service.events()
            .list(
                calendarId=CALENDAR_ID,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        items = events_result.get("items", [])
        events = []
        for item in items:
            start = item.get("start", {})
            end = item.get("end", {})
            events.append({
                "id": item.get("id", ""),
                "summary": item.get("summary", ""),
                "start": start.get("dateTime") or start.get("date", ""),
                "end": end.get("dateTime") or end.get("date", ""),
                "description": item.get("description", ""),
                "location": item.get("location", ""),
            })
        logger.info("Google Calendar: fetched %d events (%s to %s)", len(events), time_min, time_max)
        return events
    except HttpError as error:
        logger.error("Google Calendar read error: %s", error)
        return []
    except Exception as e:
        logger.error("Google Calendar read unexpected error: %s", e)
        return []

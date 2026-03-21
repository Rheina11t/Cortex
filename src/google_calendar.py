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
# Fallback static token for backward compatibility
STATIC_REFRESH_TOKEN = os.environ.get("GOOGLE_CALENDAR_REFRESH_TOKEN")
CALENDAR_ID = os.environ.get(
    "GOOGLE_CALENDAR_ID",
    "primary",
)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


def _get_refresh_token(family_id: Optional[str] = None) -> Optional[str]:
    """Get the refresh token for a family, falling back to the static env var."""
    if family_id and brain._supabase:
        try:
            result = brain._supabase.table("families").select("google_refresh_token").eq("family_id", family_id).limit(1).execute()
            if result.data and result.data[0].get("google_refresh_token"):
                return result.data[0]["google_refresh_token"]
        except Exception as exc:
            logger.warning("Failed to fetch google_refresh_token for family %s: %s", family_id, exc)
    
    return STATIC_REFRESH_TOKEN


def _get_access_token(refresh_token: str) -> Optional[str]:
    """Get a fresh access token using the refresh token via direct HTTP."""
    if not all([CLIENT_ID, CLIENT_SECRET, refresh_token]):
        logger.warning(
            "Google Calendar credentials (CLIENT_ID, CLIENT_SECRET, refresh_token) not fully configured."
        )
        return None
    
    data = urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "refresh_token": refresh_token,
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


def _get_credentials(family_id: Optional[str] = None) -> Optional[Credentials]:
    """Get Google Calendar credentials using direct token refresh."""
    refresh_token = _get_refresh_token(family_id)
    if not refresh_token:
        return None

    access_token = _get_access_token(refresh_token)
    if not access_token:
        return None

    creds = Credentials(
        token=access_token,
        refresh_token=refresh_token,
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
    family_id: Optional[str] = None,
) -> Optional[str]:
    """Creates an event on Google Calendar.

    Args:
        event_name: The name of the event.
        event_date: The date of the event in YYYY-MM-DD format.
        event_time: The time of the event in HH:MM format (optional).
        location: The location of the event (optional).
        description: A description for the event (optional).
        family_member: The family member associated with the event (optional).
        family_id: The family ID to look up the correct Google Calendar token.

    Returns:
        The Google Calendar event ID if successful, otherwise None.
    """
    creds = _get_credentials(family_id)
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

def create_recurring_event(
    family_id: str,
    title: str,
    start_datetime: str,
    recurrence_rule: str,
    recurrence_day: Optional[str] = None,
    recurrence_end: Optional[str] = None,
    recurrence_count: Optional[int] = None,
    family_member: Optional[str] = None,
) -> Optional[str]:
    """Creates a recurring event on Google Calendar.

    Args:
        family_id: The family ID to look up the correct Google Calendar token.
        title: The name of the event.
        start_datetime: The start datetime of the first occurrence in ISO format (e.g. '2026-03-24T16:00:00').
        recurrence_rule: One of "WEEKLY", "BIWEEKLY", "MONTHLY", "WEEKDAYS", "WEEKENDS".
        recurrence_day: Day of week if weekly/biweekly (e.g. "TUESDAY").
        recurrence_end: End date if mentioned (YYYY-MM-DD).
        recurrence_count: Number of occurrences if mentioned.
        family_member: The family member associated with the event.

    Returns:
        The Google Calendar event ID if successful, otherwise None.
    """
    creds = _get_credentials(family_id)
    if not creds:
        logger.warning("Skipping Google Calendar recurring event creation due to missing credentials.")
        return None

    try:
        service = build("calendar", "v3", credentials=creds)

        # Build RRULE string
        rrule_parts = []
        
        rule_upper = recurrence_rule.upper()
        if rule_upper == "WEEKLY":
            rrule_parts.append("FREQ=WEEKLY")
            if recurrence_day:
                day_map = {"MONDAY": "MO", "TUESDAY": "TU", "WEDNESDAY": "WE", "THURSDAY": "TH", "FRIDAY": "FR", "SATURDAY": "SA", "SUNDAY": "SU"}
                day_abbr = day_map.get(recurrence_day.upper())
                if day_abbr:
                    rrule_parts.append(f"BYDAY={day_abbr}")
        elif rule_upper == "BIWEEKLY":
            rrule_parts.append("FREQ=WEEKLY;INTERVAL=2")
            if recurrence_day:
                day_map = {"MONDAY": "MO", "TUESDAY": "TU", "WEDNESDAY": "WE", "THURSDAY": "TH", "FRIDAY": "FR", "SATURDAY": "SA", "SUNDAY": "SU"}
                day_abbr = day_map.get(recurrence_day.upper())
                if day_abbr:
                    rrule_parts.append(f"BYDAY={day_abbr}")
        elif rule_upper == "MONTHLY":
            rrule_parts.append("FREQ=MONTHLY")
        elif rule_upper == "WEEKDAYS":
            rrule_parts.append("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")
        elif rule_upper == "WEEKENDS":
            rrule_parts.append("FREQ=WEEKLY;BYDAY=SA,SU")
        else:
            logger.warning(f"Unknown recurrence rule: {recurrence_rule}")
            return None

        if recurrence_end:
            # Format YYYYMMDD
            end_str = recurrence_end.replace("-", "")
            rrule_parts.append(f"UNTIL={end_str}T235959Z")
        elif recurrence_count:
            rrule_parts.append(f"COUNT={recurrence_count}")

        rrule_string = f"RRULE:{';'.join(rrule_parts)}"

        summary = title
        if family_member:
            summary = f"{title} ({family_member})"

        start_dt = datetime.datetime.fromisoformat(start_datetime)
        end_dt = start_dt + datetime.timedelta(hours=1)

        event_body: dict[str, Any] = {
            "summary": summary,
            "start": {
                "dateTime": start_dt.isoformat(),
                "timeZone": "Europe/London",
            },
            "end": {
                "dateTime": end_dt.isoformat(),
                "timeZone": "Europe/London",
            },
            "recurrence": [rrule_string],
        }

        event = (
            service.events()
            .insert(calendarId=CALENDAR_ID, body=event_body)
            .execute()
        )
        logger.info(f"Recurring event created: {event.get('htmlLink')}")
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
    family_id: Optional[str] = None,
) -> list[dict]:
    """Fetch events from Google Calendar within a time range.

    Args:
        time_min: Start of range in RFC3339 format (e.g. '2026-03-20T00:00:00Z')
        time_max: End of range in RFC3339 format (e.g. '2026-03-27T23:59:59Z')
        max_results: Maximum number of events to return (default 50)
        family_id: The family ID to look up the correct Google Calendar token.

    Returns:
        List of event dicts with keys: id, summary, start, end, description, location
        Returns empty list on error or if calendar not configured.
    """
    creds = _get_credentials(family_id)
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

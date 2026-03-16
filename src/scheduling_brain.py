#!/usr/bin/env python3
"""
Family Brain – Scheduling Module.

Provides functions for managing family events, detecting schedule
conflicts, and querying the family calendar.  Used by both the
Telegram capture layer and the MCP server.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .config import get_settings

logger = logging.getLogger("open_brain.scheduling")


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_db = None
_initialised = False


def init(supabase_client=None) -> None:
    """Initialise the scheduling module with a Supabase client."""
    global _db, _initialised

    if supabase_client is not None:
        _db = supabase_client
        _initialised = True
        logger.info("Scheduling brain initialised with provided Supabase client.")
        return

    # Fall back to creating our own client
    try:
        from . import brain
        db, _ = brain._require_init()
        _db = db
        _initialised = True
        logger.info("Scheduling brain initialised from brain module.")
    except Exception as exc:
        logger.warning("Scheduling brain init failed: %s", exc)
        _initialised = False


def _require_db():
    """Return the Supabase client, raising if not initialised."""
    if not _initialised or _db is None:
        raise RuntimeError("Scheduling brain not initialised. Call init() first.")
    return _db


# ---------------------------------------------------------------------------
# Event CRUD
# ---------------------------------------------------------------------------
def add_event(
    family_member: str,
    event_name: str,
    event_date: str,
    event_time: Optional[str] = None,
    end_date: Optional[str] = None,
    location: str = "",
    recurring: bool = False,
    recurrence_pattern: str = "",
    requirements: Optional[list[str]] = None,
    notes: str = "",
    source: str = "manual",
) -> dict[str, Any]:
    """Add a new family event and return the created record."""
    db = _require_db()

    row = {
        "family_member": family_member,
        "event_name": event_name,
        "event_date": event_date,
        "event_time": event_time,
        "end_date": end_date,
        "location": location,
        "recurring": recurring,
        "recurrence_pattern": recurrence_pattern,
        "requirements": requirements or [],
        "notes": notes,
        "source": source,
    }

    result = db.table("family_events").insert(row).execute()

    if not result.data:
        raise RuntimeError("Failed to insert event — no data returned.")

    record = result.data[0]
    logger.info("Event added: %s for %s on %s (id=%s)",
                event_name, family_member, event_date, record.get("id"))
    return record


def get_events_in_range(
    start_date: str,
    end_date: str,
    family_member: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return all events between start_date and end_date (inclusive)."""
    db = _require_db()

    query = (
        db.table("family_events")
        .select("*")
        .gte("event_date", start_date)
        .lte("event_date", end_date)
        .order("event_date")
        .order("event_time", desc=False, nullsfirst=True)
    )

    if family_member:
        # Include both the specific member's events and shared "family" events
        query = query.or_(
            f"family_member.eq.{family_member},family_member.eq.family"
        )

    result = query.execute()
    return result.data or []


def get_events_on_date(
    check_date: str,
    family_member: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Return all events on a specific date."""
    return get_events_in_range(check_date, check_date, family_member)


def check_conflicts(
    event_date: str,
    family_member: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Check for scheduling conflicts on a given date.

    Returns all existing events on that date for the specified family
    member (or all members if None).
    """
    db = _require_db()

    try:
        # Try the RPC function first (created in migration 004)
        result = db.rpc(
            "check_schedule_conflicts",
            {
                "check_date": event_date,
                "check_member": family_member,
            },
        ).execute()
        return result.data or []
    except Exception:
        # Fall back to direct query
        return get_events_on_date(event_date, family_member)


def delete_event(event_id: str) -> bool:
    """Delete an event by ID. Returns True if successful."""
    db = _require_db()
    result = db.table("family_events").delete().eq("id", event_id).execute()
    return bool(result.data)


def update_event(event_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    """Update an event by ID. Returns the updated record."""
    db = _require_db()
    result = db.table("family_events").update(updates).eq("id", event_id).execute()
    if not result.data:
        raise RuntimeError(f"Event {event_id} not found.")
    return result.data[0]


# ---------------------------------------------------------------------------
# Household items CRUD
# ---------------------------------------------------------------------------
def add_household_item(
    user_id: str,
    name: str,
    category: str = "other",
    location: str = "",
    details: Optional[dict[str, Any]] = None,
    notes: str = "",
) -> dict[str, Any]:
    """Add a new household item and return the created record."""
    db = _require_db()

    row = {
        "user_id": user_id,
        "name": name,
        "category": category,
        "location": location,
        "details": details or {},
        "notes": notes,
    }

    result = db.table("household_items").insert(row).execute()
    if not result.data:
        raise RuntimeError("Failed to insert household item.")
    return result.data[0]


def get_household_items(
    category: Optional[str] = None,
    location: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query household items with optional filters."""
    db = _require_db()

    query = db.table("household_items").select("*").order("created_at", desc=True).limit(limit)

    if category:
        query = query.eq("category", category)
    if location:
        query = query.ilike("location", f"%{location}%")

    result = query.execute()
    return result.data or []


def search_household_items(search_term: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search household items by name (case-insensitive partial match)."""
    db = _require_db()

    result = (
        db.table("household_items")
        .select("*")
        .ilike("name", f"%{search_term}%")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


# ---------------------------------------------------------------------------
# Household vendors CRUD
# ---------------------------------------------------------------------------
def add_household_vendor(
    user_id: str,
    name: str,
    trade: str = "other",
    phone: str = "",
    vendor_email: str = "",
    rating: Optional[int] = None,
    notes: str = "",
) -> dict[str, Any]:
    """Add a new household vendor and return the created record."""
    db = _require_db()

    row = {
        "user_id": user_id,
        "name": name,
        "trade": trade,
        "phone": phone,
        "email": vendor_email,
        "rating": rating,
        "notes": notes,
    }

    result = db.table("household_vendors").insert(row).execute()
    if not result.data:
        raise RuntimeError("Failed to insert household vendor.")
    return result.data[0]


def get_household_vendors(
    trade: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query household vendors with optional trade filter."""
    db = _require_db()

    query = db.table("household_vendors").select("*").order("name").limit(limit)

    if trade:
        query = query.eq("trade", trade)

    result = query.execute()
    return result.data or []


def search_household_vendors(search_term: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search vendors by name (case-insensitive partial match)."""
    db = _require_db()

    result = (
        db.table("household_vendors")
        .select("*")
        .ilike("name", f"%{search_term}%")
        .order("name")
        .limit(limit)
        .execute()
    )
    return result.data or []


# ---------------------------------------------------------------------------
# Summary / digest helpers
# ---------------------------------------------------------------------------
def get_upcoming_events(days: int = 7) -> list[dict[str, Any]]:
    """Return all events in the next N days."""
    today = date.today()
    end = today + timedelta(days=days)
    return get_events_in_range(today.isoformat(), end.isoformat())


def get_recent_household_items(days: int = 7, limit: int = 10) -> list[dict[str, Any]]:
    """Return household items added in the last N days."""
    db = _require_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    result = (
        db.table("household_items")
        .select("*")
        .gte("created_at", cutoff)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


def get_recent_vendors(days: int = 7, limit: int = 10) -> list[dict[str, Any]]:
    """Return vendors added in the last N days."""
    db = _require_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    result = (
        db.table("household_vendors")
        .select("*")
        .gte("created_at", cutoff)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []

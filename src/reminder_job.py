"""
reminder_job.py — Proactive reminder system for FamilyBrain
============================================================
Runs as a scheduled job (APScheduler) and/or via a Railway cron HTTP trigger.

Responsibilities:
  1. Query family_events for events today and tomorrow
  2. Query memories for bookings, appointments, and reminders matching today/tomorrow
  3. Compose a single grouped WhatsApp message per family (not one per item)
  4. Respect per-family reminder preferences (enabled/disabled, preferred time)
  5. Respect quiet hours (07:00–21:00 Europe/London only)
  6. Schedule day-of nudges 2 hours before timed events
  7. Deduplicate: never send the same reminder twice in 24 hours

Usage (APScheduler):
    from src.reminder_job import run_daily_reminders
    scheduler.add_job(run_daily_reminders, trigger="cron", hour=8, minute=0, ...)

Usage (HTTP trigger):
    POST /whatsapp/trigger-reminders
    Header: X-Cron-Secret: <CRON_SECRET env var>
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Optional

import pytz

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_LONDON_TZ = pytz.timezone("Europe/London")
_QUIET_START = 21   # 21:00 — do not send after this hour
_QUIET_END   = 7    # 07:00 — do not send before this hour

# Memory categories / keywords that indicate a booking, appointment, or reminder
_REMINDER_CATEGORIES = {
    "booking", "appointment", "reminder", "reservation", "booking ref",
    "hotel", "flight", "dentist", "doctor", "gp", "hospital", "clinic",
    "school trip", "permission slip", "deadline", "due date", "check-in",
    "check in", "checkout", "check out", "meeting", "interview", "exam",
    "test", "vaccination", "jab", "prescription", "collection", "delivery",
    "parcel", "pickup", "pick up", "service", "mot", "insurance",
}

# Patterns used to extract booking references from memory content
_BOOKING_REF_PATTERNS = [
    re.compile(r'\b(?:booking|ref(?:erence)?|reservation|conf(?:irmation)?|order|ticket)\s*(?:no\.?|number|#|:)?\s*([A-Z0-9]{4,12})\b', re.IGNORECASE),
    re.compile(r'\b([A-Z]{2,4}[0-9]{4,8})\b'),   # e.g. BA1234567, REF8472XY
]

# Patterns to extract dates from memory content
_DATE_PATTERNS = [
    (re.compile(r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})'),
     lambda m: date(int(m.group(3)), int(m.group(2)), int(m.group(1)))),
    (re.compile(r'(\d{4})-(\d{2})-(\d{2})'),
     lambda m: date(int(m.group(1)), int(m.group(2)), int(m.group(3)))),
    (re.compile(r'(\d{1,2})\s+(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{4})', re.IGNORECASE),
     lambda m: _parse_month_date(m)),
]

_MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


def _parse_month_date(m: re.Match) -> date:
    day = int(m.group(1))
    month_str = m.group(2)[:3].lower()
    year = int(m.group(3))
    month = _MONTH_MAP.get(month_str, 1)
    return date(year, month, day)


# ---------------------------------------------------------------------------
# Quiet hours check
# ---------------------------------------------------------------------------

def _is_quiet_hours() -> bool:
    """Return True if current London time is outside 07:00–21:00."""
    now = datetime.now(_LONDON_TZ)
    return now.hour >= _QUIET_START or now.hour < _QUIET_END


# ---------------------------------------------------------------------------
# Deduplication helpers (uses cortex_briefings table)
# ---------------------------------------------------------------------------

def _reminder_hash(family_id: str, content: str) -> str:
    return hashlib.md5(f"{family_id}:{content}".encode()).hexdigest()


def _was_reminder_sent(db, family_id: str, content_hash: str, within_hours: int = 24) -> bool:
    """Return True if an identical reminder was already sent within the given window."""
    try:
        cutoff = (datetime.now(pytz.UTC) - timedelta(hours=within_hours)).isoformat()
        res = (
            db.table("cortex_briefings")
            .select("id")
            .eq("family_id", family_id)
            .eq("briefing_type", "reminder")
            .eq("content_hash", content_hash)
            .gte("delivered_at", cutoff)
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception as exc:
        logger.warning("reminder dedup check failed: %s", exc)
        return False


def _log_reminder_sent(db, family_id: str, content_hash: str) -> None:
    """Record that a reminder was sent to prevent duplicates."""
    try:
        db.table("cortex_briefings").insert({
            "family_id": family_id,
            "briefing_type": "reminder",
            "content_hash": content_hash,
        }).execute()
    except Exception as exc:
        logger.warning("Failed to log reminder: %s", exc)


# ---------------------------------------------------------------------------
# Booking reference extractor
# ---------------------------------------------------------------------------

def _extract_booking_ref(text: str) -> Optional[str]:
    for pattern in _BOOKING_REF_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Date extractor for memories
# ---------------------------------------------------------------------------

def _extract_dates_from_text(text: str) -> list[date]:
    """Return all dates found in a text string."""
    found: list[date] = []
    for pattern, parser in _DATE_PATTERNS:
        for m in pattern.finditer(text):
            try:
                found.append(parser(m))
            except (ValueError, KeyError):
                pass
    return found


# ---------------------------------------------------------------------------
# Memory relevance filter
# ---------------------------------------------------------------------------

def _is_reminder_memory(content: str, metadata: dict) -> bool:
    """Return True if this memory looks like a booking, appointment, or reminder."""
    content_lower = content.lower()
    # Check metadata category
    category = (metadata.get("category") or "").lower()
    if category in _REMINDER_CATEGORIES:
        return True
    # Check content keywords
    return any(kw in content_lower for kw in _REMINDER_CATEGORIES)


# ---------------------------------------------------------------------------
# Message formatter
# ---------------------------------------------------------------------------

def _day_label(target_date: date, today: date) -> str:
    delta = (target_date - today).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    # Day name for 2–6 days out
    return target_date.strftime("%A")


def _format_event_line(event: dict, today: date) -> str:
    """Format a single family_event row into a reminder line."""
    name = event.get("event_name") or "Event"
    member = event.get("family_member") or ""
    event_date = event.get("event_date")
    event_time = event.get("event_time") or ""
    location = (event.get("location") or "").strip()
    notes = (event.get("notes") or "").strip()
    requirements = event.get("requirements") or []

    try:
        target = date.fromisoformat(str(event_date))
        day = _day_label(target, today)
    except (ValueError, TypeError):
        day = "Upcoming"

    # Choose emoji based on content
    name_lower = name.lower()
    if any(w in name_lower for w in ("dentist", "doctor", "gp", "hospital", "clinic", "vaccination", "jab")):
        emoji = "🦷" if "dentist" in name_lower else "🏥"
    elif any(w in name_lower for w in ("school", "trip", "exam", "test", "permission")):
        emoji = "🎒"
    elif any(w in name_lower for w in ("flight", "train", "travel", "airport")):
        emoji = "✈️"
    elif any(w in name_lower for w in ("hotel", "check-in", "check in", "stay")):
        emoji = "🏨"
    elif any(w in name_lower for w in ("meeting", "interview")):
        emoji = "💼"
    elif any(w in name_lower for w in ("birthday", "party", "celebration")):
        emoji = "🎂"
    else:
        emoji = "📋" if day == "Today" else "🗓"

    # Build the line
    time_part = f" at {event_time[:5]}" if event_time else ""
    member_part = f" ({member})" if member else ""
    line = f"{emoji} *{day}*: {name}{time_part}{member_part}"

    if location:
        line += f"\n   📍 {location}"
    if notes:
        line += f"\n   📝 {notes[:120]}"
    if requirements:
        req_str = ", ".join(str(r) for r in requirements[:3])
        line += f"\n   ✅ Needed: {req_str}"

    return line


def _format_memory_line(content: str, target_date: date, today: date) -> str:
    """Format a memory into a reminder line."""
    day = _day_label(target_date, today)
    snippet = content[:120].strip()
    # Truncate at sentence boundary if possible
    for sep in (". ", "! ", "? ", "\n"):
        idx = snippet.find(sep)
        if 0 < idx < 100:
            snippet = snippet[:idx + 1]
            break

    booking_ref = _extract_booking_ref(content)
    line = f"📌 *{day}*: {snippet}"
    if booking_ref:
        line += f"\n   🔖 Ref: {booking_ref}"
    return line


# ---------------------------------------------------------------------------
# Core query functions
# ---------------------------------------------------------------------------

def _get_upcoming_events(db, family_id: str, today: date, tomorrow: date) -> list[dict]:
    """Fetch family_events for today and tomorrow for the given family."""
    try:
        res = (
            db.table("family_events")
            .select("*")
            .eq("family_id", family_id)
            .in_("event_date", [today.isoformat(), tomorrow.isoformat()])
            .order("event_date")
            .order("event_time")
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.warning("Failed to fetch upcoming events for %s: %s", family_id, exc)
        return []


def _get_reminder_memories(db, family_id: str, today: date, tomorrow: date) -> list[tuple[str, date]]:
    """
    Fetch memories that look like bookings/appointments with a date matching
    today or tomorrow. Returns list of (content, matched_date) tuples.
    """
    try:
        # Fetch recent memories with reminder-like categories
        res = (
            db.table("memories")
            .select("content, metadata")
            .contains("metadata", {"family_id": family_id})
            .order("created_at", desc=True)
            .limit(500)
            .execute()
        )
        memories = res.data or []
    except Exception as exc:
        logger.warning("Failed to fetch memories for %s: %s", family_id, exc)
        return []

    results: list[tuple[str, date]] = []
    target_dates = {today, tomorrow}

    for mem in memories:
        content = mem.get("content") or ""
        metadata = mem.get("metadata") or {}
        if not _is_reminder_memory(content, metadata):
            continue
        dates = _extract_dates_from_text(content)
        for d in dates:
            if d in target_dates:
                results.append((content, d))
                break  # only add each memory once

    return results


# ---------------------------------------------------------------------------
# Family preferences loader
# ---------------------------------------------------------------------------

def _get_family_preferences(db, family_id: str) -> dict:
    """Return reminder preferences for a family. Falls back to defaults."""
    defaults = {"reminders_enabled": True, "reminder_time": "08:00"}
    try:
        res = (
            db.table("families")
            .select("reminders_enabled, reminder_time, primary_phone")
            .eq("family_id", family_id)
            .limit(1)
            .execute()
        )
        if res.data:
            row = res.data[0]
            return {
                "reminders_enabled": row.get("reminders_enabled", True),
                "reminder_time": row.get("reminder_time") or "08:00",
                "primary_phone": row.get("primary_phone") or "",
            }
    except Exception as exc:
        logger.warning("Failed to load preferences for %s: %s", family_id, exc)
    return defaults


def _get_family_phones(db, family_id: str) -> list[str]:
    """Return all WhatsApp phone numbers for a family."""
    phones: list[str] = []
    try:
        res = (
            db.table("whatsapp_members")
            .select("phone")
            .eq("family_id", family_id)
            .execute()
        )
        phones = [r["phone"] for r in (res.data or []) if r.get("phone")]
    except Exception as exc:
        logger.warning("Failed to fetch phones for %s: %s", family_id, exc)
    return phones


# ---------------------------------------------------------------------------
# Send helper (mirrors _send_proactive_message in whatsapp_capture.py)
# ---------------------------------------------------------------------------

def _send_reminder_message(to_phone: str, body: str) -> None:
    """
    Send a WhatsApp message using the same transport as the main app.
    Imports lazily to avoid circular imports.
    """
    try:
        from . import whatsapp_capture as _wc  # type: ignore[import]
        _wc._send_proactive_message(to=f"whatsapp:{to_phone}", body=body)
    except Exception as exc:
        logger.error("Failed to send reminder to %s: %s", to_phone, exc)


# ---------------------------------------------------------------------------
# Day-of nudge scheduler
# ---------------------------------------------------------------------------

def _schedule_day_of_nudge(scheduler, family_id: str, event: dict, phones: list[str]) -> None:
    """
    If an event has a time today, schedule a nudge 2 hours before.
    Only schedules if the nudge time is still in the future.
    """
    event_time_str = event.get("event_time")
    if not event_time_str:
        return

    try:
        now = datetime.now(_LONDON_TZ)
        today = now.date()
        event_date_str = event.get("event_date")
        if not event_date_str:
            return
        event_date = date.fromisoformat(str(event_date_str))
        if event_date != today:
            return  # only schedule day-of nudges for today's events

        # Parse event time
        t_parts = str(event_time_str).split(":")
        event_hour = int(t_parts[0])
        event_minute = int(t_parts[1]) if len(t_parts) > 1 else 0
        event_dt = _LONDON_TZ.localize(datetime(today.year, today.month, today.day, event_hour, event_minute))
        nudge_dt = event_dt - timedelta(hours=2)

        if nudge_dt <= now:
            return  # too late to schedule
        if nudge_dt.hour < _QUIET_END or nudge_dt.hour >= _QUIET_START:
            return  # would fall in quiet hours

        name = event.get("event_name") or "Event"
        member = event.get("family_member") or ""
        location = (event.get("location") or "").strip()
        member_part = f" ({member})" if member else ""
        body = f"⏰ *Reminder — in 2 hours*: {name}{member_part} at {event_time_str[:5]}"
        if location:
            body += f"\n📍 {location}"
        body += "\n\nReply /help for anything else."

        job_id = f"nudge_{family_id}_{event.get('id', name)}"

        def _send_nudge():
            for phone in phones:
                _send_reminder_message(phone, body)
            logger.info("Day-of nudge sent for %s / %s", family_id, name)

        try:
            scheduler.add_job(
                _send_nudge,
                trigger="date",
                run_date=nudge_dt,
                id=job_id,
                replace_existing=True,
            )
            logger.info("Scheduled day-of nudge for %s at %s", name, nudge_dt.strftime("%H:%M"))
        except Exception as exc:
            logger.warning("Could not schedule nudge for %s: %s", name, exc)

    except Exception as exc:
        logger.warning("Error scheduling day-of nudge: %s", exc)


# ---------------------------------------------------------------------------
# Core reminder runner — one family
# ---------------------------------------------------------------------------

def _run_reminders_for_family(
    db,
    family_id: str,
    scheduler=None,
) -> bool:
    """
    Run the reminder job for a single family.
    Returns True if a reminder was sent, False otherwise.
    """
    prefs = _get_family_preferences(db, family_id)
    if not prefs.get("reminders_enabled", True):
        logger.debug("Reminders disabled for %s — skipping", family_id)
        return False

    tz = _LONDON_TZ
    now = datetime.now(tz)
    today = now.date()
    tomorrow = today + timedelta(days=1)

    # Fetch upcoming items
    events = _get_upcoming_events(db, family_id, today, tomorrow)
    memory_reminders = _get_reminder_memories(db, family_id, today, tomorrow)

    if not events and not memory_reminders:
        logger.debug("No upcoming reminders for %s", family_id)
        return False

    # Build message lines
    lines: list[str] = []

    # --- Events section ---
    today_events = [e for e in events if str(e.get("event_date")) == today.isoformat()]
    tomorrow_events = [e for e in events if str(e.get("event_date")) == tomorrow.isoformat()]

    if today_events:
        lines.append("*📅 Today:*")
        for ev in today_events:
            lines.append(_format_event_line(ev, today))
        lines.append("")

    if tomorrow_events:
        lines.append("*🗓 Tomorrow:*")
        for ev in tomorrow_events:
            lines.append(_format_event_line(ev, today))
        lines.append("")

    # --- Memory reminders section ---
    today_mems = [(c, d) for c, d in memory_reminders if d == today]
    tomorrow_mems = [(c, d) for c, d in memory_reminders if d == tomorrow]

    if today_mems:
        if not today_events:
            lines.append("*📅 Today:*")
        for content, d in today_mems:
            lines.append(_format_memory_line(content, d, today))
        lines.append("")

    if tomorrow_mems:
        if not tomorrow_events:
            lines.append("*🗓 Tomorrow:*")
        for content, d in tomorrow_mems:
            lines.append(_format_memory_line(content, d, today))
        lines.append("")

    # Footer
    lines.append("Reply /help for anything else. 💙")

    message = "\n".join(lines).strip()

    # Deduplicate
    content_hash = _reminder_hash(family_id, message)
    if _was_reminder_sent(db, family_id, content_hash, within_hours=20):
        logger.debug("Duplicate reminder suppressed for %s", family_id)
        return False

    # Get phones
    phones = _get_family_phones(db, family_id)
    if not phones:
        # Fallback to primary phone
        primary = prefs.get("primary_phone") or ""
        if primary:
            phones = [primary]

    if not phones:
        logger.warning("No phones found for family %s — cannot send reminder", family_id)
        return False

    # Send
    for phone in phones:
        _send_reminder_message(phone, message)

    _log_reminder_sent(db, family_id, content_hash)
    logger.info(
        "Reminder sent to %s (%d recipients): %d events, %d memory reminders",
        family_id, len(phones), len(events), len(memory_reminders),
    )

    # Schedule day-of nudges for today's timed events
    if scheduler:
        for ev in today_events:
            _schedule_day_of_nudge(scheduler, family_id, ev, phones)

    return True


# ---------------------------------------------------------------------------
# Main entry point — all families
# ---------------------------------------------------------------------------

def run_daily_reminders(scheduler=None) -> dict:
    """
    Main entry point. Iterates over all active families and sends reminders.
    Called by APScheduler or the /whatsapp/trigger-reminders HTTP endpoint.

    Returns a summary dict: {"families_processed": N, "reminders_sent": N, "errors": [...]}
    """
    if _is_quiet_hours():
        logger.info("Reminder job skipped — quiet hours")
        return {"families_processed": 0, "reminders_sent": 0, "skipped": "quiet_hours"}

    summary = {"families_processed": 0, "reminders_sent": 0, "errors": []}

    try:
        # Import brain lazily to avoid circular imports at module load time
        from . import brain as _brain  # type: ignore[import]
        db = _brain._supabase
        if not db:
            logger.error("Reminder job: Supabase client not available")
            summary["errors"].append("supabase_unavailable")
            return summary

        # Fetch all active families
        res = (
            db.table("families")
            .select("family_id, reminders_enabled, reminder_time")
            .eq("status", "active")
            .execute()
        )
        families = res.data or []
        logger.info("Reminder job: processing %d active families", len(families))

        for fam in families:
            family_id = fam.get("family_id")
            if not family_id:
                continue
            summary["families_processed"] += 1
            try:
                sent = _run_reminders_for_family(db, family_id, scheduler=scheduler)
                if sent:
                    summary["reminders_sent"] += 1
            except Exception as exc:
                logger.error("Reminder job failed for %s: %s", family_id, exc)
                summary["errors"].append(f"{family_id}: {exc}")

    except Exception as exc:
        logger.error("Reminder job top-level error: %s", exc)
        summary["errors"].append(str(exc))

    logger.info(
        "Reminder job complete: %d families, %d reminders sent, %d errors",
        summary["families_processed"], summary["reminders_sent"], len(summary["errors"]),
    )
    return summary


# ---------------------------------------------------------------------------
# Reminder preference updater (called from /reminders command handler)
# ---------------------------------------------------------------------------

def update_reminder_preferences(
    db,
    family_id: str,
    enabled: Optional[bool] = None,
    reminder_time: Optional[str] = None,
) -> bool:
    """
    Update reminder preferences for a family in the database.
    Returns True on success, False on failure.
    """
    updates: dict = {}
    if enabled is not None:
        updates["reminders_enabled"] = enabled
    if reminder_time is not None:
        updates["reminder_time"] = reminder_time
    if not updates:
        return False
    try:
        db.table("families").update(updates).eq("family_id", family_id).execute()
        return True
    except Exception as exc:
        logger.error("Failed to update reminder preferences for %s: %s", family_id, exc)
        return False

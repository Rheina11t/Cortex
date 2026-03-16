"""
Family Brain – Daily Digest.

Sends a daily summary to all family members via Telegram, including:
  * Recent memories captured in the last 24 hours
  * Upcoming family events in the next 7 days
  * Maintenance tasks due in the next 14 days
  * Vehicle reminders (MOT, insurance, tax)
  * Medication refills due in the next 14 days
  * Upcoming medical appointments
  * Upcoming job interviews
  * Recently added household items and vendors
  * Knowledge base statistics

Run manually:
    python -m src.daily_digest

Or schedule via cron / systemd timer:
    0 7 * * * cd /home/ubuntu/open-brain && /home/ubuntu/open-brain/.venv/bin/python -m src.daily_digest
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import telegram

from . import brain
from .config import get_settings, logger

log = logging.getLogger("open_brain.daily_digest")


async def build_digest() -> str:
    """Build the daily digest message as Markdown text."""
    settings = get_settings()
    brain.init(settings)

    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).isoformat()
    next_week = (now + timedelta(days=7)).strftime("%Y-%m-%d")
    today_str = now.strftime("%Y-%m-%d")

    sections: list[str] = []
    sections.append(f"🧠 *Family Brain — Daily Digest*\n📅 {now.strftime('%A, %B %d, %Y')}\n")

    # ── Recent memories (last 24 hours) ──────────────────────────────────
    try:
        recent = brain.list_recent_memories(limit=50)
        recent_24h = []
        for m in recent:
            created = m.get("created_at", "")
            if created and created >= yesterday:
                recent_24h.append(m)

        if recent_24h:
            sections.append(f"📝 *New Memories* ({len(recent_24h)} captured)\n")
            for m in recent_24h[:15]:
                meta = m.get("metadata", {}) or {}
                cat = meta.get("category", "other")
                source_user = meta.get("source_user", "")
                user_tag = f" [{source_user}]" if source_user else ""
                content_preview = (m.get("content", "")[:80] + "…") if len(m.get("content", "")) > 80 else m.get("content", "")
                sections.append(f"  • `{cat}`{user_tag}: {content_preview}")
            if len(recent_24h) > 15:
                sections.append(f"  _…and {len(recent_24h) - 15} more_")
            sections.append("")
        else:
            sections.append("📝 *New Memories*: None in the last 24 hours.\n")
    except Exception as exc:
        log.warning("Failed to fetch recent memories: %s", exc)
        sections.append("📝 *New Memories*: _Error fetching._\n")

    # ── Upcoming family events (next 7 days) ─────────────────────────────
    try:
        events = brain.check_family_schedule(today_str, next_week)
        if events:
            sections.append(f"📅 *Upcoming Events* (next 7 days)\n")
            for ev in events:
                date_str = ev.get("event_date", "")
                time_str = ev.get("event_time", "")
                title = ev.get("title", ev.get("event_name", "Untitled"))
                member = ev.get("family_member", "")
                location = ev.get("location", "")
                time_display = f" at {time_str}" if time_str else ""
                loc_display = f" 📍 {location}" if location else ""
                member_display = f" [{member}]" if member else ""
                sections.append(f"  • {date_str}{time_display}: *{title}*{member_display}{loc_display}")
            sections.append("")
        else:
            sections.append("📅 *Upcoming Events*: None in the next 7 days.\n")
    except Exception as exc:
        log.warning("Failed to fetch family events: %s", exc)
        sections.append("📅 *Upcoming Events*: _Error fetching._\n")

    # ── Maintenance tasks due (next 14 days) ─────────────────────────────
    try:
        tasks = brain.get_upcoming_maintenance(days_ahead=14)
        if tasks:
            sections.append(f"🔧 *Maintenance Due* (next 14 days)\n")
            for t in tasks[:10]:
                title = t.get("title", "Untitled")
                cat = t.get("category", "other")
                due = t.get("next_due", "n/a")
                freq = f" (every {t['frequency_days']}d)" if t.get("frequency_days") else ""
                sections.append(f"  • {due}: *{title}* [{cat}]{freq}")
            sections.append("")
    except Exception as exc:
        log.warning("Failed to fetch maintenance tasks: %s", exc)

    # ── Vehicle reminders ────────────────────────────────────────────────
    try:
        reminders = brain.get_vehicle_reminders(days_ahead=30)
        if reminders:
            sections.append("🚗 *Vehicle Reminders* (next 30 days)\n")
            for v in reminders:
                nickname = v.get("nickname", "Unknown")
                reg = v.get("registration", "")
                alerts = v.get("_alerts", [])
                for alert in alerts:
                    sections.append(f"  • {nickname} ({reg}): ⚠️ {alert}")
            sections.append("")
    except Exception as exc:
        log.warning("Failed to fetch vehicle reminders: %s", exc)

    # ── Medication refills due (next 14 days) ────────────────────────────
    try:
        refills = brain.get_medication_refills_due(days_ahead=14)
        if refills:
            sections.append("💊 *Medication Refills Due* (next 14 days)\n")
            for m in refills:
                name = m.get("name", "Unknown")
                member = m.get("family_member", "")
                due = m.get("refill_due", "n/a")
                pharmacy = m.get("pharmacy", "")
                pharm_str = f" @ {pharmacy}" if pharmacy else ""
                sections.append(f"  • {due}: *{name}* [{member}]{pharm_str}")
            sections.append("")
    except Exception as exc:
        log.warning("Failed to fetch medication refills: %s", exc)

    # ── Upcoming medical appointments ────────────────────────────────────
    try:
        appts = brain.get_upcoming_appointments(days_ahead=14)
        if appts:
            sections.append("🏥 *Medical Appointments* (next 14 days)\n")
            for a in appts:
                appt_date = a.get("appointment_date", "n/a")
                appt_time = a.get("appointment_time", "")
                appt_type = a.get("appointment_type", "general")
                member = a.get("family_member", "")
                provider = a.get("provider", "")
                time_str = f" at {appt_time}" if appt_time else ""
                sections.append(f"  • {appt_date}{time_str}: *{appt_type}* [{member}] — {provider}")
            sections.append("")
    except Exception as exc:
        log.warning("Failed to fetch medical appointments: %s", exc)

    # ── Upcoming job interviews ──────────────────────────────────────────
    try:
        interviews = brain.get_upcoming_interviews(days_ahead=14)
        if interviews:
            sections.append("💼 *Upcoming Interviews* (next 14 days)\n")
            for iv in interviews:
                app = iv.get("jh_applications", {})
                company = app.get("company", "n/a")
                job_title = app.get("job_title", "n/a")
                scheduled = iv.get("scheduled_at", "TBD")
                iv_type = iv.get("interview_type", "n/a")
                sections.append(f"  • {scheduled}: *{company}* — {job_title} ({iv_type})")
            sections.append("")
    except Exception as exc:
        log.warning("Failed to fetch upcoming interviews: %s", exc)

    # ── Recently added household items ───────────────────────────────────
    try:
        items = brain.list_household_items(limit=10)
        week_ago = (now - timedelta(days=7)).isoformat()
        recent_items = [i for i in items if (i.get("created_at", "") or "") >= week_ago]
        if recent_items:
            sections.append(f"🏠 *New Household Items* (last 7 days)\n")
            for item in recent_items[:10]:
                name = item.get("item_name", item.get("name", "Unknown"))
                cat = item.get("category", "")
                loc = item.get("location", "")
                details = []
                if cat:
                    details.append(cat)
                if loc:
                    details.append(f"📍 {loc}")
                detail_str = f" ({', '.join(details)})" if details else ""
                sections.append(f"  • {name}{detail_str}")
            sections.append("")
    except Exception as exc:
        log.warning("Failed to fetch household items: %s", exc)

    # ── Recently added vendors ───────────────────────────────────────────
    try:
        vendors = brain.list_household_vendors(limit=10)
        week_ago = (now - timedelta(days=7)).isoformat()
        recent_vendors = [v for v in vendors if (v.get("created_at", "") or "") >= week_ago]
        if recent_vendors:
            sections.append(f"🔧 *New Vendors/Contractors* (last 7 days)\n")
            for v in recent_vendors[:10]:
                name = v.get("vendor_name", v.get("name", "Unknown"))
                svc = v.get("service_type", v.get("trade", ""))
                phone = v.get("phone", "")
                details = []
                if svc:
                    details.append(svc)
                if phone:
                    details.append(f"📞 {phone}")
                detail_str = f" ({', '.join(details)})" if details else ""
                sections.append(f"  • {name}{detail_str}")
            sections.append("")
    except Exception as exc:
        log.warning("Failed to fetch vendors: %s", exc)

    # ── Stats ────────────────────────────────────────────────────────────
    try:
        stats = brain.get_stats()
        sections.append(
            f"📊 *Brain Stats*: {stats.get('total', 0)} total memories | "
            f"Oldest: {(stats.get('oldest') or 'n/a')[:10]} | "
            f"Newest: {(stats.get('newest') or 'n/a')[:10]}"
        )
    except Exception as exc:
        log.warning("Failed to fetch stats: %s", exc)

    return "\n".join(sections)


async def send_digest() -> None:
    """Build the digest and send it to all configured recipients."""
    settings = get_settings()
    settings.validate_telegram()

    recipients = settings.get_digest_recipients()
    if not recipients:
        log.warning("No digest recipients configured. Set FAMILY_MEMBER_N_ID or DIGEST_RECIPIENT_IDS.")
        return

    digest_text = await build_digest()
    bot = telegram.Bot(token=settings.telegram_bot_token)

    for user_id in recipients:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=digest_text,
                parse_mode="Markdown",
            )
            log.info("Digest sent to user %d", user_id)
        except Exception as exc:
            log.error("Failed to send digest to user %d: %s", user_id, exc)

    log.info("Daily digest complete. Sent to %d recipients.", len(recipients))


def main() -> None:
    """Entry point for running the daily digest."""
    log.info("Running daily digest…")
    asyncio.run(send_digest())
    log.info("Daily digest finished.")


if __name__ == "__main__":
    main()

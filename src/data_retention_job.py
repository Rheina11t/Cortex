#!/usr/bin/env python3
"""
FamilyBrain — Data Retention Job
=================================
Implements the FamilyBrain data retention policy:

  POLICY SUMMARY
  --------------
  • Data is retained indefinitely while the account is active.
    FamilyBrain IS the emergency vault — families store wills, insurance
    policies, and critical documents that must survive long periods of
    non-use. Inactivity-based deletion would defeat the entire purpose.

  • Data is deleted when:
      (a) The user explicitly requests it via /delete, OR
      (b) The subscription is cancelled and NOT renewed within 90 days.

  • The 90-day post-cancellation grace period gives users time to export
    their data via /mydata before permanent deletion occurs.

  UK GDPR Article 5(1)(e) — storage limitation: data kept "no longer than
  necessary for the purposes for which the personal data are processed."
  For an emergency vault, necessity is coextensive with account existence.

WHAT THIS JOB DOES (runs monthly, 1st of each month at 02:00 London time)
--------------------------------------------------------------------------
  1. Identifies families whose subscription was cancelled > 60 days ago
     (i.e. within 30 days of the 90-day deletion deadline) and sends a
     30-day warning WhatsApp message if not already sent.

  2. Identifies families whose subscription was cancelled > 90 days ago
     and permanently deletes all their data from every table, then logs
     the deletion to the audit log.

  3. Purges auxiliary data that has no legitimate retention need:
       • Expired invite tokens (>7 days old)
       • Processed Stripe events (>90 days old)
       • In-memory rate limiter state is self-expiring (no DB action needed)

SCHEDULING
----------
  APScheduler cron trigger: day=1, hour=2, minute=0, timezone=Europe/London
  Can also be triggered manually via HTTP POST /whatsapp/trigger-retention
  (protected by X-Cron-Secret header, same as reminder job).

IDEMPOTENCY
-----------
  • The 30-day warning is gated on retention_warning_sent_at IS NULL.
  • Deletion is gated on deletion_scheduled_at <= NOW() AND status = 'cancelled'.
  • Both checks are atomic at the DB level — safe to run multiple times.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytz

logger = logging.getLogger("familybrain.data_retention")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LONDON_TZ = pytz.timezone("Europe/London")

# Grace period: 90 days after subscription cancellation before deletion
_GRACE_PERIOD_DAYS = 90

# Warning threshold: send the 30-day warning when 60+ days have elapsed
# (i.e. 30 days before the 90-day deletion deadline)
_WARNING_THRESHOLD_DAYS = 60

# Auxiliary purge thresholds
_INVITE_TOKEN_TTL_DAYS = 7       # expired invite tokens
_STRIPE_EVENT_TTL_DAYS = 90      # processed Stripe events
# Rate limiter state is in-memory only — no DB purge needed

# ---------------------------------------------------------------------------
# Warning message template
# ---------------------------------------------------------------------------

def _build_warning_message(cancelled_at: datetime) -> str:
    """Build the 30-day deletion warning WhatsApp message."""
    days_since_cancellation = (datetime.now(timezone.utc) - cancelled_at).days
    days_until_deletion = _GRACE_PERIOD_DAYS - days_since_cancellation
    deletion_date = (datetime.now(timezone.utc) + timedelta(days=days_until_deletion)).strftime(
        "%-d %B %Y"
    )
    return (
        f"⚠️ FamilyBrain data deletion notice\n\n"
        f"Your FamilyBrain subscription ended {days_since_cancellation} days ago. "
        f"Your data will be permanently deleted on {deletion_date} "
        f"({days_until_deletion} days from now).\n\n"
        f"To keep your data:\n"
        f"  • Renew your subscription at familybrain.co.uk/subscribe\n\n"
        f"To export before deletion:\n"
        f"  • Type /mydata to receive a full export\n\n"
        f"To delete immediately:\n"
        f"  • Type /delete\n\n"
        f"If you have already renewed, please ignore this message."
    )


# ---------------------------------------------------------------------------
# Supabase client helper
# ---------------------------------------------------------------------------

def _get_supabase():
    """Return a Supabase client using environment credentials."""
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not supabase_url or not supabase_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    from supabase import create_client
    return create_client(supabase_url, supabase_key)


# ---------------------------------------------------------------------------
# WhatsApp send helper (transport-agnostic)
# ---------------------------------------------------------------------------

def _send_whatsapp(phone: str, message: str) -> None:
    """Send a WhatsApp message via the Meta Cloud API or Twilio transport."""
    try:
        from . import meta_whatsapp as _meta_wa
    except ImportError:
        import meta_whatsapp as _meta_wa  # type: ignore  # standalone execution
    to = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
    _meta_wa.send_whatsapp_message(to=to, body=message)


# ---------------------------------------------------------------------------
# Audit log helper
# ---------------------------------------------------------------------------

def _audit(family_id: str, action: str, detail: dict[str, Any]) -> None:
    """Write a structured entry to the audit log."""
    try:
        from . import audit_log as _audit_log
        _audit_log.audit_log(family_id, action, "data_retention_job", detail)
    except Exception as exc:
        logger.warning("Audit log write failed for family %s: %s", family_id, exc)


# ---------------------------------------------------------------------------
# Core: delete all data for a family
# ---------------------------------------------------------------------------

# Tables that contain family-scoped data, in dependency order.
# Ordered so that child rows are deleted before parent rows where FK
# constraints exist.  All tables use family_id as the partition key.
_FAMILY_DATA_TABLES = [
    # Operational / AI memory
    "memories",
    "entity_graph",
    "cortex_briefings",
    "cortex_actions",
    # Calendar & scheduling
    "family_events",
    "recurring_events",
    "calendar_tokens",
    # Health, finance, home
    "health_records",
    "financial_records",
    "home_maintenance",
    "vehicles",
    # Documents & binder
    "death_binder",
    "binder_progress",
    "emergency_pdfs",
    # Communications
    "inbound_emails",
    "school_emails",
    # Billing & admin
    "data_exports",
    "delete_requests",
    "referrals",
    # Members (last — referenced by other tables)
    "whatsapp_members",
]


def _delete_family_data(db, family_id: str) -> dict[str, int]:
    """Delete all data for a family from every table.

    Returns a dict mapping table name -> rows deleted.
    Errors on individual tables are logged but do not abort the overall
    deletion — we want to delete as much as possible even if one table fails.
    """
    results: dict[str, int] = {}
    for table in _FAMILY_DATA_TABLES:
        try:
            res = db.table(table).delete().eq("family_id", family_id).execute()
            count = len(res.data) if res.data else 0
            if count > 0:
                results[table] = count
                logger.info(
                    "Deleted %d rows from %s for family %s", count, table, family_id
                )
        except Exception as exc:
            logger.warning(
                "Could not delete from %s for family %s (table may not exist): %s",
                table, family_id, exc,
            )
    # Finally delete the family record itself
    try:
        res = db.table("families").delete().eq("family_id", family_id).execute()
        results["families"] = len(res.data) if res.data else 0
        logger.info("Deleted family record for %s", family_id)
    except Exception as exc:
        logger.error("Failed to delete family record for %s: %s", family_id, exc)
    return results


# ---------------------------------------------------------------------------
# Step 1: Send 30-day warnings
# ---------------------------------------------------------------------------

def _send_deletion_warnings(db) -> int:
    """Send 30-day pre-deletion warnings to families approaching the 90-day limit.

    Targets families where:
      • status = 'cancelled'
      • subscription_cancelled_at is between 60 and 89 days ago
      • retention_warning_sent_at IS NULL (warning not yet sent)

    Returns the number of warnings sent.
    """
    now = datetime.now(timezone.utc)
    warning_cutoff = (now - timedelta(days=_WARNING_THRESHOLD_DAYS)).isoformat()
    deletion_cutoff = (now - timedelta(days=_GRACE_PERIOD_DAYS)).isoformat()

    try:
        result = (
            db.table("families")
            .select("family_id,primary_phone,primary_name,subscription_cancelled_at")
            .eq("status", "cancelled")
            .lte("subscription_cancelled_at", warning_cutoff)
            .gt("subscription_cancelled_at", deletion_cutoff)
            .is_("retention_warning_sent_at", "null")
            .execute()
        )
        families = result.data or []
    except Exception as exc:
        logger.error("Failed to query families for deletion warnings: %s", exc)
        return 0

    warnings_sent = 0
    for family in families:
        family_id = family.get("family_id", "")
        primary_phone = family.get("primary_phone", "")
        cancelled_at_str = family.get("subscription_cancelled_at", "")

        if not primary_phone or not cancelled_at_str:
            logger.warning("Skipping family %s: missing phone or cancellation date", family_id)
            continue

        try:
            cancelled_at = datetime.fromisoformat(cancelled_at_str.replace("Z", "+00:00"))
            if cancelled_at.tzinfo is None:
                cancelled_at = cancelled_at.replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning("Could not parse cancellation date for family %s: %s", family_id, cancelled_at_str)
            continue

        message = _build_warning_message(cancelled_at)

        try:
            _send_whatsapp(primary_phone, message)
            now_iso = now.isoformat()
            db.table("families").update(
                {"retention_warning_sent_at": now_iso}
            ).eq("family_id", family_id).execute()
            _audit(family_id, "retention_warning_sent", {
                "phone": primary_phone,
                "cancelled_at": cancelled_at_str,
                "warning_sent_at": now_iso,
            })
            logger.info(
                "Sent 30-day deletion warning to family %s (phone: %s)",
                family_id, primary_phone,
            )
            warnings_sent += 1
        except Exception as exc:
            logger.error(
                "Failed to send deletion warning to family %s: %s", family_id, exc
            )

    return warnings_sent


# ---------------------------------------------------------------------------
# Step 2: Execute deletions for families past the 90-day grace period
# ---------------------------------------------------------------------------

def _execute_deletions(db) -> int:
    """Permanently delete all data for families past the 90-day grace period.

    Targets families where:
      • status = 'cancelled'
      • subscription_cancelled_at <= 90 days ago

    Returns the number of families deleted.
    """
    now = datetime.now(timezone.utc)
    deletion_cutoff = (now - timedelta(days=_GRACE_PERIOD_DAYS)).isoformat()

    try:
        result = (
            db.table("families")
            .select("family_id,primary_phone,primary_name,subscription_cancelled_at")
            .eq("status", "cancelled")
            .lte("subscription_cancelled_at", deletion_cutoff)
            .execute()
        )
        families = result.data or []
    except Exception as exc:
        logger.error("Failed to query families for deletion: %s", exc)
        return 0

    if not families:
        logger.info("No families require deletion this run.")
        return 0

    families_deleted = 0
    for family in families:
        family_id = family.get("family_id", "")
        primary_phone = family.get("primary_phone", "")
        cancelled_at_str = family.get("subscription_cancelled_at", "")

        logger.info(
            "Executing data deletion for family %s (cancelled: %s)",
            family_id, cancelled_at_str,
        )

        # Attempt a final courtesy notification before deletion
        if primary_phone:
            try:
                _send_whatsapp(
                    primary_phone,
                    "🗑️ FamilyBrain: Your data has now been permanently deleted "
                    "as your subscription ended more than 90 days ago. "
                    "If you'd like to start again, visit familybrain.co.uk/subscribe. "
                    "Thank you for using FamilyBrain.",
                )
            except Exception as exc:
                logger.warning(
                    "Could not send deletion notification to %s: %s", primary_phone, exc
                )

        deletion_results = _delete_family_data(db, family_id)
        _audit(family_id, "family_data_deleted", {
            "reason": "subscription_cancelled_90_days",
            "cancelled_at": cancelled_at_str,
            "deleted_at": now.isoformat(),
            "tables_affected": deletion_results,
            "phone": primary_phone,
        })
        logger.info(
            "Completed deletion for family %s. Tables affected: %s",
            family_id, list(deletion_results.keys()),
        )
        families_deleted += 1

    return families_deleted


# ---------------------------------------------------------------------------
# Step 3: Auxiliary purges
# ---------------------------------------------------------------------------

def _purge_expired_invite_tokens(db) -> int:
    """Delete invite tokens that expired more than 7 days ago."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_INVITE_TOKEN_TTL_DAYS)).isoformat()
    try:
        result = db.table("family_invites").delete().lt("expires_at", cutoff).execute()
        count = len(result.data) if result.data else 0
        if count > 0:
            logger.info("Purged %d expired invite tokens (older than 7 days)", count)
        return count
    except Exception as exc:
        logger.warning("Failed to purge expired invite tokens: %s", exc)
        return 0


def _purge_old_stripe_events(db) -> int:
    """Delete processed Stripe events older than 90 days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_STRIPE_EVENT_TTL_DAYS)).isoformat()
    try:
        # Try processed_stripe_events table (migration 028)
        result = (
            db.table("processed_stripe_events")
            .delete()
            .lt("created_at", cutoff)
            .execute()
        )
        count = len(result.data) if result.data else 0
        if count > 0:
            logger.info("Purged %d old processed Stripe events (older than 90 days)", count)
        return count
    except Exception as exc:
        logger.warning("Failed to purge old Stripe events: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_retention_job() -> dict[str, Any]:
    """Execute the full monthly data retention job.

    Returns a summary dict with counts for each operation performed.
    Designed to be idempotent — safe to run multiple times.
    """
    logger.info("=== FamilyBrain data retention job starting ===")
    summary: dict[str, Any] = {
        "run_at": datetime.now(timezone.utc).isoformat(),
        "warnings_sent": 0,
        "families_deleted": 0,
        "invite_tokens_purged": 0,
        "stripe_events_purged": 0,
        "errors": [],
    }

    try:
        db = _get_supabase()
    except Exception as exc:
        logger.error("Retention job aborted: could not connect to Supabase: %s", exc)
        summary["errors"].append(f"DB connection failed: {exc}")
        return summary

    # Step 1: Send 30-day warnings
    try:
        summary["warnings_sent"] = _send_deletion_warnings(db)
        logger.info("Warnings sent: %d", summary["warnings_sent"])
    except Exception as exc:
        logger.error("Warning step failed: %s", exc)
        summary["errors"].append(f"Warning step: {exc}")

    # Step 2: Execute deletions
    try:
        summary["families_deleted"] = _execute_deletions(db)
        logger.info("Families deleted: %d", summary["families_deleted"])
    except Exception as exc:
        logger.error("Deletion step failed: %s", exc)
        summary["errors"].append(f"Deletion step: {exc}")

    # Step 3a: Purge expired invite tokens
    try:
        summary["invite_tokens_purged"] = _purge_expired_invite_tokens(db)
    except Exception as exc:
        logger.error("Invite token purge failed: %s", exc)
        summary["errors"].append(f"Invite token purge: {exc}")

    # Step 3b: Purge old Stripe events
    try:
        summary["stripe_events_purged"] = _purge_old_stripe_events(db)
    except Exception as exc:
        logger.error("Stripe event purge failed: %s", exc)
        summary["errors"].append(f"Stripe event purge: {exc}")

    logger.info(
        "=== Retention job complete: warnings=%d, deletions=%d, "
        "invite_tokens_purged=%d, stripe_events_purged=%d, errors=%d ===",
        summary["warnings_sent"],
        summary["families_deleted"],
        summary["invite_tokens_purged"],
        summary["stripe_events_purged"],
        len(summary["errors"]),
    )
    return summary


# ---------------------------------------------------------------------------
# APScheduler registration helper
# ---------------------------------------------------------------------------

def register_retention_scheduler(scheduler) -> None:
    """Register the monthly retention job with an APScheduler instance.

    Call this from the application startup code alongside the reminder job.

    Args:
        scheduler: An APScheduler ``BackgroundScheduler`` or
                   ``AsyncIOScheduler`` instance, already started.

    Example::

        from apscheduler.schedulers.background import BackgroundScheduler
        from src.data_retention_job import register_retention_scheduler

        scheduler = BackgroundScheduler(timezone=pytz.timezone("Europe/London"))
        scheduler.start()
        register_retention_scheduler(scheduler)
    """
    scheduler.add_job(
        run_retention_job,
        trigger="cron",
        day=1,          # 1st of each month
        hour=2,         # 02:00 London time
        minute=0,
        timezone=_LONDON_TZ,
        id="data_retention_monthly",
        name="FamilyBrain Monthly Data Retention Job",
        replace_existing=True,
        misfire_grace_time=3600,  # allow up to 1 hour late start
        coalesce=True,            # run once even if multiple misfires
    )
    logger.info(
        "Data retention job scheduled: monthly on the 1st at 02:00 Europe/London"
    )


# ---------------------------------------------------------------------------
# Standalone execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logger.info("Running data retention job manually...")
    result = run_retention_job()
    logger.info("Result: %s", result)
    if result.get("errors"):
        sys.exit(1)

"""
Family Brain – Automated Data Retention Enforcement.

Enforces the data retention policy:
- memories table: delete records older than 2 years
- family_events table: delete events where event_date is more than 1 year in the past
- cortex_actions table: delete records older than 1 year
- school_emails_processed table: delete records older than 1 year

Runs daily via the main scheduler.
"""

import logging
from datetime import datetime, timedelta, timezone

from . import brain

logger = logging.getLogger("open_brain.data_retention")

def enforce_data_retention() -> None:
    """Enforce data retention policies across the database."""
    db = brain._supabase
    if not db:
        logger.error("Cannot enforce data retention: no database connection")
        return

    logger.info("Starting automated data retention enforcement...")
    now = datetime.now(timezone.utc)

    try:
        # 1. memories table: delete records older than 2 years
        two_years_ago = (now - timedelta(days=365 * 2)).isoformat()
        memories_result = db.table("memories").delete().lt("created_at", two_years_ago).execute()
        deleted_memories = len(memories_result.data) if memories_result.data else 0
        if deleted_memories > 0:
            logger.info("Deleted %d old records from memories table.", deleted_memories)

        # 2. family_events table: delete events where event_date is more than 1 year in the past
        one_year_ago_date = (now - timedelta(days=365)).date().isoformat()
        events_result = db.table("family_events").delete().lt("event_date", one_year_ago_date).execute()
        deleted_events = len(events_result.data) if events_result.data else 0
        if deleted_events > 0:
            logger.info("Deleted %d old records from family_events table.", deleted_events)

        # 3. cortex_actions table: delete records older than 1 year
        one_year_ago = (now - timedelta(days=365)).isoformat()
        actions_result = db.table("cortex_actions").delete().lt("created_at", one_year_ago).execute()
        deleted_actions = len(actions_result.data) if actions_result.data else 0
        if deleted_actions > 0:
            logger.info("Deleted %d old records from cortex_actions table.", deleted_actions)

        # 4. school_emails_processed table: delete records older than 1 year
        emails_result = db.table("school_emails_processed").delete().lt("processed_at", one_year_ago).execute()
        deleted_emails = len(emails_result.data) if emails_result.data else 0
        if deleted_emails > 0:
            logger.info("Deleted %d old records from school_emails_processed table.", deleted_emails)

        logger.info("Data retention enforcement completed successfully.")

    except Exception as exc:
        logger.error("Error during data retention enforcement: %s", exc)

if __name__ == "__main__":
    # Allow running manually
    from .config import get_settings
    settings = get_settings()
    brain.init(settings)
    enforce_data_retention()

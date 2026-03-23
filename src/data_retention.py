"""
Family Brain – Data Retention Policy.

NOTE: Automatic scheduled data deletion has been removed.
Per the updated privacy policy, data is retained for as long as the account is active.
Data is only deleted when a user explicitly requests it (e.g., via the /delete-my-data command).

This module is kept for future manual retention tasks or compliance tooling,
but it no longer runs automatic time-based deletions.
"""

import logging
from datetime import datetime, timedelta, timezone

from . import brain

logger = logging.getLogger("open_brain.data_retention")

def enforce_data_retention() -> None:
    """
    Enforce data retention policies across the database.
    
    Currently a no-op. Automatic deletion is disabled; data is retained 
    for the life of the account and deleted only on explicit user request.
    """
    logger.info("Automated data retention is disabled. Data is retained for the life of the account.")
    return

if __name__ == "__main__":
    # Allow running manually
    from .config import get_settings
    settings = get_settings()
    brain.init(settings)
    enforce_data_retention()

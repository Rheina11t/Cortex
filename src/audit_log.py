"""
FamilyBrain – Centralised Audit Logging (Phase 4).
Logs significant user actions and system events to the cortex_actions table.
"""
from __future__ import annotations
import logging
from typing import Any, Optional
from . import brain

logger = logging.getLogger("open_brain.audit")

def audit_log(
    family_id: str,
    action_type: str,
    subject: str,
    detail: Optional[dict[str, Any]] = None,
    phone_number: Optional[str] = None
) -> None:
    """Log a significant action to the cortex_actions table.
    
    Args:
        family_id:    The family ID the action belongs to.
        action_type:  The type of action (e.g., 'sos_generated', 'data_deleted').
        subject:      A human-readable summary of the action.
        detail:       Optional dictionary of structured metadata.
        phone_number: The phone number that triggered the action.
    """
    try:
        db = brain._supabase
        if not db:
            logger.warning("Audit log failed: Supabase not initialized")
            return
            
        db.table("cortex_actions").insert({
            "family_id": family_id,
            "action_type": action_type,
            "subject": subject,
            "detail": detail or {},
            "phone_number": phone_number
        }).execute()
        
        logger.info("Audit log: %s | %s | %s", family_id, action_type, subject)
    except Exception as exc:
        logger.warning("Failed to log action %s for family %s: %s", action_type, family_id, exc)

def get_audit_trail(
    family_id: str,
    limit: int = 50,
    action_type: Optional[str] = None
) -> list[dict[str, Any]]:
    """Retrieve the audit trail for a family."""
    try:
        db = brain._supabase
        if not db:
            return []
            
        query = db.table("cortex_actions").select("*").eq("family_id", family_id)
        if action_type:
            query = query.eq("action_type", action_type)
            
        result = query.order("created_at", desc=True).limit(limit).execute()
        return result.data or []
    except Exception as exc:
        logger.error("Failed to fetch audit trail for family %s: %s", family_id, exc)
        return []

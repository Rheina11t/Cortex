"""FamilyBrain – Structured security event logging (Phase 2 security hardening).

Outputs JSON-formatted security events to stderr for ingestion by
log aggregation systems (e.g., Railway logs, Datadog, etc.).

Event types
-----------
    prompt_injection_blocked  – Jailbreak / prompt injection attempt detected
    rate_limit_hit            – Per-phone or global rate limit exceeded
    webhook_signature_failed  – Meta or Twilio webhook signature verification failed
    stripe_webhook_failed     – Stripe webhook signature or parse error
    invalid_token             – Invalid invite token format
    expired_token             – Expired invite token used
    used_token                – Already-used invite token attempted
    token_not_found           – Invite token not found in database
    input_validation_failed   – Input failed validation (phone, category, etc.)
    output_filter_triggered   – LLM output contained sensitive data and was blocked
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("open_brain.security")


def security_log(
    event_type: str,
    details: dict[str, Any],
    phone: Optional[str] = None,
    severity: str = "WARNING",
) -> None:
    """Emit a structured JSON security log entry.

    Args:
        event_type: Category of security event (see module docstring).
        details:    Arbitrary dict of event-specific details.
        phone:      Optional phone number associated with the event.
        severity:   Log severity – one of DEBUG, INFO, WARNING, ERROR, CRITICAL.
    """
    # Phase 5 Item 29: Include correlation ID in security logs
    try:
        from . import correlation as _corr
        cid = _corr.get_correlation_id()
    except Exception:
        cid = None

    entry: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "security",
        "event_type": event_type,
        "severity": severity,
        "details": details,
    }
    if cid:
        entry["correlation_id"] = cid
    if phone:
        entry["phone"] = phone

    level = getattr(logging, severity.upper(), logging.WARNING)
    logger.log(level, json.dumps(entry))

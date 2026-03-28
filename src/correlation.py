"""FamilyBrain — Request correlation IDs (Phase 5 gap analysis, Item 29).

Generates and propagates a unique correlation ID for every inbound webhook
request, creating an audit trail:

    meta_webhook_id -> internal_message_id -> openai_request_id -> db_txn_id

Usage::

    from src.correlation import get_correlation_id, set_correlation_id

    # At webhook entry:
    set_correlation_id()  # generates a new UUID

    # Throughout the request:
    cid = get_correlation_id()
    logger.info("Processing message", extra={"correlation_id": cid})

    # Pass to OpenAI:
    client.chat.completions.create(..., user=cid)

    # Include in security logs:
    security_log("event", {..., "correlation_id": cid})
"""
from __future__ import annotations

import logging
import threading
import uuid

logger = logging.getLogger("familybrain.correlation")

# Thread-local storage for the current request's correlation ID
_local = threading.local()


def set_correlation_id(cid: str | None = None) -> str:
    """Set (or generate) a correlation ID for the current request.

    Returns the correlation ID.
    """
    if cid is None:
        cid = str(uuid.uuid4())
    _local.correlation_id = cid
    return cid


def get_correlation_id() -> str:
    """Return the current request's correlation ID, or generate one if unset."""
    cid = getattr(_local, "correlation_id", None)
    if cid is None:
        cid = set_correlation_id()
    return cid


def clear_correlation_id() -> None:
    """Clear the correlation ID (call at end of request)."""
    _local.correlation_id = None

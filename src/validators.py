"""FamilyBrain – Input validation utilities (Phase 2 security hardening).

Centralised validation for phone numbers, invite tokens, categories,
and generic string inputs.  Imported by whatsapp_capture.py, family_invites.py,
and other modules that accept external input.
"""
from __future__ import annotations

import re
from typing import Optional


# ---------------------------------------------------------------------------
# E.164 phone number validation
# ---------------------------------------------------------------------------
_E164_PATTERN = re.compile(r'^\+[1-9]\d{6,14}$')


def validate_phone_e164(phone: str) -> Optional[str]:
    """Validate and normalise a phone number to E.164 format.

    Strips ``whatsapp:`` prefix if present, removes whitespace, dashes, and
    parentheses.  Returns the normalised phone number or ``None`` if invalid.
    """
    if not isinstance(phone, str):
        return None
    cleaned = phone.strip()
    if cleaned.startswith("whatsapp:"):
        cleaned = cleaned[len("whatsapp:"):]
    cleaned = (
        cleaned.strip()
        .replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
    )
    if not cleaned.startswith("+"):
        return None
    if not _E164_PATTERN.match(cleaned):
        return None
    return cleaned


# ---------------------------------------------------------------------------
# Invite token format validation
# ---------------------------------------------------------------------------
_TOKEN_PATTERN = re.compile(r'^[A-Za-z0-9_-]{8,64}$')


def validate_invite_token(token: str) -> Optional[str]:
    """Validate an invite token format.

    Accepts URL-safe base64 characters, 8–64 chars long.
    Returns the token if valid, ``None`` otherwise.
    """
    if not isinstance(token, str):
        return None
    token = token.strip()
    if not _TOKEN_PATTERN.match(token):
        return None
    return token


# ---------------------------------------------------------------------------
# Category allowlist validation
# ---------------------------------------------------------------------------
VALID_CATEGORIES: frozenset[str] = frozenset({
    "general", "reference", "health", "finance", "home", "vehicle",
    "school", "work", "family", "travel", "shopping", "recipe",
    "hmrc_letter", "government_letter", "insurance", "legal",
    "medical", "dental", "prescription", "appointment",
    "bill", "subscription", "warranty", "manual",
    "funeral", "digital", "legal_docs", "financial_accounts",
    "insurance_policies", "property", "personal_wishes", "medical_info",
    "dependents", "other",
})


def validate_category(category: str) -> str:
    """Validate a category against the allowlist.

    Returns the category if valid, ``'other'`` otherwise.
    """
    if not isinstance(category, str):
        return "other"
    category = category.strip().lower()
    if category in VALID_CATEGORIES:
        return category
    return "other"


# ---------------------------------------------------------------------------
# Generic string sanitisation
# ---------------------------------------------------------------------------
def sanitise_string(text: str, max_length: int = 10000) -> str:
    """Basic string sanitisation: strip, truncate, remove null bytes."""
    if not isinstance(text, str):
        return ""
    text = text.strip().replace("\x00", "")
    return text[:max_length]

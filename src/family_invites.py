# -*- coding: utf-8 -*-
"""
FamilyBrain — Family Invite System
====================================
Handles the "add family member without phone number" feature.

Flow:
  1. Existing member sends "add Sarah" or "add family member" via WhatsApp.
  2. Bot generates a unique invite token, stores it in family_invites table.
  3. Bot replies with a shareable invite link: familybrain.co.uk/join/<token>
  4. Invitee taps the link → redirected to wa.me with pre-filled "join <token>"
  5. FamilyBrain receives "join <token>", validates, adds member, sends welcome.

Routes (registered as a Flask Blueprint):
  GET /join/<token>  — Validates token, redirects to WhatsApp deep link.
"""

from __future__ import annotations

import logging
import os
import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from flask import Blueprint, Response, redirect
from . import security_logger
from . import validators

logger = logging.getLogger("familybrain.family_invites")

# Invite tokens expire after this many days
_INVITE_EXPIRY_DAYS = 7

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FAMILYBRAIN_BASE_URL: str = os.environ.get(
    "FAMILYBRAIN_BASE_URL", "https://cortex-production-eb84.up.railway.app"
).rstrip("/")

# The public-facing FamilyBrain WhatsApp number users will message to join.
# This is the E.164 number WITHOUT the "whatsapp:" prefix, e.g. "447782384375".
# It is used to construct wa.me deep links.
FAMILYBRAIN_WHATSAPP_NUMBER: str = os.environ.get(
    "FAMILYBRAIN_WHATSAPP_NUMBER", ""
).strip().lstrip("+")

# Fallback: derive from TWILIO_WHATSAPP_FROM if the dedicated env var is absent
if not FAMILYBRAIN_WHATSAPP_NUMBER:
    _twilio_from = os.environ.get("TWILIO_WHATSAPP_FROM", "").replace("whatsapp:", "").strip().lstrip("+")
    if _twilio_from:
        FAMILYBRAIN_WHATSAPP_NUMBER = _twilio_from

SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY: str = os.environ.get("SUPABASE_SERVICE_KEY", "")

# ---------------------------------------------------------------------------
# Blueprint
# ---------------------------------------------------------------------------
invites_bp = Blueprint("family_invites", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_supabase():
    """Return a Supabase client."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _generate_token(length: int = 32) -> str:
    """Generate a URL-safe token using secrets.token_urlsafe.

    Length 32 gives ~192 bits of entropy — hardened in Phase 2.
    """
    return secrets.token_urlsafe(length)


def _unique_token(db) -> str:
    """Generate a token that does not already exist in family_invites."""
    for _ in range(10):
        token = _generate_token()
        existing = (
            db.table("family_invites")
            .select("id")
            .eq("invite_token", token)
            .limit(1)
            .execute()
        )
        if not existing.data:
            return token
    # Extremely unlikely to reach here, but fall back to a longer token
    return _generate_token(48)


def create_invite(
    family_id: str,
    invited_name: str,
    invited_by_phone: str,
) -> Optional[str]:
    """Create a new invite record and return the invite token.

    Args:
        family_id:        The family's identifier (e.g. "family_abc123").
        invited_name:     The name the inviter gave for the new member.
        invited_by_phone: E.164 phone of the inviting member (no "whatsapp:" prefix).

    Returns:
        The invite token string, or None on failure.
    """
    try:
        db = _get_supabase()
        token = _unique_token(db)
        now = datetime.now(timezone.utc)
        db.table("family_invites").insert({
            "invite_token": token,
            "family_id": family_id,
            "invited_name": invited_name,
            "invited_by_phone": invited_by_phone,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(days=_INVITE_EXPIRY_DAYS)).isoformat(),
        }).execute()
        logger.info(
            "Created invite token=%s family=%s name=%s by=%s",
            token, family_id, invited_name, invited_by_phone,
        )
        return token
    except Exception as exc:
        logger.error("Failed to create invite: %s", exc)
        return None


def get_invite(token: str) -> Optional[dict]:
    """Look up an invite by token.  Returns the row dict or None."""
    try:
        db = _get_supabase()
        result = (
            db.table("family_invites")
            .select("*")
            .eq("invite_token", token)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error("Failed to look up invite token=%s: %s", token, exc)
        return None


def mark_invite_used(token: str, used_by_phone: str) -> bool:
    """Mark an invite as used.  Returns True on success."""
    try:
        db = _get_supabase()
        db.table("family_invites").update({
            "used_at": datetime.now(timezone.utc).isoformat(),
            "used_by_phone": used_by_phone,
        }).eq("invite_token", token).execute()
        logger.info("Marked invite token=%s used by %s", token, used_by_phone)
        return True
    except Exception as exc:
        logger.error("Failed to mark invite used token=%s: %s", token, exc)
        return False


def build_invite_message(
    invited_name: str,
    family_display_name: str,
    token: str,
    base_url: str = "",
) -> str:
    """Build the WhatsApp message the inviter forwards to the new member.

    Args:
        invited_name:       First name of the person being invited.
        family_display_name: The family's display name, e.g. "The Jones".
        token:              The invite token.
        base_url:           Override for the public base URL (optional).

    Returns:
        A ready-to-send WhatsApp message string.
    """
    _base = (base_url or FAMILYBRAIN_BASE_URL).rstrip("/")
    invite_url = f"https://familybrain.co.uk/join/{token}"
    return (
        f"To add {invited_name} to your FamilyBrain, forward this to them:\n\n"
        f"👋 You've been invited to join {family_display_name}'s FamilyBrain.\n\n"
        f"Tap to join: {invite_url}\n\n"
        f"FamilyBrain helps families stay organised — all through WhatsApp. "
        f"No app to download."
    )


# ---------------------------------------------------------------------------
# /join/<token> route — validates token and redirects to WhatsApp deep link
# ---------------------------------------------------------------------------

_JOIN_ERROR_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>FamilyBrain — Invalid Invite</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:wght@700&family=Inter:wght@400;500&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
      font-family: 'Inter', sans-serif;
      background: #FAF8F4;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .card {{
      background: #fff;
      border-radius: 20px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.08);
      padding: 48px 40px;
      max-width: 440px;
      width: 100%;
      text-align: center;
    }}
    .icon {{ font-size: 3rem; margin-bottom: 20px; }}
    h1 {{
      font-family: 'Fraunces', Georgia, serif;
      font-size: 1.75rem;
      color: #2C2C2C;
      margin-bottom: 12px;
    }}
    p {{ color: #7A7A7A; line-height: 1.65; font-size: 0.95rem; }}
    .back {{
      display: inline-block;
      margin-top: 28px;
      color: #4A8C7A;
      font-weight: 600;
      font-size: 0.9rem;
      text-decoration: none;
    }}
    .back:hover {{ color: #3a7264; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">🔗</div>
    <h1>{title}</h1>
    <p>{message}</p>
    <a class="back" href="https://familybrain.co.uk">Back to FamilyBrain &rarr;</a>
  </div>
</body>
</html>
"""


@invites_bp.route("/join/<token>", methods=["GET"])
def join_via_token(token: str) -> Response:
    """Validate an invite token and redirect to WhatsApp with a pre-filled message.

    Valid token → 302 redirect to:
        https://wa.me/<FAMILYBRAIN_WHATSAPP_NUMBER>?text=join+<token>

    Invalid / used token → friendly HTML error page.
    """
    # Sanitise token — validate format using centralised validator
    validated_token = validators.validate_invite_token(token)
    if validated_token is None:
        logger.warning("Invalid token format: %r", token)
        security_logger.security_log(
            "invalid_token",
            {"reason": "format_validation_failed", "token_preview": token[:8]},
        )
        return Response(
            _JOIN_ERROR_HTML.format(
                title="Invalid invite link",
                message="This invite link doesn't look right. Please ask the person who invited you to send a new link.",
            ),
            status=400,
            mimetype="text/html",
        )
    token = validated_token

    invite = get_invite(token)

    if invite is None:
        logger.warning("Token not found: %s", token)
        security_logger.security_log(
            "token_not_found",
            {"token_preview": token[:8]},
        )
        return Response(
            _JOIN_ERROR_HTML.format(
                title="Invite not found",
                message="This invite link doesn't exist. It may have been mistyped. Please ask the person who invited you to send a fresh link.",
            ),
            status=404,
            mimetype="text/html",
        )

    if invite.get("used_at"):
        logger.info("Token already used: %s", token)
        security_logger.security_log(
            "used_token",
            {"token_preview": token[:8], "family_id": invite.get("family_id")},
        )
        return Response(
            _JOIN_ERROR_HTML.format(
                title="Invite already used",
                message="This invite link has already been used. Each link can only be used once. Ask your family member to generate a new one.",
            ),
            status=410,
            mimetype="text/html",
        )

    # --- Expiry check (Phase 2 hardening) ---
    expires_at_str = invite.get("expires_at")
    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > expires_at:
                logger.info("Token expired: %s (expired at %s)", token, expires_at_str)
                security_logger.security_log(
                    "expired_token",
                    {"token_preview": token[:8], "expired_at": expires_at_str},
                )
                return Response(
                    _JOIN_ERROR_HTML.format(
                        title="Invite expired",
                        message="This invite link has expired. Invite links are valid for 7 days. Ask your family member to send a new one.",
                    ),
                    status=410,
                    mimetype="text/html",
                )
        except (ValueError, TypeError):
            pass  # If we can't parse the date, allow the invite through

    # Build WhatsApp deep link
    wa_number = FAMILYBRAIN_WHATSAPP_NUMBER
    if not wa_number:
        logger.error("FAMILYBRAIN_WHATSAPP_NUMBER is not configured")
        return Response(
            _JOIN_ERROR_HTML.format(
                title="Configuration error",
                message="Something went wrong on our end. Please try again later or contact support.",
            ),
            status=500,
            mimetype="text/html",
        )

    # Strip any leading + for wa.me URLs (they expect digits only)
    wa_number_clean = wa_number.lstrip("+")
    wa_url = f"https://wa.me/{wa_number_clean}?text=join+{token}"

    logger.info(
        "Redirecting invite token=%s (family=%s, name=%s) to WhatsApp",
        token, invite.get("family_id"), invite.get("invited_name"),
    )
    return redirect(wa_url, code=302)

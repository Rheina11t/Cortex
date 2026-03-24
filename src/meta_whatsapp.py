"""
Family Brain – Meta WhatsApp Cloud API Integration.

This module replaces Twilio as the WhatsApp transport layer when the
USE_META_API environment variable is set to "true".

=============================================================================
SETUP GUIDE — Meta WhatsApp Cloud API
=============================================================================

1. Go to https://developers.facebook.com and create a Meta App (type: Business).
2. Add the "WhatsApp" product to your app.
3. In the WhatsApp > Getting Started section, note your:
   - Phone Number ID  (a numeric string, e.g. "123456789012345")
   - Permanent Access Token (generate a System User token with
     whatsapp_business_messaging permission — the temporary token expires
     in 24 hours and is NOT suitable for production).

4. Set these environment variables on Railway (or in your .env file):

   USE_META_API=true
       Feature flag. When "true", all WhatsApp messaging uses Meta Cloud API.
       When "false" (default), Twilio is used. This allows safe rollback.

   WHATSAPP_ACCESS_TOKEN=<your permanent access token>
       The System User access token with whatsapp_business_messaging scope.
       Generate at: Business Settings > System Users > Generate Token.

   WHATSAPP_PHONE_NUMBER_ID=<your phone number ID>
       Found in: WhatsApp > Getting Started > Phone Number ID.

   WHATSAPP_VERIFY_TOKEN=<any random string you choose>
       Used to verify the webhook URL with Meta. You choose this value —
       it just needs to match what you enter in the Meta webhook config.
       Example: "familybrain-verify-2024"

5. Configure the Meta webhook:
   - URL: https://your-railway-url.up.railway.app/webhook/whatsapp
   - Verify token: the same string you set in WHATSAPP_VERIFY_TOKEN
   - Subscribe to: messages

6. Once verified, set USE_META_API=true and redeploy. The app will
   start processing Meta webhooks and sending via the Cloud API.

=============================================================================
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import requests as http_requests

logger = logging.getLogger("open_brain.meta_whatsapp")

# ---------------------------------------------------------------------------
# Configuration — read from environment
# ---------------------------------------------------------------------------
WHATSAPP_ACCESS_TOKEN: str = os.getenv("WHATSAPP_ACCESS_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID: str = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN: str = os.getenv("WHATSAPP_VERIFY_TOKEN", "")

GRAPH_API_VERSION = "v19.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


def _headers() -> dict[str, str]:
    """Return authorization headers for Meta Graph API calls."""
    return {
        "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Webhook verification (GET handler for Meta's hub.challenge flow)
# ---------------------------------------------------------------------------
def verify_webhook(hub_mode: str, hub_verify_token: str, hub_challenge: str) -> tuple[str, int]:
    """Handle Meta's webhook verification GET request.

    Returns (response_body, status_code).
    Meta sends:
        GET /webhook/whatsapp?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=<challenge>
    We must return the hub.challenge value with HTTP 200 if the token matches.
    """
    if hub_mode == "subscribe" and hub_verify_token == WHATSAPP_VERIFY_TOKEN:
        logger.info("Meta webhook verification successful")
        return hub_challenge, 200
    logger.warning(
        "Meta webhook verification failed: mode=%s, token_match=%s",
        hub_mode,
        hub_verify_token == WHATSAPP_VERIFY_TOKEN,
    )
    return "Forbidden", 403


# ---------------------------------------------------------------------------
# Inbound message parsing
# ---------------------------------------------------------------------------
def parse_incoming_message(payload: dict) -> Optional[dict]:
    """Parse a Meta Cloud API webhook payload into a normalised message dict.

    Returns a dict with keys:
        from_number: str   — sender phone in "whatsapp:+<number>" format (Twilio-compat)
        body: str          — text body (empty string for media-only messages)
        num_media: int     — number of media attachments
        media_id: str      — Meta media ID of the first attachment (if any)
        media_mime_type: str — MIME type of the first attachment (if any)
        message_id: str    — Meta message ID (for read receipts, etc.)
        timestamp: str     — Unix timestamp of the message

    Returns None if the payload does not contain a user message (e.g. status
    updates, delivery receipts, etc.).
    """
    try:
        entry = payload.get("entry", [])
        if not entry:
            return None

        changes = entry[0].get("changes", [])
        if not changes:
            return None

        value = changes[0].get("value", {})

        # Status updates (sent, delivered, read) — not user messages
        if "statuses" in value and "messages" not in value:
            return None

        messages = value.get("messages", [])
        if not messages:
            return None

        msg = messages[0]
        msg_type = msg.get("type", "")
        sender = msg.get("from", "")  # e.g. "447700900000"

        # Normalise to Twilio-compatible format for downstream code
        from_number = f"whatsapp:+{sender}" if not sender.startswith("+") else f"whatsapp:{sender}"

        # Extract text body
        body = ""
        if msg_type == "text":
            body = msg.get("text", {}).get("body", "")
        elif msg_type == "interactive":
            # Button replies or list replies
            interactive = msg.get("interactive", {})
            if interactive.get("type") == "button_reply":
                body = interactive.get("button_reply", {}).get("title", "")
            elif interactive.get("type") == "list_reply":
                body = interactive.get("list_reply", {}).get("title", "")

        # Extract media info
        media_id = ""
        media_mime_type = ""
        num_media = 0

        if msg_type in ("image", "document", "audio", "video", "sticker"):
            media_obj = msg.get(msg_type, {})
            media_id = media_obj.get("id", "")
            media_mime_type = media_obj.get("mime_type", "")
            num_media = 1
            # Caption (for images/videos/documents)
            caption = media_obj.get("caption", "")
            if caption and not body:
                body = caption

        return {
            "from_number": from_number,
            "body": body,
            "num_media": num_media,
            "media_id": media_id,
            "media_mime_type": media_mime_type,
            "message_id": msg.get("id", ""),
            "timestamp": msg.get("timestamp", ""),
        }

    except (IndexError, KeyError, TypeError) as exc:
        logger.error("Failed to parse Meta webhook payload: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Media download (Meta sends a media ID, not a direct URL)
# ---------------------------------------------------------------------------
def download_media(media_id: str) -> tuple[bytes, str]:
    """Download media from Meta's Cloud API.

    Steps:
        1. GET /v19.0/{media_id} → returns JSON with a "url" field
        2. GET that URL with Authorization header → returns the binary media

    Returns (media_bytes, mime_type).
    Raises on failure.
    """
    # Step 1: Get the download URL
    meta_url = f"{GRAPH_API_BASE}/{media_id}"
    resp = http_requests.get(meta_url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    media_info = resp.json()

    download_url = media_info.get("url", "")
    mime_type = media_info.get("mime_type", "application/octet-stream")

    if not download_url:
        raise ValueError(f"No download URL returned for media_id={media_id}")

    # Step 2: Download the actual media binary
    media_resp = http_requests.get(
        download_url,
        headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"},
        timeout=60,
    )
    media_resp.raise_for_status()

    return media_resp.content, mime_type


# ---------------------------------------------------------------------------
# Sending messages
# ---------------------------------------------------------------------------
def send_text_message(to: str, body: str) -> dict:
    """Send a text message via Meta WhatsApp Cloud API.

    Args:
        to: Recipient phone number. Accepts either:
            - "whatsapp:+447700900000" (Twilio format — prefix is stripped)
            - "+447700900000"
            - "447700900000"
        body: The message text.

    Returns the API response JSON.
    """
    # Normalise: strip "whatsapp:" prefix and leading "+"
    recipient = to.replace("whatsapp:", "").lstrip("+").strip()

    url = f"{GRAPH_API_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }

    resp = http_requests.post(url, headers=_headers(), json=payload, timeout=30)

    if resp.status_code != 200:
        logger.error(
            "Meta send_text_message failed: status=%d body=%s",
            resp.status_code,
            resp.text[:500],
        )
    resp.raise_for_status()

    result = resp.json()
    logger.info(
        "Meta message sent to %s (msg_id=%s)",
        recipient,
        result.get("messages", [{}])[0].get("id", "?"),
    )
    return result


def send_document_message(to: str, document_url: str, caption: str = "", filename: str = "document.pdf") -> dict:
    """Send a document (e.g. PDF) via a public URL.

    Args:
        to: Recipient phone number (any format).
        document_url: A publicly accessible URL for the document.
        caption: Optional caption text.
        filename: Display filename.

    Returns the API response JSON.
    """
    recipient = to.replace("whatsapp:", "").lstrip("+").strip()

    url = f"{GRAPH_API_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "document",
        "document": {
            "link": document_url,
            "filename": filename,
        },
    }
    if caption:
        payload["document"]["caption"] = caption

    resp = http_requests.post(url, headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def send_image_message(to: str, image_url: str, caption: str = "") -> dict:
    """Send an image via a public URL."""
    recipient = to.replace("whatsapp:", "").lstrip("+").strip()

    url = f"{GRAPH_API_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "image",
        "image": {"link": image_url},
    }
    if caption:
        payload["image"]["caption"] = caption

    resp = http_requests.post(url, headers=_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()


def mark_as_read(message_id: str) -> None:
    """Mark an incoming message as read (sends blue ticks)."""
    if not message_id:
        return
    url = f"{GRAPH_API_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": message_id,
    }
    try:
        http_requests.post(url, headers=_headers(), json=payload, timeout=10)
    except Exception as exc:
        logger.warning("Failed to mark message %s as read: %s", message_id, exc)


# ---------------------------------------------------------------------------
# Feature flag helper
# ---------------------------------------------------------------------------
def is_meta_api_enabled() -> bool:
    """Return True if USE_META_API is set to a truthy value."""
    return os.getenv("USE_META_API", "false").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Unified send helper — used by all code paths
# ---------------------------------------------------------------------------
def send_whatsapp_message(
    to: str,
    body: str,
    media_url: Optional[str] = None,
    media_type: str = "document",
    media_filename: str = "document.pdf",
    media_caption: str = "",
) -> None:
    """Send a WhatsApp message using whichever transport is active.

    This is the SINGLE function that all outbound WhatsApp sends should use.
    It checks the USE_META_API feature flag and routes accordingly.

    Args:
        to: Recipient phone. Accepts "whatsapp:+447..." or "+447..." or "447...".
        body: Text message body.
        media_url: Optional public URL for a media attachment.
        media_type: "document" or "image" (only used when media_url is set).
        media_filename: Filename for document attachments.
        media_caption: Caption for media attachments.
    """
    if is_meta_api_enabled():
        _send_via_meta(to, body, media_url, media_type, media_filename, media_caption)
    else:
        _send_via_twilio(to, body, media_url)


def _send_via_meta(
    to: str,
    body: str,
    media_url: Optional[str],
    media_type: str,
    media_filename: str,
    media_caption: str,
) -> None:
    """Send via Meta Cloud API."""
    try:
        if media_url:
            if media_type == "image":
                send_image_message(to, media_url, caption=media_caption or body)
            else:
                send_document_message(to, media_url, caption=media_caption or body, filename=media_filename)
            # If there's also a text body distinct from the caption, send it separately
            if body and body != media_caption:
                send_text_message(to, body)
        else:
            send_text_message(to, body)
    except Exception as exc:
        logger.error("Meta send failed to %s: %s", to, exc)
        raise


def _send_via_twilio(to: str, body: str, media_url: Optional[str]) -> None:
    """Send via Twilio (legacy path)."""
    try:
        from twilio.rest import Client as TwilioClient
        from .config import get_settings

        _s = get_settings()
        if not _s.twilio_account_sid or not _s.twilio_auth_token:
            logger.error("Twilio credentials not configured — cannot send message")
            return

        client = TwilioClient(_s.twilio_account_sid, _s.twilio_auth_token)

        # Ensure the 'to' number has the whatsapp: prefix
        if not to.startswith("whatsapp:"):
            to = f"whatsapp:{to}"

        kwargs: dict[str, Any] = {
            "from_": _s.twilio_whatsapp_from,
            "to": to,
            "body": body,
        }
        if media_url:
            kwargs["media_url"] = [media_url]

        client.messages.create(**kwargs)
        logger.info("Twilio message sent to %s", to)
    except Exception as exc:
        logger.error("Twilio send failed to %s: %s", to, exc)
        raise

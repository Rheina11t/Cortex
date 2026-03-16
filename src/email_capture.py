#!/usr/bin/env python3
"""
Family Brain – Email Capture Layer.

Polls a Gmail inbox via IMAP, extracts text from new emails, processes
them through the LLM for metadata extraction, generates embeddings, and
stores them in the Supabase memories table.

Usage:
    python -m src.email_capture          # from the project root

Required environment variables (see .env.example):
    FAMILY_BRAIN_EMAIL, FAMILY_BRAIN_EMAIL_PASSWORD,
    SUPABASE_URL, SUPABASE_SERVICE_KEY,
    OPENAI_API_KEY

Optional:
    EMAIL_POLL_INTERVAL_SECONDS  – polling interval (default: 300 = 5 min)
    EMAIL_IMAP_HOST              – IMAP server (default: imap.gmail.com)
    EMAIL_IMAP_PORT              – IMAP port (default: 993)
"""

from __future__ import annotations

import email
import email.header
import email.utils
import html
import imaplib
import json
import logging
import os
import re
import time
import traceback
from datetime import datetime
from email.message import Message
from typing import Any, Optional

from .config import get_settings, logger as root_logger
from . import brain

logger = logging.getLogger("open_brain.email")

# ---------------------------------------------------------------------------
# Initialise settings and core brain module
# ---------------------------------------------------------------------------
settings = get_settings()
brain.init(settings)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_EMAIL_ADDRESS = os.getenv("FAMILY_BRAIN_EMAIL", "").strip()
_EMAIL_PASSWORD = os.getenv("FAMILY_BRAIN_EMAIL_PASSWORD", "").strip()
_IMAP_HOST = os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com").strip()
_IMAP_PORT = int(os.getenv("EMAIL_IMAP_PORT", "993"))
_POLL_INTERVAL = int(os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "300"))

# Sender allowlist (optional) — only process emails from these addresses
_ALLOWED_SENDERS_RAW = os.getenv("EMAIL_ALLOWED_SENDERS", "").strip()
_ALLOWED_SENDERS: set[str] = set()
if _ALLOWED_SENDERS_RAW:
    _ALLOWED_SENDERS = {s.strip().lower() for s in _ALLOWED_SENDERS_RAW.split(",") if s.strip()}


def _validate_email_config() -> None:
    """Raise if required email config is missing."""
    if not _EMAIL_ADDRESS:
        raise RuntimeError("FAMILY_BRAIN_EMAIL is not set")
    if not _EMAIL_PASSWORD:
        raise RuntimeError("FAMILY_BRAIN_EMAIL_PASSWORD is not set")


# ---------------------------------------------------------------------------
# Email parsing helpers
# ---------------------------------------------------------------------------
def _decode_header(header_value: str) -> str:
    """Decode an RFC 2047 encoded header value."""
    parts = email.header.decode_header(header_value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _extract_sender_email(from_header: str) -> str:
    """Extract the bare email address from a From header."""
    _, addr = email.utils.parseaddr(from_header)
    return addr.lower()


def _extract_body(msg: Message) -> str:
    """Extract the plain-text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))

            # Skip attachments
            if "attachment" in disposition:
                continue

            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")

            if content_type == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html_text = payload.decode(charset, errors="replace")
                    return _html_to_text(html_text)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if msg.get_content_type() == "text/html":
                return _html_to_text(text)
            return text

    return ""


def _html_to_text(html_content: str) -> str:
    """Simple HTML to plain-text conversion."""
    # Remove HTML tags
    text = re.sub(r"<br\s*/?>", "\n", html_content, flags=re.IGNORECASE)
    text = re.sub(r"<p\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # Collapse whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Email processing pipeline
# ---------------------------------------------------------------------------
def _process_email(msg: Message) -> Optional[dict[str, Any]]:
    """Process a single email message and store it as a memory."""
    from_header = _decode_header(msg.get("From", ""))
    sender_email = _extract_sender_email(from_header)
    subject = _decode_header(msg.get("Subject", "(no subject)"))
    date_str = msg.get("Date", "")
    body = _extract_body(msg)

    if not body.strip():
        logger.info("Skipping email with empty body: %s", subject)
        return None

    # Check sender allowlist
    if _ALLOWED_SENDERS and sender_email not in _ALLOWED_SENDERS:
        logger.debug("Skipping email from non-allowed sender: %s", sender_email)
        return None

    logger.info("Processing email: '%s' from %s", subject, sender_email)

    # Compose the full text for the LLM
    full_text = f"Email Subject: {subject}\nFrom: {from_header}\nDate: {date_str}\n\n{body}"

    # Truncate very long emails
    if len(full_text) > 8000:
        full_text = full_text[:8000] + "\n\n[... truncated ...]"

    try:
        # Extract metadata via LLM
        metadata = brain.extract_metadata(full_text)
        cleaned_content = metadata.pop("cleaned_content", full_text[:4000])

        # Enrich metadata
        metadata["source"] = "email"
        metadata["email_from"] = sender_email
        metadata["email_subject"] = subject
        metadata["email_date"] = date_str

        # Generate embedding
        embedding = brain.generate_embedding(cleaned_content)

        # Store in Supabase
        record = brain.store_memory(
            content=cleaned_content,
            embedding=embedding,
            metadata=metadata,
        )

        memory_id = record.get("id", "n/a")
        logger.info(
            "Email memory captured: id=%s, subject='%s', category=%s",
            memory_id, subject, metadata.get("category", "other"),
        )
        return record

    except Exception as exc:
        logger.error(
            "Failed to process email '%s': %s\n%s",
            subject, exc, traceback.format_exc(),
        )
        return None


# ---------------------------------------------------------------------------
# IMAP polling loop
# ---------------------------------------------------------------------------
def _poll_inbox() -> int:
    """Connect to IMAP, fetch unseen messages, process them. Returns count."""
    processed = 0

    try:
        mail = imaplib.IMAP4_SSL(_IMAP_HOST, _IMAP_PORT)
        mail.login(_EMAIL_ADDRESS, _EMAIL_PASSWORD)
        mail.select("INBOX")

        # Search for unseen messages
        status, data = mail.search(None, "UNSEEN")
        if status != "OK":
            logger.warning("IMAP search failed: %s", status)
            return 0

        message_ids = data[0].split()
        if not message_ids:
            logger.debug("No new emails.")
            return 0

        logger.info("Found %d unseen email(s).", len(message_ids))

        for msg_id in message_ids:
            try:
                status, msg_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                result = _process_email(msg)
                if result:
                    processed += 1

                # Mark as seen (IMAP does this on fetch, but be explicit)
                mail.store(msg_id, "+FLAGS", "\\Seen")

            except Exception as exc:
                logger.error("Error processing email id=%s: %s", msg_id, exc)

        mail.close()
        mail.logout()

    except imaplib.IMAP4.error as exc:
        logger.error("IMAP error: %s", exc)
    except Exception as exc:
        logger.error("Email polling error: %s\n%s", exc, traceback.format_exc())

    return processed


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Run the email capture polling loop."""
    _validate_email_config()

    logger.info(
        "Starting Family Brain Email Capture Layer…\n"
        "  IMAP host:     %s:%d\n"
        "  Email:         %s\n"
        "  Poll interval: %d seconds\n"
        "  Allowed senders: %s",
        _IMAP_HOST, _IMAP_PORT,
        _EMAIL_ADDRESS,
        _POLL_INTERVAL,
        ", ".join(_ALLOWED_SENDERS) if _ALLOWED_SENDERS else "(all)",
    )

    while True:
        try:
            count = _poll_inbox()
            if count:
                logger.info("Processed %d email(s) this cycle.", count)
        except Exception as exc:
            logger.error("Poll cycle error: %s", exc)

        time.sleep(_POLL_INTERVAL)


if __name__ == "__main__":
    main()

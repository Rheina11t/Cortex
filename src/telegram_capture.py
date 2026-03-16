#!/usr/bin/env python3
"""
Family Brain – Telegram Capture Layer.

A python-telegram-bot application that listens for messages, photos, and
documents sent to the Family Brain Telegram bot.  Supports multiple
authorised family members, OCR via Google Vision (with pytesseract
fallback), PDF text extraction, and automatic event/conflict detection.

Usage:
    python -m src.telegram_capture          # from the project root

Required environment variables (see .env.example):
    TELEGRAM_BOT_TOKEN,
    SUPABASE_URL, SUPABASE_SERVICE_KEY,
    OPENAI_API_KEY

Optional:
    FAMILY_MEMBER_*_ID / FAMILY_MEMBER_*_NAME  – authorised family members
    GOOGLE_VISION_API_KEY                      – for Google Vision OCR
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import tempfile
import traceback
from datetime import datetime, date
from typing import Any, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import get_settings, logger as root_logger
from . import brain

logger = logging.getLogger("open_brain.telegram")

# ---------------------------------------------------------------------------
# Initialise settings and core brain module
# ---------------------------------------------------------------------------
settings = get_settings()
settings.validate_telegram()
brain.init(settings)

# ---------------------------------------------------------------------------
# Family member registry
# ---------------------------------------------------------------------------
FAMILY_MEMBERS: dict[int, str] = {}
_WELCOMED_USERS: set[int] = set()

# Load family members from env vars (FAMILY_MEMBER_1_ID, FAMILY_MEMBER_1_NAME, etc.)
for i in range(1, 20):  # support up to 20 family members
    uid_str = os.getenv(f"FAMILY_MEMBER_{i}_ID", "").strip()
    name = os.getenv(f"FAMILY_MEMBER_{i}_NAME", "").strip()
    if uid_str and name:
        try:
            FAMILY_MEMBERS[int(uid_str)] = name
        except ValueError:
            logger.warning("Invalid FAMILY_MEMBER_%d_ID=%r (not an integer)", i, uid_str)

# Fallback: also check TELEGRAM_ALLOWED_USER_IDS for backward compat
if not FAMILY_MEMBERS:
    _raw_ids = os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").strip()
    if _raw_ids:
        for uid_str in _raw_ids.split(","):
            uid_str = uid_str.strip()
            if uid_str:
                try:
                    FAMILY_MEMBERS[int(uid_str)] = f"User-{uid_str}"
                except ValueError:
                    pass

if FAMILY_MEMBERS:
    logger.info(
        "Family members registered: %s",
        ", ".join(f"{name} ({uid})" for uid, name in FAMILY_MEMBERS.items()),
    )
else:
    logger.info("No family members configured — bot is open to all users.")


def _get_family_name(user_id: int) -> Optional[str]:
    """Return the family member name for a user ID, or None if not authorised."""
    if not FAMILY_MEMBERS:
        return "Unknown"  # no restrictions configured
    return FAMILY_MEMBERS.get(user_id)


# ---------------------------------------------------------------------------
# OCR backend selection
# ---------------------------------------------------------------------------
_GOOGLE_VISION_KEY = os.getenv("GOOGLE_VISION_API_KEY", "").strip()
_USE_GOOGLE_VISION = bool(_GOOGLE_VISION_KEY) and _GOOGLE_VISION_KEY != "your_key_here"

if _USE_GOOGLE_VISION:
    logger.info("OCR backend: Google Vision API")
else:
    logger.info("OCR backend: pytesseract (local fallback)")


def _ocr_google_vision(image_bytes: bytes) -> str:
    """Extract text from image bytes using Google Vision API."""
    import requests as _requests

    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "requests": [{
            "image": {"content": b64_image},
            "features": [{"type": "TEXT_DETECTION", "maxResults": 1}],
        }]
    }
    url = f"https://vision.googleapis.com/v1/images:annotate?key={_GOOGLE_VISION_KEY}"
    resp = _requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    annotations = data.get("responses", [{}])[0].get("textAnnotations", [])
    if annotations:
        return annotations[0].get("description", "").strip()
    return ""


def _ocr_pytesseract(image_bytes: bytes) -> str:
    """Extract text from image bytes using pytesseract (local fallback)."""
    try:
        from PIL import Image
        import pytesseract
        img = Image.open(io.BytesIO(image_bytes))
        return pytesseract.image_to_string(img).strip()
    except Exception as exc:
        logger.warning("pytesseract OCR failed: %s", exc)
        return ""


def _extract_text_from_image(image_bytes: bytes) -> str:
    """Extract text from image using the best available OCR backend."""
    if _USE_GOOGLE_VISION:
        try:
            return _ocr_google_vision(image_bytes)
        except Exception as exc:
            logger.warning("Google Vision OCR failed (%s); falling back to pytesseract", exc)
            return _ocr_pytesseract(image_bytes)
    return _ocr_pytesseract(image_bytes)


def _extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from a PDF file using pdfplumber."""
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n\n".join(text_parts).strip()
    except Exception as exc:
        logger.warning("pdfplumber extraction failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Document type detection prompt
# ---------------------------------------------------------------------------
_DOC_TYPE_SYSTEM_PROMPT = """\
You are a document classification assistant for a Family Brain system.

Given extracted text from a document (photo or PDF), you MUST return a JSON object with:

{
  "cleaned_content": "<concise summary of the document's key information>",
  "document_type": "<one of: insurance, receipt, school_letter, booking, medical, pension, utility, warranty, invoice, contract, other>",
  "tags": ["<relevant topic tags>"],
  "people": ["<names of people mentioned, if any>"],
  "category": "<one of: idea, meeting-notes, decision, action-item, reference, personal, household, other>",
  "action_items": ["<any action items or deadlines extracted>"],
  "key_fields": {"<field_name>": "<value>"},
  "dates_mentioned": ["<any dates found in YYYY-MM-DD format>"],
  "source": "telegram-photo"
}

Rules:
- Return ONLY valid JSON. No markdown fences, no commentary.
- key_fields should extract the most important structured data (amounts, dates, reference numbers, addresses, etc.)
- If a field has no value, use an empty list [] or empty string "" or empty object {}.
- Keep cleaned_content as a faithful, concise summary.
"""


# ---------------------------------------------------------------------------
# Event detection helpers
# ---------------------------------------------------------------------------
_EVENT_DETECTION_PROMPT = """\
You are an event detection assistant for a Family Brain system.

Given a message, determine if it contains a schedulable event (appointment, meeting, activity, deadline, etc.)

Return a JSON object:
{
  "is_event": true/false,
  "event_name": "<name of the event>",
  "event_date": "<YYYY-MM-DD or null>",
  "event_time": "<HH:MM or null>",
  "location": "<location or empty string>",
  "requirements": ["<any requirements or things to bring>"],
  "family_member": "<who this event is for, or 'family' if shared>"
}

Today's date is """ + datetime.now().strftime("%Y-%m-%d") + """.

Rules:
- Return ONLY valid JSON.
- If the message does not contain a schedulable event, set is_event to false and leave other fields empty.
- Parse relative dates like "tomorrow", "next Tuesday", "this Friday" relative to today.
- If no specific person is mentioned, default family_member to the sender's name.
"""


def _detect_event(text: str, sender_name: str) -> Optional[dict[str, Any]]:
    """Use the LLM to detect if a message contains a schedulable event."""
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_embedding_base_url,
        )
        response = client.chat.completions.create(
            model=settings.llm_model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _EVENT_DETECTION_PROMPT},
                {"role": "user", "content": f"Sender: {sender_name}\n\nMessage: {text}"},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        parsed = json.loads(content)
        if parsed.get("is_event") and parsed.get("event_date"):
            return parsed
    except Exception as exc:
        logger.warning("Event detection failed: %s", exc)
    return None


def _check_conflicts_and_store_event(
    event_data: dict[str, Any],
    sender_name: str,
) -> tuple[Optional[str], Optional[str]]:
    """Store an event and check for conflicts. Returns (event_id, conflict_warning)."""
    try:
        db, _ = brain._require_init()

        event_date = event_data.get("event_date")
        event_time = event_data.get("event_time")
        family_member = event_data.get("family_member", sender_name)
        event_name = event_data.get("event_name", "Untitled event")

        # Check for conflicts
        conflict_msg = None
        try:
            conflicts = db.rpc(
                "check_schedule_conflicts",
                {"check_date": event_date, "check_member": None},
            ).execute()

            if conflicts.data:
                conflict_lines = []
                for c in conflicts.data:
                    time_str = c.get("event_time", "")
                    time_display = f" at {time_str}" if time_str else ""
                    conflict_lines.append(
                        f"  • {c['event_name']} for {c['family_member']}{time_display}"
                    )
                conflict_msg = (
                    f"⚠️ Heads up — this clashes with existing events on {event_date}:\n"
                    + "\n".join(conflict_lines)
                )
        except Exception as exc:
            logger.warning("Conflict check failed (table may not exist yet): %s", exc)

        # Store the event
        row = {
            "family_member": family_member,
            "event_name": event_name,
            "event_date": event_date,
            "event_time": event_time if event_time else None,
            "location": event_data.get("location", ""),
            "recurring": False,
            "recurrence_pattern": "",
            "requirements": event_data.get("requirements", []),
            "notes": "",
            "source": "telegram",
        }

        try:
            result = db.table("family_events").insert(row).execute()
            event_id = result.data[0].get("id") if result.data else None
            return event_id, conflict_msg
        except Exception as exc:
            logger.warning("Event storage failed (table may not exist yet): %s", exc)
            return None, conflict_msg

    except Exception as exc:
        logger.warning("Event processing failed: %s", exc)
        return None, None


# ---------------------------------------------------------------------------
# Command handler: /start
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message and usage instructions."""
    if update.message is None:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    family_name = _get_family_name(user_id)

    if family_name is None:
        await update.message.reply_text(
            "Sorry, this is a private Family Brain bot. "
            "Please ask the bot owner to add your Telegram user ID to the authorised list."
        )
        return

    await update.message.reply_text(
        f"🧠 *Family Brain is online\\!*\n\n"
        f"Welcome, {_escape(family_name)}\\! Here's what I can do:\n\n"
        "📝 *Text messages* — I'll capture them as memories with smart tagging\n"
        "📸 *Photos* — I'll OCR the text and store the document\n"
        "📄 *PDFs* — I'll extract the text and categorise the document\n"
        "📅 *Events* — I'll detect dates and check for family schedule conflicts\n\n"
        "Everything is stored in your shared Family Brain, searchable by "
        "meaning, tags, people, or category\\.\n\n"
        "Commands:\n"
        "  /start  \\– show this message\n"
        "  /status \\– check if the bot is running\n",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ---------------------------------------------------------------------------
# Command handler: /status
# ---------------------------------------------------------------------------
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Health-check command."""
    if update.message is None:
        return

    user_id = update.effective_user.id if update.effective_user else 0
    family_name = _get_family_name(user_id)

    if family_name is None:
        await update.message.reply_text("⛔ You are not authorised to use this bot.")
        return

    members = ", ".join(FAMILY_MEMBERS.values()) if FAMILY_MEMBERS else "open to all"
    await update.message.reply_text(
        f"✅ Family Brain is running\\.\n"
        f"👨‍👩‍👧‍👦 Family members: {_escape(members)}\n"
        f"🔍 OCR: {'Google Vision' if _USE_GOOGLE_VISION else 'pytesseract (local)'}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ---------------------------------------------------------------------------
# Message handler: capture text memories
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process every text message sent to the bot."""
    if update.message is None or update.effective_user is None:
        return

    user = update.effective_user
    raw_text: str = (update.message.text or "").strip()

    if not raw_text:
        return

    # Auth check
    family_name = _get_family_name(user.id)
    if family_name is None:
        logger.warning("Rejected message from unauthorised user id=%s", user.id)
        await update.message.reply_text(
            "Sorry, you're not authorised to use this Family Brain bot. "
            "Please ask the bot owner to add your Telegram user ID."
        )
        return

    # First-time welcome for new family members
    if user.id not in _WELCOMED_USERS and FAMILY_MEMBERS:
        _WELCOMED_USERS.add(user.id)
        if len(_WELCOMED_USERS) == 1 or family_name != list(FAMILY_MEMBERS.values())[0]:
            # Don't send welcome for the primary user's first message
            # but do send for secondary users
            if user.id != list(FAMILY_MEMBERS.keys())[0]:
                await update.message.reply_text(
                    f"👋 Welcome to Family Brain, {family_name}! "
                    "Everything you send here will be stored in the shared family knowledge base. "
                    "Send me text, photos, or PDFs — I'll capture and categorise them all. "
                    "Your memories will be tagged with your name so we know who captured what."
                )

    logger.info(
        "Received message from %s (id=%s): %d chars",
        family_name, user.id, len(raw_text),
    )

    thinking_msg = await update.message.reply_text("🧠 Capturing memory…")

    try:
        # Step 1: Extract metadata via LLM
        extracted = brain.extract_metadata(raw_text)
        cleaned_content: str = extracted.pop("cleaned_content", raw_text)

        # Enrich metadata with family context
        extracted["telegram_user_id"] = user.id
        extracted["telegram_username"] = user.username or ""
        extracted["source"] = "telegram"
        extracted["source_user"] = family_name.lower()
        extracted["family_member"] = family_name

        # Step 2: Generate embedding
        embedding = brain.generate_embedding(cleaned_content)

        # Step 3: Store in Supabase
        record = brain.store_memory(
            content=cleaned_content,
            embedding=embedding,
            metadata=extracted,
        )

        memory_id = record.get("id", "n/a")
        tags = extracted.get("tags", [])
        category = extracted.get("category", "other")
        action_items: list[str] = extracted.get("action_items", [])

        # Step 4: Check for events
        event_info = ""
        event_data = _detect_event(raw_text, family_name)
        if event_data:
            event_id, conflict_warning = _check_conflicts_and_store_event(
                event_data, family_name
            )
            if event_id:
                event_info = f"\n📅 *Event detected:* {_escape(event_data.get('event_name', ''))}"
                event_info += f" on {_escape(event_data.get('event_date', ''))}"
            if conflict_warning:
                event_info += f"\n\n{_escape(conflict_warning)}"

        # Step 5: Build confirmation
        tags_str = ", ".join(f"`{_escape(t)}`" for t in tags) if tags else "_none_"
        action_str = (
            "\n".join(f"  • {_escape(item)}" for item in action_items)
            if action_items
            else "_none_"
        )

        confirmation = (
            f"✅ *Memory captured by {_escape(family_name)}\\!*\n\n"
            f"📂 *Category:* {_escape(category)}\n"
            f"🏷 *Tags:* {tags_str}\n"
            f"🎯 *Action items:* {action_str}\n"
            f"🆔 *ID:* `{_escape(str(memory_id))}`"
            f"{event_info}"
        )

        await thinking_msg.edit_text(confirmation, parse_mode=ParseMode.MARKDOWN_V2)
        logger.info("Memory captured by %s (id=%s)", family_name, memory_id)

    except Exception as exc:
        logger.error("Failed to capture memory: %s\n%s", exc, traceback.format_exc())
        await thinking_msg.edit_text(
            f"⚠️ Failed to capture memory\\.\n\n`{_escape(str(exc))}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ---------------------------------------------------------------------------
# Photo handler: OCR + capture
# ---------------------------------------------------------------------------
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process photos sent to the bot — OCR text extraction and memory capture."""
    if update.message is None or update.effective_user is None:
        return

    user = update.effective_user
    family_name = _get_family_name(user.id)

    if family_name is None:
        await update.message.reply_text(
            "Sorry, you're not authorised to use this Family Brain bot."
        )
        return

    # Get the highest-resolution photo
    photo = update.message.photo[-1] if update.message.photo else None
    if not photo:
        return

    logger.info("Received photo from %s (id=%s)", family_name, user.id)
    thinking_msg = await update.message.reply_text("📸 Processing image…")

    try:
        # Download the photo
        file = await context.bot.get_file(photo.file_id)
        photo_bytes = await file.download_as_bytearray()

        # OCR
        extracted_text = _extract_text_from_image(bytes(photo_bytes))

        if not extracted_text:
            await thinking_msg.edit_text(
                "⚠️ Could not extract any text from this image\\. "
                "Try sending a clearer photo or type the content manually\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Include any caption
        caption = (update.message.caption or "").strip()
        full_text = f"{caption}\n\n{extracted_text}" if caption else extracted_text

        # Use document-type extraction prompt
        metadata = _extract_document_metadata(full_text)
        cleaned_content = metadata.pop("cleaned_content", full_text)
        doc_type = metadata.get("document_type", "other")
        key_fields = metadata.get("key_fields", {})

        # Enrich metadata
        metadata["telegram_user_id"] = user.id
        metadata["source"] = "telegram-photo"
        metadata["source_user"] = family_name.lower()
        metadata["family_member"] = family_name
        metadata["document_type"] = doc_type

        # Generate embedding and store
        embedding = brain.generate_embedding(cleaned_content)
        record = brain.store_memory(
            content=cleaned_content,
            embedding=embedding,
            metadata=metadata,
        )

        memory_id = record.get("id", "n/a")

        # Build summary of key fields
        key_summary = ""
        if key_fields:
            key_lines = [f"  • {k}: {v}" for k, v in list(key_fields.items())[:5]]
            key_summary = "\n" + "\n".join(_escape(line) for line in key_lines)

        confirmation = (
            f"✅ *Got it — {_escape(doc_type)} document captured\\!*\n\n"
            f"👤 *Captured by:* {_escape(family_name)}\n"
            f"📄 *Type:* {_escape(doc_type)}\n"
            f"🏷 *Tags:* {', '.join(f'`{_escape(t)}`' for t in metadata.get('tags', [])) or '_none_'}\n"
            f"🆔 *ID:* `{_escape(str(memory_id))}`"
            f"{key_summary}"
        )

        await thinking_msg.edit_text(confirmation, parse_mode=ParseMode.MARKDOWN_V2)
        logger.info("Photo memory captured by %s (type=%s, id=%s)", family_name, doc_type, memory_id)

    except Exception as exc:
        logger.error("Failed to process photo: %s\n%s", exc, traceback.format_exc())
        await thinking_msg.edit_text(
            f"⚠️ Failed to process image\\.\n\n`{_escape(str(exc))}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ---------------------------------------------------------------------------
# Document handler: PDF extraction + capture
# ---------------------------------------------------------------------------
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process PDF documents sent to the bot."""
    if update.message is None or update.effective_user is None:
        return

    user = update.effective_user
    family_name = _get_family_name(user.id)

    if family_name is None:
        await update.message.reply_text(
            "Sorry, you're not authorised to use this Family Brain bot."
        )
        return

    doc = update.message.document
    if not doc:
        return

    # Only process PDFs
    mime = doc.mime_type or ""
    file_name = doc.file_name or "document"

    if "pdf" not in mime.lower() and not file_name.lower().endswith(".pdf"):
        # Try to handle images sent as documents
        if mime.startswith("image/"):
            await _handle_image_document(update, context, user, family_name, doc)
            return
        await update.message.reply_text(
            "📄 I currently support PDF documents and images\\. "
            "Please send a PDF or photo\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    logger.info("Received PDF from %s (id=%s): %s", family_name, user.id, file_name)
    thinking_msg = await update.message.reply_text("📄 Processing PDF…")

    try:
        # Download the PDF
        file = await context.bot.get_file(doc.file_id)
        pdf_bytes = await file.download_as_bytearray()

        # Extract text
        extracted_text = _extract_text_from_pdf(bytes(pdf_bytes))

        if not extracted_text:
            await thinking_msg.edit_text(
                "⚠️ Could not extract text from this PDF\\. "
                "The file may be image\\-based \\(scanned\\)\\. "
                "Try sending individual page photos instead\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Include caption
        caption = (update.message.caption or "").strip()
        full_text = f"Document: {file_name}\n{caption}\n\n{extracted_text}" if caption else f"Document: {file_name}\n\n{extracted_text}"

        # Truncate very long PDFs for the LLM
        if len(full_text) > 8000:
            full_text = full_text[:8000] + "\n\n[... truncated for processing ...]"

        # Use document-type extraction
        metadata = _extract_document_metadata(full_text)
        cleaned_content = metadata.pop("cleaned_content", full_text[:4000])
        doc_type = metadata.get("document_type", "other")
        key_fields = metadata.get("key_fields", {})

        # Enrich metadata
        metadata["telegram_user_id"] = user.id
        metadata["source"] = "telegram-pdf"
        metadata["source_user"] = family_name.lower()
        metadata["family_member"] = family_name
        metadata["document_type"] = doc_type
        metadata["file_name"] = file_name

        # Generate embedding and store
        embedding = brain.generate_embedding(cleaned_content)
        record = brain.store_memory(
            content=cleaned_content,
            embedding=embedding,
            metadata=metadata,
        )

        memory_id = record.get("id", "n/a")

        key_summary = ""
        if key_fields:
            key_lines = [f"  • {k}: {v}" for k, v in list(key_fields.items())[:5]]
            key_summary = "\n" + "\n".join(_escape(line) for line in key_lines)

        confirmation = (
            f"✅ *Got it — {_escape(doc_type)} document captured\\!*\n\n"
            f"👤 *Captured by:* {_escape(family_name)}\n"
            f"📄 *File:* {_escape(file_name)}\n"
            f"📂 *Type:* {_escape(doc_type)}\n"
            f"🏷 *Tags:* {', '.join(f'`{_escape(t)}`' for t in metadata.get('tags', [])) or '_none_'}\n"
            f"🆔 *ID:* `{_escape(str(memory_id))}`"
            f"{key_summary}"
        )

        await thinking_msg.edit_text(confirmation, parse_mode=ParseMode.MARKDOWN_V2)
        logger.info("PDF memory captured by %s (type=%s, id=%s)", family_name, doc_type, memory_id)

    except Exception as exc:
        logger.error("Failed to process PDF: %s\n%s", exc, traceback.format_exc())
        await thinking_msg.edit_text(
            f"⚠️ Failed to process PDF\\.\n\n`{_escape(str(exc))}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


async def _handle_image_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user,
    family_name: str,
    doc,
) -> None:
    """Handle images sent as document attachments (not compressed photos)."""
    thinking_msg = await update.message.reply_text("📸 Processing image…")

    try:
        file = await context.bot.get_file(doc.file_id)
        image_bytes = await file.download_as_bytearray()

        extracted_text = _extract_text_from_image(bytes(image_bytes))

        if not extracted_text:
            await thinking_msg.edit_text(
                "⚠️ Could not extract text from this image\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        caption = (update.message.caption or "").strip()
        full_text = f"{caption}\n\n{extracted_text}" if caption else extracted_text

        metadata = _extract_document_metadata(full_text)
        cleaned_content = metadata.pop("cleaned_content", full_text)
        doc_type = metadata.get("document_type", "other")

        metadata["telegram_user_id"] = user.id
        metadata["source"] = "telegram-photo"
        metadata["source_user"] = family_name.lower()
        metadata["family_member"] = family_name

        embedding = brain.generate_embedding(cleaned_content)
        record = brain.store_memory(
            content=cleaned_content,
            embedding=embedding,
            metadata=metadata,
        )

        memory_id = record.get("id", "n/a")
        confirmation = (
            f"✅ *Got it — {_escape(doc_type)} document captured\\!*\n\n"
            f"👤 *Captured by:* {_escape(family_name)}\n"
            f"🆔 *ID:* `{_escape(str(memory_id))}`"
        )
        await thinking_msg.edit_text(confirmation, parse_mode=ParseMode.MARKDOWN_V2)

    except Exception as exc:
        logger.error("Failed to process image document: %s", exc)
        await thinking_msg.edit_text(
            f"⚠️ Failed to process image\\.\n\n`{_escape(str(exc))}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ---------------------------------------------------------------------------
# Document metadata extraction via LLM
# ---------------------------------------------------------------------------
def _extract_document_metadata(text: str) -> dict[str, Any]:
    """Use the LLM to classify a document and extract structured metadata."""
    if settings.llm_backend == "anthropic" and brain._anthropic_client:
        return _extract_doc_meta_anthropic(text)
    return _extract_doc_meta_openai(text)


def _extract_doc_meta_openai(text: str) -> dict[str, Any]:
    """Document metadata extraction via OpenAI."""
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_embedding_base_url,
        )
        response = client.chat.completions.create(
            model=settings.llm_model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _DOC_TYPE_SYSTEM_PROMPT},
                {"role": "user", "content": text[:6000]},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)
    except Exception as exc:
        logger.warning("Document metadata extraction failed: %s", exc)
        return {
            "cleaned_content": text[:2000],
            "document_type": "other",
            "tags": [],
            "people": [],
            "category": "reference",
            "action_items": [],
            "key_fields": {},
        }


def _extract_doc_meta_anthropic(text: str) -> dict[str, Any]:
    """Document metadata extraction via Anthropic."""
    try:
        response = brain._anthropic_client.messages.create(
            model=settings.llm_model,
            max_tokens=1024,
            temperature=0.0,
            system=_DOC_TYPE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": text[:6000]}],
        )
        raw = response.content[0].text if response.content else "{}"
        raw = raw.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        return json.loads(raw)
    except Exception as exc:
        logger.warning("Anthropic document extraction failed: %s", exc)
        return {
            "cleaned_content": text[:2000],
            "document_type": "other",
            "tags": [],
            "people": [],
            "category": "reference",
            "action_items": [],
            "key_fields": {},
        }


# ---------------------------------------------------------------------------
# Helper: escape special characters for MarkdownV2
# ---------------------------------------------------------------------------
_MD_SPECIAL = r"\_*[]()~`>#+-=|{}.!"


def _escape(text: str) -> str:
    """Escape all MarkdownV2 special characters in *text*."""
    for ch in _MD_SPECIAL:
        text = text.replace(ch, f"\\{ch}")
    return text


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Build and start the Telegram bot using long-polling."""
    logger.info("Starting Family Brain Telegram capture layer…")

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )

    # Register handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )
    app.add_handler(
        MessageHandler(filters.PHOTO, handle_photo)
    )
    app.add_handler(
        MessageHandler(filters.Document.ALL, handle_document)
    )

    logger.info("Bot is polling for messages. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

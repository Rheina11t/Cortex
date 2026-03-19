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
import html as _html_module
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
# Conversation history
# ---------------------------------------------------------------------------
_conversation_history: dict[int, list[dict]] = {}


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
    logger.info("Google Vision API request sent, HTTP status: %d", resp.status_code)

    if resp.status_code in (400, 403):
        error_data = resp.json()
        error_message = error_data.get("error", {}).get("message", "Unknown error")
        logger.error("Google Vision API error (%d): %s", resp.status_code, error_message)
        raise RuntimeError(f"Google Vision API error: {error_message}")

    resp.raise_for_status()
    data = resp.json()

    annotations = data.get("responses", [{}])[0].get("textAnnotations", [])
    if annotations:
        return annotations[0].get("description", "").strip()

    logger.warning("Google Vision returned no text annotations. Full response: %s", data)
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
from datetime import datetime
_DOC_TYPE_SYSTEM_PROMPT = f'''\
You are a document classification assistant for a Family Brain system.

Given extracted text from a document (photo or PDF), you MUST return a JSON object with:

{{
  "cleaned_content": "<concise summary of the document's key information>",
  "document_type": "<one of: insurance, receipt, school_letter, booking, medical, pension, pension_statement, utility, warranty, invoice, contract, mot_certificate, vehicle, vehicle_finance, contract_hire, finance_agreement, tax, hmrc_letter, government_letter, legal, bank_statement, payslip, other>",
  "tags": ["<relevant topic tags>"],
  "people": ["<names of people mentioned, if any>"],
  "category": "<one of: idea, meeting-notes, decision, action-item, reference, personal, household, other>",
  "action_items": ["<any action items or deadlines extracted>"],
  "key_fields": {{"<field_name>": "<value>"}},
  "dates_mentioned": ["<any dates found in YYYY-MM-DD format>"],
  "source": "telegram-photo"
}}

Rules:
- CRITICAL: You MUST extract every labelled field you can see in the document text into key_fields. If a label like 'MOT test number', 'Location of the test', 'Testing organisation', or 'Inspector name' appears in the text, its value MUST appear in key_fields. Failure to include labelled fields is an error.
- Return ONLY valid JSON. No markdown fences, no commentary.
- `key_fields` should extract the most important structured data.
- IMPORTANT: Letters from Volkswagen Financial Services, VWFS, VW FS, Black Horse, Lex Autolease, Moneybarn, Close Brothers, or any company with "Financial Services" in the name that relates to a vehicle should be classified as `vehicle_finance`, NOT `insurance`. Insurance documents come from insurers like AXA, Aviva, Direct Line, Admiral, etc.
- Letters from HMRC (HM Revenue & Customs), DVLA, DWP, Companies House, or any UK government body should be classified as `hmrc_letter` or `government_letter` as appropriate.
- Documents related to Self Assessment, tax returns, tax codes, P60, P45, P11D should be classified as `tax`.
- Bank statements should be classified as `bank_statement`.
- Payslips should be classified as `payslip`.
- Legal documents, court letters, solicitor correspondence should be classified as `legal`.
- For vehicle documents (mot_certificate, vehicle), you MUST extract ALL of the following fields if present in the text. Do NOT omit any field that appears in the document:
  - `mot_test_number`: REQUIRED for mot_certificate — the MOT test number (a long number, e.g. "1778 7252 2687"). Look for "MOT test number" label in the text.
  - `test_location`: REQUIRED for mot_certificate — the full address where the test was carried out. Look for "Location of the test" label.
  - `testing_organisation`: REQUIRED for mot_certificate — the name of the testing centre. Look for "Testing organisation" label (e.g. "Kwik Fit", "V102841 KWIK FIT").
  - `inspector_name`: REQUIRED for mot_certificate — the inspector's name. Look for the name after the testing organisation code.
  - `earliest_retest_date`: The earliest date the vehicle can be presented for retest in YYYY-MM-DD format.
  - `vehicle_identification_number`: The VIN number.
  - `registration_number`: The vehicle registration plate.
  - `make_and_model`: Make and model of the vehicle.
  - `test_result`: Pass or Fail.
  - `mileage`: Mileage at time of test.
  - `date_of_test`: Date of the test in YYYY-MM-DD.
  - `expiry_date`: MOT expiry date in YYYY-MM-DD.
- For vehicle finance documents (vehicle_finance, contract_hire, finance_agreement):
  - `agreement_number`: The finance agreement or contract number
  - `vehicle_model`: The vehicle make and model
  - `registration_number`: The vehicle registration plate
  - `provider_name`: The finance provider (e.g. "Volkswagen Financial Services", "Black Horse", "Lex Autolease")
  - `monthly_payment`: Monthly payment amount if present
  - `contract_end_date`: When the agreement ends
  - `mileage_allowance`: Annual or total mileage allowance if present
  - `contact_phone`: Provider contact phone number
  - `contact_email`: Provider contact email
- For tax documents (tax, hmrc_letter):
  - `utr`: Unique Taxpayer Reference number
  - `case_ref`: Case reference number
  - `tax_year`: The tax year the document relates to
  - `deadline`: Any deadline mentioned in the document (YYYY-MM-DD)
  - `amount_owed`: Any tax amount owed or due
  - `amount_refund`: Any refund amount
  - `contact_phone`: HMRC contact phone number
  - `contact_email`: HMRC contact email
  - `reference_number`: Any other reference number on the document
- For financial documents (receipt, invoice, warranty, utility, pension_statement, bank_statement, payslip):
  - `provider_name`: The name of the company/provider (e.g. "British Gas", "Apple")
  - `total_amount_due`: The total amount of the bill/receipt
  - `due_date`: The due date of the bill (YYYY-MM-DD)
  - `payment_method`: How the bill is paid (e.g. "Direct Debit", "Credit Card")
  - `iban`: IBAN if present
  - `account_number`: Account number if present
  - `sort_code`: Sort code if present
- `cleaned_content` should be a human-readable summary. DO NOT just repeat the input.
- For `action_items`: NEVER include action items for dates that are in the past (before today). Only include action items for future dates or undated items. Today's date is {datetime.now().strftime('%Y-%m-%d')}.
- NEVER include "make the payment" or "pay the direct debit" as an action item if the payment_method is "direct debit" or "DD" — direct debits are automatic and require no action.
- Always extract any reference numbers, certificate numbers, test numbers, or unique identifiers present in the document into key_fields, even if not explicitly listed above.
'''


# ---------------------------------------------------------------------------
# Event Detection
# ---------------------------------------------------------------------------
_EVENT_TAGS = {
    "event", "booking", "schedule", "appointment", "travel", "meeting", "reminder"
}

def _extract_event_details(raw_text: str) -> dict:
    """Use a dedicated LLM call to extract structured event details."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    # Build a name list for the notify_members hint
    name_list = ", ".join(FAMILY_MEMBERS.values()) if FAMILY_MEMBERS else "family members"
    system_prompt = f"""\
You are an event extraction assistant for a family calendar. Today is {today_str}.
Extract event details from the user message and return a JSON object.

Rules:
- ANY mention of a person being somewhere on a specific date counts as an event. Set has_event=true.
- Travel, trips, visits, meetings, appointments, school events, sports, etc. are all events.
- Resolve dates like "Thursday 26th March", "next Tuesday", "tomorrow", "24th March" to YYYY-MM-DD.
- If you cannot determine the year, assume {today_str[:4]}.
- If there is truly NO date mentioned at all, set has_event=false.
- event_name should be a short descriptive label like "Dan in London" or "School trip".
- is_all_day: set true if no specific start time is given, or if the time mentioned is a return/end time (e.g. "Back by 18:00", "home by 6pm"). Set false only if a clear start time is given (e.g. "meeting at 10am").
- event_time: only set this if is_all_day is false and a clear START time is given. Leave null for all-day events.
- end_time: set this if a return/end time is mentioned (e.g. "Back by 18:00" → "18:00"). Leave null otherwise.
- notify_members: list of family member names to notify about this event. Known members: {name_list}. Include anyone explicitly mentioned as needing to know, or leave empty.

Return ONLY valid JSON with keys: has_event, event_name, event_date, is_all_day, event_time, end_time, location, notify_members.
"""
    json_schema = {
        "type": "object",
        "properties": {
            "has_event": {"type": "boolean"},
            "event_name": {"type": "string", "description": "A concise name for the event."},
            "event_date": {"type": "string", "description": "The event date in YYYY-MM-DD format, or null."},
            "is_all_day": {"type": "boolean", "description": "True if no specific start time, or if time mentioned is a return/end time."},
            "event_time": {"type": "string", "description": "Start time in HH:MM format, only if is_all_day is false. Null otherwise."},
            "end_time": {"type": "string", "description": "Return/end time in HH:MM format if mentioned, null otherwise."},
            "location": {"type": "string", "description": "The location of the event, or null."},
            "notify_members": {"type": "array", "items": {"type": "string"}, "description": "Names of family members to notify."},
        },
        "required": ["has_event"],
    }

    try:
        event_details = brain.get_llm_reply(
            system_message=system_prompt,
            user_message=raw_text,
            json_schema=json_schema,
        )
        logger.info("[EVENT DEBUG RAW] LLM returned: %s", event_details)
        if isinstance(event_details, str):
            event_details = json.loads(event_details)
        
        # Basic validation
        if event_details.get("has_event") and event_details.get("event_date"):
            logger.info("Extracted event details: %s", event_details)
            return event_details
        return {"has_event": False}

    except Exception as exc:
        logger.warning("Could not extract event details: %s", exc)
        return {"has_event": False}


# ---------------------------------------------------------------------------
# Query intent detection
# ---------------------------------------------------------------------------
_QUESTION_WORDS = (
    "when", "where", "what", "who", "which", "how", "why",
    "did", "do", "does", "is", "are", "was", "were", "have", "has", "had",
    "can", "could", "would", "should",
    "tell", "show", "find", "search", "look", "remind", "recall", "remember",
)
_QUERY_PATTERNS = (
    "when did", "where did", "what did", "who did",
    "have i", "do i", "did i",
    "do we", "did we", "have we",
    "is my", "are my", "was my", "were my",
)


def _is_query(text: str, user_id: int) -> bool:
    """
    Determine if a message is a question/query vs. something to be captured.
    """
    text_lower = text.lower().strip()
    if not text_lower:
        return False

    # 1. Context-aware check for short follow-up questions
    history = _conversation_history.get(user_id, [])
    if history and len(text.split()) <= 4:
        logger.info("Short message with history detected as query: '%s'", text)
        return True

    # 2. Check for question mark
    if text_lower.endswith("?"):
        return True

    # 3. Check for start with question words
    if text_lower.startswith(_QUESTION_WORDS):
        return True

    # 4. Check for mid-sentence query patterns
    if any(p in text_lower for p in _QUERY_PATTERNS):
        return True

    # 5. LLM fallback for ambiguous cases
    try:
        reply = brain.get_llm_reply(
            "Is this message a QUESTION/QUERY asking for information, or is it a STATEMENT/FACT providing information to remember? Examples of statements: 'Dan in London Tuesday 24th March', 'Car MOT due next month', 'Emma has dentist 3pm Friday'. Examples of queries: 'When is my MOT?', 'What insurance do I have?'. Reply with only 'query' or 'store'.",
            user_message=text,
            max_tokens=5,
        ).lower()
        logger.info("LLM intent classification for '%s': %s", text, reply)
        return "query" in reply
    except Exception as exc:
        logger.warning("LLM intent classification failed: %s", exc)
        return False  # default to capture if LLM fails


async def _answer_query(
    raw_text: str,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    conversation_history: list[dict] | None = None,
) -> None:
    """
    Answer a query by searching the brain and synthesising a response.
    """
    if not update.message:
        return

    await update.message.reply_text("🔍 Searching...")

    # 1. Expand query with synonyms
    synonyms = set()
    text_lower = raw_text.lower()
    if "lease" in text_lower:
        synonyms.add("contract hire")
    if "contract hire" in text_lower:
        synonyms.add("lease")
    if any(w in text_lower for w in ("end", "ends", "ending")):
        synonyms.add("expiry")
        synonyms.add("expires")
    
    expanded_query = raw_text
    if synonyms:
        expanded_query += " " + " ".join(synonyms)
        logger.info("Expanded query to: '%s'", expanded_query)

    # 2. Perform search (semantic + metadata fallback)
    results = brain.semantic_search(expanded_query, match_threshold=0.3, match_count=5)
    if not results:
        logger.info("Semantic search for '%s' returned no results, trying metadata.", raw_text)
        results = brain.query_by_metadata(raw_text, limit=3)

    # 3. Synthesise answer
    if results:
        memories = "\n".join(
            f"- {r.get('content', '')} (ID: {r.get('id')})"
            for r in results
        )
        prompt = (
            "You are Family Brain, a personal AI assistant. "
            "Based on these stored memories, answer the user's question concisely. "
            "Use the conversation history for context if needed. "
            "Never mention the memory IDs in your answer."
        )
        
        messages = [{"role": "system", "content": prompt}]
        if conversation_history:
            messages.extend(conversation_history)
        messages.append({"role": "user", "content": f"Question: {raw_text}\n\nRelevant memories:\n{memories}"})

        answer = brain.get_llm_reply(messages=messages)
        source_ids = ", ".join(f'<code>{_escape(str(r.get("id")))}</code>' for r in results)
        answer_truncated = answer[:3000] + ("..." if len(answer) > 3000 else "")
        reply_text = f'{_escape(answer_truncated)}\n\n<b>Sources:</b> {source_ids}'

        # Update conversation history
        if update.effective_user:
            user_id = update.effective_user.id
            if user_id not in _conversation_history:
                _conversation_history[user_id] = []
            _conversation_history[user_id].append({"role": "user", "content": raw_text})
            _conversation_history[user_id].append({"role": "assistant", "content": answer})
            _conversation_history[user_id] = _conversation_history[user_id][-6:] # keep last 3 turns

    else:
        reply_text = "I don't have anything stored about that yet. Send me the information and I'll remember it for next time."

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Telegram command handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a welcome message when the /start command is issued."""
    if not update.effective_user:
        return
    
    user = update.effective_user
    family_name = _get_family_name(user.id)

    if family_name and user.id not in _WELCOMED_USERS:
        await update.message.reply_text(f"Hello {_escape(user.first_name)}! Welcome to the Family Brain bot.")
        _WELCOMED_USERS.add(user.id)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return the status of the bot and its configuration."""
    if not update.message:
        return

    ocr_backend = "Google Vision" if _USE_GOOGLE_VISION else "pytesseract (local)"
    reply = f'''<b>Family Brain Status:</b>

<b>OCR Backend:</b> <code>{_escape(ocr_backend)}</code>
<b>Family Members:</b> <code>{_escape(str(len(FAMILY_MEMBERS)))}</code>'''
    await update.message.reply_text(reply, parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# Telegram message handlers
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages: capture as memory or answer if it's a query."""
    if not update.message or not update.message.text or not update.effective_user:
        return

    user = update.effective_user
    raw_text = update.message.text
    family_name = _get_family_name(user.id)

    if not family_name:
        logger.warning("Ignoring message from unauthorised user: %d", user.id)
        return

    # --- Intent detection: is it a query or something to store? ---
    if _is_query(raw_text, user.id):
        history = _conversation_history.get(user.id, [])
        await _answer_query(raw_text, update, context, conversation_history=history)
        return

    # --- Capture flow: store as a memory ---
    logger.info("Capturing text from %s: '%s'", family_name, raw_text)
    try:
        metadata = brain.extract_metadata(raw_text, source="telegram-text")
        tags = metadata.get("tags", [])
        category = metadata.get("category", "other")
        action_items = metadata.get("action_items", [])

        embedding = brain.generate_embedding(raw_text)
        memory_id = brain.store_memory(
            content=raw_text,
            embedding=embedding,
            metadata=metadata,
            user_id=str(user.id),
            family_member=family_name,
        )

        tags_str = ", ".join(_escape(t) for t in tags[:10]) or "none"
        # Cap each action item and total action_str to avoid Telegram 4096-char limit
        action_items_short = [a[:80] for a in action_items[:5]]
        action_str = ", ".join(_escape(a) for a in action_items_short) or "none"
        if len(action_str) > 300:
            action_str = action_str[:300] + "…"
        # store_memory returns a dict; extract the UUID string
        if isinstance(memory_id, dict):
            mem_id_str = str(memory_id.get("id", memory_id))
        else:
            mem_id_str = str(memory_id)
        reply_text = (
            f"✅ Memory captured by {family_name}!\n\n"
            f"Category: {category}\n"
            f"Tags: {tags_str}\n"
            f"Action items: {action_str}\n"
            f"ID: {mem_id_str}"
        )
        await update.message.reply_text(reply_text)

        # --- Event detection ---
        event_tags_found = _EVENT_TAGS.intersection(tags)
        logger.info("[EVENT DEBUG] tags=%s event_tags_found=%s", tags, event_tags_found)
        if event_tags_found or metadata.get("document_type") == "booking":
            logger.info("Potential event detected from tags: %s. Extracting details...", event_tags_found)
            event_details = _extract_event_details(raw_text)
            logger.info("[EVENT DEBUG] event_details=%s", event_details)
            if event_details.get("has_event"):
                # Merge dedicated event details into the broader metadata
                merged_data = {**metadata, **event_details}
                await _check_conflicts_and_store_event(merged_data, update, family_name)

        # --- Recurring bill detection ---
        if "bill" in tags and metadata.get("payment_method", "").lower() == "direct debit":
            await _maybe_store_financial_details(metadata, "recurring_bill", update, family_name)

    except Exception as exc:
        logger.error("Failed to process text message: %s", exc, exc_info=True)
        try:
            err_str = str(exc)
            if "message is too long" in err_str.lower() or "too long" in err_str.lower():
                await update.message.reply_text("⚠️ Reply was too long to send. Memory was stored successfully.")
            else:
                err_msg = err_str[:150]
                await update.message.reply_text(f"⚠️ Failed to process message: {err_msg}")
        except Exception:
            pass  # give up silently if we can't even send the error


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos: OCR and process as a document."""
    if not update.message or not update.message.photo or not update.effective_user:
        return

    user = update.effective_user
    family_name = _get_family_name(user.id)
    if not family_name:
        logger.warning("Ignoring photo from unauthorised user: %d", user.id)
        return

    await update.message.reply_text("📄 Processing photo...")

    try:
        photo_file = await context.bot.get_file(update.message.photo[-1].file_id)
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, os.path.basename(photo_file.file_path or "image.jpg"))
            await photo_file.download_to_drive(file_path)
            with open(file_path, "rb") as f:
                image_bytes = f.read()

        await _handle_image_document(
            image_bytes=image_bytes,
            file_name=os.path.basename(file_path),
            update=update,
            family_name=family_name,
            source="telegram-photo",
        )

    except Exception as exc:
        logger.error("Failed to process photo: %s", exc, exc_info=True)
        err_msg = str(exc)[:200]
        await update.message.reply_text(f"⚠️ Failed to process photo.\n\n<code>{_escape(err_msg)}</code>", parse_mode=ParseMode.HTML)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle documents: PDF text extraction or OCR for images."""
    if not update.message or not update.message.document or not update.effective_user:
        return

    user = update.effective_user
    family_name = _get_family_name(user.id)
    if not family_name:
        logger.warning("Ignoring document from unauthorised user: %d", user.id)
        return

    doc = update.message.document
    if not doc.file_name:
        return

    if doc.mime_type and doc.mime_type.startswith("image/"):
        await update.message.reply_text("📄 Processing image document...")
    elif doc.mime_type == "application/pdf":
        await update.message.reply_text("📄 Processing PDF document...")
    else:
        await update.message.reply_text(
            "📄 I currently support PDF documents and images. Please send a PDF or photo."
        )
        return

    try:
        doc_file = await context.bot.get_file(doc.file_id)
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, doc.file_name)
            await doc_file.download_to_drive(file_path)
            with open(file_path, "rb") as f:
                doc_bytes = f.read()

        if doc.mime_type and doc.mime_type.startswith("image/"):
            await _handle_image_document(
                image_bytes=doc_bytes,
                file_name=doc.file_name,
                update=update,
                family_name=family_name,
                source="telegram-document-image",
            )
        elif doc.mime_type == "application/pdf":
            extracted_text = _extract_text_from_pdf(doc_bytes)
            if not extracted_text:
                await update.message.reply_text(
                    "⚠️ Could not extract text from this PDF. The file may be image-based (scanned). Try sending individual page photos instead."
                )
                return
            
            await _process_and_store_document(
                extracted_text=extracted_text,
                file_name=doc.file_name,
                update=update,
                family_name=family_name,
                source="telegram-pdf",
            )

    except Exception as exc:
        logger.error("Failed to process document: %s", exc, exc_info=True)
        err_msg = str(exc)[:200]
        await update.message.reply_text(f"⚠️ Failed to process PDF.\n\n<code>{_escape(err_msg)}</code>", parse_mode=ParseMode.HTML)


async def _handle_image_document(
    image_bytes: bytes,
    file_name: str,
    update: Update,
    family_name: str,
    source: str,
) -> None:
    """Shared logic for handling any image-based document (photo or file)."""
    extracted_text = _extract_text_from_image(image_bytes)
    if not extracted_text:
        await update.message.reply_text("⚠️ Could not extract text from this image.")
        return

    await _process_and_store_document(
        extracted_text=extracted_text,
        file_name=file_name,
        update=update,
        family_name=family_name,
        source=source,
    )


async def _process_and_store_document(
    extracted_text: str,
    file_name: str,
    update: Update,
    family_name: str,
    source: str,
) -> None:
    """Shared logic to get metadata, store, and reply for any document."""
    if not update.effective_user:
        return

    metadata = brain.extract_metadata(extracted_text, source=source)
    cleaned_content = metadata.get("cleaned_content", "")
    doc_type = metadata.get("document_type", "other")
    key_fields = metadata.get("key_fields", {})

    enriched_content = _enrich_content_with_key_fields(cleaned_content, key_fields, doc_type)

    embedding = brain.generate_embedding(enriched_content)
    memory_id = brain.store_memory(
        content=enriched_content,
        embedding=embedding,
        metadata=metadata,
        user_id=str(update.effective_user.id),
        family_member=family_name,
    )

    key_summary = ""
    if key_fields:
        key_lines = [f"  • <b>{_escape(k)}:</b> {_escape(v)}" for k, v in key_fields.items() if v]
        if key_lines:
            key_summary = "\n\n<b>Key Details:</b>\n" + "\n".join(key_lines)

    financial_note = ""
    if metadata.get("payment_method", "").lower() == "direct debit":
        financial_note = "\n\n<i>This looks like a recurring bill. I'll keep an eye on it.</i>"

    tags_str = ", ".join(f'<code>{_escape(t)}</code>' for t in metadata.get('tags', [])) or "<i>none</i>"
    reply_text = (
        f"✅ <b>Got it — {_escape(doc_type)} document captured!</b>\n\n"
        f"👤 <b>Captured by:</b> {_escape(family_name)}\n"
        f"📄 <b>File:</b> {_escape(file_name)}\n"
        f"🏷️ <b>Type:</b> {_escape(doc_type)}\n"
        f"️ <b>Tags:</b> {tags_str}\n"
        f"🆔 <b>ID:</b> <code>{_escape(str(memory_id))}</code>"
        f"{key_summary}"
        f"{financial_note}"
    )
    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)

    # --- Event detection ---
    event_tags_found = _EVENT_TAGS.intersection(metadata.get("tags", []))
    if event_tags_found or doc_type == "booking":
        logger.info("Potential event detected from document tags: %s. Extracting details...", event_tags_found)
        # Use the cleaned content from the document for event extraction
        event_details = _extract_event_details(cleaned_content)
        if event_details.get("has_event"):
            # Merge dedicated event details into the broader metadata
            merged_data = {**metadata, **event_details}
            await _check_conflicts_and_store_event(merged_data, update, family_name)

    # --- Recurring bill detection ---
    if "bill" in metadata.get("tags", []) and metadata.get("payment_method", "").lower() == "direct debit":
        await _maybe_store_financial_details(metadata, "recurring_bill", update, family_name)


def _enrich_content_with_key_fields(cleaned_content: str, key_fields: dict, doc_type: str) -> str:
    """Enrich content with key fields for better semantic search."""
    if not key_fields:
        return cleaned_content

    field_str = " | ".join(f'{k}: {v}' for k, v in key_fields.items() if v)
    if not field_str:
        return cleaned_content

    return f'{cleaned_content}\n\nKey details for this {doc_type.replace("_", " ")}:\n{field_str}'


async def _check_conflicts_and_store_event(
    event_data: dict[str, Any],
    update: Update,
    sender_name: str,
) -> None:
    """Store an event and notify of any conflicts."""
    if not update.message:
        return

    event_name = event_data.get("event_name") or event_data.get("cleaned_content") or ""
    event_name = str(event_name)[:100]  # truncate to avoid Telegram message length issues
    event_date_str = event_data.get("event_date")
    is_all_day = event_data.get("is_all_day", True)
    event_time = None if is_all_day else event_data.get("event_time")
    end_time = event_data.get("end_time")
    family_member = event_data.get("family_member")
    notify_members = event_data.get("notify_members") or []

    if not event_name or not event_date_str:
        return

    try:
        event_date = datetime.strptime(event_date_str, "%Y-%m-%d").date()
    except ValueError:
        logger.warning("Could not parse event date: %s", event_date_str)
        return

    # Check for conflicts
    conflicts = brain.get_events_on_date(event_date)
    if conflicts:
        await update.message.reply_text(
            f"⚠️ Event '{event_name}' on {event_date_str} might conflict with an existing event on that day."
        )

    # Store the event
    event_id = brain.store_event(
        event_name=event_name,
        event_date=event_date,
        event_time=event_time,
        metadata=event_data,
        family_member=family_member,
    )
    await update.message.reply_text(
        f"✅ Event stored: {event_name} on {event_date_str}\nID: {event_id}"
    )

    # Also push to Google Calendar (best-effort, never crash the handler)
    try:
        try:
            from . import google_calendar as _gcal
        except ImportError as ie:
            logger.warning("Google Calendar module not available: %s", ie)
            _gcal = None
        if _gcal is not None:
            gcal_event_id = _gcal.create_event(
                event_name=event_name,
                event_date=event_date.isoformat() if hasattr(event_date, 'isoformat') else str(event_date),
                event_time=event_time,  # None for all-day events
                end_time=end_time,
                location=event_data.get("location", ""),
                description=f"Captured by {sender_name} via Family Brain",
                family_member=family_member,
            )
            if gcal_event_id:
                logger.info("Event pushed to Google Calendar: %s", gcal_event_id)
    except Exception as exc:
        logger.warning("Google Calendar push failed: %s", exc)

    # Notify family members (best-effort)
    if notify_members:
        # Build reverse lookup: name -> chat_id
        name_to_id = {name.lower(): uid for uid, name in FAMILY_MEMBERS.items()}
        for notify_name in notify_members:
            target_id = name_to_id.get(notify_name.lower())
            if target_id and target_id != update.effective_user.id:
                try:
                    time_str = f" at {end_time}" if end_time else ""
                    await update.get_bot().send_message(
                        chat_id=target_id,
                        text=f"📅 FYI: {event_name} on {event_date_str}{time_str} — added by {sender_name}"
                    )
                    logger.info("Notified %s (chat_id=%s) about event %s", notify_name, target_id, event_name)
                except Exception as exc:
                    logger.warning("Failed to notify %s: %s", notify_name, exc)


async def _maybe_store_financial_details(
    metadata: dict[str, Any],
    table_name: str,
    update: Update,
    sender_name: str,
) -> None:
    """Store financial details (e.g. recurring bill) if key fields are present."""
    if not update.message:
        return

    provider = metadata.get("key_fields", {}).get("provider_name")
    amount = metadata.get("key_fields", {}).get("total_amount_due")

    if provider and amount:
        try:
            try:
                amount_float = float(str(amount).replace('£','').replace(',','').strip())
            except (ValueError, AttributeError):
                amount_float = 0.0
            brain.add_recurring_bill(
                name=provider,
                provider=provider,
                amount_gbp=amount_float,
                notes=f"Captured by {sender_name}",
            )
            await update.message.reply_text(
                f"✅ Recurring bill for <b>{_escape(provider)}</b> noted.",
                parse_mode=ParseMode.HTML
            )
        except Exception as exc:
            logger.warning("Failed to store recurring bill: %s", exc)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Errors caused by Updates."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    # Optionally, notify the user that an error occurred.
    if isinstance(update, Update) and update.message:
        try:
            await update.message.reply_text("Sorry, an unexpected error occurred. The team has been notified.")
        except Exception as e:
            logger.error("Failed to send error message to user: %s", e)


def _escape(text: str) -> str:
    """Escape text for Telegram HTML parse mode."""
    return _html_module.escape(str(text)) if text else ""


def main() -> None:
    """Start the bot."""
    application = Application.builder().token(settings.telegram_bot_token).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    application.add_error_handler(error_handler)

    logger.info("Telegram bot starting...")
    application.run_polling()


if __name__ == "__main__":
    main()

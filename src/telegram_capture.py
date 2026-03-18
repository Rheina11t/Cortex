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
_DOC_TYPE_SYSTEM_PROMPT = f"""\
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
- For financial documents (insurance, invoices, utilities), you MUST extract the following fields if present:
  - `provider_name`: The name of the company providing the service.
  - `policy_number`: The policy number.
  - `reference_number`: Any other reference or account number.
  - `bank_account_number`: The bank account number for payments.
  - `sort_code`: The sort code for payments.
  - `direct_debit_amount`: The amount of the direct debit.
  - `payment_frequency`: How often the payment is made (e.g., monthly, annually).
- If a field has no value, use an empty list [] or empty string "" or empty object {{}}.
- Always extract any reference numbers, certificate numbers, test numbers, or unique identifiers present in the document into key_fields, even if not explicitly listed above.
- Keep cleaned_content as a faithful, concise summary.
- For action_items: NEVER include action items for dates that are in the past (before today). Only include action items for future dates or undated items. Today's date is {datetime.now().strftime('%Y-%m-%d')}.
- NEVER include "make the payment" or "pay the direct debit" as an action item if the payment_method is "direct debit" or "DD" — direct debits are automatic and require no action.
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

            # Also push to Google Calendar
            try:
                from . import google_calendar
                gcal_event_id = google_calendar.create_event(
                    event_name=event_name,
                    event_date=event_date,
                    event_time=event_time if event_time else None,
                    location=event_data.get("location", ""),
                    description=f"Captured by {sender_name} via Family Brain",
                    family_member=family_member,
                )
                if gcal_event_id:
                    logger.info("Event pushed to Google Calendar: %s", gcal_event_id)
            except Exception as exc:
                logger.warning("Google Calendar push failed: %s", exc)

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
        f"🔍 OCR: {_escape('Google Vision' if _USE_GOOGLE_VISION else 'pytesseract (local)')}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ---------------------------------------------------------------------------
# Intent detection: determine if a message is a query or something to capture
# ---------------------------------------------------------------------------

_QUESTION_WORDS = (
    "when", "where", "what", "who", "which", "how", "why", "did", "do", "does", "is", "are", "was", "were", "have", "has", "had", "can", "could", "would", "should", "tell", "show", "find", "search", "look", "remind", "recall", "remember"
)
_QUERY_PHRASES = (
    "when did", "where did", "what did", "who did", "have i", "do i", "did i", "do we", "did we", "have we", "is my", "are my", "was my", "were my"
)

def _is_query(text: str, user_id: int) -> bool:
    """Return True if the text is likely a question/query, not something to be stored."""
    # Context-aware check for short follow-up questions
    history = _conversation_history.get(user_id, [])
    if history and len(text.split()) <= 4:
        logger.info("Treating short message from user %d as a query due to recent conversation history.", user_id)
        return True
    text_lower = text.lower()
    if text_lower.endswith("?"):
        return True
    if text_lower.startswith(_QUESTION_WORDS):
        return True
    if any(phrase in text_lower for phrase in _QUERY_PHRASES):
        return True

    # Fallback to a quick LLM check for ambiguous cases
    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_embedding_base_url,
        )
        response = client.chat.completions.create(
            model="gpt-4.1-nano",  # Use a fast, cheap model for this
            temperature=0.0,
            max_tokens=5,
            messages=[
                {
                    "role": "system",
                    "content": "Is this message a QUESTION/QUERY asking for information, or is it a STATEMENT providing information to store? Reply with only 'query' or 'store'."
                },
                {"role": "user", "content": text},
            ],
        )
        reply = response.choices[0].message.content or ""
        logger.info("Intent detection for %r: %s", text, reply)
        return "query" in reply.lower()
    except Exception as exc:
        logger.warning("Intent detection LLM call failed: %s", exc)
        return False  # Default to capture if intent detection fails

# ---------------------------------------------------------------------------
# Query handler: search for answers and synthesise a reply
# ---------------------------------------------------------------------------

_SYNTHESIS_PROMPT = '''You are Family Brain, a personal AI assistant. Based on these stored memories, answer the user's question concisely. Question: {question}

Relevant memories:
{memories}

Answer:'''

async def _answer_query(
    raw_text: str, 
    update: Update, 
    context: ContextTypes.DEFAULT_TYPE, 
    conversation_history: list[dict] | None = None
) -> None:
    """Handle a message that has been identified as a query."""
    if update.message is None:
        return

    thinking_msg = await update.message.reply_text("🔍 Searching the family brain…")

    try:
        # Step 1: Expand query with synonyms and perform semantic search
        synonyms = []
        if "lease" in raw_text.lower():
            synonyms.append("contract hire")
        if "contract hire" in raw_text.lower():
            synonyms.append("lease")
        if any(word in raw_text.lower() for word in ["end", "ends", "ending"]):
            synonyms.extend(["expiry", "expires"])
        
        expanded_query = raw_text + " " + " ".join(synonyms)
        results = brain.semantic_search(expanded_query, match_threshold=0.3, match_count=5)
        if not results:
            # Fallback for exact-match queries on policy numbers, etc.
            logger.info("Semantic search returned no results; trying metadata query for: %s", raw_text)
            try:
                # This is a broad search across all metadata values.
                # It relies on the user's query being a plausible substring.
                metadata_results = brain.query_by_metadata(raw_text, limit=3)
                if metadata_results:
                    logger.info("Metadata fallback search found %d results", len(metadata_results))
                    results.extend(metadata_results)
            except Exception as meta_exc:
                logger.warning("Metadata fallback search failed: %s", meta_exc)

        if not results:
            await thinking_msg.edit_text(
                "I don't have anything stored about that yet. "
                "Send me the information and I'll remember it for next time."
            )
            return

        # Step 2: Synthesise an answer from the results, including conversation history
        memories_str = "\n---\n".join(
            "Memory ID: {}\nContent: {}".format(r.get("id"), r.get("content"))
            for r in results
        )
        prompt = _SYNTHESIS_PROMPT.format(question=raw_text, memories=memories_str)

        # Prepend a system message, then prior conversation turns, then the current prompt
        messages = [
            {"role": "system", "content": "You are Family Brain, a helpful AI assistant for a family. Use the conversation history for context when answering follow-up questions."}
        ]
        messages.extend(conversation_history or [])
        messages.append({"role": "user", "content": prompt})

        from openai import OpenAI
        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_embedding_base_url,
        )
        response = client.chat.completions.create(
            model=settings.llm_model,
            temperature=0.1,
            messages=messages,
        )
        answer = response.choices[0].message.content or "I found some information but couldn't synthesise an answer."

        # Step 3: Update conversation history
        if update.effective_user:
            user_id = update.effective_user.id
            history = _conversation_history.get(user_id, [])
            history.append({"role": "user", "content": raw_text})
            history.append({"role": "assistant", "content": answer})
            _conversation_history[user_id] = history[-6:]  # Keep last 3 turns

        # Step 4: Format and send the reply
        source_ids = ", ".join(f"`{_escape(str(r.get('id')))}`" for r in results)
        reply = f"{_escape(answer)}\n\n*Sources:* {source_ids}"

        await thinking_msg.edit_text(reply, parse_mode=ParseMode.MARKDOWN_V2)
        logger.info("Answered query with %d sources", len(results))

    except Exception as exc:
        logger.error("Failed to answer query: %s\n%s", exc, traceback.format_exc())
        await thinking_msg.edit_text(
            f"⚠️ Failed to answer query\\.\n\n`{_escape(str(exc))}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ---------------------------------------------------------------------------
# Message handler: capture text memories
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process every text message sent to the bot, routing to query or capture."""
    if update.message is None or update.effective_user is None:
        return

    user = update.effective_user
    raw_text: str = (update.message.text or "").strip()

    if not raw_text:
        return

    # --- Auth check ---
    family_name = _get_family_name(user.id)
    if family_name is None:
        logger.warning("Rejected message from unauthorised user id=%s", user.id)
        await update.message.reply_text(
            "Sorry, you're not authorised to use this Family Brain bot. "
            "Please ask the bot owner to add your Telegram user ID."
        )
        return

    # --- First-time welcome ---
    if user.id not in _WELCOMED_USERS and FAMILY_MEMBERS:
        _WELCOMED_USERS.add(user.id)
        if user.id != list(FAMILY_MEMBERS.keys())[0]:
            await update.message.reply_text(
                f"👋 Welcome to Family Brain, {family_name}! "
                "I can answer questions based on what you've told me, or I can store new information. "
                "Just send me a message, photo, or PDF."
            )

    logger.info(
        "Received message from %s (id=%s): %d chars",
        family_name, user.id, len(raw_text),
    )

    # --- Intent detection: Query vs Capture ---
    if _is_query(raw_text, user.id):
        history = _conversation_history.get(user.id, [])
        await _answer_query(raw_text, update, context, conversation_history=history)
        return

    # --- Default to capture flow ---
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
                _ename = event_data.get('event_name', '')
                _edate = event_data.get('event_date', '')
                event_info = f'\n\U0001f4c5 *Event detected:* {_escape(_ename)} on {_escape(_edate)}'
            if conflict_warning:
                event_info += '\n\n' + _escape(conflict_warning)

        # Step 5: Build confirmation
        tags_str = ", ".join(f'`{_escape(t)}`' for t in tags) if tags else "_none_"
        action_str = (
            "\n".join(f'  • {_escape(item)}' for item in action_items)
            if action_items
            else "_none_"
        )

        confirmation = (
            f'✅ *Memory captured by {_escape(family_name)}\\\!*\n\n'
            f'📂 *Category:* {_escape(category)}\n'
            f'🏷 *Tags:* {tags_str}\n'
            f'🎯 *Action items:* {action_str}\n'
            f'🆔 *ID:* `{_escape(str(memory_id))}`'
            f'{event_info}'
        )

        await thinking_msg.edit_text(confirmation, parse_mode=ParseMode.MARKDOWN_V2)
        logger.info("Memory captured by %s (id=%s)", family_name, memory_id)

    except Exception as exc:
        logger.error("Failed to capture memory: %s\n%s", exc, traceback.format_exc())
        await thinking_msg.edit_text(
            f'⚠️ Failed to capture memory\\.\n\n`{_escape(str(exc))}`',
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

        # Enrich content with key fields for better searchability
        enriched_content = _enrich_content_with_key_fields(
            cleaned_content, key_fields, doc_type
        )

        # Generate embedding and store
        embedding = brain.generate_embedding(enriched_content)
        record = brain.store_memory(
            content=enriched_content,
            embedding=embedding,
            metadata=metadata,
        )

        memory_id = record.get("id", "n/a")

        # Route financial details to recurring_bills table
        financial_summary = _maybe_store_financial_details(
            doc_type, key_fields, cleaned_content, family_name
        )

        # Build summary of key fields (up to 8, always show bank/sort if present)
        key_summary = ""
        if key_fields:
            priority_keys = {"bank_account_number", "sort_code"}
            priority_items = [(k, v) for k, v in key_fields.items() if k in priority_keys]
            other_items = [(k, v) for k, v in key_fields.items() if k not in priority_keys]
            combined = other_items[:8] + [i for i in priority_items if i not in other_items[:8]]
            key_lines = [f"  • {_escape(k)}: {_escape(v)}" for k, v in combined[:8]]
            key_summary = "\n" + "\n".join(line for line in key_lines)

        financial_note = f"\n{_escape(financial_summary)}" if financial_summary else ""

        confirmation = (
            f"\u2705 *Got it \u2014 {_escape(doc_type)} document captured\\!*\n\n"
            f"\U0001f464 *Captured by:* {_escape(family_name)}\n"
            f"\U0001f4c4 *Type:* {_escape(doc_type)}\n"
            "\U0001f3f7 *Tags:* " + (", ".join(f'`{_escape(t)}`' for t in metadata.get('tags', [])) or '_none_') + "\n"
            f"\U0001f194 *ID:* `{_escape(str(memory_id))}`"
            f"{key_summary}"
            f"{financial_note}"
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

        # Enrich content with key fields for better searchability
        enriched_content = _enrich_content_with_key_fields(
            cleaned_content, key_fields, doc_type
        )

        # Generate embedding and store
        embedding = brain.generate_embedding(enriched_content)
        record = brain.store_memory(
            content=enriched_content,
            embedding=embedding,
            metadata=metadata,
        )

        memory_id = record.get("id", "n/a")

        # Route financial details to recurring_bills table
        financial_summary = _maybe_store_financial_details(
            doc_type, key_fields, cleaned_content, family_name
        )

        # Build summary of key fields (up to 8, always show bank/sort if present)
        key_summary = ""
        if key_fields:
            priority_keys = {"bank_account_number", "sort_code"}
            priority_items = [(k, v) for k, v in key_fields.items() if k in priority_keys]
            other_items = [(k, v) for k, v in key_fields.items() if k not in priority_keys]
            combined = other_items[:8] + [i for i in priority_items if i not in other_items[:8]]
            key_lines = [f"  • {_escape(k)}: {_escape(v)}" for k, v in combined[:8]]
            key_summary = "\n" + "\n".join(line for line in key_lines)

        financial_note = f"\n{_escape(financial_summary)}" if financial_summary else ""

        confirmation = (
            f"\u2705 *Got it \u2014 {_escape(doc_type)} document captured\\!*\n\n"
            f"\U0001f464 *Captured by:* {_escape(family_name)}\n"
            f"\U0001f4c4 *File:* {_escape(file_name)}\n"
            f"\U0001f4c2 *Type:* {_escape(doc_type)}\n"
            "\U0001f3f7 *Tags:* " + (", ".join(f'`{_escape(t)}`' for t in metadata.get('tags', [])) or '_none_') + "\n"
            f"\U0001f194 *ID:* `{_escape(str(memory_id))}`"
            f"{key_summary}"
            f"{financial_note}"
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
# Content enrichment helper
# ---------------------------------------------------------------------------
def _enrich_content_with_key_fields(
    cleaned_content: str, key_fields: dict, doc_type: str
) -> str:
    """Enrich content summary with a structured block of key-value fields."""
    if not key_fields:
        return cleaned_content

    # Format key fields into a pipe-separated string
    details = " | ".join(
        "{}: {}".format(k.replace("_", " ").title(), v) for k, v in key_fields.items() if v
    )

    return "{}\n\nKey details for this {} document:\n{}".format(cleaned_content, doc_type, details)


# ---------------------------------------------------------------------------
# Financial details router: store to recurring_bills if applicable
# ---------------------------------------------------------------------------

# Document types that may carry financial / billing information
_FINANCIAL_DOC_TYPES = {"insurance", "utility", "invoice", "contract", "pension", "other"}

# key_fields key fragments that indicate a monetary amount
_AMOUNT_KEY_FRAGMENTS = ("amount", "premium", "payment", "cost", "price", "fee", "gbp", "£")


def _parse_amount(raw: str) -> Optional[float]:
    """Extract a float from a raw amount string such as '£12.50/month' or '12.50'."""
    if not raw:
        return None
    # Strip currency symbols, commas, and trailing text after the first number
    cleaned = re.sub(r"[£$€,]", "", str(raw))
    match = re.search(r"\d+(?:\.\d+)?", cleaned)
    if match:
        try:
            return float(match.group())
        except ValueError:
            return None
    return None


def _map_doc_type_to_category(doc_type: str, cleaned_content: str) -> str:
    """Map a document_type string to a valid recurring_bills category."""
    dt = doc_type.lower()
    if dt == "insurance":
        return "insurance"
    if dt == "pension":
        return "other"
    if dt == "utility":
        content_lower = cleaned_content.lower()
        if any(w in content_lower for w in ("broadband", "internet", "fibre", "bt ", "sky ", "virgin")):
            return "broadband"
        if any(w in content_lower for w in ("water", "sewage", "thames", "anglian", "severn")):
            return "water"
        # Default utility → energy
        return "energy"
    if dt in ("invoice", "contract"):
        return "other"
    return "other"


def _maybe_store_financial_details(
    doc_type: str,
    key_fields: dict[str, Any],
    cleaned_content: str,
    family_name: str,
) -> str:
    """Attempt to store financial details in the recurring_bills table.

    Returns a brief human-readable summary string if a bill was stored,
    or an empty string if nothing was stored.  Failures are caught and
    logged so they never interrupt the main photo/document flow.
    """
    try:
        # Only act on financially relevant document types
        if doc_type.lower() not in _FINANCIAL_DOC_TYPES:
            return ""

        # Locate an amount field in key_fields
        amount_raw: Optional[str] = None
        amount_key: Optional[str] = None
        for k, v in key_fields.items():
            if any(frag in k.lower() for frag in _AMOUNT_KEY_FRAGMENTS):
                amount_raw = str(v)
                amount_key = k
                break

        if amount_raw is None:
            # No monetary field found — nothing to store
            return ""

        amount_gbp = _parse_amount(amount_raw)
        if amount_gbp is None:
            return ""

        # --- Derive bill fields from key_fields ---

        # Provider
        provider = (
            key_fields.get("provider_name")
            or key_fields.get("provider")
            or key_fields.get("insurer")
            or key_fields.get("company")
            or ""
        )

        # Account / policy reference
        account_ref = (
            key_fields.get("policy_number")
            or key_fields.get("reference_number")
            or key_fields.get("account_number")
            or key_fields.get("account_ref")
            or ""
        )

        # Payment method — default to "direct debit" when banking details present
        has_bank_details = bool(
            key_fields.get("bank_account_number") or key_fields.get("sort_code")
        )
        payment_method = (
            key_fields.get("payment_method")
            or ("direct debit" if has_bank_details else "")
        )

        # Payment frequency
        frequency_raw = (
            key_fields.get("payment_frequency")
            or key_fields.get("frequency")
            or "monthly"
        )
        # Normalise to the allowed CHECK values
        freq_map = {
            "week": "weekly", "weekly": "weekly",
            "fortnight": "fortnightly", "fortnightly": "fortnightly", "bi-weekly": "fortnightly",
            "month": "monthly", "monthly": "monthly",
            "quarter": "quarterly", "quarterly": "quarterly",
            "year": "annually", "annual": "annually", "annually": "annually", "yearly": "annually",
        }
        frequency = freq_map.get(str(frequency_raw).lower().strip(), "monthly")

        # Bill name: prefer provider, else doc_type
        bill_name = str(provider).strip() if provider else doc_type.capitalize()

        # Category
        category = _map_doc_type_to_category(doc_type, cleaned_content)

        # Notes: include family member and any bank details for audit trail
        notes_parts = [f"Captured by {family_name} via Telegram"]
        if key_fields.get("bank_account_number"):
            notes_parts.append(f"Bank account: {key_fields['bank_account_number']}")
        if key_fields.get("sort_code"):
            notes_parts.append(f"Sort code: {key_fields['sort_code']}")
        notes = "; ".join(notes_parts)

        # Store the bill
        record = brain.add_recurring_bill(
            name=bill_name,
            category=category,
            amount_gbp=amount_gbp,
            frequency=frequency,
            provider=str(provider),
            account_ref=str(account_ref),
            payment_method=str(payment_method),
            auto_pay=has_bank_details,
            notes=notes,
        )
        bill_id = record.get("id", "n/a")
        logger.info(
            "Financial details stored in recurring_bills (id=%s, name=%s, amount=%.2f)",
            bill_id, bill_name, amount_gbp,
        )
        return f"💳 Recurring bill stored: {bill_name} £{amount_gbp:.2f}/{frequency} (id: {bill_id})"

    except Exception as exc:
        logger.warning("_maybe_store_financial_details failed (non-fatal): %s", exc)
        return ""


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

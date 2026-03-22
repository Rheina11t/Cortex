#!/usr/bin/env python3
"""
Family Brain – WhatsApp Capture Layer (via Twilio).

A Flask webhook server that listens for incoming Twilio WhatsApp messages and
routes them through the same brain pipeline as the Telegram capture layer.
Supports text messages, images (with OCR), and PDF documents.

Usage:
    python -m src.whatsapp_capture          # from the project root

Required environment variables (see .env.example):
    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM,
    SUPABASE_URL, SUPABASE_SERVICE_KEY,
    OPENAI_API_KEY

Optional:
    WHATSAPP_FAMILY_MEMBER_*_PHONE / WHATSAPP_FAMILY_MEMBER_*_NAME
        – authorised family members by WhatsApp number (e.g. "whatsapp:+447700900000")
    GOOGLE_VISION_API_KEY  – for Google Vision OCR (falls back to pytesseract)
    PORT                   – HTTP port to listen on (default: 8080)
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
from datetime import datetime, timedelta
import pytz
import hashlib
from functools import wraps
from typing import Any, Callable, Optional

import requests as http_requests
from flask import Flask, Response, request

try:
    from dateutil.rrule import rrulestr as _rrulestr
    _DATEUTIL_AVAILABLE = True
except ImportError:
    _rrulestr = None  # type: ignore
    _DATEUTIL_AVAILABLE = False

from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from .config import get_settings, logger as root_logger
from . import brain

logger = logging.getLogger("open_brain.whatsapp")

# ---------------------------------------------------------------------------
# Initialise settings and core brain module
# ---------------------------------------------------------------------------
settings = get_settings()
settings.validate_twilio()
brain.init(settings)

# ---------------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------------
_conversation_history: dict[str, list[dict]] = {}


# ---------------------------------------------------------------------------
# Family member registry
# ---------------------------------------------------------------------------
# Loaded from WHATSAPP_FAMILY_MEMBER_N_PHONE / WHATSAPP_FAMILY_MEMBER_N_NAME.
# Phone numbers must include the "whatsapp:" prefix, e.g. "whatsapp:+447700900000".
FAMILY_MEMBERS: dict[str, str] = {}

for _i in range(1, 20):
    _phone = os.getenv(f"WHATSAPP_FAMILY_MEMBER_{_i}_PHONE", "").strip()
    _name = os.getenv(f"WHATSAPP_FAMILY_MEMBER_{_i}_NAME", "").strip()
    if _phone and _name:
        FAMILY_MEMBERS[_phone] = _name

if FAMILY_MEMBERS:
    logger.info(
        "WhatsApp family members registered: %s",
        ", ".join(f"{name} ({phone})" for phone, name in FAMILY_MEMBERS.items()),
    )
else:
    logger.info("No WhatsApp family members configured — handler is open to all senders.")


# Cache for DB-based phone -> (family_name, family_id) lookups
_phone_cache: dict[str, tuple[str, str]] = {}


def log_action(family_id: str, action_type: str, subject: str, detail: Optional[dict] = None, phone_number: Optional[str] = None) -> None:
    """Log a significant action to the cortex_actions table."""
    try:
        db = brain._supabase
        if not db:
            return
        db.table("cortex_actions").insert({
            "family_id": family_id,
            "action_type": action_type,
            "subject": subject,
            "detail": detail or {},
            "phone_number": phone_number
        }).execute()
    except Exception as exc:
        logger.warning("Failed to log action %s for family %s: %s", action_type, family_id, exc)

def get_recent_actions(family_id: str, action_type: Optional[str] = None, hours: int = 24, subject_contains: Optional[str] = None) -> list[dict]:
    """Query cortex_actions for recent entries."""
    try:
        db = brain._supabase
        if not db:
            return []
        
        cutoff = datetime.now(pytz.UTC) - timedelta(hours=hours)
        query = db.table("cortex_actions").select("*").eq("family_id", family_id).gte("created_at", cutoff.isoformat())
        
        if action_type:
            query = query.eq("action_type", action_type)
            
        if subject_contains:
            query = query.ilike("subject", f"%{subject_contains}%")
            
        result = query.order("created_at", desc=True).execute()
        return result.data or []
    except Exception as exc:
        logger.warning("Failed to get recent actions for family %s: %s", family_id, exc)
        return []

def _lookup_phone_in_db(phone_number: str) -> Optional[tuple[str, str]]:
    """Look up a phone number in the whatsapp_members Supabase table.
    Returns (family_name, family_id) or None if not found.
    Caches results in memory to avoid repeated DB calls.
    """
    # Normalise: strip 'whatsapp:' prefix for DB lookup
    normalised = phone_number.replace("whatsapp:", "").strip()
    cache_key = normalised
    if cache_key in _phone_cache:
        return _phone_cache[cache_key]
    try:
        from supabase import create_client as _create_client
        _sb = _create_client(
            os.environ.get("SUPABASE_URL", ""),
            os.environ.get("SUPABASE_SERVICE_KEY", ""),
        )
        result = _sb.table("whatsapp_members").select("name,family_id").eq("phone", normalised).limit(1).execute()
        if result.data:
            row = result.data[0]
            name = row.get("name") or "Family Member"
            family_id = row.get("family_id") or "default"
            _phone_cache[cache_key] = (name, family_id)
            return (name, family_id)
    except Exception as exc:
        logger.warning("DB phone lookup failed for %s: %s", phone_number, exc)
    return None


def _get_family_name(phone_number: str) -> Optional[str]:
    """Return the family member name for a WhatsApp number, or None if not authorised.

    Checks env-var config first (for existing single-family deployments),
    then falls back to the whatsapp_members Supabase table (for multi-tenant).
    When no family members are configured at all, returns 'Unknown' (open mode).
    """
    # 1. Check env-var registry (backward compat for single-family deployments)
    if FAMILY_MEMBERS:
        return FAMILY_MEMBERS.get(phone_number)
    # 2. Check Supabase whatsapp_members table (multi-tenant)
    db_result = _lookup_phone_in_db(phone_number)
    if db_result:
        return db_result[0]
    # 3. Open mode — no auth configured
    return "Unknown"


def _get_family_id_for_phone(phone_number: str) -> str:
    """Return the family_id for a phone number.
    Falls back to settings.family_id for single-family deployments.
    """
    # Check Supabase first
    db_result = _lookup_phone_in_db(phone_number)
    if db_result:
        return db_result[1]
    # Fall back to env-var family_id
    return settings.family_id


# ---------------------------------------------------------------------------
# OCR backend selection (mirrors telegram_capture.py)
# ---------------------------------------------------------------------------
_GOOGLE_VISION_KEY = os.getenv("GOOGLE_VISION_API_KEY", "").strip()
_USE_GOOGLE_VISION = bool(_GOOGLE_VISION_KEY) and _GOOGLE_VISION_KEY != "your_key_here"

if _USE_GOOGLE_VISION:
    logger.info("OCR backend: Google Vision API")
else:
    logger.info("OCR backend: pytesseract (local fallback)")


def _ocr_google_vision(image_bytes: bytes) -> str:
    """Extract text from image bytes using Google Vision REST API."""
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "requests": [{
            "image": {"content": b64_image},
            "features": [{"type": "TEXT_DETECTION", "maxResults": 1}],
        }]
    }
    url = f"https://vision.googleapis.com/v1/images:annotate?key={_GOOGLE_VISION_KEY}"
    resp = http_requests.post(url, json=payload, timeout=30)
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
    """Extract text from image bytes using the best available OCR backend."""
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
        text_parts: list[str] = []
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
# Document type detection prompt (identical to telegram_capture.py)
# ---------------------------------------------------------------------------
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
  "source": "whatsapp-photo"
}}

Rules:
- CRITICAL: You MUST extract every labelled field you can see in the document text into key_fields. If a label like 'MOT test number', 'Location of the test', 'Testing organisation', or 'Inspector name' appears in the text, its value MUST appear in key_fields. Failure to include labelled fields is an error.
- Return ONLY valid JSON. No markdown fences, no commentary.
- key_fields should extract the most important structured data.
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
# Event detection helpers (mirrors telegram_capture.py)
# ---------------------------------------------------------------------------
def _get_event_detection_prompt() -> str:
    """Return the event detection prompt with today's date evaluated at call time."""
    return (
        "You are an event detection assistant for a Family Brain system.\n"
        "Given a message, determine if it contains a schedulable event (appointment, meeting, activity, deadline, etc.)\n"
        "Return a JSON object:\n"
        "{\n"
        '  "is_event": true/false,\n'
        '  "event_name": "<name of the event>",\n'
        '  "event_date": "<YYYY-MM-DD or null>",\n'
        '  "event_time": "<HH:MM or null — the START time>",\n'
        '  "end_time": "<HH:MM or null — the END time, e.g. from \'11:00-14:00\' extract \'14:00\'>",\n'
        '  "location": "<location or empty string>",\n'
        '  "requirements": ["<any requirements or things to bring>"],\n'
        '  "family_member": "<who this event is for, or \'family\' if shared>",\n'
        '  "is_recurring": true/false,\n'
        '  "recurrence_rule": "<one of: WEEKLY, BIWEEKLY, MONTHLY, WEEKDAYS, WEEKENDS, or null>",\n'
        '  "recurrence_day": "<day of week if weekly/biweekly, e.g. TUESDAY, or null>",\n'
        '  "recurrence_end": "<end date if mentioned (YYYY-MM-DD), or null if ongoing>",\n'
        '  "recurrence_count": <number of occurrences if mentioned, e.g. 6, or null>\n'
        "}\n"
        f"Today's date is {datetime.now().strftime('%Y-%m-%d')}.\n"
        "Rules:\n"
        "- Return ONLY valid JSON.\n"
        "- If the message does not contain a schedulable event, set is_event to false and leave other fields empty.\n"
        "- Parse relative dates like 'tomorrow', 'next Tuesday', 'this Friday' relative to today.\n"
        "- If a time range is given (e.g. '11:00-14:00' or 'from 11 to 2pm'), set event_time to the start and end_time to the end.\n"
        "- If no specific person is mentioned, default family_member to the sender's name.\n"
        "- If the message describes a recurring event, extract recurrence details:\n"
        "  - 'every Tuesday', 'every week on Monday' -> WEEKLY + day\n"
        "  - 'every other week', 'fortnightly' -> BIWEEKLY + day\n"
        "  - 'every month', 'monthly' -> MONTHLY\n"
        "  - 'every weekday', 'Monday to Friday' -> WEEKDAYS\n"
        "  - 'every weekend' -> WEEKENDS\n"
        "  - 'until [date]', 'until July' -> recurrence_end\n"
        "  - 'for 6 weeks' -> recurrence_count\n"
    )


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
                {"role": "system", "content": _get_event_detection_prompt()},
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
    family_id: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Store an event and check for conflicts. Returns (event_id, conflict_warning)."""
    try:
        db, _ = brain._require_init()

        event_date = event_data.get("event_date")
        event_time = event_data.get("event_time")
        family_member = event_data.get("family_member", sender_name)
        event_name = event_data.get("event_name", "Untitled event")

        # Check for conflicts — direct date-based query (no RPC needed)
        conflict_msg = None
        try:
            conflicts_result = db.table("family_events").select(
                "event_name, event_time, family_member"
            ).eq("event_date", event_date).execute()

            if conflicts_result.data:
                conflict_lines = []
                for c in conflicts_result.data:
                    time_str = c.get("event_time", "")
                    time_display = f" at {time_str}" if time_str else ""
                    conflict_lines.append(
                        f"  • {c.get('event_name', 'Unknown event')}{time_display} "
                        f"({c.get('family_member', 'someone')})"
                    )
                conflict_msg = (
                    f"⚠️ Heads up — this clashes with existing events on {event_date}:\n"
                    + "\n".join(conflict_lines)
                )
        except Exception as exc:
            logger.warning("Conflict check failed (table may not exist yet): %s", exc)

        end_time = event_data.get("end_time") or None

        # Store the event — only columns that actually exist in the live table
        # NOTE: 'title' is a required NOT NULL column — set it to event_name
        row = {
            "title": event_name,
            "family_member": family_member,
            "event_name": event_name,
            "event_date": event_date,
            "event_time": event_time if event_time else None,
            "end_time": end_time,
            "location": event_data.get("location", ""),
            "recurring": "none",
            "notes": "",
            "source": "whatsapp",
        }

        is_recurring = event_data.get("is_recurring", False)
        if is_recurring:
            row["is_recurring"] = True
            row["recurrence_rule"] = event_data.get("recurrence_rule")
            row["recurrence_end"] = event_data.get("recurrence_end")

        try:
            # Also push to Google Calendar first to get the ID
            gcal_event_id = None
            try:
                from . import google_calendar
                if is_recurring:
                    start_dt_str = f"{event_date}T{event_time if event_time else '00:00'}:00"
                    gcal_event_id = google_calendar.create_recurring_event(
                        family_id=family_id,
                        title=event_name,
                        start_datetime=start_dt_str,
                        recurrence_rule=event_data.get("recurrence_rule", "WEEKLY"),
                        recurrence_day=event_data.get("recurrence_day"),
                        recurrence_end=event_data.get("recurrence_end"),
                        recurrence_count=event_data.get("recurrence_count"),
                        family_member=family_member,
                    )
                else:
                    gcal_event_id = google_calendar.create_event(
                        event_name=event_name,
                        event_date=event_date,
                        event_time=event_time if event_time else None,
                        end_time=end_time,
                        location=event_data.get("location", ""),
                        description=f"Captured by {sender_name} via Family Brain",
                        family_member=family_member,
                        family_id=family_id,
                    )
                
                if gcal_event_id:
                    logger.info("Event pushed to Google Calendar: %s", gcal_event_id)
                    row["google_event_id"] = gcal_event_id
                    # Pre-mark so the poll loop doesn't re-notify the sender about their own event
                    _gcal_wa_pushed_event_ids.add(gcal_event_id)
                    _gcal_notified_event_ids.add(gcal_event_id)
            except Exception as exc:
                logger.warning("Google Calendar push failed: %s", exc)

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
# Document metadata extraction via LLM (mirrors telegram_capture.py)
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
        if doc_type.lower() not in _FINANCIAL_DOC_TYPES:
            return ""

        # Locate an amount field in key_fields
        amount_raw: Optional[str] = None
        for k, v in key_fields.items():
            if any(frag in k.lower() for frag in _AMOUNT_KEY_FRAGMENTS):
                amount_raw = str(v)
                break

        if amount_raw is None:
            return ""

        amount_gbp = _parse_amount(amount_raw)
        if amount_gbp is None:
            return ""

        # --- Derive bill fields from key_fields ---
        provider = (
            key_fields.get("provider_name")
            or key_fields.get("provider")
            or key_fields.get("insurer")
            or key_fields.get("company")
            or ""
        )

        account_ref = (
            key_fields.get("policy_number")
            or key_fields.get("reference_number")
            or key_fields.get("account_number")
            or key_fields.get("account_ref")
            or ""
        )

        has_bank_details = bool(
            key_fields.get("bank_account_number") or key_fields.get("sort_code")
        )
        payment_method = (
            key_fields.get("payment_method")
            or ("direct debit" if has_bank_details else "")
        )

        frequency_raw = (
            key_fields.get("payment_frequency")
            or key_fields.get("frequency")
            or "monthly"
        )
        freq_map = {
            "week": "weekly", "weekly": "weekly",
            "fortnight": "fortnightly", "fortnightly": "fortnightly", "bi-weekly": "fortnightly",
            "month": "monthly", "monthly": "monthly",
            "quarter": "quarterly", "quarterly": "quarterly",
            "year": "annually", "annual": "annually", "annually": "annually", "yearly": "annually",
        }
        frequency = freq_map.get(str(frequency_raw).lower().strip(), "monthly")

        bill_name = str(provider).strip() if provider else doc_type.capitalize()
        category = _map_doc_type_to_category(doc_type, cleaned_content)

        notes_parts = [f"Captured by {family_name} via WhatsApp"]
        if key_fields.get("bank_account_number"):
            notes_parts.append(f"Bank account: {key_fields['bank_account_number']}")
        if key_fields.get("sort_code"):
            notes_parts.append(f"Sort code: {key_fields['sort_code']}")
        notes = "; ".join(notes_parts)

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
# Emergency category mapping
# ---------------------------------------------------------------------------

# Map document_type strings to emergency category numbers (1-10)
_DOC_TYPE_TO_EMERGENCY_CATEGORY: dict[str, str] = {
    # Category 1 — Legal & Personal Documents
    "will": "1",
    "lpa": "1",
    "power_of_attorney": "1",
    "passport": "1",
    "birth_certificate": "1",
    "marriage_certificate": "1",
    "legal": "1",
    "government_letter": "1",
    "hmrc_letter": "1",
    # Category 2 — Financial Accounts & Access
    "bank_statement": "2",
    "bank": "2",
    "tax": "2",
    "payslip": "2",
    # Category 3 — Insurance Policies
    "insurance": "3",
    # Category 4 — Pensions & Investments
    "pension": "4",
    "pension_statement": "4",
    "investment": "4",
    "isa": "4",
    # Category 5 — Bills, Debts & Regular Payments
    "mortgage": "5",
    "utility": "5",
    "utility_bill": "5",
    "subscription": "5",
    "invoice": "5",
    # Category 6 — Assets & Possessions
    "property": "6",
    "vehicle": "6",
    "mot_certificate": "6",
    "vehicle_finance": "6",
    "contract_hire": "6",
    "finance_agreement": "6",
    # Category 7 — Contacts & Professionals
    "contract": "7",
    # Category 10 — Emergency Contacts & Family Details
    "medical": "10",
    "nhs": "10",
    "prescription": "10",
    "health": "10",
    "warranty": "10",
}

# Category number to name mapping
_EMERGENCY_CATEGORY_NAMES: dict[str, str] = {
    "1": "Legal Docs",
    "2": "Bank/Finance",
    "3": "Insurance",
    "4": "Pensions",
    "5": "Bills/Debts",
    "6": "Assets/Car",
    "7": "Contacts",
    "8": "Funeral Wishes",
    "9": "Digital Legacy",
    "10": "Family/Medical",
}


def _map_doc_type_to_emergency_category(doc_type: str, content: str = "") -> Optional[str]:
    """Map a document_type string to an emergency category number (1-10), or None."""
    dt = doc_type.lower().strip()
    if dt in _DOC_TYPE_TO_EMERGENCY_CATEGORY:
        return _DOC_TYPE_TO_EMERGENCY_CATEGORY[dt]
    # Fuzzy match on content keywords
    content_lower = content.lower()
    if any(w in content_lower for w in ("will ", "lasting power", "lpa", "probate")):
        return "1"
    if any(w in content_lower for w in ("bank account", "sort code", "account number")):
        return "2"
    if any(w in content_lower for w in ("insurance", "policy number", "insurer", "premium")):
        return "3"
    if any(w in content_lower for w in ("pension", "isa", "investment", "annuity")):
        return "4"
    if any(w in content_lower for w in ("mortgage", "direct debit", "utility", "broadband", "subscription")):
        return "5"
    if any(w in content_lower for w in ("property", "v5", "mot", "vehicle", "car reg")):
        return "6"
    if any(w in content_lower for w in ("solicitor", "accountant", "gp", "dentist", "school", "executor")):
        return "7"
    if any(w in content_lower for w in ("funeral", "cremation", "burial", "organ donation")):
        return "8"
    if any(w in content_lower for w in ("social media", "crypto", "digital legacy", "password manager")):
        return "9"
    if any(w in content_lower for w in ("nhs", "allerg", "medication", "blood type", "medical")):
        return "10"
    return None


# Per-phone state: track last stored memory_id for category update, and doc count
_last_stored_memory: dict[str, str] = {}   # phone -> memory_id
_doc_count: dict[str, int] = {}            # phone -> total docs stored this session
# Per-phone pending category prompt: {phone: True} means we're waiting for a 1-10 reply
_pending_category_prompt: dict[str, bool] = {}


# ---------------------------------------------------------------------------
# Twilio request validation decorator
# ---------------------------------------------------------------------------
def _validate_twilio_request(f: Callable) -> Callable:
    """Decorator that validates every request is genuinely from Twilio.

    Uses the TWILIO_AUTH_TOKEN to verify the X-Twilio-Signature header.
    Returns HTTP 403 if the signature is invalid.
    """
    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        auth_token = settings.twilio_auth_token
        if not auth_token:
            # If no auth token is configured, skip validation (dev/test mode)
            logger.warning(
                "TWILIO_AUTH_TOKEN not set — skipping Twilio signature validation. "
                "Set this in production!"
            )
            return f(*args, **kwargs)

        # Allow bypassing signature validation for sandbox/testing
        skip_validation = os.environ.get("TWILIO_SKIP_SIGNATURE_VALIDATION", "").lower() in ("1", "true", "yes")
        if skip_validation:
            logger.warning("Twilio signature validation SKIPPED (TWILIO_SKIP_SIGNATURE_VALIDATION=true) — disable in production!")
            return f(*args, **kwargs)

        validator = RequestValidator(auth_token)
        # Railway sits behind a reverse proxy that terminates TLS.
        # request.url will be http:// but Twilio signs against the https:// URL.
        # Try both https and the forwarded proto header.
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "https")
        url = request.url.replace("http://", f"{forwarded_proto}://", 1)
        post_vars = request.form.to_dict()
        signature = request.headers.get("X-Twilio-Signature", "")
        if not validator.validate(url, post_vars, signature):
            logger.warning(
                "Rejected request with invalid Twilio signature from %s",
                request.remote_addr,
            )
            return Response("Forbidden", status=403)
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/whatsapp/health", methods=["GET"])
def health_check() -> dict[str, str]:
    """Health check endpoint — returns {"status": "ok"}."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Kitchen Calendar — helper and routes
# ---------------------------------------------------------------------------

# Colour map for family members (CSS colour values)
_MEMBER_COLOURS: dict[str, str] = {
    "dan": "#2563EB",      # blue
    "emma": "#A855F7",    # pink/purple
    "izzy": "#16A34A",    # green
    "edi": "#EA580C",     # orange
    "family": "#6B7280",  # grey
}
_DEFAULT_COLOUR = "#6B7280"


def _get_or_create_calendar_token(family_id: str) -> Optional[str]:
    """Return the calendar_token for a family, generating one if absent."""
    import secrets as _secrets
    db = brain._supabase
    if not db:
        logger.error("calendar_token: no DB connection")
        return None
    try:
        result = db.table("families").select("calendar_token").eq("family_id", family_id).limit(1).execute()
        if result.data:
            existing_token = result.data[0].get("calendar_token")
            if existing_token:
                return existing_token
            # Generate a new token and save it
            new_token = _secrets.token_urlsafe(16)
            db.table("families").update({"calendar_token": new_token}).eq("family_id", family_id).execute()
            logger.info("Generated calendar_token for family_id=%s", family_id)
            return new_token
        else:
            # Family row doesn't exist yet — insert a minimal row
            new_token = _secrets.token_urlsafe(16)
            db.table("families").insert({
                "family_id": family_id,
                "calendar_token": new_token,
                "primary_name": family_id,
                "primary_phone": "",
                "plan": "free",
                "status": "active",
            }).execute()
            logger.info("Inserted families row with calendar_token for family_id=%s", family_id)
            return new_token
    except Exception as exc:
        logger.error("_get_or_create_calendar_token failed: %s", exc)
        return None


def _build_calendar_events_json(family_id: str) -> str:
    """Fetch family_events for current + next month and return a JSON array string."""
    import json as _json
    from datetime import date as _date, timedelta as _timedelta

    db = brain._supabase
    if not db:
        return "[]"

    today = _date.today()
    # Start of current month
    range_start = today.replace(day=1)
    # End of next month
    if today.month == 12:
        range_end = _date(today.year + 1, 2, 1) - _timedelta(days=1)
    else:
        next_month = today.month + 1
        year = today.year
        if next_month > 12:
            next_month = 1
            year += 1
        # End of the month after next
        if next_month == 12:
            range_end = _date(year + 1, 1, 1) - _timedelta(days=1)
        else:
            range_end = _date(year, next_month + 1, 1) - _timedelta(days=1)

    try:
        # We need to fetch all recurring events regardless of start date,
        # plus non-recurring events in the current range.
        result = db.table("family_events") \
            .select("id,title,event_name,event_date,event_time,end_date,end_time,family_member,notes,source,is_recurring,recurrence_rule,recurrence_end") \
            .eq("family_id", family_id) \
            .execute()
    except Exception as exc:
        logger.error("_build_calendar_events_json query failed: %s", exc)
        return "[]"

    events = []

    try:
        from datetime import datetime as _datetime
        london_tz = pytz.timezone("Europe/London")
        range_start_dt = london_tz.localize(_datetime.combine(range_start, _datetime.min.time()))
        range_end_dt = london_tz.localize(_datetime.combine(range_end, _datetime.max.time()))
    except Exception as _tz_exc:
        logger.error("_build_calendar_events_json: timezone setup failed: %s", _tz_exc)
        return "[]"

    for row in (result.data or []):
        member_raw = (row.get("family_member") or "").strip()
        member_lower = member_raw.lower()
        colour = _MEMBER_COLOURS.get(member_lower, _DEFAULT_COLOUR)

        # Prefer title, fall back to event_name
        raw_title = (row.get("title") or row.get("event_name") or "Event").strip()
        # Append member name in parentheses if set and not already in title
        if member_raw and member_raw.lower() not in ("family", "") and member_raw.lower() not in raw_title.lower():
            display_title = f"{raw_title} ({member_raw})"
        else:
            display_title = raw_title

        event_date = row.get("event_date", "")
        if not event_date:
            continue
            
        event_time = (row.get("event_time") or "").strip()
        end_date = row.get("end_date") or ""
        end_time = (row.get("end_time") or "").strip()
        
        is_recurring = bool(row.get("is_recurring", False))

        if not is_recurring:
            try:
                # Check if it falls in our range
                ev_date_obj = _date.fromisoformat(event_date)
                if not (range_start <= ev_date_obj <= range_end):
                    continue

                # Build ISO start
                if event_time:
                    start_iso = f"{event_date}T{event_time}:00"
                else:
                    start_iso = event_date

                # Build ISO end
                end_iso = ""
                if end_date and end_time:
                    end_iso = f"{end_date}T{end_time}:00"
                elif end_date:
                    end_iso = end_date
                elif event_time and end_time:
                    end_iso = f"{event_date}T{end_time}:00"

                ev: dict[str, Any] = {
                    "id": str(row.get("id", "")),
                    "title": display_title,
                    "start": start_iso,
                    "color": colour,
                    "extendedProps": {
                        "member": member_raw,
                        "notes": row.get("notes") or "",
                    },
                }
                if end_iso:
                    ev["end"] = end_iso

                events.append(ev)
            except Exception as _row_exc:
                logger.warning("Skipping malformed event row %s: %s", row.get('id'), _row_exc)
        else:
            # Handle recurring event expansion
            if not _DATEUTIL_AVAILABLE:
                # dateutil not installed — fall back to showing the base event date
                logger.warning("dateutil unavailable; showing recurring event %s as single occurrence", row.get('id'))
                if event_time:
                    start_iso = f"{event_date}T{event_time}:00"
                else:
                    start_iso = event_date
                ev: dict[str, Any] = {
                    "id": str(row.get("id", "")),
                    "title": display_title,
                    "start": start_iso,
                    "color": colour,
                    "extendedProps": {
                        "member": member_raw,
                        "notes": row.get("notes") or "",
                    },
                }
                events.append(ev)
                continue

            rule_str = row.get("recurrence_rule")
            if not rule_str:
                # No rule stored — show as single occurrence on its base date
                try:
                    ev_date_obj = _date.fromisoformat(event_date)
                    if range_start <= ev_date_obj <= range_end:
                        start_iso = f"{event_date}T{event_time}:00" if event_time else event_date
                        ev = {
                            "id": str(row.get("id", "")),
                            "title": display_title,
                            "start": start_iso,
                            "color": colour,
                            "extendedProps": {"member": member_raw, "notes": row.get("notes") or ""},
                        }
                        events.append(ev)
                except Exception:
                    pass
                continue

            try:
                # Build RRULE string for dateutil
                rrule_parts = []
                rule_upper = rule_str.upper()
                if rule_upper == "WEEKLY":
                    rrule_parts.append("FREQ=WEEKLY")
                elif rule_upper == "BIWEEKLY":
                    rrule_parts.append("FREQ=WEEKLY;INTERVAL=2")
                elif rule_upper == "MONTHLY":
                    rrule_parts.append("FREQ=MONTHLY")
                elif rule_upper == "WEEKDAYS":
                    rrule_parts.append("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR")
                elif rule_upper == "WEEKENDS":
                    rrule_parts.append("FREQ=WEEKLY;BYDAY=SA,SU")
                else:
                    # Try to parse as raw RRULE if it doesn't match our simple types
                    if rule_str.startswith("RRULE:"):
                        rrule_parts.append(rule_str[6:])
                    else:
                        rrule_parts.append(rule_str)

                rec_end = row.get("recurrence_end")
                if rec_end:
                    end_str = str(rec_end).replace("-", "")
                    rrule_parts.append(f"UNTIL={end_str}T235959Z")

                rrule_string = f"RRULE:{';'.join(rrule_parts)}"

                # Parse start datetime
                if event_time:
                    dt_start = _datetime.fromisoformat(f"{event_date}T{event_time}")
                else:
                    dt_start = _datetime.fromisoformat(f"{event_date}T00:00:00")

                dt_start = london_tz.localize(dt_start)

                # Generate occurrences within our display range
                rule = _rrulestr(rrule_string, dtstart=dt_start)
                occurrences = rule.between(
                    range_start_dt - _timedelta(days=1),
                    range_end_dt + _timedelta(days=1),
                    inc=True
                )

                for i, occ in enumerate(occurrences):
                    occ_date = occ.strftime("%Y-%m-%d")

                    if event_time:
                        start_iso = f"{occ_date}T{event_time}:00"
                    else:
                        start_iso = occ_date

                    end_iso = ""
                    if end_time:
                        end_iso = f"{occ_date}T{end_time}:00"

                    ev = {
                        "id": f"{row.get('id', '')}_{i}",
                        "title": display_title,
                        "start": start_iso,
                        "color": colour,
                        "extendedProps": {
                            "member": member_raw,
                            "notes": row.get("notes") or "",
                        },
                    }
                    if end_iso:
                        ev["end"] = end_iso

                    events.append(ev)
            except Exception as e:
                logger.warning("Failed to expand recurring event %s: %s", row.get('id'), e)

    return _json.dumps(events, ensure_ascii=False)


def _render_calendar_page(family_id: str, calendar_token: str) -> str:
    """Return a self-contained HTML page for the kitchen calendar."""
    events_json = _build_calendar_events_json(family_id)
    base_url = os.environ.get("FAMILYBRAIN_BASE_URL", "https://cortex-production-eb84.up.railway.app").rstrip("/")
    calendar_url = f"{base_url}/calendar/{calendar_token}"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Family Calendar</title>
  <script src="https://cdn.jsdelivr.net/npm/fullcalendar@6.1.11/index.global.min.js"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      background: #f0f4f8;
      color: #1a202c;
      min-height: 100vh;
    }}

    #header {{
      background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%);
      color: white;
      padding: 16px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      box-shadow: 0 2px 8px rgba(0,0,0,0.2);
    }}

    #header h1 {{
      font-size: 1.5rem;
      font-weight: 700;
      letter-spacing: -0.5px;
    }}

    #header .subtitle {{
      font-size: 0.8rem;
      opacity: 0.8;
      margin-top: 2px;
    }}

    #legend {{
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      padding: 12px 20px;
      background: white;
      border-bottom: 1px solid #e2e8f0;
    }}

    .legend-item {{
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 0.82rem;
      font-weight: 500;
    }}

    .legend-dot {{
      width: 12px;
      height: 12px;
      border-radius: 50%;
      flex-shrink: 0;
    }}

    #calendar-container {{
      padding: 16px;
      max-width: 1200px;
      margin: 0 auto;
    }}

    /* FullCalendar overrides */
    .fc {{
      background: white;
      border-radius: 12px;
      box-shadow: 0 1px 6px rgba(0,0,0,0.08);
      overflow: hidden;
    }}

    .fc .fc-toolbar {{
      padding: 14px 16px;
      background: #f8fafc;
      border-bottom: 1px solid #e2e8f0;
    }}

    .fc .fc-toolbar-title {{
      font-size: 1.2rem;
      font-weight: 700;
      color: #1e3a5f;
    }}

    .fc .fc-button {{
      background: #2563eb !important;
      border-color: #2563eb !important;
      border-radius: 8px !important;
      font-weight: 600 !important;
      padding: 6px 14px !important;
      font-size: 0.85rem !important;
    }}

    .fc .fc-button:hover {{
      background: #1d4ed8 !important;
      border-color: #1d4ed8 !important;
    }}

    .fc .fc-button-active {{
      background: #1e40af !important;
      border-color: #1e40af !important;
    }}

    .fc .fc-col-header-cell {{
      background: #f1f5f9;
      font-weight: 700;
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: #475569;
      padding: 8px 0;
    }}

    .fc .fc-daygrid-day-number {{
      font-size: 0.9rem;
      font-weight: 600;
      color: #374151;
      padding: 4px 8px;
    }}

    .fc .fc-day-today {{
      background: #eff6ff !important;
    }}

    .fc .fc-day-today .fc-daygrid-day-number {{
      background: #2563eb;
      color: white;
      border-radius: 50%;
      width: 28px;
      height: 28px;
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 4px;
    }}

    .fc .fc-event {{
      border-radius: 5px !important;
      border: none !important;
      padding: 2px 5px !important;
      font-size: 0.78rem !important;
      font-weight: 500 !important;
      cursor: default !important;
      margin-bottom: 2px !important;
    }}

    .fc .fc-event-title {{
      font-weight: 500;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .fc .fc-daygrid-day-frame {{
      min-height: 80px;
    }}

    /* Tooltip */
    #tooltip {{
      position: fixed;
      background: rgba(15, 23, 42, 0.92);
      color: white;
      padding: 10px 14px;
      border-radius: 8px;
      font-size: 0.82rem;
      max-width: 240px;
      pointer-events: none;
      z-index: 9999;
      display: none;
      line-height: 1.5;
      box-shadow: 0 4px 12px rgba(0,0,0,0.3);
    }}

    /* Mobile tweaks */
    @media (max-width: 640px) {{
      #header h1 {{ font-size: 1.2rem; }}
      #calendar-container {{ padding: 8px; }}
      .fc .fc-toolbar {{ flex-direction: column; gap: 8px; }}
      .fc .fc-toolbar-title {{ font-size: 1rem; }}
      .fc .fc-daygrid-day-frame {{ min-height: 60px; }}
      .fc .fc-event {{ font-size: 0.7rem !important; }}
    }}
  </style>
</head>
<body>
  <div id="header">
    <div>
      <h1>&#128197; Family Calendar</h1>
      <div class="subtitle">Auto-refreshes every 5 minutes</div>
    </div>
  </div>

  <div id="legend">
    <div class="legend-item">
      <div class="legend-dot" style="background:#2563EB"></div><span>Dan</span>
    </div>
    <div class="legend-item">
      <div class="legend-dot" style="background:#A855F7"></div><span>Emma</span>
    </div>
    <div class="legend-item">
      <div class="legend-dot" style="background:#16A34A"></div><span>Izzy</span>
    </div>
    <div class="legend-item">
      <div class="legend-dot" style="background:#EA580C"></div><span>Edi</span>
    </div>
    <div class="legend-item">
      <div class="legend-dot" style="background:#6B7280"></div><span>Family</span>
    </div>
  </div>

  <div id="calendar-container">
    <div id="calendar"></div>
  </div>

  <div id="tooltip"></div>

  <script>
    var EVENTS = {events_json};

    document.addEventListener('DOMContentLoaded', function () {{
      var calendarEl = document.getElementById('calendar');
      var tooltip = document.getElementById('tooltip');

      var calendar = new FullCalendar.Calendar(calendarEl, {{
        initialView: 'dayGridMonth',
        headerToolbar: {{
          left: 'prev,next today',
          center: 'title',
          right: ''
        }},
        events: EVENTS,
        height: 'auto',
        firstDay: 1,
        eventDisplay: 'block',
        dayMaxEvents: 4,
        eventTimeFormat: {{ hour: '2-digit', minute: '2-digit', hour12: false }},
        eventMouseEnter: function(info) {{
          var props = info.event.extendedProps;
          var lines = [];
          lines.push('<strong>' + info.event.title + '</strong>');
          if (info.event.start) {{
            var d = info.event.start;
            var timeStr = d.getHours() || d.getMinutes()
              ? d.toLocaleTimeString([], {{hour:'2-digit', minute:'2-digit'}})
              : '';
            if (timeStr) lines.push('\u23f0 ' + timeStr);
          }}
          if (props.member) lines.push('Who: ' + props.member);
          if (props.notes) lines.push('Notes: ' + props.notes);
          tooltip.innerHTML = lines.join('<br>');
          tooltip.style.display = 'block';
        }},
        eventMouseLeave: function() {{
          tooltip.style.display = 'none';
        }},
        eventDidMount: function(info) {{
          // Make sure colour is applied
          if (info.event.backgroundColor) {{
            info.el.style.backgroundColor = info.event.backgroundColor;
          }}
        }}
      }});

      calendar.render();

      // Move tooltip with mouse
      document.addEventListener('mousemove', function(e) {{
        tooltip.style.left = (e.clientX + 14) + 'px';
        tooltip.style.top = (e.clientY + 14) + 'px';
      }});
    }});

    // Auto-refresh every 5 minutes
    setTimeout(function() {{ location.reload(); }}, 5 * 60 * 1000);
  </script>
</body>
</html>"""
    return html


@app.route("/calendar/<family_token>", methods=["GET"])
def kitchen_calendar(family_token: str) -> Response:
    """Read-only kitchen calendar page — no login required.

    Looks up the family by calendar_token, fetches their events for the
    current and next month, and returns a self-contained FullCalendar HTML page.
    """
    db = brain._supabase
    if not db:
        logger.error("kitchen_calendar: no DB connection")
        return Response("<h1>Service unavailable</h1>", status=503, mimetype="text/html")

    try:
        result = db.table("families").select("family_id").eq("calendar_token", family_token).limit(1).execute()
        if not result.data:
            logger.warning("kitchen_calendar: unknown token %s", family_token)
            return Response(
                "<h1>Calendar not found</h1><p>This link is invalid or has expired.</p>",
                status=404,
                mimetype="text/html",
            )
        family_id = result.data[0]["family_id"]
    except Exception as exc:
        logger.error("kitchen_calendar: DB lookup failed: %s", exc)
        return Response("<h1>Error</h1><p>Could not load calendar.</p>", status=500, mimetype="text/html")

    try:
        html = _render_calendar_page(family_id, family_token)
        return Response(html, status=200, mimetype="text/html")
    except Exception as exc:
        logger.error("kitchen_calendar: render failed: %s", exc)
        return Response("<h1>Error</h1><p>Could not render calendar.</p>", status=500, mimetype="text/html")


# ---------------------------------------------------------------------------
# Google Calendar OAuth Routes
# ---------------------------------------------------------------------------

@app.route("/calendar-debug/<family_token>", methods=["GET"])
def kitchen_calendar_debug(family_token: str) -> Response:
    """Debug endpoint to expose render errors."""
    import traceback as _tb
    db = brain._supabase
    if not db:
        return Response("No DB connection", status=503, mimetype="text/plain")
    try:
        result = db.table("families").select("family_id").eq("calendar_token", family_token).limit(1).execute()
        if not result.data:
            return Response(f"Token not found: {family_token}", status=404, mimetype="text/plain")
        family_id = result.data[0]["family_id"]
        events_json = _build_calendar_events_json(family_id)
        # Now try the full render - exactly like kitchen_calendar does
        try:
            html = _render_calendar_page(family_id, family_token)
            # Return the actual HTML to see if the Response() call works
            return Response(html, status=200, mimetype="text/html")
        except Exception as render_exc:
            return Response(f"RENDER ERROR: {render_exc}\n\n{_tb.format_exc()}\n\nevents_json={events_json[:300]}", status=500, mimetype="text/plain")
    except Exception as exc:
        return Response(f"ERROR: {exc}\n\n{_tb.format_exc()}", status=500, mimetype="text/plain")


# ---------------------------------------------------------------------------
# iCal / webcal Feed Endpoint
# ---------------------------------------------------------------------------

@app.route("/calendar/feed/<family_token>.ics", methods=["GET"])
def ical_feed(family_token: str) -> Response:
    """Serve the family's events as a standards-compliant iCalendar (.ics) file.

    Auth-free: the ``family_token`` (stored in ``families.calendar_token``) acts
    as the shared secret.  The same token is already used by the kitchen-calendar
    HTML page, so no new DB columns are required.

    Supports:
    - One-off events (VEVENT with DTSTART / DTEND)
    - Recurring events (RRULE expansion up to 2 years ahead)
    - All-day events (DATE value type) and timed events (DATE-TIME)

    The ``webcal://`` scheme is simply ``https://`` with the protocol swapped;
    iOS Safari / Apple Calendar intercepts ``webcal://`` links and offers a
    one-tap "Subscribe" prompt, giving iPhone users instant read-only access
    without any Settings navigation.
    """
    import uuid as _uuid
    from datetime import date as _date, datetime as _datetime, timedelta as _timedelta

    db = brain._supabase
    if not db:
        logger.error("ical_feed: no DB connection")
        return Response("Service unavailable", status=503, mimetype="text/plain")

    # ---- 1. Resolve family_id from token --------------------------------
    try:
        fam_res = db.table("families").select("family_id").eq("calendar_token", family_token).limit(1).execute()
        if not fam_res.data:
            logger.warning("ical_feed: unknown token %s", family_token)
            return Response("Calendar not found.", status=404, mimetype="text/plain")
        family_id: str = fam_res.data[0]["family_id"]
    except Exception as exc:
        logger.error("ical_feed: DB lookup failed: %s", exc)
        return Response("Error loading calendar.", status=500, mimetype="text/plain")

    # ---- 2. Fetch events ------------------------------------------------
    try:
        ev_res = db.table("family_events") \
            .select("id,title,event_name,event_date,event_time,end_date,end_time,"
                    "family_member,notes,location,is_recurring,recurrence_rule,recurrence_end") \
            .eq("family_id", family_id) \
            .execute()
        rows = ev_res.data or []
    except Exception as exc:
        logger.error("ical_feed: events query failed: %s", exc)
        return Response("Error loading events.", status=500, mimetype="text/plain")

    # ---- 3. Build iCalendar text ----------------------------------------
    london_tz = pytz.timezone("Europe/London")
    now_utc = datetime.now(pytz.utc)
    dtstamp = now_utc.strftime("%Y%m%dT%H%M%SZ")

    # Horizon for recurring-event expansion: 2 years from today
    expand_until = _date.today().replace(year=_date.today().year + 2)

    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//FamilyBrain//FamilyBrain Calendar//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Family Calendar",
        "X-WR-TIMEZONE:Europe/London",
        "X-WR-CALDESC:Your FamilyBrain family calendar",
        # VTIMEZONE block for Europe/London (abbreviated; clients fall back gracefully)
        "BEGIN:VTIMEZONE",
        "TZID:Europe/London",
        "BEGIN:STANDARD",
        "DTSTART:19701025T020000",
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=10",
        "TZOFFSETFROM:+0100",
        "TZOFFSETTO:+0000",
        "TZNAME:GMT",
        "END:STANDARD",
        "BEGIN:DAYLIGHT",
        "DTSTART:19700329T010000",
        "RRULE:FREQ=YEARLY;BYDAY=-1SU;BYMONTH=3",
        "TZOFFSETFROM:+0000",
        "TZOFFSETTO:+0100",
        "TZNAME:BST",
        "END:DAYLIGHT",
        "END:VTIMEZONE",
    ]

    def _fold(line: str) -> str:
        """RFC 5545 line folding: max 75 octets per line, continuation with a space."""
        encoded = line.encode("utf-8")
        if len(encoded) <= 75:
            return line
        result_parts: list[str] = []
        while len(encoded) > 75:
            # Find safe split point (avoid splitting multi-byte chars)
            split = 75
            while split > 0 and (encoded[split] & 0xC0) == 0x80:
                split -= 1
            result_parts.append(encoded[:split].decode("utf-8"))
            encoded = b" " + encoded[split:]
        result_parts.append(encoded.decode("utf-8"))
        return "\r\n".join(result_parts)

    def _ical_escape(text: str) -> str:
        """Escape special characters per RFC 5545."""
        return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    def _emit_vevent(
        uid: str,
        summary: str,
        dtstart_date: _date,
        dtstart_time: str,
        dtend_date: _date,
        dtend_time: str,
        description: str,
        location_str: str,
        rrule_str: str = "",
        recurrence_end: _date | None = None,
    ) -> list[str]:
        """Build VEVENT lines for one event (or recurring series)."""
        ev_lines: list[str] = ["BEGIN:VEVENT"]
        ev_lines.append(f"UID:{uid}")
        ev_lines.append(f"DTSTAMP:{dtstamp}")

        if dtstart_time:
            # Timed event
            try:
                dt = _datetime.fromisoformat(f"{dtstart_date}T{dtstart_time}")
                dt_london = london_tz.localize(dt)
                ev_lines.append(f"DTSTART;TZID=Europe/London:{dt_london.strftime('%Y%m%dT%H%M%S')}")
            except Exception:
                ev_lines.append(f"DTSTART;VALUE=DATE:{dtstart_date.strftime('%Y%m%d')}")
        else:
            # All-day event
            ev_lines.append(f"DTSTART;VALUE=DATE:{dtstart_date.strftime('%Y%m%d')}")

        if dtend_time and dtstart_time:
            try:
                end_d = dtend_date if dtend_date else dtstart_date
                dt_end = _datetime.fromisoformat(f"{end_d}T{dtend_time}")
                dt_end_london = london_tz.localize(dt_end)
                ev_lines.append(f"DTEND;TZID=Europe/London:{dt_end_london.strftime('%Y%m%dT%H%M%S')}")
            except Exception:
                pass
        elif dtend_date and not dtstart_time:
            # All-day multi-day: DTEND is exclusive (next day)
            try:
                exclusive_end = dtend_date + _timedelta(days=1)
                ev_lines.append(f"DTEND;VALUE=DATE:{exclusive_end.strftime('%Y%m%d')}")
            except Exception:
                pass

        if rrule_str:
            # Append UNTIL to the RRULE if we have a recurrence_end
            if recurrence_end and "UNTIL" not in rrule_str:
                rrule_str = rrule_str.rstrip(";") + f";UNTIL={recurrence_end.strftime('%Y%m%d')}T235959Z"
            ev_lines.append(f"RRULE:{rrule_str}")

        ev_lines.append(_fold(f"SUMMARY:{_ical_escape(summary)}"))
        if description:
            ev_lines.append(_fold(f"DESCRIPTION:{_ical_escape(description)}"))
        if location_str:
            ev_lines.append(_fold(f"LOCATION:{_ical_escape(location_str)}"))
        ev_lines.append("END:VEVENT")
        return ev_lines

    for row in rows:
        try:
            event_date_str = row.get("event_date", "")
            if not event_date_str:
                continue

            dtstart_date = _date.fromisoformat(str(event_date_str))
            event_time = (row.get("event_time") or "").strip()
            end_date_str = (row.get("end_date") or "").strip()
            end_time = (row.get("end_time") or "").strip()
            dtend_date = _date.fromisoformat(end_date_str) if end_date_str else dtstart_date

            raw_title = (row.get("title") or row.get("event_name") or "Event").strip()
            member = (row.get("family_member") or "").strip()
            summary = f"{raw_title} ({member})" if member and member.lower() not in ("family", "") else raw_title

            notes = (row.get("notes") or "").strip()
            location_str = (row.get("location") or "").strip()
            uid = f"{row.get('id', _uuid.uuid4())}@familybrain"

            is_recurring = bool(row.get("is_recurring", False))
            recurrence_rule_raw = (row.get("recurrence_rule") or "").strip()
            rec_end_str = (row.get("recurrence_end") or "").strip()
            rec_end_date: _date | None = None
            if rec_end_str:
                try:
                    rec_end_date = _date.fromisoformat(rec_end_str)
                except Exception:
                    pass

            # Build RRULE string from our shorthand or raw value
            rrule_str = ""
            if is_recurring and recurrence_rule_raw:
                rule_upper = recurrence_rule_raw.upper()
                if rule_upper == "WEEKLY":
                    rrule_str = "FREQ=WEEKLY"
                elif rule_upper == "BIWEEKLY":
                    rrule_str = "FREQ=WEEKLY;INTERVAL=2"
                elif rule_upper == "MONTHLY":
                    rrule_str = "FREQ=MONTHLY"
                elif rule_upper == "WEEKDAYS":
                    rrule_str = "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
                elif rule_upper == "WEEKENDS":
                    rrule_str = "FREQ=WEEKLY;BYDAY=SA,SU"
                elif recurrence_rule_raw.startswith("RRULE:"):
                    rrule_str = recurrence_rule_raw[6:]
                else:
                    rrule_str = recurrence_rule_raw

                # Cap expansion at 2 years if no explicit end
                if not rec_end_date:
                    rec_end_date = expand_until

            ev_lines = _emit_vevent(
                uid=uid,
                summary=summary,
                dtstart_date=dtstart_date,
                dtstart_time=event_time,
                dtend_date=dtend_date,
                dtend_time=end_time,
                description=notes,
                location_str=location_str,
                rrule_str=rrule_str,
                recurrence_end=rec_end_date,
            )
            lines.extend(ev_lines)
        except Exception as row_exc:
            logger.warning("ical_feed: skipping malformed row %s: %s", row.get("id"), row_exc)

    lines.append("END:VCALENDAR")

    # RFC 5545 requires CRLF line endings
    ical_body = "\r\n".join(lines) + "\r\n"

    return Response(
        ical_body,
        status=200,
        mimetype="text/calendar",
        headers={
            "Content-Disposition": f'attachment; filename="family-calendar.ics"',
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        },
    )



# ---------------------------------------------------------------------------
# Apple Calendar Subscribe Redirect Page
# ---------------------------------------------------------------------------
@app.route("/calendar/subscribe/<family_token>", methods=["GET"])
def apple_calendar_subscribe(family_token: str) -> Response:
    """Redirect page for Apple Calendar subscription.
    WhatsApp does not render webcal:// as a tappable link, so we serve
    a normal https:// page that immediately redirects to webcal://.
    iOS Safari intercepts the webcal:// redirect and opens Apple Calendar
    with a one-tap Subscribe prompt.
    """
    base_url = os.environ.get("FAMILYBRAIN_BASE_URL", "https://cortex-production-eb84.up.railway.app").rstrip("/")
    feed_url = f"{base_url}/calendar/feed/{family_token}.ics"
    webcal_url = feed_url.replace("https://", "webcal://", 1).replace("http://", "webcal://", 1)
    
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Add to Apple Calendar</title>
  <meta http-equiv="refresh" content="1;url={webcal_url}">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
           display: flex; align-items: center; justify-content: center;
           min-height: 100vh; margin: 0; background: #f5f5f7; }}
    .card {{ background: white; border-radius: 16px; padding: 32px 24px;
             max-width: 360px; text-align: center; box-shadow: 0 4px 24px rgba(0,0,0,0.08); }}
    h1 {{ font-size: 20px; font-weight: 600; color: #1d1d1f; margin: 0 0 8px; }}
    p {{ font-size: 15px; color: #6e6e73; margin: 0 0 24px; line-height: 1.5; }}
    a.btn {{ display: block; background: #007AFF; color: white; text-decoration: none;
             padding: 14px 20px; border-radius: 12px; font-size: 16px; font-weight: 600; }}
    .note {{ font-size: 13px; color: #8e8e93; margin-top: 16px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Add to Apple Calendar</h1>
    <p>Opening your family calendar subscription...<br>
       Tap the button below if it does not open automatically.</p>
    <a href="{webcal_url}" class="btn">Subscribe to Calendar</a>
    <p class="note">Read-only - Updates automatically - No login needed</p>
  </div>
  <script>
    setTimeout(function() {{ window.location.href = "{webcal_url}"; }}, 500);
  </script>
</body>
</html>"""
    return Response(html, status=200, mimetype="text/html")


@app.route("/gcal/connect", methods=["GET"])
def gcal_connect() -> Response:
    """Validates the one-time token and redirects to Google OAuth consent screen."""
    token = request.args.get("token")
    if not token:
        return Response("<h1>Error</h1><p>Missing token.</p>", status=400)

    db = brain._supabase
    if not db:
        return Response("<h1>Error</h1><p>Database connection failed.</p>", status=500)

    # Validate token
    try:
        result = db.table("gcal_connect_tokens").select("*").eq("token", token).execute()
        if not result.data:
            return Response("<h1>Error</h1><p>Invalid or expired token.</p>", status=400)
        
        token_data = result.data[0]
        from datetime import datetime, timezone
        expires_at = datetime.fromisoformat(token_data["expires_at"].replace("Z", "+00:00"))
        if datetime.now(timezone.utc) > expires_at:
            # Delete expired token
            db.table("gcal_connect_tokens").delete().eq("token", token).execute()
            return Response("<h1>Error</h1><p>Token has expired. Please request a new link.</p>", status=400)
    except Exception as exc:
        logger.error("Token validation failed: %s", exc)
        return Response("<h1>Error</h1><p>Internal server error.</p>", status=500)

    # Build Google OAuth URL
    from google_auth_oauthlib.flow import Flow
    from . import google_calendar
    
    client_config = {
        "web": {
            "client_id": google_calendar.CLIENT_ID,
            "client_secret": google_calendar.CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    base_url = os.environ.get("FAMILYBRAIN_BASE_URL", request.host_url.rstrip("/")).rstrip("/")
    redirect_uri = os.environ.get("GOOGLE_CALENDAR_OAUTH_REDIRECT_URI", f"{base_url}/gcal/callback")
    
    try:
        flow = Flow.from_client_config(
            client_config,
            scopes=google_calendar.SCOPES,
            redirect_uri=redirect_uri
        )
        
        # Pass the token as state so we can retrieve it in the callback
        auth_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=token
        )
        
        from flask import redirect
        return redirect(auth_url)
    except Exception as exc:
        logger.error("Failed to build OAuth URL: %s", exc)
        return Response("<h1>Error</h1><p>Failed to initiate Google login.</p>", status=500)


@app.route("/gcal/callback", methods=["GET"])
def gcal_callback() -> Response:
    """Receives the OAuth callback, exchanges code for tokens, and saves to DB."""
    error = request.args.get("error")
    if error:
        return Response(f"<h1>Error</h1><p>Google login failed: {error}</p>", status=400)
        
    code = request.args.get("code")
    state_token = request.args.get("state")
    
    if not code or not state_token:
        return Response("<h1>Error</h1><p>Missing code or state parameter.</p>", status=400)
        
    db = brain._supabase
    if not db:
        return Response("<h1>Error</h1><p>Database connection failed.</p>", status=500)
        
    # Validate token again
    try:
        result = db.table("gcal_connect_tokens").select("*").eq("token", state_token).execute()
        if not result.data:
            return Response("<h1>Error</h1><p>Invalid or expired session.</p>", status=400)
            
        token_data = result.data[0]
        family_id = token_data["family_id"]
        phone = token_data["phone"]
    except Exception as exc:
        logger.error("Token validation failed in callback: %s", exc)
        return Response("<h1>Error</h1><p>Internal server error.</p>", status=500)
        
    # Exchange code for tokens
    from google_auth_oauthlib.flow import Flow
    from . import google_calendar
    
    client_config = {
        "web": {
            "client_id": google_calendar.CLIENT_ID,
            "client_secret": google_calendar.CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    }
    
    base_url = os.environ.get("FAMILYBRAIN_BASE_URL", request.host_url.rstrip("/")).rstrip("/")
    redirect_uri = os.environ.get("GOOGLE_CALENDAR_OAUTH_REDIRECT_URI", f"{base_url}/gcal/callback")
    
    try:
        flow = Flow.from_client_config(
            client_config,
            scopes=google_calendar.SCOPES,
            redirect_uri=redirect_uri
        )
        
        # We need to reconstruct the full URL to pass to fetch_token
        # Railway might be behind a proxy, so ensure https
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "https")
        auth_response = request.url.replace("http://", f"{forwarded_proto}://", 1)
        
        flow.fetch_token(authorization_response=auth_response)
        credentials = flow.credentials
        
        refresh_token = credentials.refresh_token
        if not refresh_token:
            # If no refresh token is returned, it might be because the user already granted access
            # and Google only returns it on the first authorization.
            # We could try to revoke and re-prompt, but for now we'll show an error.
            return Response(
                "<h1>Error</h1><p>No refresh token received. Please go to your Google Account settings, "
                "remove access for this app, and try again.</p>", 
                status=400
            )
            
        # Save to families table
        db.table("families").update({"google_refresh_token": refresh_token}).eq("family_id", family_id).execute()
        
        # Also save to whatsapp_members table for this specific phone
        db.table("whatsapp_members").update({"google_refresh_token": refresh_token}).eq("phone", phone).execute()
        
        # Delete the used token
        db.table("gcal_connect_tokens").delete().eq("token", state_token).execute()
        
        # Send confirmation WhatsApp message and school email onboarding prompt
        try:
            from twilio.rest import Client as TwilioClient
            _s = get_settings()
            if _s.twilio_account_sid and _s.twilio_auth_token:
                twilio_client = TwilioClient(_s.twilio_account_sid, _s.twilio_auth_token)
                twilio_client.messages.create(
                    from_=_s.twilio_whatsapp_from,
                    to=f"whatsapp:{phone}",
                    body="✅ Google Calendar connected successfully! I will now sync your events.",
                )
                
                # Send follow-up prompt for school email watching
                twilio_client.messages.create(
                    from_=_s.twilio_whatsapp_from,
                    to=f"whatsapp:{phone}",
                    body="One more thing — want me to watch for school emails too? I'll automatically pick up letters, trip reminders, and payment deadlines. I only look at emails from your child's school — never personal, work, or financial emails.\n\nReply YES to connect school emails, or SKIP to set up manually later.",
                )
                
                # Set pending state for school email onboarding
                _pending_school_email_onboarding[f"whatsapp:{phone}"] = family_id
                
        except Exception as exc:
            logger.warning("Failed to send confirmation WhatsApp message: %s", exc)
            
        return Response(
            "<html><body style='font-family: sans-serif; text-align: center; padding: 50px;'>"
            "<h1>✅ Google Calendar connected!</h1>"
            "<p>You can close this tab and return to WhatsApp.</p>"
            "</body></html>",
            status=200
        )
        
    except Exception as exc:
        logger.error("Failed to exchange code for tokens: %s", exc)
        return Response("<h1>Error</h1><p>Failed to complete Google login.</p>", status=500)


@app.route("/whatsapp", methods=["POST"])
@_validate_twilio_request
def handle_whatsapp() -> Response:
    """Main Twilio webhook handler for incoming WhatsApp messages.

    Twilio sends a POST with form-encoded fields including:
      - From: the sender's WhatsApp number (e.g. "whatsapp:+447700900000")
      - Body: the text body of the message
      - NumMedia: number of media attachments
      - MediaUrl0, MediaContentType0: URL and MIME type of the first attachment
    """
    from_number: str = request.values.get("From", "").strip()
    message_body: str = request.values.get("Body", "").strip()
    num_media: int = int(request.values.get("NumMedia", "0"))

    logger.info(
        "Incoming WhatsApp message from=%s, body_len=%d, num_media=%d",
        from_number, len(message_body), num_media,
    )

    # --- Authorisation check ---
    family_name = _get_family_name(from_number)
    if family_name is None:
        logger.warning("Rejected message from unauthorised number: %s", from_number)
        twiml = MessagingResponse()
        twiml.message(
            "Sorry, this is a private Family Brain bot. "
            "Your number is not authorised. "
            "Please ask the bot owner to add your WhatsApp number."
        )
        return Response(str(twiml), mimetype="application/xml")

    # --- Route to appropriate handler ---
    if num_media > 0:
        media_url: str = request.values.get("MediaUrl0", "")
        mime_type: str = request.values.get("MediaContentType0", "")
        return _handle_media_message(
            media_url=media_url,
            mime_type=mime_type,
            caption=message_body,
            family_name=family_name,
            from_number=from_number,
        )

    return _handle_text_message(
        text=message_body,
        family_name=family_name,
        from_number=from_number,
    )


# ---------------------------------------------------------------------------
# Google Calendar Connect Helper
# ---------------------------------------------------------------------------
def _send_gcal_connect_link(phone: str, family_id: str) -> tuple[str, str]:
    """Generate a one-time Google OAuth token and return (google_connect_url, webcal_url).

    Returns a tuple of:
    - google_connect_url: the one-time OAuth link (expires in 1 hour)
    - webcal_url: the auth-free webcal:// subscription link for Apple Calendar

    Either value may be an empty string on failure.
    """
    import secrets
    from datetime import datetime, timedelta, timezone

    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    db = brain._supabase
    if not db:
        logger.error("Cannot send gcal connect link: no database connection")
        return ("", "")

    try:
        db.table("gcal_connect_tokens").insert({
            "token": token,
            "phone": phone,
            "family_id": family_id,
            "expires_at": expires_at
        }).execute()

        base_url = os.environ.get("FAMILYBRAIN_BASE_URL", "https://cortex-production-eb84.up.railway.app").rstrip("/")
        connect_url = f"{base_url}/gcal/connect?token={token}"

        # Build the webcal:// subscription URL using the family's calendar_token
        webcal_url = ""
        try:
            cal_token = _get_or_create_calendar_token(family_id)
            if cal_token:
                # webcal:// is https:// with the scheme replaced; iOS Safari intercepts it
                feed_https = f"{base_url}/calendar/feed/{cal_token}.ics"
                webcal_url = feed_https.replace("https://", "webcal://", 1).replace("http://", "webcal://", 1)
        except Exception as wc_exc:
            logger.warning("Could not build webcal URL for %s: %s", phone, wc_exc)

        logger.info("Generated hybrid calendar links for %s (gcal=%s, webcal=%s)", phone, bool(connect_url), bool(webcal_url))
        return (connect_url, webcal_url)
    except Exception as exc:
        logger.error("Failed to generate gcal connect link: %s", exc)
        return ("", "")


# ---------------------------------------------------------------------------
# Intent detection: determine if a message is a query or something to capture
# ---------------------------------------------------------------------------

_QUESTION_WORDS = (
    "when", "where", "what", "who", "which", "how", "why", "did", "do", "does", "is", "are", "was", "were", "have", "has", "had", "can", "could", "would", "should", "tell", "show", "find", "search", "look", "remind", "recall", "remember"
)
_QUERY_PHRASES = (
    "when did", "where did", "what did", "who did", "have i", "do i", "did i", "do we", "did we", "have we", "is my", "are my", "was my", "were my"
)

def _is_query(text: str, from_number: str) -> bool:
    """Return True if the text is likely a question/query, not something to be stored."""
    # Context-aware check for short follow-up questions
    history = _conversation_history.get(from_number, [])
    if history and len(text.split()) <= 4:
        logger.info("Treating short message from %s as a query due to recent conversation history.", from_number)
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

def _answer_query(text: str, from_number: str, conversation_history: list[dict] | None = None) -> Response:
    """Handle a message that has been identified as a query."""
    twiml = MessagingResponse()
    logger.info("Handling message as a query: %s", text)
    family_name = _get_family_name(from_number) or "Unknown"
    family_id = _get_family_id_for_phone(from_number)

    try:
        # Step 1: Expand query with synonyms and perform semantic search
        synonyms = []
        if "lease" in text.lower():
            synonyms.append("contract hire")
        if "contract hire" in text.lower():
            synonyms.append("lease")
        if any(word in text.lower() for word in ["end", "ends", "ending"]):
            synonyms.extend(["expiry", "expires"])

        # Reframe availability questions: "is Izzy free tomorrow?" → "what does Izzy have on tomorrow"
        # This dramatically improves semantic match against schedule/event memories
        import re as _re
        text_lower = text.lower()
        avail_pattern = _re.search(r'is\s+(\w+)\s+(free|available|busy|around)', text_lower)
        time_ref = ""  # initialise here so it's always defined for the supplement block below
        if avail_pattern:
            person = avail_pattern.group(1).capitalize()
            # Extract time reference from original text (tomorrow, Monday, next week, etc.)
            time_refs = _re.findall(r'(tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday|next week|this week|\d{1,2}(?:st|nd|rd|th)?(?:\s+\w+)?)', text_lower)
            time_ref = time_refs[0] if time_refs else ""
            synonyms.append(f"what does {person} have on {time_ref}")
            synonyms.append(f"{person} schedule {time_ref}")
            synonyms.append(f"{person} activity event {time_ref}")
            logger.info("Availability query reframed for %s on %s", person, time_ref)

        # For broad inventory-style queries ("what X do I have", "list my X"), use lower threshold and higher count
        broad_query_words = ("what", "list", "all", "how many", "do i have", "my cars", "my vehicles", "my policies", "my accounts")
        is_broad_query = any(w in text.lower() for w in broad_query_words)
        # Availability queries also need wider search
        is_availability_query = bool(avail_pattern)
        _threshold = 0.2 if (is_broad_query or is_availability_query) else 0.3
        _count = 15 if (is_broad_query or is_availability_query) else 8
        expanded_query = text + " " + " ".join(synonyms)
        results = brain.semantic_search(expanded_query, match_threshold=_threshold, match_count=_count, family_id=family_id)

        # --- Issue 2 fix: For availability queries, ALSO do a direct text search ---
        # Semantic search may rank an old high-similarity memory (e.g. kickboxing) above
        # a newly stored event for today.  We supplement with a direct DB scan for ALL
        # memories that mention the person's name AND today's date string so nothing is
        # missed regardless of embedding similarity score.
        if is_availability_query and avail_pattern:
            try:
                from datetime import date as _avail_date
                _today_iso = _avail_date.today().isoformat()          # e.g. "2026-03-21"
                _today_human = _avail_date.today().strftime("%d %B %Y")  # e.g. "21 March 2026"
                _person_lower = avail_pattern.group(1).lower()
                # Resolve "today" / "tomorrow" time references to actual dates
                _time_ref_lower = time_ref.lower() if time_ref else "today"
                if _time_ref_lower in ("today", ""):
                    _target_date_iso = _today_iso
                    _target_date_human = _today_human
                elif _time_ref_lower == "tomorrow":
                    from datetime import timedelta as _td
                    _target_date_iso = (_avail_date.today() + _td(days=1)).isoformat()
                    _target_date_human = (_avail_date.today() + _td(days=1)).strftime("%d %B %Y")
                else:
                    _target_date_iso = _today_iso
                    _target_date_human = _today_human

                # Fetch recent memories and filter by person name + target date in content
                _all_recent = brain.list_recent_memories(limit=100, family_id=family_id)
                _seen_ids = {r.get("id") for r in results}
                for _mem in _all_recent:
                    _c = (_mem.get("content") or "").lower()
                    # Must mention the person AND the target date (ISO or human-readable)
                    if (
                        _person_lower in _c
                        and (_target_date_iso in _c or _target_date_human.lower() in _c)
                        and _mem.get("id") not in _seen_ids
                    ):
                        results.append(_mem)
                        _seen_ids.add(_mem.get("id"))
                        logger.info(
                            "Availability supplement: added memory id=%s for %s on %s",
                            _mem.get("id"), _person_lower, _target_date_iso,
                        )
            except Exception as _exc:
                logger.warning("Availability supplement search failed (non-fatal): %s", _exc)

        reply_text = ""

        if results:
            # Issue 3 fix: include created_at so the LLM can judge memory freshness.
            # Relative-time words (tonight, tomorrow, this week) are only trustworthy
            # when the memory was stored recently; we surface the timestamp so the LLM
            # can detect and discard stale relative-date references.
            def _fmt_memory_line(r: dict) -> str:
                content = r.get("content", "")
                created_at_raw = r.get("created_at", "")
                if created_at_raw:
                    # Normalise to a short human-readable form: "21 Mar 2026 13:31 UTC"
                    try:
                        from datetime import datetime as _dt, timezone as _tz
                        _ts = _dt.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
                        _ts_utc = _ts.astimezone(_tz.utc)
                        created_label = _ts_utc.strftime("%d %b %Y %H:%M UTC")
                    except Exception:
                        created_label = str(created_at_raw)[:19]
                    return f"- [stored: {created_label}] {content}"
                return f"- {content}"

            memories_text = "\n".join(_fmt_memory_line(r) for r in results)

            # 3a. Check if the answer likely involves a business/location and is missing contact details
            web_context = ""
            contact_keywords = ("phone", "number", "call", "contact", "email", "address", "where", "garage", "book", "appointment", "them", "their")
            memory_lower = memories_text.lower()
            query_lower = text.lower()
            has_contact_intent = any(k in query_lower for k in contact_keywords)
            missing_phone = "phone" not in memory_lower and "tel" not in memory_lower
            
            # Build context for business extraction: memories + recent conversation history
            history_text = ""
            if conversation_history:
                history_text = "\n".join(
                    f"{m['role'].title()}: {m['content'][:200]}"
                    for m in conversation_history[-4:]  # last 2 turns
                )
            lookup_context = (memories_text[:400] + ("\n\nRecent conversation:\n" + history_text) if history_text else memories_text[:400])
            
            # Look for a business name + location in memories/history to search for
            if has_contact_intent or missing_phone:
                try:
                    # Ask LLM to extract business name + location for web lookup
                    lookup_prompt = (
                        "Extract the business name and location from these memories and conversation history for a web search. "
                        "Return JSON with keys: business_name (string or null), location (string or null). "
                        "Only extract if there is a clear business name (e.g. 'Kwik Fit', 'Tesla Service Centre', 'Tesco'). "
                        "Use the conversation history to resolve references like 'them' or 'their'. "
                        "Return {\"business_name\": null} if no clear business is mentioned."
                    )
                    lookup_result = brain.get_llm_reply(
                        system_message=lookup_prompt,
                        user_message=lookup_context,
                        json_schema={"type": "object", "properties": {
                            "business_name": {"type": ["string", "null"]},
                            "location": {"type": ["string", "null"]},
                        }}
                    )
                    import json as _json
                    if isinstance(lookup_result, str):
                        lookup_result = _json.loads(lookup_result)
                    biz_name = lookup_result.get("business_name")
                    biz_location = lookup_result.get("location") or ""
                    if biz_name:
                        from ddgs import DDGS
                        search_query = f"{biz_name} {biz_location} phone number contact"
                        logger.info("Web enrichment search: %s", search_query)
                        with DDGS() as ddgs:
                            web_results = list(ddgs.text(search_query, max_results=3))
                        if web_results:
                            snippets = "\n".join(r.get("body", "")[:200] for r in web_results)
                            web_context = f"\n\nWeb search results for '{biz_name} {biz_location}':\n{snippets}"
                            logger.info("Web enrichment found %d results", len(web_results))
                except Exception as exc:
                    logger.warning("Web enrichment failed (non-fatal): %s", exc)

            from datetime import date as _date, datetime as _datetime, timezone as _tz_utc
            today_str = _date.today().strftime("%d %B %Y")
            now_utc = _datetime.now(_tz_utc.utc)
            prompt = (
                f"You are Family Brain, a personal AI assistant for the {family_name} family. "
                f"The person asking this question is {family_name}. "
                f"Today's date is {today_str}. "
                "Each stored memory below is prefixed with a [stored: <timestamp>] label showing when it was saved. "
                "MEMORY FRESHNESS RULE (STRICT): If a memory uses relative time words such as 'tonight', 'today', "
                "'tomorrow', 'this week', 'next week', 'yesterday', or similar, you MUST check its [stored:] "
                "timestamp. If the memory was stored MORE THAN 24 HOURS AGO relative to now, you MUST "
                "COMPLETELY IGNORE that memory for any availability or schedule question. Do NOT mention it, "
                "do NOT include it in your answer, do NOT say it might be stale — simply exclude it entirely. "
                "A memory stored 2+ days ago saying 'kickboxing tonight' is irrelevant to today's schedule and "
                "must be silently discarded. Only memories stored within the last 24 hours may use relative "
                "time words to describe current-day events. "
                "Answer the user's question based on the stored memories below. "
                "Do NOT invent details that are not in the memories or web results. "
                "If the memories contain conflicting information, use the most specific and detailed one. "
                "If the question asks about ALL items of a type (e.g. 'what cars do I have', 'list my policies'), "
                "make sure to include EVERY relevant item found in the memories, not just the first one. "
                "IMPORTANT: If any stored item contains a date that is today, tomorrow, or within the next 7 days "
                "(e.g. contract end, renewal, expiry, payment due, MOT due), you MUST start your answer with a "
                "\u26a0\ufe0f URGENT alert line before anything else. Example: '\u26a0\ufe0f URGENT: Your VW ID.Buzz contract hire ends TODAY (20 March 2026). You should contact VW Financial Services immediately.' "
                "Do not bury time-sensitive dates in the middle of a list \u2014 always lead with them. "
                "If web search results are provided, you may use them to supplement missing contact details "
                "(phone numbers, emails, opening hours) \u2014 but clearly indicate these came from a web search, not stored memory. "
                "If information is genuinely missing and not found online, say so and offer to store it. "
                "Refer to the asker by name. Use the conversation history for context if needed. "
                "Never mention memory IDs in your answer."
            )
            
            messages = [{"role": "system", "content": prompt}]
            if conversation_history:
                messages.extend(conversation_history)
            messages.append({"role": "user", "content": f"Question: {text}\n\nStored memories:\n{memories_text}{web_context}"})

            answer = brain.get_llm_reply(messages=messages)
            reply_text = answer[:3800]

            # Update conversation history
            if from_number not in _conversation_history:
                _conversation_history[from_number] = []
            _conversation_history[from_number].append({"role": "user", "content": text})
            _conversation_history[from_number].append({"role": "assistant", "content": answer})
            _conversation_history[from_number] = _conversation_history[from_number][-6:] # keep last 3 turns

        else:
            # No memories found — but if we have conversation history, try web enrichment
            # for follow-up questions like "do you have their number?"
            contact_keywords = ("phone", "number", "call", "contact", "email", "book", "them", "their")
            query_lower = text.lower()
            has_contact_intent = any(k in query_lower for k in contact_keywords)
            # Also trigger if user is confirming a previous offer to look something up
            affirmative_words = ("yes", "yeah", "yep", "yup", "ok", "okay", "sure", "please", "go ahead", "do it", "thanks")
            is_affirmative = any(query_lower.strip().startswith(w) for w in affirmative_words) and len(text.split()) <= 5
            bot_offered_lookup = False
            if conversation_history and is_affirmative:
                last_bot = next((m["content"].lower() for m in reversed(conversation_history) if m["role"] == "assistant"), "")
                lookup_phrases = ("look it up", "look that up", "search for", "find the number", "find a number", "would you like me to", "shall i look", "i can look")
                bot_offered_lookup = any(p in last_bot for p in lookup_phrases)
            web_fallback_answer = None

            if (has_contact_intent or bot_offered_lookup) and conversation_history:
                try:
                    history_text = "\n".join(
                        f"{m['role'].title()}: {m['content'][:200]}"
                        for m in conversation_history[-4:]
                    )
                    lookup_prompt = (
                        "Extract the business name and location from this conversation history for a web search. "
                        "Return JSON with keys: business_name (string or null), location (string or null). "
                        "Use references like 'them' or 'their' to identify the business from context. "
                        "Return {\"business_name\": null} if no clear business is mentioned."
                    )
                    import json as _json
                    lookup_result = brain.get_llm_reply(
                        system_message=lookup_prompt,
                        user_message=f"Current question: {text}\n\nConversation:\n{history_text}",
                        json_schema={"type": "object", "properties": {
                            "business_name": {"type": ["string", "null"]},
                            "location": {"type": ["string", "null"]},
                        }}
                    )
                    if isinstance(lookup_result, str):
                        lookup_result = _json.loads(lookup_result)
                    biz_name = lookup_result.get("business_name")
                    biz_location = lookup_result.get("location") or ""
                    if biz_name:
                        from ddgs import DDGS
                        search_query = f"{biz_name} {biz_location} phone number contact"
                        logger.info("Web fallback search: %s", search_query)
                        with DDGS() as ddgs:
                            web_results = list(ddgs.text(search_query, max_results=3))
                        if web_results:
                            snippets = "\n".join(r.get("body", "")[:200] for r in web_results)
                            web_context = f"Web search results for '{biz_name} {biz_location}':\n{snippets}"
                            prompt = (
                                f"You are Family Brain, a personal AI assistant for the {family_name} family. "
                                f"The person asking is {family_name}. "
                                "The user is asking a follow-up question. There are no stored memories for this specific query. "
                                "Use the web search results and conversation history to answer. "
                                "Clearly indicate that contact details came from a web search, not stored memory. "
                                "Offer to store the information for next time."
                            )
                            messages = [{"role": "system", "content": prompt}]
                            messages.extend(conversation_history)
                            messages.append({"role": "user", "content": f"Question: {text}\n\n{web_context}"})
                            web_fallback_answer = brain.get_llm_reply(messages=messages)
                except Exception as exc:
                    logger.warning("Web fallback enrichment failed: %s", exc)

            if web_fallback_answer:
                reply_text = web_fallback_answer[:3800]
                # Update conversation history
                if from_number not in _conversation_history:
                    _conversation_history[from_number] = []
                _conversation_history[from_number].append({"role": "user", "content": text})
                _conversation_history[from_number].append({"role": "assistant", "content": web_fallback_answer})
                _conversation_history[from_number] = _conversation_history[from_number][-6:]
            else:
                reply_text = "I don't have anything stored about that yet. Send me the information and I'll remember it for next time."

        twiml.message(reply_text)
        logger.info("Answered query with %d sources", len(results))
        log_action(family_id, 'query_answered', subject=text[:50], detail={'sources': len(results)}, phone_number=from_number)

    except Exception as exc:
        logger.error("Failed to answer query: %s\n%s", exc, traceback.format_exc())
        twiml.message(f"⚠️ Failed to answer query: {exc}")

    return Response(str(twiml), mimetype="application/xml")


# ---------------------------------------------------------------------------
# Memory management handler (delete, edit, list)
# ---------------------------------------------------------------------------
# Pending delete confirmations: {from_number: {memory_id, preview}}
_pending_deletes: dict[str, dict] = {}
# Pending edit confirmations: {from_number: {memory_id, preview, new_content}}
_pending_edits: dict[str, dict] = {}
# Pending recurring event confirmations: {from_number: event_data_dict}
_pending_recurring_events: dict[str, dict] = {}
# Pending GDPR full-data-deletion confirmations: {from_number: family_id}
_pending_data_deletion: dict[str, str] = {}
# Pending school email onboarding: {from_number: family_id}
_pending_school_email_onboarding: dict[str, str] = {}


def _handle_memory_management(text: str, family_name: str, from_number: str) -> Response | None:
    """Handle memory management commands. Returns a Response if handled, else None."""
    twiml = MessagingResponse()
    family_id = _get_family_id_for_phone(from_number)
    text_lower = text.lower().strip()

    # --- Handle pending emergency category prompt (user replies 1-10) ---
    if _pending_category_prompt.get(from_number):
        # Check if the reply is a number 1-10
        import re as _re_cat
        cat_match = _re_cat.match(r'^(10|[1-9])$', text_lower.strip())
        if cat_match:
            cat_num = cat_match.group(1)
            memory_id = _last_stored_memory.get(from_number)
            del _pending_category_prompt[from_number]
            if memory_id:
                db = brain._supabase
                if db:
                    try:
                        # Fetch current metadata
                        result = db.table("memories").select("metadata").eq("id", memory_id).limit(1).execute()
                        if result.data:
                            current_meta = result.data[0].get("metadata") or {}
                            current_meta["emergency_category"] = cat_num
                            db.table("memories").update({"metadata": current_meta}).eq("id", memory_id).execute()
                            cat_name = _EMERGENCY_CATEGORY_NAMES.get(cat_num, cat_num)
                            twiml.message(f"✅ Categorised under '{cat_name}' in your emergency file.")
                            logger.info("Emergency category %s set on memory %s by %s", cat_num, memory_id, from_number)
                        else:
                            twiml.message("✅ Got it! (Memory not found to update, but noted.)")
                    except Exception as exc:
                        logger.warning("Failed to update emergency_category: %s", exc)
                        twiml.message("✅ Got it! (Couldn't save category, but no worries.)")
                else:
                    twiml.message("✅ Got it!")
            else:
                twiml.message("✅ Got it!")
            return Response(str(twiml), mimetype="application/xml")
        elif text_lower.strip() in ("skip", "no", "cancel", "later"):
            del _pending_category_prompt[from_number]
            twiml.message("OK, skipped. You can always send /sos to generate your emergency file.")
            return Response(str(twiml), mimetype="application/xml")
        # If it's not a number or skip, fall through to normal handling
        # (don't consume the message)
        del _pending_category_prompt[from_number]

    # --- Confirm pending delete ---
    if from_number in _pending_deletes:
        pending = _pending_deletes[from_number]
        if text_lower in ("yes", "y", "confirm", "delete", "ok"):
            del _pending_deletes[from_number]
            db = brain._supabase
            if db:
                db.table("memories").delete().eq("id", pending["memory_id"]).execute()
            twiml.message(f"✅ Memory deleted.")
            return Response(str(twiml), mimetype="application/xml")
        elif text_lower in ("no", "n", "cancel", "keep"):
            del _pending_deletes[from_number]
            twiml.message("OK, kept. Nothing was deleted.")
            return Response(str(twiml), mimetype="application/xml")

    # --- Confirm pending edit ---
    if from_number in _pending_edits:
        pending = _pending_edits[from_number]
        if text_lower in ("yes", "y", "confirm", "ok"):
            del _pending_edits[from_number]
            memory_id = pending["memory_id"]
            new_content = pending["new_content"]
            db = brain._supabase
            if db:
                try:
                    new_embedding = brain.generate_embedding(new_content)
                    db.table("memories").update({"content": new_content, "embedding": new_embedding}).eq("id", memory_id).execute()
                    twiml.message(f"✅ Memory updated.")
                except Exception as exc:
                    twiml.message(f"⚠️ Update failed: {exc}")
            return Response(str(twiml), mimetype="application/xml")
        elif text_lower in ("no", "n", "cancel"):
            del _pending_edits[from_number]
            twiml.message("OK, cancelled. Nothing was changed.")
            return Response(str(twiml), mimetype="application/xml")

    # --- Handle pending school email onboarding ---
    if from_number in _pending_school_email_onboarding:
        family_id = _pending_school_email_onboarding[from_number]
        if text_lower in ("yes", "y", "sure", "ok", "connect"):
            del _pending_school_email_onboarding[from_number]
            db = brain._supabase
            if db:
                db.table("families").update({"school_email_watch": True}).eq("family_id", family_id).execute()
            twiml.message("✅ School email watching connected! I'll keep an eye out for letters, trips, and deadlines.")
            return Response(str(twiml), mimetype="application/xml")
        elif text_lower in ("skip", "no", "n", "later"):
            del _pending_school_email_onboarding[from_number]
            
            # Get family token for fallback email
            fallback_email = "your-family@familybrain.co"
            db = brain._supabase
            if db:
                try:
                    res = db.table("families").select("calendar_token").eq("family_id", family_id).execute()
                    if res.data and res.data[0].get("calendar_token"):
                        fallback_email = f"{res.data[0]['calendar_token']}@familybrain.co"
                except Exception:
                    pass
                    
            twiml.message(f"No problem! You can forward school emails to {fallback_email} any time and I'll handle them automatically.")
            return Response(str(twiml), mimetype="application/xml")

    # --- Confirm pending recurring event ---
    if from_number in _pending_recurring_events:
        if text_lower in ("yes", "yeah", "yep", "confirm", "ok", "correct", "y"):
            event_data = _pending_recurring_events.pop(from_number)
            event_id, conflict_warning = _check_conflicts_and_store_event(event_data, family_name, family_id=family_id)
            
            event_name = event_data.get("event_name", "Event")
            event_time = event_data.get("event_time", "")
            time_str = f" at {event_time}" if event_time else ""
            day_str = event_data.get("recurrence_day", "").capitalize()
            rule = event_data.get("recurrence_rule", "")
            
            if rule == "WEEKLY" and day_str:
                freq_str = f"every {day_str}"
            elif rule == "BIWEEKLY" and day_str:
                freq_str = f"every other {day_str}"
            elif rule == "MONTHLY":
                freq_str = "monthly"
            elif rule == "WEEKDAYS":
                freq_str = "every weekday"
            elif rule == "WEEKENDS":
                freq_str = "every weekend"
            else:
                freq_str = "recurring"
                
            reply = f"✅ Done! I've added {event_name} {freq_str}{time_str}. It'll show on your family calendar."
            if conflict_warning:
                reply += f"\n\n{conflict_warning}"
            if event_id:
                log_action(family_id, 'event_created', subject=f"{event_name} {freq_str}", detail={'event_id': event_id, 'recurrence_rule': rule, 'family_member': event_data.get('family_member', family_name)}, phone_number=from_number)
                
            twiml.message(reply)
            return Response(str(twiml), mimetype="application/xml")
        elif text_lower in ("no", "n", "cancel", "stop"):
            del _pending_recurring_events[from_number]
            twiml.message("No problem — I've cancelled that. Send me the corrected details whenever you're ready.")
            return Response(str(twiml), mimetype="application/xml")

    # --- List recent memories ---
    list_patterns = ("show my memories", "list my memories", "what have you stored", "show recent", "list recent", "what do you know about me", "show me what you've stored")
    if any(text_lower.startswith(p) or p in text_lower for p in list_patterns):
        memories = brain.list_recent_memories(limit=8, family_id=family_id)
        if not memories:
            twiml.message("You don't have any stored memories yet.")
        else:
            lines = ["Here are your 8 most recent memories:\n"]
            for i, m in enumerate(memories, 1):
                snippet = m.get("content", "")[:80]
                lines.append(f"{i}. {snippet}...")
            lines.append("\nTo delete one, say \"delete memory 3\" (or whichever number).")
            lines.append("To correct one, say \"correct memory 3: [new text]\"")
            twiml.message("\n".join(lines))
        return Response(str(twiml), mimetype="application/xml")

    # --- Delete by number (after list) ---
    import re as _re2
    delete_num_match = _re2.match(r'delete\s+(?:memory\s+)?(\d+)', text_lower)
    if delete_num_match:
        idx = int(delete_num_match.group(1)) - 1
        memories = brain.list_recent_memories(limit=8, family_id=family_id)
        if 0 <= idx < len(memories):
            mem = memories[idx]
            _pending_deletes[from_number] = {"memory_id": mem["id"], "preview": mem.get("content", "")[:120]}
            twiml.message(f"Are you sure you want to delete this memory?\n\n\"{_pending_deletes[from_number]['preview']}\"\n\nReply YES to confirm or NO to cancel.")
        else:
            twiml.message(f"I couldn't find memory number {idx+1}. Say \"show my memories\" to see the list.")
        return Response(str(twiml), mimetype="application/xml")

    # --- Delete by description ---
    delete_desc_match = _re2.match(r'delete\s+(?:the\s+)?(?:memory\s+)?(?:about\s+)?(.+)', text_lower)
    if delete_desc_match and not delete_num_match:
        query = delete_desc_match.group(1).strip()
        if len(query) > 3:  # avoid matching very short fragments
            results = brain.semantic_search(query, match_threshold=0.3, match_count=1, family_id=family_id)
            if results:
                mem = results[0]
                _pending_deletes[from_number] = {"memory_id": mem["id"], "preview": mem.get("content", "")[:120]}
                twiml.message(f"Did you mean this memory?\n\n\"{_pending_deletes[from_number]['preview']}\"\n\nReply YES to delete or NO to cancel.")
            else:
                twiml.message(f"I couldn't find a memory matching \"{query}\". Say \"show my memories\" to see the full list.")
            return Response(str(twiml), mimetype="application/xml")

    # --- Correct/update by number ---
    correct_match = _re2.match(r'(?:correct|update|edit|change)\s+(?:memory\s+)?(\d+)\s*[:\-]?\s*(.+)', text_lower)
    if correct_match:
        idx = int(correct_match.group(1)) - 1
        new_content = correct_match.group(2).strip()
        memories = brain.list_recent_memories(limit=8, family_id=family_id)
        if 0 <= idx < len(memories):
            mem = memories[idx]
            _pending_edits[from_number] = {"memory_id": mem["id"], "preview": mem.get("content", "")[:120], "new_content": new_content}
            twiml.message(
                f"Update this memory:\n\nOLD: \"{_pending_edits[from_number]['preview']}\"\n\nNEW: \"{new_content}\"\n\nReply YES to confirm or NO to cancel."
            )
        else:
            twiml.message(f"I couldn't find memory number {idx+1}. Say \"show my memories\" to see the list.")
        return Response(str(twiml), mimetype="application/xml")

    # --- Correct/update by description ---
    correct_desc_match = _re2.match(r'(?:correct|update|edit|change|fix)\s+(?:the\s+)?(?:memory\s+)?(?:about\s+)?(.+?)\s*[:\-]\s*(.+)', text_lower)
    if correct_desc_match:
        query = correct_desc_match.group(1).strip()
        new_content = correct_desc_match.group(2).strip()
        if len(query) > 3:
            results = brain.semantic_search(query, match_threshold=0.3, match_count=1, family_id=family_id)
            if results:
                mem = results[0]
                _pending_edits[from_number] = {"memory_id": mem["id"], "preview": mem.get("content", "")[:120], "new_content": new_content}
                twiml.message(
                    f"Update this memory?\n\nOLD: \"{_pending_edits[from_number]['preview']}\"\n\nNEW: \"{new_content}\"\n\nReply YES to confirm or NO to cancel."
                )
            else:
                twiml.message(f"I couldn't find a memory matching \"{query}\". Say \"show my memories\" to see the full list.")
            return Response(str(twiml), mimetype="application/xml")

    # --- Forget everything (nuclear option) ---
    if text_lower in ("forget everything", "delete all memories", "clear all memories", "wipe everything"):
        count = len(brain.list_recent_memories(limit=1000, family_id=family_id))
        _pending_deletes[from_number] = {"memory_id": "__ALL__", "preview": f"ALL {count} memories"}
        twiml.message(f"⚠️ Are you sure you want to delete ALL {count} memories? This cannot be undone.\n\nReply YES to confirm or NO to cancel.")
        return Response(str(twiml), mimetype="application/xml")

    return None  # Not a memory management command


# ---------------------------------------------------------------------------
# GDPR data deletion handler  (/delete-my-data)
# ---------------------------------------------------------------------------

def _handle_delete_my_data_command(from_number: str, family_id: str) -> Response:
    """Handle the /delete-my-data command — ask for confirmation.

    Stores the pending deletion in ``_pending_data_deletion`` keyed by
    ``from_number``.  The confirmation is resolved in
    ``_handle_data_deletion_confirmation()`` when the user replies DELETE.
    """
    _pending_data_deletion[from_number] = family_id
    twiml = MessagingResponse()
    twiml.message(
        "Are you sure you want to delete all your FamilyBrain data? "
        "This will remove all your memories, documents, and calendar events "
        "stored by FamilyBrain.\n\n"
        "Reply DELETE to confirm."
    )
    logger.info(
        "Data deletion confirmation requested by %s (family_id=%s)",
        from_number, family_id,
    )
    return Response(str(twiml), mimetype="application/xml")


def _execute_family_data_deletion(family_id: str, from_number: str) -> dict[str, Any]:
    """Delete all data for a family from every relevant table.

    Deletes rows from:
      - memories          (family-scoped via metadata JSONB)
      - family_events     (family_id column)
      - cortex_briefings  (family_id column)

    Returns a dict with keys 'deleted' (table -> count) and 'errors' (list).
    """
    db = brain._supabase
    results: dict[str, Any] = {"deleted": {}, "errors": []}

    if not db:
        results["errors"].append("No database connection")
        return results

    # 1. Delete all memories for this family (scoped via metadata JSONB)
    try:
        mem_result = (
            db.table("memories")
            .delete()
            .contains("metadata", {"family_id": family_id})
            .execute()
        )
        count = len(mem_result.data) if mem_result.data else 0
        results["deleted"]["memories"] = count
        logger.info(
            "Data deletion: removed %d memories for family_id=%s", count, family_id
        )
    except Exception as exc:
        msg = f"memories: {exc}"
        results["errors"].append(msg)
        logger.error("Data deletion error — %s", msg)

    # 2. Delete all family_events for this family
    try:
        ev_result = (
            db.table("family_events")
            .delete()
            .eq("family_id", family_id)
            .execute()
        )
        count = len(ev_result.data) if ev_result.data else 0
        results["deleted"]["family_events"] = count
        logger.info(
            "Data deletion: removed %d family_events for family_id=%s", count, family_id
        )
    except Exception as exc:
        msg = f"family_events: {exc}"
        results["errors"].append(msg)
        logger.error("Data deletion error — %s", msg)

    # 3. Delete all cortex_briefings for this family
    try:
        br_result = (
            db.table("cortex_briefings")
            .delete()
            .eq("family_id", family_id)
            .execute()
        )
        count = len(br_result.data) if br_result.data else 0
        results["deleted"]["cortex_briefings"] = count
        logger.info(
            "Data deletion: removed %d cortex_briefings for family_id=%s",
            count, family_id,
        )
    except Exception as exc:
        msg = f"cortex_briefings: {exc}"
        results["errors"].append(msg)
        logger.error("Data deletion error — %s", msg)

    # 4. Attempt to delete any emergency PDFs from Supabase Storage
    try:
        db.storage.from_("emergency-pdfs").remove([family_id + "/"])
        logger.info(
            "Data deletion: attempted removal of emergency PDFs for family_id=%s",
            family_id,
        )
    except Exception as exc:
        # Storage deletion failure is non-fatal — log and continue
        logger.warning(
            "Data deletion: could not remove emergency PDFs for family_id=%s: %s",
            family_id, exc,
        )

    return results


def _handle_data_deletion_confirmation(text: str, from_number: str) -> Response | None:
    """Check whether the user is confirming or cancelling a /delete-my-data request.

    Returns a Response if this message is part of the deletion flow, else None.
    """
    if from_number not in _pending_data_deletion:
        return None

    family_id = _pending_data_deletion[from_number]
    text_stripped = text.strip()
    twiml = MessagingResponse()

    if text_stripped == "DELETE":
        # User confirmed — execute deletion
        del _pending_data_deletion[from_number]
        logger.info(
            "Data deletion CONFIRMED by %s (family_id=%s)", from_number, family_id
        )

        deletion_results = _execute_family_data_deletion(family_id, from_number)

        if deletion_results["errors"]:
            # Partial failure
            error_summary = "; ".join(deletion_results["errors"])
            twiml.message(
                "\u26a0\ufe0f Your data deletion completed with some errors. "
                "Most of your data has been removed, but please contact "
                "privacy@familybrain.co to confirm full deletion.\n\n"
                f"Details: {error_summary}"
            )
            logger.error(
                "Data deletion partial failure for %s: %s",
                from_number, error_summary,
            )
        else:
            twiml.message(
                "Done. All your FamilyBrain data has been deleted. "
                "You can start fresh by sending any message."
            )
            logger.info(
                "Data deletion completed successfully for %s (family_id=%s). "
                "Deleted: %s",
                from_number, family_id, deletion_results["deleted"],
            )

        # Clear any other in-memory state for this phone number
        _conversation_history.pop(from_number, None)
        _pending_deletes.pop(from_number, None)
        _pending_edits.pop(from_number, None)
        _pending_recurring_events.pop(from_number, None)
        _pending_category_prompt.pop(from_number, None)
        _last_stored_memory.pop(from_number, None)
        _doc_count.pop(from_number, None)
        _phone_cache.pop(from_number.replace("whatsapp:", "").strip(), None)

        return Response(str(twiml), mimetype="application/xml")

    else:
        # User replied something other than DELETE — cancel
        del _pending_data_deletion[from_number]
        twiml.message("No problem \u2014 your data has been kept.")
        logger.info(
            "Data deletion CANCELLED by %s (replied: %r)", from_number, text_stripped
        )
        return Response(str(twiml), mimetype="application/xml")


# ---------------------------------------------------------------------------
# Text message handler
# ---------------------------------------------------------------------------
def _handle_text_message(text: str, family_name: str, from_number: str) -> Response:
    """Process a plain text WhatsApp message, routing to query or capture."""
    _family_id = _get_family_id_for_phone(from_number)
    if not text:
        twiml = MessagingResponse()
        twiml.message(
            "Hi! Send me a question, a thought, a photo, or a PDF and I'll either answer it or store it in the Family Brain."
        )
        return Response(str(twiml), mimetype="application/xml")

    logger.info("Processing text message from %s (%s): %d chars", family_name, from_number, len(text))
    
    # --- Google Calendar Connect Command ---
    text_lower = text.lower().strip()
    if text_lower in ("/connect calendar", "/setup calendar", "connect calendar", "setup calendar", "/connect", "/connect google", "connect google calendar") or text_lower.startswith("/connect cal") or text_lower.startswith("/setup cal"):
        gcal_url, webcal_url = _send_gcal_connect_link(from_number.replace("whatsapp:", ""), _family_id)
        twiml = MessagingResponse()
        if gcal_url:
            # Send as separate messages so each link is unambiguous in WhatsApp
            twiml.message(
                "Option 1 - Google Calendar (Android & iPhone, two-way sync):\n"
                + gcal_url + "\n"
                + "Sign in with Google. Events sync both ways. Link expires in 1 hour."
            )
            if webcal_url:
                import re as _re
                subscribe_url = webcal_url.replace("webcal://", "https://", 1)
                subscribe_url = _re.sub(r"/calendar/feed/(.+)\.ics$", r"/calendar/subscribe/\1", subscribe_url)
                twiml.message(
                    "Option 2 - Apple Calendar (iPhone, one-tap):\n"
                    + subscribe_url + "\n"
                    + "Tap the link on your iPhone. Opens a page with a Subscribe button - no login needed."
                )
            twiml.message(
                "For families with children: by connecting your calendar you confirm parental consent "
                "to share events involving under-18s with FamilyBrain. Stored securely, never shared outside your family."
            )
        else:
            twiml.message("Sorry, could not generate your calendar link right now. Please try again.")
        return Response(str(twiml), mimetype="application/xml")

    # --- GDPR Data Deletion Command ---
    if text_lower in ("/delete-my-data", "/deletemydata", "/gdpr-delete", "/delete my data"):
        return _handle_delete_my_data_command(from_number, _family_id)

    # --- SOS / Emergency File Command ---
    if text_lower in ("/sos", "/emergency", "/ifanythinghappens"):
        return _handle_sos_command(from_number, family_name, _family_id)

    # --- History Command ---
    if text_lower in ("/history", "history", "what have you done", "show history", "what did you do this week"):
        twiml = MessagingResponse()
        try:
            cutoff_7d = datetime.now(pytz.UTC) - timedelta(days=7)
            db = brain._supabase
            if db:
                result = db.table("cortex_actions") \
                    .select("action_type") \
                    .eq("family_id", _family_id) \
                    .gte("created_at", cutoff_7d.isoformat()) \
                    .execute()
                rows = result.data or []
            else:
                rows = []

            counts: dict[str, int] = {}
            for row in rows:
                at = row.get("action_type", "other")
                counts[at] = counts.get(at, 0) + 1

            lines = ["Here's what I've done for your family this week:\n"]
            if counts.get("event_created"):
                lines.append(f"📅 {counts['event_created']} event{'s' if counts['event_created'] != 1 else ''} created")
            if counts.get("document_stored"):
                lines.append(f"📄 {counts['document_stored']} document{'s' if counts['document_stored'] != 1 else ''} stored")
            if counts.get("school_email_processed"):
                lines.append(f"📚 {counts['school_email_processed']} school email{'s' if counts['school_email_processed'] != 1 else ''} processed")
            if counts.get("query_answered"):
                lines.append(f"💬 {counts['query_answered']} question{'s' if counts['query_answered'] != 1 else ''} answered")
            if counts.get("alert_sent"):
                lines.append(f"⏰ {counts['alert_sent']} reminder{'s' if counts['alert_sent'] != 1 else ''} sent")
            if counts.get("briefing_sent"):
                lines.append(f"🌅 {counts['briefing_sent']} briefing{'s' if counts['briefing_sent'] != 1 else ''} sent")
            if counts.get("memory_stored"):
                lines.append(f"🧠 {counts['memory_stored']} memor{'ies' if counts['memory_stored'] != 1 else 'y'} stored")

            if len(lines) == 1:
                lines.append("Nothing logged yet this week. Start chatting to build your family's memory!")

            twiml.message("\n".join(lines))
        except Exception as exc:
            logger.error("Failed to generate history: %s", exc)
            twiml.message("⚠️ Sorry, I couldn't retrieve your history right now.")
        return Response(str(twiml), mimetype="application/xml")

    # --- Kitchen Calendar Link Command ---
    if text_lower in ("/calendar", "calendar", "show calendar", "family calendar"):
        twiml = MessagingResponse()
        try:
            token = _get_or_create_calendar_token(_family_id)
            if token:
                base_url = os.environ.get("FAMILYBRAIN_BASE_URL", "https://cortex-production-eb84.up.railway.app").rstrip("/")
                calendar_url = f"{base_url}/calendar/{token}"
                twiml.message(
                    f"Here's your family calendar: {calendar_url}\n"
                    "Bookmark this link \u2014 it always shows your latest events."
                )
            else:
                twiml.message("\u26a0\ufe0f Sorry, I couldn't generate your calendar link right now. Please try again.")
        except Exception as exc:
            logger.error("Failed to generate calendar link: %s", exc)
            twiml.message("\u26a0\ufe0f Sorry, something went wrong generating your calendar link.")
        return Response(str(twiml), mimetype="application/xml")
        
    # --- Auto-prompt for new users ---
    # If this is their first message (no conversation history) and they don't have a calendar connected
    if from_number not in _conversation_history:
        try:
            db = brain._supabase
            if db:
                # Check if family has a token
                fam_res = db.table("families").select("google_refresh_token").eq("family_id", _family_id).execute()
                has_token = False
                if fam_res.data and fam_res.data[0].get("google_refresh_token"):
                    has_token = True
                    
                if not has_token:
                    # Check if we already sent them a link recently to avoid spamming
                    recent_tokens = db.table("gcal_connect_tokens").select("token").eq("phone", from_number.replace("whatsapp:", "")).execute()
                    if not recent_tokens.data:
                        # Generate hybrid calendar links (Google OAuth + webcal subscription)
                        gcal_url, webcal_url = _send_gcal_connect_link(
                            from_number.replace("whatsapp:", ""), _family_id
                        )

                        if gcal_url:
                            welcome_lines = [
                                "👋 Welcome to FamilyBrain! I'm your family's memory and calendar assistant.",
                                "",
                                "To get the most out of me, connect your family calendar:",
                                "",
                                "*Option 1 — Google Calendar (Android & iPhone, two-way sync):*",
                                gcal_url,
                                "Sign in with Google — events sync both ways. Link expires in 1 hour.",
                            ]
                            if webcal_url:
                                import re as _re2
                                _sub_url = webcal_url.replace("webcal://", "https://", 1)
                                _sub_url = _re2.sub(r"/calendar/feed/(.+)\.ics$", r"/calendar/subscribe/\1", _sub_url)
                                welcome_lines += [
                                    "",
                                    "*Option 2 — Apple Calendar (iPhone, one-tap subscribe):*",
                                    _sub_url,
                                    "Tap the link above — it opens a page that subscribes you to the family calendar instantly. Read-only, no login needed.",
                                ]
                            welcome_lines += [
                                "",
                                "You can also just start chatting — send me anything to remember, or ask me a question.",
                                "",
                                "📌 *For families with children:* By connecting your calendar you confirm you have parental consent to share any events involving under-18s with FamilyBrain. Events are stored securely and never shared outside your family.",
                                "",
                                "🔞 *FamilyBrain is for adults (18+) only. By using this service you confirm you are 18 or over.*",
                            ]
                            welcome_body = "\n".join(welcome_lines)

                            # We don't return here — normal processing of their first message continues
                            from twilio.rest import Client as TwilioClient
                            _s = get_settings()
                            if _s.twilio_account_sid and _s.twilio_auth_token:
                                twilio_client = TwilioClient(_s.twilio_account_sid, _s.twilio_auth_token)
                                twilio_client.messages.create(
                                    from_=_s.twilio_whatsapp_from,
                                    to=from_number,
                                    body=welcome_body,
                                )
        except Exception as exc:
            logger.warning("Failed to auto-prompt for calendar connection: %s", exc)

    # --- GDPR data deletion confirmation (must run before memory management) ---
    deletion_confirm_result = _handle_data_deletion_confirmation(text, from_number)
    if deletion_confirm_result is not None:
        return deletion_confirm_result

    # --- Memory management commands (delete, edit, list) ---
    mem_mgmt_result = _handle_memory_management(text, family_name, from_number)
    if mem_mgmt_result is not None:
        return mem_mgmt_result
    # --- Early interception for recurring events ---
    # If the message contains recurring event keywords, bypass query detection
    # and go straight to capture/event detection.
    recurring_keywords = ["every", "weekly", "monthly", "fortnightly", "weekdays", "weekends"]
    is_recurring_message = any(kw in text.lower() for kw in recurring_keywords)

    # --- Intent detection: Query vs Capture ---
    if not is_recurring_message and _is_query(text, from_number):
        history = _conversation_history.get(from_number, [])
        return _answer_query(text, from_number, conversation_history=history)

    # --- Default to capture flow ---
    twiml = MessagingResponse()
    try:
        # Step 1: Extract metadata via LLM
        extracted = brain.extract_metadata(text)
        cleaned_content: str = extracted.pop("cleaned_content", text)

        # Enrich with WhatsApp-specific context
        extracted["whatsapp_from"] = from_number
        extracted["source"] = "whatsapp"
        extracted["source_user"] = family_name.lower()
        extracted["family_member"] = family_name

        # Step 2: Generate embedding
        embedding = brain.generate_embedding(cleaned_content)

        # Step 3: Store in Supabase
        record = brain.store_memory(
            content=cleaned_content,
            embedding=embedding,
            metadata=extracted,
            family_id=_family_id,
        )
        memory_id = record.get("id", "n/a")
        tags: list[str] = extracted.get("tags", [])
        category: str = extracted.get("category", "other")
        action_items: list[str] = extracted.get("action_items", [])

        # Step 4: Check for schedulable events
        event_info = ""
        event_data = _detect_event(text, family_name)
        if event_data and event_data.get("event_date"):
            # Re-store memory with resolved date in content so semantic search works
            resolved_date = event_data.get("event_date", "")
            event_name = event_data.get("event_name", "")
            event_time = event_data.get("event_time", "")
            event_member = event_data.get("family_member", family_name)
            end_time_str = event_data.get("end_time", "")
            if event_time and end_time_str:
                time_str = f" from {event_time} to {end_time_str}"
            elif event_time:
                time_str = f" at {event_time}"
            else:
                time_str = ""
            resolved_content = f"{event_member} has {event_name} on {resolved_date}{time_str}."
            # Update the stored memory with the resolved content and new embedding
            try:
                new_embedding = brain.generate_embedding(resolved_content)
                brain._supabase.table("memories").update(
                    {"content": resolved_content, "embedding": new_embedding}
                ).eq("id", memory_id).execute()
                logger.info("Memory content updated with resolved date: %s", resolved_content)
            except Exception as exc:
                logger.warning("Failed to update memory with resolved date: %s", exc)
            
            # Check if it's a recurring event
            if event_data.get("is_recurring"):
                _pending_recurring_events[from_number] = event_data
                
                day_str = event_data.get("recurrence_day", "").capitalize()
                rule = event_data.get("recurrence_rule", "")
                
                if rule == "WEEKLY" and day_str:
                    freq_str = f"Every {day_str}"
                elif rule == "BIWEEKLY" and day_str:
                    freq_str = f"Every other {day_str}"
                elif rule == "MONTHLY":
                    freq_str = "Monthly"
                elif rule == "WEEKDAYS":
                    freq_str = "Every weekday"
                elif rule == "WEEKENDS":
                    freq_str = "Every weekend"
                else:
                    freq_str = "Recurring"
                    
                end_str = "Ongoing"
                if event_data.get("recurrence_end"):
                    end_str = f"Until {event_data.get('recurrence_end')}"
                elif event_data.get("recurrence_count"):
                    end_str = f"For {event_data.get('recurrence_count')} occurrences"
                    
                display_name = event_name
                if event_member and event_member.lower() != "family":
                    display_name = f"{event_name} ({event_member})"
                    
                reply = (
                    f"📅 Got it! I'll add {display_name} as a recurring event:\n"
                    f"🔁 {freq_str}{time_str}\n"
                    f"{end_str}\n\n"
                    f"Reply YES to confirm, or tell me if anything needs changing."
                )
                twiml.message(reply)
                return Response(str(twiml), mimetype="application/xml")
            else:
                # Store in family_events and push to Google Calendar
                event_id, conflict_warning = _check_conflicts_and_store_event(event_data, family_name, family_id=_family_id)
                if event_id:
                    event_info = f"\n📅 Added to calendar: {event_name} on {resolved_date}"
                    log_action(_family_id, 'event_created', subject=f"{event_name} {resolved_date}", detail={'event_id': event_id, 'event_date': resolved_date, 'family_member': event_member}, phone_number=from_number)
                if conflict_warning:
                    event_info += f"\n\n{conflict_warning}"
        # Step 5: Classify into emergency category
        emergency_cat = _map_doc_type_to_emergency_category(
            extracted.get("category", "other"), cleaned_content
        )
        if emergency_cat:
            try:
                db = brain._supabase
                if db:
                    result = db.table("memories").select("metadata").eq("id", memory_id).limit(1).execute()
                    if result.data:
                        current_meta = result.data[0].get("metadata") or {}
                        current_meta["emergency_category"] = emergency_cat
                        db.table("memories").update({"metadata": current_meta}).eq("id", memory_id).execute()
                        logger.info("Emergency category %s auto-set on memory %s", emergency_cat, memory_id)
            except Exception as exc:
                logger.warning("Failed to auto-set emergency_category on text memory: %s", exc)

        # Track last stored memory and doc count for progress/category prompts
        _last_stored_memory[from_number] = memory_id
        _doc_count[from_number] = _doc_count.get(from_number, 0) + 1

        # Step 6: Build TwiML confirmation reply (clean, no raw IDs)
        tags_str = ", ".join(tags) if tags else "none"
        reply = (
            f"\u2705 Got it, {family_name}! Stored under {category}."
            f"{event_info}"
        )
        twiml.message(reply)
        logger.info("Text memory captured by %s (id=%s)", family_name, memory_id)

        # Step 7: If category unclear, prompt user to clarify
        if not emergency_cat:
            _pending_category_prompt[from_number] = True
            try:
                from twilio.rest import Client as _TwilioClient
                _s = get_settings()
                if _s.twilio_account_sid and _s.twilio_auth_token:
                    _tc = _TwilioClient(_s.twilio_account_sid, _s.twilio_auth_token)
                    _tc.messages.create(
                        from_=_s.twilio_whatsapp_from,
                        to=from_number,
                        body=(
                            "\u2705 Saved! Which category is this for your emergency file?\n"
                            "Reply with a number:\n"
                            "1\ufe0f\u20e3 Legal Docs\n"
                            "2\ufe0f\u20e3 Bank/Finance\n"
                            "3\ufe0f\u20e3 Insurance\n"
                            "4\ufe0f\u20e3 Pensions\n"
                            "5\ufe0f\u20e3 Bills/Debts\n"
                            "6\ufe0f\u20e3 Assets/Car\n"
                            "7\ufe0f\u20e3 Contacts\n"
                            "8\ufe0f\u20e3 Funeral Wishes\n"
                            "9\ufe0f\u20e3 Digital Legacy\n"
                            "\U0001f51f Family/Medical"
                        ),
                    )
            except Exception as exc:
                logger.warning("Failed to send category prompt: %s", exc)

        # Step 8: Every 3rd document, check coverage and send progress update
        if _doc_count.get(from_number, 0) % 3 == 0:
            _send_emergency_progress_update(from_number, _family_id)

    except Exception as exc:
        logger.error("Failed to capture text memory: %s\n%s", exc, traceback.format_exc())
        twiml.message(f"⚠️ Failed to capture memory: {exc}")

    return Response(str(twiml), mimetype="application/xml")


# ---------------------------------------------------------------------------
# Media message handler (images and PDFs)
# ---------------------------------------------------------------------------
def _handle_media_message(
    media_url: str,
    mime_type: str,
    caption: str,
    family_name: str,
    from_number: str,
) -> Response:
    """Process a WhatsApp message that contains an image or PDF attachment.
    Downloads the media from Twilio (authenticated), runs OCR or PDF
    extraction, then stores the result via the same brain pipeline used
    by the Telegram capture layer.
    """
    _family_id = _get_family_id_for_phone(from_number)
    twiml = MessagingResponse()
    logger.info(
        "Processing media from %s (%s): mime=%s", family_name, from_number, mime_type
    )

    try:
        # --- Download media from Twilio ---
        auth = (settings.twilio_account_sid, settings.twilio_auth_token)
        media_resp = http_requests.get(media_url, auth=auth, timeout=60)
        media_resp.raise_for_status()
        media_bytes: bytes = media_resp.content

        # --- Extract text based on MIME type ---
        extracted_text = ""
        source_type = "whatsapp-media"

        if "pdf" in mime_type.lower():
            logger.info("Extracting text from PDF (%d bytes)", len(media_bytes))
            extracted_text = _extract_text_from_pdf(media_bytes)
            source_type = "whatsapp-pdf"

        elif mime_type.lower().startswith("image/"):
            logger.info("Running OCR on image (%d bytes)", len(media_bytes))
            extracted_text = _extract_text_from_image(media_bytes)
            source_type = "whatsapp-photo"

        elif mime_type.lower().startswith("audio/"):
            logger.info("Transcribing audio (%d bytes)", len(media_bytes))
            try:
                from openai import OpenAI
                client = OpenAI(
                    api_key=settings.openai_api_key,
                    base_url=settings.openai_base_url,
                )
                
                # Determine extension based on mime type
                ext = ".ogg"
                if "mp4" in mime_type: ext = ".mp4"
                elif "mpeg" in mime_type: ext = ".mp3"
                elif "webm" in mime_type: ext = ".webm"
                elif "amr" in mime_type: ext = ".amr"
                
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as temp_audio:
                    temp_audio.write(media_bytes)
                    temp_audio.flush()
                    
                    with open(temp_audio.name, "rb") as audio_file:
                        transcript_response = client.audio.transcriptions.create(
                            model="whisper-1",
                            file=audio_file
                        )
                        extracted_text = transcript_response.text
                
                import os
                try:
                    os.unlink(temp_audio.name)
                except Exception:
                    pass
                    
                source_type = "whatsapp-voice"
                
            except Exception as exc:
                logger.error("Audio transcription failed: %s", exc)
                twiml.message(f"⚠️ Failed to transcribe voice note: {exc}")
                return Response(str(twiml), mimetype="application/xml")

        else:
            logger.warning("Unsupported media type: %s", mime_type)
            twiml.message(
                f"⚠️ I don't know how to process this file type ({mime_type}). "
                "Please send a photo, PDF, or voice note."
            )
            return Response(str(twiml), mimetype="application/xml")

        if not extracted_text:
            twiml.message(
                "⚠️ Could not extract any text from the media. "
                "Try sending a clearer image or type the content as a message."
            )
            return Response(str(twiml), mimetype="application/xml")

        # Combine caption (if any) with extracted text
        full_text = f"{caption}\n\n{extracted_text}" if caption else extracted_text

        # Truncate very long documents before sending to the LLM
        if len(full_text) > 8000:
            full_text = full_text[:8000] + "\n\n[... truncated for processing ...]"

        # --- Classify and extract structured metadata ---
        metadata = _extract_document_metadata(full_text)
        cleaned_content: str = metadata.pop("cleaned_content", full_text[:4000])
        doc_type: str = metadata.get("document_type", "other")
        key_fields: dict[str, Any] = metadata.get("key_fields", {})

        # Enrich with WhatsApp-specific context
        metadata["whatsapp_from"] = from_number
        metadata["source"] = source_type
        metadata["source_user"] = family_name.lower()
        metadata["family_member"] = family_name
        metadata["document_type"] = doc_type

        # --- Enrich content, generate embedding, and store memory ---
        enriched_content = _enrich_content_with_key_fields(
            cleaned_content, key_fields, doc_type
        )
        embedding = brain.generate_embedding(enriched_content)
        record = brain.store_memory(
            content=enriched_content,
            embedding=embedding,
            metadata=metadata,
            family_id=_family_id,
        )
        memory_id = record.get("id", "n/a")

        # --- Route financial details to recurring_bills table ---
        financial_summary = _maybe_store_financial_details(
            doc_type, key_fields, cleaned_content, family_name
        )

        # --- Build summary of key fields (up to 8, prioritise bank/sort) ---
        key_summary = ""
        if key_fields:
            priority_keys = {"bank_account_number", "sort_code"}
            priority_items = [(k, v) for k, v in key_fields.items() if k in priority_keys]
            other_items = [(k, v) for k, v in key_fields.items() if k not in priority_keys]
            combined = other_items[:8] + [i for i in priority_items if i not in other_items[:8]]
            key_lines = [f"  • {k}: {v}" for k, v in combined[:8]]
            key_summary = "\n" + "\n".join(key_lines)

        financial_note = f"\n{financial_summary}" if financial_summary else ""

        # --- Emergency category tagging ---
        emergency_cat = _map_doc_type_to_emergency_category(doc_type, enriched_content)
        if emergency_cat:
            try:
                db = brain._supabase
                if db:
                    result = db.table("memories").select("metadata").eq("id", memory_id).limit(1).execute()
                    if result.data:
                        current_meta = result.data[0].get("metadata") or {}
                        current_meta["emergency_category"] = emergency_cat
                        db.table("memories").update({"metadata": current_meta}).eq("id", memory_id).execute()
                        logger.info("Emergency category %s auto-set on media memory %s", emergency_cat, memory_id)
            except Exception as exc:
                logger.warning("Failed to auto-set emergency_category on media memory: %s", exc)

        # Track last stored memory and doc count
        _last_stored_memory[from_number] = memory_id
        _doc_count[from_number] = _doc_count.get(from_number, 0) + 1

        if source_type == "whatsapp-voice":
            transcript_preview = extracted_text[:200] + ("..." if len(extracted_text) > 200 else "")
            reply = (
                f"🎙️ Voice note transcribed and captured!\n\n"
                f"Transcript: {transcript_preview}\n\n"
                f"✅ {doc_type} stored (ID: {memory_id})"
            )
        else:
            reply = (
                f"✅ {doc_type.capitalize()} document captured!\n\n"
                f"👤 Captured by: {family_name}\n"
                f"📄 Type: {doc_type}\n"
                f"🏷 Tags: {', '.join(metadata.get('tags', [])) or 'none'}\n"
                f"🆔 ID: {memory_id}"
                f"{key_summary}"
                f"{financial_note}"
            )
        twiml.message(reply)
        logger.info(
            "Media memory captured by %s (type=%s, id=%s)", family_name, doc_type, memory_id
        )
        log_action(_family_id, 'document_stored', subject=f"{doc_type} {source_type}", detail={'memory_id': memory_id, 'doc_type': doc_type, 'source_type': source_type, 'tags': metadata.get('tags', [])}, phone_number=from_number)

        # --- Category prompt if unclear ---
        if not emergency_cat and source_type != "whatsapp-voice":
            _pending_category_prompt[from_number] = True
            try:
                from twilio.rest import Client as _TwilioClient
                _s = get_settings()
                if _s.twilio_account_sid and _s.twilio_auth_token:
                    _tc = _TwilioClient(_s.twilio_account_sid, _s.twilio_auth_token)
                    _tc.messages.create(
                        from_=_s.twilio_whatsapp_from,
                        to=from_number,
                        body=(
                            "\u2705 Saved! Which category is this for your emergency file?\n"
                            "Reply with a number:\n"
                            "1\ufe0f\u20e3 Legal Docs\n"
                            "2\ufe0f\u20e3 Bank/Finance\n"
                            "3\ufe0f\u20e3 Insurance\n"
                            "4\ufe0f\u20e3 Pensions\n"
                            "5\ufe0f\u20e3 Bills/Debts\n"
                            "6\ufe0f\u20e3 Assets/Car\n"
                            "7\ufe0f\u20e3 Contacts\n"
                            "8\ufe0f\u20e3 Funeral Wishes\n"
                            "9\ufe0f\u20e3 Digital Legacy\n"
                            "\U0001f51f Family/Medical"
                        ),
                    )
            except Exception as exc:
                logger.warning("Failed to send category prompt for media: %s", exc)

        # --- Progress update every 3rd document ---
        if _doc_count.get(from_number, 0) % 3 == 0:
            _send_emergency_progress_update(from_number, _family_id)

    except Exception as exc:
        logger.error("Failed to process media: %s\n%s", exc, traceback.format_exc())
        twiml.message(f"\u26a0\ufe0f Failed to process media: {exc}")

    return Response(str(twiml), mimetype="application/xml")



# ---------------------------------------------------------------------------
# Emergency file helpers: progress tracking and /sos command
# ---------------------------------------------------------------------------

def _send_emergency_progress_update(from_number: str, family_id: str) -> None:
    """After every 3rd document, send a progress update on emergency file coverage."""
    try:
        db = brain._supabase
        if not db:
            return

        # Fetch all memories for this family with an emergency_category set
        result = db.table("memories") \
            .select("metadata") \
            .contains("metadata", {"family_id": family_id}) \
            .order("created_at", desc=True) \
            .limit(1000) \
            .execute()

        covered_categories: set[str] = set()
        for row in (result.data or []):
            meta = row.get("metadata") or {}
            cat = meta.get("emergency_category")
            if cat:
                cat_str = str(cat)
                if cat_str.isdigit() and 1 <= int(cat_str) <= 10:
                    covered_categories.add(cat_str)

        n_covered = len(covered_categories)
        if n_covered >= 10:
            return  # All sections covered, no need to prompt

        missing_cats = [
            _EMERGENCY_CATEGORY_NAMES.get(str(i), str(i))
            for i in range(1, 11)
            if str(i) not in covered_categories
        ]
        missing_str = ", ".join(missing_cats)

        msg = (
            f"\U0001f4cb You've covered {n_covered}/10 sections of your family emergency file. "
            f"Missing: {missing_str}.\n"
            "Send /sos when you're ready to generate your full 'If Anything Happens' PDF."
        )

        from twilio.rest import Client as _TwilioClient
        _s = get_settings()
        if _s.twilio_account_sid and _s.twilio_auth_token:
            _tc = _TwilioClient(_s.twilio_account_sid, _s.twilio_auth_token)
            _tc.messages.create(
                from_=_s.twilio_whatsapp_from,
                to=from_number,
                body=msg,
            )
            logger.info("Emergency progress update sent to %s (%d/10 covered)", from_number, n_covered)
    except Exception as exc:
        logger.warning("Failed to send emergency progress update: %s", exc)


def _handle_sos_command(from_number: str, family_name: str, family_id: str) -> Response:
    """Handle /sos, /emergency, /ifanythinghappens — generate and send the emergency PDF."""
    twiml = MessagingResponse()
    twiml.message("\u23f3 Generating your family emergency file... This may take a moment.")

    try:
        from twilio.rest import Client as _TwilioClient
        _s = get_settings()
        twilio_client = _TwilioClient(_s.twilio_account_sid, _s.twilio_auth_token)

        # Step 1: Generate the PDF
        from .emergency_pdf import generate_emergency_pdf, CATEGORIES
        pdf_bytes = generate_emergency_pdf(family_id)

        # Check if PDF has meaningful content
        db = brain._supabase
        item_count = 0
        cat_count = 0
        if db:
            try:
                result = db.table("memories") \
                    .select("metadata") \
                    .contains("metadata", {"family_id": family_id}) \
                    .limit(1000) \
                    .execute()
                covered_cats: set[str] = set()
                for row in (result.data or []):
                    meta = row.get("metadata") or {}
                    cat = meta.get("emergency_category")
                    if cat:
                        item_count += 1
                        covered_cats.add(str(cat))
                cat_count = len(covered_cats)
            except Exception as exc:
                logger.warning("SOS: failed to count items: %s", exc)

        if item_count < 3:
            twiml.message(
                "\U0001f4cb Your emergency file is mostly empty. "
                "Start by sending me key documents \u2014 insurance policies, NHS numbers, "
                "passport details \u2014 and I'll build it up automatically."
            )
            return Response(str(twiml), mimetype="application/xml")

        # Step 2: Save PDF to temp file
        import tempfile
        date_str = datetime.now().strftime("%Y%m%d")
        storage_path = f"{family_id}/emergency_{date_str}.pdf"

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        # Step 3: Upload to Supabase storage
        signed_url = None
        try:
            if db:
                # Ensure bucket exists
                try:
                    db.storage.create_bucket("emergency-pdfs", options={"public": False})
                except Exception:
                    pass  # Bucket already exists

                # Upload the file
                with open(tmp_path, "rb") as f:
                    upload_result = db.storage.from_("emergency-pdfs").upload(
                        path=storage_path,
                        file=f,
                        file_options={"content-type": "application/pdf", "upsert": "true"},
                    )
                logger.info("Emergency PDF uploaded to Supabase storage: %s", storage_path)

                # Get signed URL (valid 24 hours = 86400 seconds)
                signed_result = db.storage.from_("emergency-pdfs").create_signed_url(
                    path=storage_path,
                    expires_in=86400,
                )
                signed_url = signed_result.get("signedURL") or signed_result.get("signed_url")
                if not signed_url and isinstance(signed_result, dict):
                    # Try nested structure
                    signed_url = signed_result.get("data", {}).get("signedURL")
                logger.info("Signed URL generated for emergency PDF")
        except Exception as exc:
            logger.error("Failed to upload emergency PDF to Supabase: %s", exc)
        finally:
            try:
                import os as _os
                _os.unlink(tmp_path)
            except Exception:
                pass

        # Step 4: Send via Twilio (with media URL if available)
        if signed_url:
            try:
                twilio_client.messages.create(
                    from_=_s.twilio_whatsapp_from,
                    to=from_number,
                    media_url=[signed_url],
                    body="Your family emergency file is attached.",
                )
                logger.info("Emergency PDF sent via Twilio media to %s", from_number)
            except Exception as exc:
                logger.warning("Failed to send PDF via Twilio media (will send text fallback): %s", exc)
                # Fallback: send just the URL as text
                twilio_client.messages.create(
                    from_=_s.twilio_whatsapp_from,
                    to=from_number,
                    body=f"Your family emergency file is ready. Download it here (valid 24h): {signed_url}",
                )
        else:
            # No signed URL — inform the user
            twilio_client.messages.create(
                from_=_s.twilio_whatsapp_from,
                to=from_number,
                body=(
                    "\u26a0\ufe0f Your emergency file was generated but couldn't be uploaded. "
                    "Please try again in a moment."
                ),
            )

        # Step 5: Send follow-up summary
        followup_msg = (
            f"\u2705 Your emergency file is ready! "
            f"{item_count} item{'s' if item_count != 1 else ''} across {cat_count} "
            f"categor{'ies' if cat_count != 1 else 'y'}. "
            "Keep it somewhere safe \u2014 and update it whenever something changes."
        )
        twilio_client.messages.create(
            from_=_s.twilio_whatsapp_from,
            to=from_number,
            body=followup_msg,
        )
        logger.info(
            "Emergency PDF flow complete for %s: %d items, %d categories",
            family_name, item_count, cat_count,
        )

    except Exception as exc:
        logger.error("SOS command failed: %s\n%s", exc, traceback.format_exc())
        twiml.message(
            "\u26a0\ufe0f Sorry, I couldn't generate your emergency file right now. "
            "Please try again in a moment."
        )

    return Response(str(twiml), mimetype="application/xml")


# ---------------------------------------------------------------------------
# Briefing and Deduplication Helpers
# ---------------------------------------------------------------------------
def _is_quiet_hours() -> bool:
    """Return True if current time in Europe/London is between 21:00 and 07:00."""
    tz = pytz.timezone("Europe/London")
    now = datetime.now(tz)
    return now.hour >= 21 or now.hour < 7

def _was_briefing_sent(family_id: str, briefing_type: str, content_hash: str, within_hours: int = 24) -> bool:
    """Check if an identical briefing was sent recently."""
    db = brain._supabase
    if not db:
        return False
    try:
        cutoff = datetime.now(pytz.UTC) - timedelta(hours=within_hours)
        res = db.table("cortex_briefings").select("id").eq("family_id", family_id).eq("briefing_type", briefing_type).eq("content_hash", content_hash).gte("delivered_at", cutoff.isoformat()).limit(1).execute()
        return bool(res.data)
    except Exception as exc:
        logger.warning("Failed to check briefing deduplication: %s", exc)
        return False

def _log_briefing(family_id: str, briefing_type: str, content_hash: str) -> None:
    """Log a sent briefing to prevent duplicates."""
    db = brain._supabase
    if not db:
        return
    try:
        db.table("cortex_briefings").insert({
            "family_id": family_id,
            "briefing_type": briefing_type,
            "content_hash": content_hash
        }).execute()
    except Exception as exc:
        logger.warning("Failed to log briefing: %s", exc)

def _get_content_hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8')).hexdigest()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _send_daily_expiry_alerts() -> None:
    """Scan all memories for dates expiring today or within 7 days and send proactive WhatsApp alerts."""
    try:
        from datetime import date, timedelta
        import re as _re
        settings = get_settings()
        today = date.today()
        window_end = today + timedelta(days=7)
        # Fetch all families
        db_client = brain._supabase
        if db_client is None:
            return
        families_result = db_client.table("families").select("family_id, primary_phone, primary_name").eq("status", "active").execute()
        families = families_result.data or []
        # Date patterns to scan for: DD/MM/YYYY, DD-MM-YYYY, DD Month YYYY, YYYY-MM-DD
        date_patterns = [
            (r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', lambda m: date(int(m.group(3)), int(m.group(2)), int(m.group(1)))),
            (r'(\d{1,2})\s+(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+(\d{4})',
             lambda m: date(int(m.group(3)), {'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12}[m.group(2)[:3].lower()], int(m.group(1)))),
            (r'(\d{4})-(\d{2})-(\d{2})', lambda m: date(int(m.group(1)), int(m.group(2)), int(m.group(3)))),
        ]
        expiry_keywords = ('expir', 'renew', 'end', 'due', 'valid until', 'contract', 'mot', 'insurance', 'warranty', 'subscription')
        for family in families:
            family_id = family['family_id']
            primary_phone = family['primary_phone']
            family_name = family['primary_name']
            # Get recent memories for this family
            memories = brain.list_recent_memories(limit=100, family_id=family_id)
            alerts = []
            for mem in memories:
                content = mem.get('content', '')
                content_lower = content.lower()
                if not any(kw in content_lower for kw in expiry_keywords):
                    continue
                for pattern, parser in date_patterns:
                    for match in _re.finditer(pattern, content, _re.IGNORECASE):
                        try:
                            d = parser(match)
                            if today <= d <= window_end:
                                days_away = (d - today).days
                                label = 'TODAY' if days_away == 0 else f'in {days_away} day{"s" if days_away != 1 else ""}'
                                alerts.append((d, label, content[:200]))
                        except Exception:
                            pass
            if not alerts:
                continue
            # Deduplicate and sort by date
            seen = set()
            unique_alerts = []
            for d, label, snippet in sorted(alerts, key=lambda x: x[0]):
                key = (d, snippet[:80])
                if key not in seen:
                    seen.add(key)
                    unique_alerts.append((d, label, snippet))
            # Build message
            lines = [f"⚠️ FamilyBrain Daily Alert — {today.strftime('%d %B %Y')}\n"]
            for d, label, snippet in unique_alerts[:5]:
                lines.append(f"• {d.strftime('%d %b %Y')} ({label}): {snippet[:120]}...")
            message = "\n".join(lines)
            # Send via Twilio
            if _is_quiet_hours():
                logger.debug("Skipping daily expiry alert for %s due to quiet hours", family_id)
                continue
                
            content_hash = _get_content_hash(message)
            if _was_briefing_sent(family_id, "expiry_alert", content_hash):
                logger.debug("Skipping duplicate expiry alert for %s", family_id)
                continue

            try:
                from twilio.rest import Client as TwilioClient
                twilio_client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
                twilio_client.messages.create(
                    from_=settings.twilio_whatsapp_from,
                    to=f"whatsapp:{primary_phone}",
                    body=message,
                )
                logger.info("Daily expiry alert sent to %s (%d alerts)", primary_phone, len(unique_alerts))
                _log_briefing(family_id, "expiry_alert", content_hash)
                for _d, _label, _snippet in unique_alerts[:5]:
                    _alert_subject = _snippet[:60].strip()
                    log_action(family_id, 'alert_sent', subject=f"{_alert_subject} expiry", detail={'date': str(_d), 'label': _label}, phone_number=primary_phone)
            except Exception as exc:
                logger.warning("Failed to send daily alert to %s: %s", primary_phone, exc)
    except Exception as exc:
        logger.error("Daily expiry alert job failed: %s", exc)


# ---------------------------------------------------------------------------
# Google Calendar → WhatsApp two-way sync
# ---------------------------------------------------------------------------
# In-memory set of Google Calendar event IDs already notified this session.
# On restart the set is empty, so we rely on the look-back window (48 h) being
# short enough that truly new events still get notified while avoiding spam for
# events that were created long ago.  A Supabase-backed persistent store would
# be needed for a production deployment; the in-memory set is sufficient for a
# single-process deployment that restarts infrequently.
_gcal_notified_event_ids: set[str] = set()
# Google Calendar event IDs pushed FROM WhatsApp — skip re-notification for these
_gcal_wa_pushed_event_ids: set[str] = set()


def _poll_google_calendar_and_notify() -> None:
    """Poll Google Calendar for new/updated events and notify family members via WhatsApp.

    Runs on a background scheduler (every 15 minutes by default).  For each
    event found in the next 30 days that has not already been notified:
      1. Stores the event as a memory in Supabase (family_id=family-dan).
      2. Sends a WhatsApp notification to all registered family members.

    Event IDs are tracked in ``_gcal_notified_event_ids`` to prevent duplicate
    notifications within a single process lifetime.  On first run after a
    restart, events from the past 48 hours are also checked so that any events
    added while the process was down are not silently missed.
    """
    global _gcal_notified_event_ids
    try:
        from datetime import datetime, timedelta, timezone
        from . import google_calendar

        now_utc = datetime.now(timezone.utc)
        # Look back 48 h on first run (set is empty) so we catch events added
        # while the process was offline; otherwise only look forward.
        if _gcal_notified_event_ids:
            time_min = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            time_min = (now_utc - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
        time_max = (now_utc + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Fetch all active families to poll their calendars
        db_client = brain._supabase
        families = []
        if db_client:
            try:
                families_result = db_client.table("families").select("family_id").eq("status", "active").execute()
                families = [row["family_id"] for row in (families_result.data or [])]
            except Exception as exc:
                logger.warning("GCal sync: could not fetch families: %s", exc)
        
        if not families:
            # Fallback to default if no families found
            families = ["family-dan"]

        all_new_events = []
        for family_id in families:
            events = google_calendar.get_events(time_min=time_min, time_max=time_max, max_results=50, family_id=family_id)
            if not events:
                continue

            for e in events:
                if e.get("id") and e["id"] not in _gcal_notified_event_ids and e["id"] not in _gcal_wa_pushed_event_ids:
                    e["_family_id"] = family_id
                    all_new_events.append(e)

        if not all_new_events:
            logger.debug("Google Calendar poll: no new events found in window %s – %s", time_min, time_max)
            return

        logger.info("Google Calendar poll: %d new event(s) to process", len(all_new_events))

        # Fetch all active family members for notifications
        family_phones_by_id: dict[str, list[tuple[str, str]]] = {}
        if db_client:
            try:
                members_result = db_client.table("whatsapp_members").select(
                    "phone, name, family_id"
                ).execute()
                for row in (members_result.data or []):
                    if row.get("phone") and row.get("family_id"):
                        fid = row["family_id"]
                        if fid not in family_phones_by_id:
                            family_phones_by_id[fid] = []
                        family_phones_by_id[fid].append((row["phone"], row.get("name", "Family Member")))
            except Exception as exc:
                logger.warning("GCal sync: could not fetch family members: %s", exc)

        # Twilio client for WhatsApp notifications
        twilio_client = None
        try:
            from twilio.rest import Client as TwilioClient
            _s = get_settings()
            if _s.twilio_account_sid and _s.twilio_auth_token:
                twilio_client = TwilioClient(_s.twilio_account_sid, _s.twilio_auth_token)
        except Exception as exc:
            logger.warning("GCal sync: could not initialise Twilio client: %s", exc)

        for event in all_new_events:
            event_id = event["id"]
            event_family_id = event.get("_family_id", "family-dan")
            family_phones = family_phones_by_id.get(event_family_id, [])
            summary = event.get("summary") or "(no title)"
            start_raw = event.get("start", "")
            end_raw = event.get("end", "")
            description = event.get("description", "")
            location = event.get("location", "")

            # Parse start into a human-readable date + time
            try:
                if "T" in start_raw:
                    # Timed event — strip timezone suffix for fromisoformat compat
                    start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                    date_str = start_dt.strftime("%d %B %Y")
                    time_str = start_dt.strftime("%H:%M")
                else:
                    # All-day event
                    from datetime import date as _date
                    start_dt_date = _date.fromisoformat(start_raw)
                    date_str = start_dt_date.strftime("%d %B %Y")
                    time_str = ""
            except Exception:
                date_str = start_raw
                time_str = ""

            # 1. Store as a memory in Supabase
            try:
                content_parts = [f"Google Calendar event: {summary} on {date_str}"]
                if time_str:
                    content_parts.append(f"at {time_str}")
                if location:
                    content_parts.append(f"at {location}")
                if description:
                    content_parts.append(f"— {description[:200]}")
                memory_content = " ".join(content_parts)

                embedding = brain.generate_embedding(memory_content)
                brain.store_memory(
                    content=memory_content,
                    embedding=embedding,
                    metadata={
                        "source": "google_calendar",
                        "gcal_event_id": event_id,
                        "event_name": summary,
                        "event_date": start_raw[:10] if start_raw else "",
                        "event_time": time_str,
                        "location": location,
                        "tags": ["calendar", "event"],
                        "category": "reference",
                    },
                    family_id=event_family_id,
                )
                logger.info("GCal sync: stored memory for event '%s' (%s)", summary, event_id)
            except Exception as exc:
                logger.warning("GCal sync: failed to store memory for event '%s': %s", summary, exc)

            # 2. Send WhatsApp notification to all family members
            if twilio_client and family_phones:
                time_display = f" at {time_str}" if time_str else ""
                location_display = f" ({location})" if location else ""
                notification_body = (
                    f"\U0001f4c5 New calendar event: {summary}\n"
                    f"📆 {date_str}{time_display}{location_display}"
                )
                _s = get_settings()
                # Determine the creator's phone from the event organiser field (if present)
                creator_email = (
                    event.get("organizer", {}).get("email", "") or
                    event.get("creator", {}).get("email", "")
                )
                # Check if event is starting within 60 minutes
                is_urgent = False
                try:
                    if start_raw and "T" in start_raw:
                        start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                        now_utc = datetime.now(pytz.UTC)
                        if start_dt.tzinfo is None:
                            start_dt = pytz.UTC.localize(start_dt)
                        time_diff = (start_dt - now_utc).total_seconds() / 60
                        if 0 <= time_diff <= 60:
                            is_urgent = True
                except Exception:
                    pass

                if _is_quiet_hours() and not is_urgent:
                    logger.debug("Skipping GCal notification for %s due to quiet hours", event_family_id)
                else:
                    content_hash = _get_content_hash(notification_body)
                    if _was_briefing_sent(event_family_id, "gcal_sync", content_hash):
                        logger.debug("Skipping duplicate GCal notification for %s", event_family_id)
                    else:
                        for phone, member_name in family_phones:
                            try:
                                twilio_client.messages.create(
                                    from_=_s.twilio_whatsapp_from,
                                    to=f"whatsapp:{phone}",
                                    body=notification_body,
                                )
                                logger.info(
                                    "GCal sync: WhatsApp notification sent to %s (%s) for event '%s'",
                                    member_name, phone, summary,
                                )
                            except Exception as exc:
                                logger.warning(
                                    "GCal sync: failed to notify %s (%s): %s", member_name, phone, exc
                                )
                        _log_briefing(event_family_id, "gcal_sync", content_hash)
            else:
                logger.debug(
                    "GCal sync: skipping WhatsApp notification for '%s' "
                    "(Twilio not configured or no family members)",
                    summary,
                )

            # Mark as notified regardless of notification success to avoid
            # re-processing on the next poll cycle
            _gcal_notified_event_ids.add(event_id)

    except Exception as exc:
        logger.error("Google Calendar poll job failed: %s", exc, exc_info=True)



# ---------------------------------------------------------------------------
# Morning and Evening Briefings
# ---------------------------------------------------------------------------
def _send_morning_briefing() -> None:
    """Send a daily morning summary of events and expiring items."""
    if _is_quiet_hours():
        logger.debug("Skipping morning briefing due to quiet hours")
        return

    try:
        db = brain._supabase
        if not db:
            return
            
        family_id = "family-dan"
        tz = pytz.timezone("Europe/London")
        today_date = datetime.now(tz).date()
        today_str = today_date.isoformat()

        # --- PART 4: Check cortex_actions — skip if briefing sent in last 6 hours ---
        recent_briefings = get_recent_actions(family_id, action_type='briefing_sent', hours=6, subject_contains='morning briefing')
        if recent_briefings:
            logger.debug("Skipping morning briefing: already sent within the last 6 hours (cortex_actions)")
            return

        # Fetch today's events
        events_res = db.table("family_events").select("*").eq("event_date", today_str).execute()
        events = events_res.data or []
        
        # Sort events by time
        events.sort(key=lambda x: x.get("event_time") or "23:59")

        # --- PART 4: Fetch alert_sent actions from last 48 hours to suppress repeated alerts ---
        recent_alert_actions = get_recent_actions(family_id, action_type='alert_sent', hours=48)
        recently_alerted_subjects = set()
        for _act in recent_alert_actions:
            _subj = (_act.get('subject') or '').lower()
            if _subj:
                recently_alerted_subjects.add(_subj)

        # Fetch expiring memories
        memories = brain.list_recent_memories(limit=200, family_id=family_id)
        expiring_today = []
        
        # Simple date extraction for expiry
        import re as _re
        date_patterns = [
            (r'(\d{1,2})[/-](\d{1,2})[/-](\d{4})', lambda m: datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date()),
            (r'(\d{4})-(\d{2})-(\d{2})', lambda m: datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()),
        ]
        expiry_keywords = ('expir', 'renew', 'end', 'due', 'valid until', 'contract', 'mot', 'insurance', 'warranty', 'subscription')
        
        for mem in memories:
            content = mem.get('content', '')
            content_lower = content.lower()
            if not any(kw in content_lower for kw in expiry_keywords):
                continue
            for pattern, parser in date_patterns:
                for match in _re.finditer(pattern, content, _re.IGNORECASE):
                    try:
                        d = parser(match)
                        if d == today_date:
                            snippet = content[:100]
                            # Suppress if this alert was already sent in the last 48 hours
                            snippet_key = snippet[:60].strip().lower() + ' expiry'
                            if snippet_key not in recently_alerted_subjects:
                                expiring_today.append(snippet)
                    except Exception:
                        pass

        # Compose message
        lines = ["🌅 Good morning! Here's your family day:\n"]
        lines.append("📅 Today's events:")
        
        if events:
            for e in events:
                time_str = e.get("event_time") or "All day"
                name = e.get("event_name") or "Event"
                member = e.get("family_member") or "Family"
                lines.append(f"• [{time_str}] {name} ({member})")
        else:
            lines.append("No events today")
            
        if expiring_today:
            lines.append("\n⚠️ Expiring today:")
            for item in set(expiring_today):
                lines.append(f"• {item}")
                
        lines.append("\nHave a great day! 💙")
        message = "\n".join(lines)
        
        content_hash = _get_content_hash(message)
        if _was_briefing_sent(family_id, "morning_briefing", content_hash):
            logger.debug("Skipping duplicate morning briefing")
            return
            
        # Fetch family members
        members_res = db.table("whatsapp_members").select("phone").eq("family_id", family_id).execute()
        phones = [row["phone"] for row in (members_res.data or []) if row.get("phone")]
        
        if not phones:
            # Fallback to primary phone
            fam_res = db.table("families").select("primary_phone").eq("family_id", family_id).execute()
            if fam_res.data and fam_res.data[0].get("primary_phone"):
                phones = [fam_res.data[0]["primary_phone"]]
                
        if phones:
            from twilio.rest import Client as TwilioClient
            _s = get_settings()
            twilio_client = TwilioClient(_s.twilio_account_sid, _s.twilio_auth_token)
            
            for phone in phones:
                try:
                    twilio_client.messages.create(
                        from_=_s.twilio_whatsapp_from,
                        to=f"whatsapp:{phone}",
                        body=message,
                    )
                except Exception as exc:
                    logger.warning("Failed to send morning briefing to %s: %s", phone, exc)
                    
            _log_briefing(family_id, "morning_briefing", content_hash)
            log_action(family_id, 'briefing_sent', subject='morning briefing', detail={'events_count': len(events), 'recipients': len(phones)})
            logger.info("Morning briefing sent to %d members", len(phones))

    except Exception as exc:
        logger.error("Morning briefing failed: %s", exc)

def _send_evening_preview() -> None:
    """Send a daily evening preview of tomorrow's events."""
    if _is_quiet_hours():
        logger.debug("Skipping evening preview due to quiet hours")
        return

    try:
        db = brain._supabase
        if not db:
            return
            
        family_id = "family-dan"
        tz = pytz.timezone("Europe/London")
        tomorrow_date = datetime.now(tz).date() + timedelta(days=1)
        tomorrow_str = tomorrow_date.isoformat()

        # --- PART 4: Check cortex_actions — skip if evening preview sent in last 6 hours ---
        recent_evening = get_recent_actions(family_id, action_type='briefing_sent', hours=6, subject_contains='evening preview')
        if recent_evening:
            logger.debug("Skipping evening preview: already sent within the last 6 hours (cortex_actions)")
            return

        # Fetch tomorrow's events
        events_res = db.table("family_events").select("*").eq("event_date", tomorrow_str).execute()
        events = events_res.data or []
        events.sort(key=lambda x: x.get("event_time") or "23:59")

        # Compose message
        lines = ["🌙 Evening update — here's tomorrow:\n"]
        lines.append("📅 Tomorrow's events:")
        
        prep_tip = ""
        if events:
            for e in events:
                time_str = e.get("event_time") or "All day"
                name = e.get("event_name") or "Event"
                member = e.get("family_member") or "Family"
                lines.append(f"• [{time_str}] {name} ({member})")
                
                # Generate a simple prep tip for the first morning event
                if not prep_tip and time_str != "All day" and time_str < "12:00":
                    if "swim" in name.lower():
                        prep_tip = f"💡 {member} has swimming at {time_str} — don't forget the kit!"
                    elif "school" in name.lower() or "class" in name.lower():
                        prep_tip = f"💡 {member} has {name} at {time_str} — bags packed?"
        else:
            lines.append("Nothing scheduled tomorrow")
            
        if prep_tip:
            lines.append(f"\n{prep_tip}")
            
        lines.append("\nSleep well! 🌙")
        message = "\n".join(lines)
        
        content_hash = _get_content_hash(message)
        if _was_briefing_sent(family_id, "evening_preview", content_hash):
            logger.debug("Skipping duplicate evening preview")
            return
            
        # Fetch family members
        members_res = db.table("whatsapp_members").select("phone").eq("family_id", family_id).execute()
        phones = [row["phone"] for row in (members_res.data or []) if row.get("phone")]
        
        if not phones:
            fam_res = db.table("families").select("primary_phone").eq("family_id", family_id).execute()
            if fam_res.data and fam_res.data[0].get("primary_phone"):
                phones = [fam_res.data[0]["primary_phone"]]
                
        if phones:
            from twilio.rest import Client as TwilioClient
            _s = get_settings()
            twilio_client = TwilioClient(_s.twilio_account_sid, _s.twilio_auth_token)
            
            for phone in phones:
                try:
                    twilio_client.messages.create(
                        from_=_s.twilio_whatsapp_from,
                        to=f"whatsapp:{phone}",
                        body=message,
                    )
                except Exception as exc:
                    logger.warning("Failed to send evening preview to %s: %s", phone, exc)
                    
            _log_briefing(family_id, "evening_preview", content_hash)
            log_action(family_id, 'briefing_sent', subject='evening preview', detail={'events_count': len(events), 'recipients': len(phones)})
            logger.info("Evening preview sent to %d members", len(phones))

    except Exception as exc:
        logger.error("Evening preview failed: %s", exc)

def main() -> None:
    """Start the Flask server for the WhatsApp capture layer."""
    port = int(os.environ.get("PORT", "8080"))
    logger.info("Starting Family Brain WhatsApp capture layer on port %d…", port)
    # Start background schedulers
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        alert_scheduler = BackgroundScheduler()

        # Daily expiry alerts (runs at 08:00 every day)
        alert_scheduler.add_job(
            _send_daily_expiry_alerts,
            trigger="cron",
            hour=8,
            minute=0,
            id="daily_expiry_alerts",
        )

        # Google Calendar → WhatsApp two-way sync (runs every 15 minutes)
        alert_scheduler.add_job(
            _poll_google_calendar_and_notify,
            trigger="interval",
            minutes=15,
            id="gcal_sync",
            next_run_time=__import__("datetime").datetime.now(),  # run immediately on startup
        )

        # Gmail School Email Watcher (runs every 15 minutes)
        try:
            from . import gmail_watcher
            alert_scheduler.add_job(
                gmail_watcher.poll_school_emails,
                trigger="interval",
                minutes=15,
                id="gmail_school_watcher",
                next_run_time=__import__("datetime").datetime.now() + __import__("datetime").timedelta(minutes=2),  # offset slightly from gcal sync
            )
        except Exception as exc:
            logger.warning("Could not register Gmail watcher job: %s", exc)

        # Morning Briefing (runs at 07:15 Europe/London)
        alert_scheduler.add_job(
            _send_morning_briefing,
            trigger="cron",
            hour=7,
            minute=15,
            timezone=pytz.timezone("Europe/London"),
            id="morning_briefing",
        )

        # Evening Preview (runs at 18:30 Europe/London)
        alert_scheduler.add_job(
            _send_evening_preview,
            trigger="cron",
            hour=18,
            minute=30,
            timezone=pytz.timezone("Europe/London"),
            id="evening_preview",
        )

        alert_scheduler.start()
        logger.info(
            "Schedulers started: daily expiry alerts (08:00), "
            "Google Calendar sync (every 15 min)"
        )
    except Exception as exc:
        logger.warning("Could not start schedulers: %s", exc)
    # Use threaded=True so concurrent Twilio webhooks are handled correctly
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()

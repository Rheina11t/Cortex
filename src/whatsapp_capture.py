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
from datetime import datetime
from functools import wraps
from typing import Any, Callable, Optional

import requests as http_requests
from flask import Flask, Response, request

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
                        f"  • {c.get('event_name', 'Unknown event')}{time_display} "
                        f"({c.get('family_member', 'someone')})"
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
            "source": "whatsapp",
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
        
        # For broad inventory-style queries ("what X do I have", "list my X"), use lower threshold and higher count
        broad_query_words = ("what", "list", "all", "how many", "do i have", "my cars", "my vehicles", "my policies", "my accounts")
        is_broad_query = any(w in text.lower() for w in broad_query_words)
        _threshold = 0.2 if is_broad_query else 0.3
        _count = 15 if is_broad_query else 8
        expanded_query = text + " " + " ".join(synonyms)
        results = brain.semantic_search(expanded_query, match_threshold=_threshold, match_count=_count, family_id=family_id)

        reply_text = ""

        if results:
            memories_text = "\n".join(
                f"- {r.get('content', '')}"
                for r in results
            )

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

            from datetime import date as _date
            today_str = _date.today().strftime("%d %B %Y")
            prompt = (
                f"You are Family Brain, a personal AI assistant for the {family_name} family. "
                f"The person asking this question is {family_name}. "
                f"Today's date is {today_str}. "
                "Answer the user's question based on the stored memories below. "
                "Do NOT invent details that are not in the memories or web results. "
                "If the memories contain conflicting information, use the most specific and detailed one. "
                "If the question asks about ALL items of a type (e.g. 'what cars do I have', 'list my policies'), "
                "make sure to include EVERY relevant item found in the memories, not just the first one. "
                "IMPORTANT: If any stored item contains a date that is today, tomorrow, or within the next 7 days "
                "(e.g. contract end, renewal, expiry, payment due, MOT due), you MUST start your answer with a "
                "⚠️ URGENT alert line before anything else. Example: '⚠️ URGENT: Your VW ID.Buzz contract hire ends TODAY (20 March 2026). You should contact VW Financial Services immediately.' "
                "Do not bury time-sensitive dates in the middle of a list — always lead with them. "
                "If web search results are provided, you may use them to supplement missing contact details "
                "(phone numbers, emails, opening hours) — but clearly indicate these came from a web search, not stored memory. "
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

    except Exception as exc:
        logger.error("Failed to answer query: %s\n%s", exc, traceback.format_exc())
        twiml.message(f"⚠️ Failed to answer query: {exc}")

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

    # --- Intent detection: Query vs Capture ---
    if _is_query(text, from_number):
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
        if event_data:
            event_id, conflict_warning = _check_conflicts_and_store_event(event_data, family_name)
            if event_id:
                event_info = (
                    f"\n📅 Event detected: {event_data.get('event_name', '')} "
                    f"on {event_data.get('event_date', '')}"
                )
            if conflict_warning:
                event_info += f"\n\n{conflict_warning}"

        # Step 5: Build TwiML confirmation reply
        tags_str = ", ".join(tags) if tags else "none"
        action_str = "\n  • ".join(action_items) if action_items else "none"

        reply = (
            f"✅ Memory captured by {family_name}!\n\n"
            f"📂 Category: {category}\n"
            f"🏷 Tags: {tags_str}\n"
            f"🎯 Action items: {action_str}\n"
            f"🆔 ID: {memory_id}"
            f"{event_info}"
        )
        twiml.message(reply)
        logger.info("Text memory captured by %s (id=%s)", family_name, memory_id)

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

    except Exception as exc:
        logger.error("Failed to process media: %s\n%s", exc, traceback.format_exc())
        twiml.message(f"⚠️ Failed to process media: {exc}")

    return Response(str(twiml), mimetype="application/xml")


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
            try:
                from twilio.rest import Client as TwilioClient
                twilio_client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
                twilio_client.messages.create(
                    from_=settings.twilio_whatsapp_from,
                    to=f"whatsapp:{primary_phone}",
                    body=message,
                )
                logger.info("Daily expiry alert sent to %s (%d alerts)", primary_phone, len(unique_alerts))
            except Exception as exc:
                logger.warning("Failed to send daily alert to %s: %s", primary_phone, exc)
    except Exception as exc:
        logger.error("Daily expiry alert job failed: %s", exc)


def main() -> None:
    """Start the Flask server for the WhatsApp capture layer."""
    port = int(os.environ.get("PORT", "8080"))
    logger.info("Starting Family Brain WhatsApp capture layer on port %d…", port)
    # Start daily expiry alert scheduler (runs at 8am every day)
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        alert_scheduler = BackgroundScheduler()
        alert_scheduler.add_job(
            _send_daily_expiry_alerts,
            trigger="cron",
            hour=8,
            minute=0,
            id="daily_expiry_alerts",
        )
        alert_scheduler.start()
        logger.info("Daily expiry alert scheduler started (runs at 08:00 daily)")
    except Exception as exc:
        logger.warning("Could not start alert scheduler: %s", exc)
    # Use threaded=True so concurrent Twilio webhooks are handled correctly
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()

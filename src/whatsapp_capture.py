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


def _get_family_name(phone_number: str) -> Optional[str]:
    """Return the family member name for a WhatsApp number, or None if not authorised.

    When no family members are configured the handler is open to everyone and
    returns "Unknown" so that the caller can still tag the memory.
    """
    if not FAMILY_MEMBERS:
        return "Unknown"
    return FAMILY_MEMBERS.get(phone_number)


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
  "source": "whatsapp-photo"
}

Rules:
- Return ONLY valid JSON. No markdown fences, no commentary.
- key_fields should extract the most important structured data.
- For financial documents (insurance, invoices, utilities), you MUST extract the following fields if present:
  - `provider_name`: The name of the company providing the service.
  - `policy_number`: The policy number.
  - `reference_number`: Any other reference or account number.
  - `bank_account_number`: The bank account number for payments.
  - `sort_code`: The sort code for payments.
  - `direct_debit_amount`: The amount of the direct debit.
  - `payment_frequency`: How often the payment is made (e.g., monthly, annually).
- If a field has no value, use an empty list [] or empty string "" or empty object {}.
- Keep cleaned_content as a faithful, concise summary.
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

        validator = RequestValidator(auth_token)
        # Use the full URL including query string for signature validation
        url = request.url
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
    "what", "who", "where", "when", "how", "which", "do", "does", "did", "is", "are",
    "was", "were", "can", "could", "should", "would", "have", "has",
)
_QUERY_PHRASES = (
    "do we have", "do i have", "have we got", "what is", "tell me",
    "remind me", "show me", "find", "search", "look up",
)

def _is_query(text: str) -> bool:
    """Return True if the text is likely a question/query, not something to be stored."""
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
                    "content": "Is the following user message a question/query, or is it information to be stored? Reply with only the word \'query\' or \'capture\'."
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

def _answer_query(text: str) -> Response:
    """Handle a message that has been identified as a query."""
    twiml = MessagingResponse()
    logger.info("Handling message as a query: %s", text)

    try:
        # Step 1: Perform semantic search
        results = brain.semantic_search(text, match_threshold=0.4, match_count=5)

        if not results:
            twiml.message(
                "I don't have anything stored about that yet. "
                "Send me the information and I'll remember it for next time."
            )
            return Response(str(twiml), mimetype="application/xml")

        # Step 2: Synthesise an answer from the results
        memories_str = "\n---\n".join(
            "Memory ID: {}\nContent: {}".format(r.get("id"), r.get("content"))
            for r in results
        )
        prompt = _SYNTHESIS_PROMPT.format(question=text, memories=memories_str)

        from openai import OpenAI
        client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_embedding_base_url,
        )
        response = client.chat.completions.create(
            model=settings.llm_model,
            temperature=0.1,
            messages=[
                {"role": "system", "content": "You are a helpful AI assistant for a family."},
                {"role": "user", "content": prompt}
            ],
        )
        answer = response.choices[0].message.content or "I found some information but couldn't synthesise an answer."

        # Step 3: Format and send the reply
        source_ids = ", ".join(str(r.get("id")) for r in results)
        reply = "{}\n\nSources: {}".format(answer, source_ids)

        twiml.message(reply)
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
    if not text:
        twiml = MessagingResponse()
        twiml.message(
            "Hi! Send me a question, a thought, a photo, or a PDF and I'll either answer it or store it in the Family Brain."
        )
        return Response(str(twiml), mimetype="application/xml")

    logger.info("Processing text message from %s (%s): %d chars", family_name, from_number, len(text))

    # --- Intent detection: Query vs Capture ---
    if _is_query(text):
        return _answer_query(text)

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

        else:
            logger.warning("Unsupported media type: %s", mime_type)
            twiml.message(
                f"⚠️ I don't know how to process this file type ({mime_type}). "
                "Please send a photo or PDF."
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

        # --- Generate embedding and store memory ---
        embedding = brain.generate_embedding(cleaned_content)
        record = brain.store_memory(
            content=cleaned_content,
            embedding=embedding,
            metadata=metadata,
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
def main() -> None:
    """Start the Flask server for the WhatsApp capture layer."""
    port = int(os.environ.get("PORT", "8080"))
    logger.info("Starting Family Brain WhatsApp capture layer on port %d…", port)
    # Use threaded=True so concurrent Twilio webhooks are handled correctly
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()

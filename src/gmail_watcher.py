# -*- coding: utf-8 -*-
"""Gmail School Email Watcher for Family Brain."""

import base64
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from . import brain
from . import google_calendar

logger = logging.getLogger("open_brain.gmail_watcher")

# Narrow query to find school-related emails
SCHOOL_EMAIL_QUERY = (
    "{"
    "from:school from:academy from:primary from:secondary from:arbor "
    "from:parentpay from:schoolcomms from:bromcom from:edulink from:weduc from:classdojo "
    "subject:school subject:term subject:trip subject:permission "
    "subject:\"parents evening\" subject:attendance subject:uniform"
    "}"
)

_EXTRACTION_SYSTEM_PROMPT = """\
You are a school email extraction assistant for a Family Brain system.

Given the text of a school email, you MUST return a JSON object with exactly these keys:

{
  "summary": "<a concise 1-2 sentence summary of the email>",
  "action_required": <boolean: true if payment, permission slip, or reply is needed, else false>,
  "event_name": "<name of the event if one is mentioned, else empty string>",
  "event_date": "<date of the event in YYYY-MM-DD format if mentioned, else empty string>",
  "event_time": "<time of the event in HH:MM format if mentioned, else empty string>",
  "deadline": "<deadline date in YYYY-MM-DD format if action is required, else empty string>",
  "amount_due": "<amount due if payment is required, else empty string>"
}

Rules:
- Return ONLY valid JSON. No markdown fences, no commentary.
- If a field has no value, use an empty string "" (or false for action_required).
- Keep the summary clear and actionable.
"""

def _get_gmail_service(family_id: str) -> Optional[Any]:
    """Get an authenticated Gmail API service for a family."""
    creds = google_calendar._get_credentials(family_id)
    if not creds:
        return None
    try:
        return build("gmail", "v1", credentials=creds)
    except Exception as e:
        logger.error("Failed to build Gmail service for %s: %s", family_id, e)
        return None

def _extract_email_body(payload: dict) -> str:
    """Extract plain text body from a Gmail message payload."""
    body = ""
    if "parts" in payload:
        for part in payload["parts"]:
            if part["mimeType"] == "text/plain":
                data = part["body"].get("data")
                if data:
                    body += base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            elif "parts" in part:
                body += _extract_email_body(part)
    else:
        data = payload.get("body", {}).get("data")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
    return body

def _extract_text_from_pdf(pdf_bytes: bytes) -> str:
    """Extract text from a PDF file using pdfminer.six."""
    try:
        from pdfminer.high_level import extract_text
        return extract_text(io.BytesIO(pdf_bytes)).strip()
    except Exception as exc:
        logger.warning("pdfminer extraction failed: %s", exc)
        # Fallback to PyPDF2 if pdfminer fails or isn't installed
        try:
            import PyPDF2
            reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
            text = ""
            for page in reader.pages:
                text += page.extract_text() + "\n"
            return text.strip()
        except Exception as exc2:
            logger.warning("PyPDF2 fallback extraction failed: %s", exc2)
            return ""

def _extract_text_from_docx(docx_bytes: bytes) -> str:
    """Extract text from a Word document using python-docx."""
    try:
        import docx
        doc = docx.Document(io.BytesIO(docx_bytes))
        return "\n".join([paragraph.text for paragraph in doc.paragraphs]).strip()
    except Exception as exc:
        logger.warning("python-docx extraction failed: %s", exc)
        return ""

def _process_attachments(service: Any, msg_id: str, payload: dict) -> list[dict[str, str]]:
    """Download and extract text from PDF and Word attachments."""
    attachments = []
    
    def _find_attachments(parts: list) -> None:
        for part in parts:
            filename = part.get("filename", "")
            if filename and part.get("body", {}).get("attachmentId"):
                mime_type = part.get("mimeType", "")
                # Check if it's a PDF or Word doc
                if mime_type == "application/pdf" or filename.lower().endswith(".pdf"):
                    attachments.append({"id": part["body"]["attachmentId"], "filename": filename, "type": "pdf"})
                elif mime_type in ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/msword") or filename.lower().endswith((".docx", ".doc")):
                    attachments.append({"id": part["body"]["attachmentId"], "filename": filename, "type": "docx"})
            
            if "parts" in part:
                _find_attachments(part["parts"])
                
    if "parts" in payload:
        _find_attachments(payload["parts"])
        
    extracted_attachments = []
    for att in attachments:
        try:
            att_obj = service.users().messages().attachments().get(
                userId="me", messageId=msg_id, id=att["id"]
            ).execute()
            
            data = att_obj.get("data")
            if not data:
                continue
                
            file_bytes = base64.urlsafe_b64decode(data)
            text = ""
            
            if att["type"] == "pdf":
                text = _extract_text_from_pdf(file_bytes)
            elif att["type"] == "docx":
                text = _extract_text_from_docx(file_bytes)
                
            if text:
                extracted_attachments.append({
                    "filename": att["filename"],
                    "text": text
                })
        except Exception as exc:
            logger.warning("Failed to process attachment %s: %s", att["filename"], exc)
            
    return extracted_attachments

def _extract_metadata_with_llm(email_text: str) -> dict[str, Any]:
    """Use OpenAI to extract structured data from the email text."""
    if not brain._llm_client or not brain._settings:
        logger.warning("LLM client not initialized, skipping extraction.")
        return {}

    try:
        response = brain._llm_client.chat.completions.create(
            model=brain._settings.llm_model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": email_text[:4000]}, # Truncate to avoid token limits
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)
    except Exception as exc:
        logger.error("OpenAI extraction failed for school email: %s", exc)
        return {}

def poll_school_emails() -> None:
    """Poll Gmail for school emails for families that have opted in."""
    db = brain._supabase
    if not db:
        logger.error("Cannot poll school emails: no database connection")
        return

    try:
        # Fetch families with school_email_watch = true and a valid refresh token
        families_result = db.table("families").select("family_id").eq("school_email_watch", True).not_.is_("google_refresh_token", "null").execute()
        families = [row["family_id"] for row in (families_result.data or [])]
    except Exception as exc:
        logger.error("Failed to fetch families for school email polling: %s", exc)
        return

    if not families:
        logger.debug("No families opted in to school email watch.")
        return

    # Fetch all active family members for notifications
    family_phones_by_id: dict[str, list[tuple[str, str]]] = {}
    try:
        members_result = db.table("whatsapp_members").select("phone, name, family_id").execute()
        for row in (members_result.data or []):
            if row.get("phone") and row.get("family_id"):
                fid = row["family_id"]
                if fid not in family_phones_by_id:
                    family_phones_by_id[fid] = []
                family_phones_by_id[fid].append((row["phone"], row.get("name", "Family Member")))
    except Exception as exc:
        logger.warning("Could not fetch family members for notifications: %s", exc)

    # Twilio client for WhatsApp notifications
    twilio_client = None
    try:
        from twilio.rest import Client as TwilioClient
        from .config import get_settings
        _s = get_settings()
        if _s.twilio_account_sid and _s.twilio_auth_token:
            twilio_client = TwilioClient(_s.twilio_account_sid, _s.twilio_auth_token)
    except Exception as exc:
        logger.warning("Could not initialise Twilio client: %s", exc)

    # Calculate time window (last 24 hours)
    yesterday = int((datetime.now(timezone.utc) - timedelta(days=1)).timestamp())
    query = f"{SCHOOL_EMAIL_QUERY} after:{yesterday}"

    for family_id in families:
        service = _get_gmail_service(family_id)
        if not service:
            continue

        try:
            # Search for matching emails
            results = service.users().messages().list(userId="me", q=query, maxResults=20).execute()
            messages = results.get("messages", [])

            for msg in messages:
                msg_id = msg["id"]

                # Check if already processed
                check_result = db.table("school_emails_processed").select("id").eq("family_id", family_id).eq("gmail_message_id", msg_id).execute()
                if check_result.data:
                    continue # Already processed

                # Fetch full message
                full_msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
                
                # Extract sender and subject
                headers = full_msg.get("payload", {}).get("headers", [])
                sender = next((h["value"] for h in headers if h["name"].lower() == "from"), "School")
                subject = next((h["value"] for h in headers if h["name"].lower() == "subject"), "No Subject")
                
                # Clean up sender (e.g., "School Name <info@school.com>" -> "School Name")
                if "<" in sender:
                    sender = sender.split("<")[0].strip()

                # Extract body
                body = _extract_email_body(full_msg.get("payload", {}))
                if not body:
                    body = full_msg.get("snippet", "")
                    
                # Process attachments
                attachments = _process_attachments(service, msg_id, full_msg.get("payload", {}))
                
                # Combine text for LLM
                combined_text = f"Subject: {subject}\n\nBody:\n{body}"
                for att in attachments:
                    combined_text += f"\n\n--- Attachment: {att['filename']} ---\n{att['text']}"
                
                # Run LLM extraction
                extracted_info = _extract_metadata_with_llm(combined_text)
                if not extracted_info:
                    continue
                    
                # Add attachment info to extracted_info for storage
                if attachments:
                    extracted_info["attachments"] = [{"filename": a["filename"], "extracted_text_length": len(a["text"])} for a in attachments]

                summary = extracted_info.get("summary", subject)
                action_required = extracted_info.get("action_required", False)
                event_name = extracted_info.get("event_name", "")
                event_date = extracted_info.get("event_date", "")
                event_time = extracted_info.get("event_time", "")
                deadline = extracted_info.get("deadline", "")
                amount_due = extracted_info.get("amount_due", "")

                # 1. Create event if found
                gcal_event_id = None
                if event_name and event_date:
                    gcal_event_id = google_calendar.create_event(
                        event_name=event_name,
                        event_date=event_date,
                        event_time=event_time if event_time else None,
                        description=f"From school email: {summary}",
                        family_id=family_id
                    )
                    
                    # Store in family_events
                    brain.add_family_event(
                        title=event_name,
                        event_date=event_date,
                        family_member="Child", # Generic for now
                        event_time=event_time,
                        notes=f"From school email: {summary}",
                        added_by="Gmail Watcher"
                    )

                # 2. Store memory
                memory_content = f"School email from {sender}: {summary}"
                if action_required:
                    memory_content += f" (Action required by {deadline})" if deadline else " (Action required)"
                if amount_due:
                    memory_content += f" - Amount due: {amount_due}"
                
                embedding = brain.generate_embedding(memory_content)
                brain.store_memory(
                    content=memory_content,
                    embedding=embedding,
                    metadata={
                        "source": "gmail_watcher",
                        "category": "reference",
                        "tags": ["school", "email"],
                        "sender": sender,
                        "subject": subject,
                        "action_required": action_required,
                        "deadline": deadline,
                        "amount_due": amount_due
                    },
                    family_id=family_id
                )

                # 3. Mark as processed
                db.table("school_emails_processed").insert({
                    "family_id": family_id,
                    "gmail_message_id": msg_id,
                    "extracted_events": {"event_name": event_name, "event_date": event_date, "gcal_event_id": gcal_event_id} if event_name else None,
                    "extracted_info": extracted_info
                }).execute()

                # 3b. Log to cortex_actions
                try:
                    from .whatsapp_capture import log_action as _log_action
                    _log_action(family_id, 'school_email_processed', subject=subject[:100], detail={'sender': sender, 'event_name': event_name, 'event_date': event_date, 'action_required': action_required})
                except Exception as _log_exc:
                    logger.warning("Failed to log school_email_processed action: %s", _log_exc)

                # 4. Send WhatsApp notification
                if twilio_client and family_id in family_phones_by_id:
                    notification_body = ""
                    if event_name and event_date:
                        notification_body = f"📚 School email from {sender}: {summary}\n\n✅ '{event_name}' added to your calendar for {event_date}."
                    elif action_required:
                        deadline_str = f" Deadline: {deadline}." if deadline else ""
                        notification_body = f"📚 Action needed from school: {summary}.{deadline_str}"
                    
                    if notification_body:
                        for phone, member_name in family_phones_by_id[family_id]:
                            try:
                                twilio_client.messages.create(
                                    from_=_s.twilio_whatsapp_from,
                                    to=f"whatsapp:{phone}",
                                    body=notification_body,
                                )
                                logger.info("Sent school email notification to %s", phone)
                            except Exception as exc:
                                logger.warning("Failed to notify %s: %s", phone, exc)

        except HttpError as error:
            logger.error("Gmail API error for family %s: %s", family_id, error)
        except Exception as e:
            logger.error("Unexpected error processing school emails for family %s: %s", family_id, e)

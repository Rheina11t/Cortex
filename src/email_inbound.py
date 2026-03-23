"""
FamilyBrain Inbound Email Webhook via Mailgun.

This module provides a Flask webhook endpoint that receives parsed inbound emails
from Mailgun, extracts attachments, processes them using the same logic as the
Gmail watcher, stores memories in Supabase, and sends WhatsApp notifications.

Mailgun Configuration:
1. Create a Mailgun account and add familybrain.co.uk as a domain.
2. Set up a catch-all route: match_recipient(".*@familybrain.co.uk") -> forward to this webhook URL.
3. Add the webhook signing key to Railway env vars (MAILGUN_WEBHOOK_SIGNING_KEY).
"""

import hashlib
import hmac
import json
import logging
import os
import re
from typing import Any, Optional

from flask import Flask, request, jsonify

from . import brain
from .config import get_settings
from .gmail_watcher import _extract_text_from_pdf, _extract_text_from_docx, _extract_metadata_with_llm

logger = logging.getLogger("open_brain.email_inbound")

settings = get_settings()
brain.init(settings)

app = Flask(__name__)

def verify_mailgun_signature(token: str, timestamp: str, signature: str) -> bool:
    """Verify the Mailgun webhook signature."""
    signing_key = os.environ.get("MAILGUN_WEBHOOK_SIGNING_KEY", "")
    if not signing_key:
        logger.warning("MAILGUN_WEBHOOK_SIGNING_KEY not set, skipping signature verification")
        return True
        
    hmac_digest = hmac.new(
        key=signing_key.encode(),
        msg=f"{timestamp}{token}".encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()
    
    return hmac.compare_digest(signature, hmac_digest)

def get_family_email_address(family_id: str) -> str:
    """Return the inbound email address for a family."""
    domain = os.environ.get("MAILGUN_DOMAIN", "familybrain.co.uk")
    return f"{family_id}@{domain}"

def _extract_family_id_from_recipient(recipient: str) -> Optional[str]:
    """Extract family_id from the recipient email address."""
    # e.g. family-dan@familybrain.co.uk -> family-dan
    match = re.search(r"([^@<]+)@", recipient)
    if match:
        return match.group(1).strip()
    return None

def _notify_family(family_id: str, sender: str, subject: str, summary: str, action_required: bool, deadline: str, event_name: str, event_date: str) -> None:
    """Send a WhatsApp notification to all family members."""
    db = brain._supabase
    if not db:
        return

    # Fetch all active family members for notifications
    family_phones = []
    try:
        members_result = db.table("whatsapp_members").select("phone").eq("family_id", family_id).execute()
        for row in (members_result.data or []):
            if row.get("phone"):
                family_phones.append(row["phone"])
    except Exception as exc:
        logger.warning("Could not fetch family members for notifications: %s", exc)
        return

    if not family_phones:
        return

    # Twilio client for WhatsApp notifications
    twilio_client = None
    try:
        from twilio.rest import Client as TwilioClient
        if settings.twilio_account_sid and settings.twilio_auth_token:
            twilio_client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    except Exception as exc:
        logger.warning("Could not initialise Twilio client: %s", exc)
        return

    if not twilio_client:
        return

    notification_body = f"📧 New email from {sender}: {subject}\n\n{summary}"
    
    if event_name and event_date:
        notification_body += f"\n\n✅ '{event_name}' detected for {event_date}."
    elif action_required:
        deadline_str = f" Deadline: {deadline}." if deadline else ""
        notification_body += f"\n\n⚠️ Action needed.{deadline_str}"

    for phone in family_phones:
        try:
            twilio_client.messages.create(
                from_=settings.twilio_whatsapp_from,
                to=f"whatsapp:{phone}",
                body=notification_body,
            )
            logger.info("Sent inbound email notification to %s", phone)
        except Exception as exc:
            logger.warning("Failed to notify %s: %s", phone, exc)

@app.route("/webhook/email-inbound", methods=["POST"])
def email_inbound_webhook():
    """Handle inbound emails from Mailgun."""
    # Verify signature
    token = request.form.get("token", "")
    timestamp = request.form.get("timestamp", "")
    signature = request.form.get("signature", "")
    
    if not verify_mailgun_signature(token, timestamp, signature):
        return jsonify({"error": "Invalid signature"}), 401

    recipient = request.form.get("recipient", "")
    sender = request.form.get("sender", "")
    subject = request.form.get("subject", "No Subject")
    body_plain = request.form.get("body-plain", "")
    message_id = request.form.get("Message-Id", "")

    family_id = _extract_family_id_from_recipient(recipient)
    if not family_id:
        logger.warning("Could not extract family_id from recipient: %s", recipient)
        return jsonify({"error": "Invalid recipient"}), 400

    db = brain._supabase
    if not db:
        logger.error("No database connection")
        return jsonify({"error": "Database error"}), 500

    # Validate family exists
    try:
        family_check = db.table("families").select("id").eq("family_id", family_id).execute()
        if not family_check.data:
            logger.warning("Family not found: %s", family_id)
            return jsonify({"error": "Family not found"}), 404
    except Exception as exc:
        logger.error("Error checking family: %s", exc)
        return jsonify({"error": "Database error"}), 500

    # Check if already processed
    try:
        check_result = db.table("inbound_emails_processed").select("id").eq("family_id", family_id).eq("message_id", message_id).execute()
        if check_result.data:
            logger.info("Email already processed: %s", message_id)
            return jsonify({"status": "already processed"}), 200
    except Exception as exc:
        logger.warning("Error checking processed emails: %s", exc)

    # Process attachments
    attachments = []
    attachment_count = int(request.form.get("attachment-count", 0))
    
    for i in range(1, attachment_count + 1):
        file_obj = request.files.get(f"attachment-{i}")
        if file_obj:
            filename = file_obj.filename or f"attachment_{i}"
            file_bytes = file_obj.read()
            text = ""
            
            if filename.lower().endswith(".pdf"):
                text = _extract_text_from_pdf(file_bytes)
            elif filename.lower().endswith((".docx", ".doc")):
                text = _extract_text_from_docx(file_bytes)
            # For images, we could use OCR here if needed, but keeping it simple for now
            # or we could import _extract_text_from_image from whatsapp_capture
                
            if text:
                attachments.append({
                    "filename": filename,
                    "text": text
                })

    # Combine text for LLM
    combined_text = f"Subject: {subject}\n\nBody:\n{body_plain}"
    for att in attachments:
        combined_text += f"\n\n--- Attachment: {att['filename']} ---\n{att['text']}"
    
    # Run LLM extraction
    extracted_info = _extract_metadata_with_llm(combined_text)
    
    summary = extracted_info.get("summary", subject)
    action_required = extracted_info.get("action_required", False)
    event_name = extracted_info.get("event_name", "")
    event_date = extracted_info.get("event_date", "")
    event_time = extracted_info.get("event_time", "")
    deadline = extracted_info.get("deadline", "")
    amount_due = extracted_info.get("amount_due", "")

    # Store memory
    memory_content = f"Inbound email from {sender}: {summary}"
    if action_required:
        memory_content += f" (Action required by {deadline})" if deadline else " (Action required)"
    if amount_due:
        memory_content += f" - Amount due: {amount_due}"
    
    try:
        embedding = brain.generate_embedding(memory_content)
        brain.store_memory(
            content=memory_content,
            embedding=embedding,
            metadata={
                "source": "email_inbound",
                "category": "reference",
                "tags": ["email", "inbound"],
                "sender": sender,
                "subject": subject,
                "action_required": action_required,
                "deadline": deadline,
                "amount_due": amount_due,
                "attachments": [a["filename"] for a in attachments]
            },
            family_id=family_id
        )
    except Exception as exc:
        logger.error("Failed to store memory for inbound email: %s", exc)

    # Mark as processed
    try:
        db.table("inbound_emails_processed").insert({
            "family_id": family_id,
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "attachment_count": attachment_count
        }).execute()
    except Exception as exc:
        logger.warning("Failed to mark email as processed: %s", exc)

    # Send WhatsApp notification
    _notify_family(
        family_id=family_id,
        sender=sender,
        subject=subject,
        summary=summary,
        action_required=action_required,
        deadline=deadline,
        event_name=event_name,
        event_date=event_date
    )

    return jsonify({"status": "success"}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8081))
    app.run(host="0.0.0.0", port=port)

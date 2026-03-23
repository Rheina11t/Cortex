# -*- coding: utf-8 -*-
"""
FamilyBrain Onboarding Backend

A Flask service that handles:
  1. POST /signup        — Collect family details, create Stripe checkout session
  2. GET  /signup/return — Stripe redirect after payment (success/cancel)
  3. POST /stripe/webhook — Stripe webhook: on checkout.session.completed,
                            provision the family in Supabase and send welcome WhatsApp

Environment variables required:
  STRIPE_SECRET_KEY           — Stripe secret key (sk_live_... or sk_test_...)
  STRIPE_WEBHOOK_SECRET       — Stripe webhook signing secret (whsec_...)
  STRIPE_PRICE_ID_MONTHLY     — Stripe Price ID for £19.99/month plan
  STRIPE_PRICE_ID_ANNUAL      — Stripe Price ID for £179/year plan
  STRIPE_PRICE_ID_FOUNDING    — Stripe Price ID for £9.99/month founding member plan
  SUPABASE_URL                — Supabase project URL
  SUPABASE_SERVICE_KEY        — Supabase service role key
  TWILIO_ACCOUNT_SID          — Twilio account SID
  TWILIO_AUTH_TOKEN           — Twilio auth token
  TWILIO_WHATSAPP_FROM        — Twilio WhatsApp sender (e.g. whatsapp:+447XXXXXXXXX)
  ONBOARDING_BASE_URL         — Public base URL of this service (e.g. https://api.familybrain.co)
  GOOGLE_CALENDAR_OAUTH_URL   — URL of the Google Calendar OAuth flow (optional)

Optional:
  FOUNDING_MEMBER_SLOTS       — Number of founding member slots remaining (default 100)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

import stripe
from flask import Flask, Response, jsonify, redirect, request
from supabase import create_client
from twilio.rest import Client as TwilioClient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("familybrain.onboarding")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID_MONTHLY = os.environ.get("STRIPE_PRICE_ID_MONTHLY", "")
STRIPE_PRICE_ID_ANNUAL = os.environ.get("STRIPE_PRICE_ID_ANNUAL", "")
STRIPE_PRICE_ID_FOUNDING = os.environ.get("STRIPE_PRICE_ID_FOUNDING", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
ONBOARDING_BASE_URL = os.environ.get("ONBOARDING_BASE_URL", "https://familybrain.co")
GCAL_OAUTH_URL = os.environ.get("GOOGLE_CALENDAR_OAUTH_URL", "")
FOUNDING_MEMBER_SLOTS = int(os.environ.get("FOUNDING_MEMBER_SLOTS", "100"))

stripe.api_key = STRIPE_SECRET_KEY

# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------
def _get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# ---------------------------------------------------------------------------
# Twilio client
# ---------------------------------------------------------------------------
def _get_twilio() -> Optional[TwilioClient]:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        logger.warning("Twilio credentials not configured — WhatsApp messages will be skipped")
        return None
    return TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)


# ---------------------------------------------------------------------------
# Family provisioning
# ---------------------------------------------------------------------------
def _generate_family_id(primary_phone: str) -> str:
    """Generate a stable, unique family_id from the primary phone number."""
    h = hashlib.sha256(primary_phone.encode()).hexdigest()[:12]
    return f"family_{h}"


def _provision_family(
    family_id: str,
    primary_name: str,
    primary_phone: str,
    member_phones: list[str],
    plan: str,
    stripe_customer_id: str,
    stripe_subscription_id: str,
) -> dict[str, Any]:
    """Create a record in the families table in Supabase."""
    sb = _get_supabase()
    record = {
        "family_id": family_id,
        "primary_name": primary_name,
        "primary_phone": primary_phone,
        "member_phones": member_phones,
        "plan": plan,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    result = sb.table("families").upsert(record).execute()
    logger.info("Provisioned family %s (plan=%s)", family_id, plan)
    return result.data[0] if result.data else record


def _send_welcome_whatsapp(
    family_id: str,
    primary_name: str,
    primary_phone: str,
    member_phones: list[str],
    plan: str,
) -> None:
    """Send a welcome WhatsApp message to the primary number and all family members."""
    twilio = _get_twilio()
    if not twilio or not TWILIO_WHATSAPP_FROM:
        logger.warning("Skipping welcome WhatsApp — Twilio not configured")
        return

    first_name = primary_name.split()[0] if primary_name else "there"
    plan_display = {
        "founding": "Founding Member (£9.99/month, locked in forever 🎉)",
        "monthly": "Monthly (£19.99/month)",
        "annual": "Annual (£179/year)",
    }.get(plan, plan)

    gcal_line = ""
    if GCAL_OAUTH_URL:
        gcal_line = f"\n\n📅 *Connect your Google Calendar* (optional but recommended):\n{GCAL_OAUTH_URL}?family_id={family_id}"

    welcome_msg = (
        f"👋 Welcome to FamilyBrain, {first_name}!\n\n"
        f"You're all set on the *{plan_display}* plan.\n\n"
        f"Here's how to get started:\n\n"
        f"📸 *Send anything* — photos of documents, insurance letters, school trip forms, MOT certificates, prescriptions. I'll read them and remember the important details.\n\n"
        f"🎤 *Voice notes* — just talk. I'll transcribe and store what you say.\n\n"
        f"❓ *Ask anything* — \"When does my car insurance renew?\", \"Is Edi free Thursday?\", \"What's Jake's prescription?\"\n\n"
        f"🧠 *Every Sunday morning* I'll send you a proactive summary of anything that needs your attention — renewals, deadlines, things to update.\n\n"
        f"{gcal_line}\n\n"
        f"To add other family members, just forward them this number and ask them to say hello — I'll recognise their number automatically.\n\n"
        f"Let's get started. What's the first thing you'd like me to remember? 🗂️"
    )

    # Send to primary number
    to_number = primary_phone if primary_phone.startswith("whatsapp:") else f"whatsapp:{primary_phone}"
    try:
        twilio.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_number,
            body=welcome_msg,
        )
        logger.info("Welcome WhatsApp sent to %s", primary_phone)
    except Exception as exc:
        logger.error("Failed to send welcome WhatsApp to %s: %s", primary_phone, exc)

    # Send a shorter onboarding message to additional family members
    if member_phones:
        member_msg = (
            f"👋 Hi! {first_name} has added you to their FamilyBrain.\n\n"
            f"You can send me documents, photos, voice notes, or questions — "
            f"I'll remember everything and share it with the whole family.\n\n"
            f"What would you like me to remember first? 🗂️"
        )
        for phone in member_phones:
            if phone == primary_phone:
                continue
            to = phone if phone.startswith("whatsapp:") else f"whatsapp:{phone}"
            try:
                twilio.messages.create(
                    from_=TWILIO_WHATSAPP_FROM,
                    to=to,
                    body=member_msg,
                )
                logger.info("Member welcome sent to %s", phone)
            except Exception as exc:
                logger.error("Failed to send member welcome to %s: %s", phone, exc)


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------
app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "familybrain-onboarding"}


@app.route("/signup", methods=["POST"])
def signup() -> Response:
    """
    Create a Stripe Checkout session for a new family signup.

    Expected JSON body:
    {
        "primary_name": "Dan Smith",
        "primary_phone": "+447700900000",
        "member_phones": ["+447700900001"],   // optional additional members
        "plan": "founding" | "monthly" | "annual"
    }

    Returns:
    {
        "checkout_url": "https://checkout.stripe.com/..."
    }
    """
    data = request.get_json(force=True, silent=True) or {}
    primary_name = (data.get("primary_name") or "").strip()
    primary_phone = (data.get("primary_phone") or "").strip()
    member_phones = data.get("member_phones") or []
    plan = (data.get("plan") or "monthly").strip().lower()

    if not primary_name or not primary_phone:
        return jsonify({"error": "primary_name and primary_phone are required"}), 400

    # Normalise phone — ensure E.164 format
    if not primary_phone.startswith("+"):
        return jsonify({"error": "primary_phone must be in E.164 format (e.g. +447700900000)"}), 400

    # Select price ID
    price_map = {
        "founding": STRIPE_PRICE_ID_FOUNDING,
        "monthly": STRIPE_PRICE_ID_MONTHLY,
        "annual": STRIPE_PRICE_ID_ANNUAL,
    }
    price_id = price_map.get(plan)
    if not price_id:
        return jsonify({"error": f"Unknown plan: {plan}. Must be founding, monthly, or annual"}), 400
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe not configured on this server"}), 500

    family_id = _generate_family_id(primary_phone)

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{ONBOARDING_BASE_URL}/signup/return?session_id={{CHECKOUT_SESSION_ID}}&status=success",
            cancel_url=f"{ONBOARDING_BASE_URL}/signup/return?status=cancelled",
            metadata={
                "family_id": family_id,
                "primary_name": primary_name,
                "primary_phone": primary_phone,
                "member_phones": json.dumps(member_phones),
                "plan": plan,
            },
            customer_email=None,  # We're phone-first, not email-first
            phone_number_collection={"enabled": False},
        )
        logger.info("Stripe checkout session created: %s for family %s", session.id, family_id)
        return jsonify({"checkout_url": session.url, "family_id": family_id})
    except stripe.StripeError as exc:
        logger.error("Stripe error creating checkout session: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.route("/signup/return", methods=["GET"])
def signup_return() -> Response:
    """
    Stripe redirects here after checkout.
    For success: show a confirmation page.
    For cancel: redirect back to the landing page.
    """
    status = request.args.get("status", "")
    session_id = request.args.get("session_id", "")

    if status == "cancelled":
        return redirect(f"{ONBOARDING_BASE_URL}/?signup=cancelled")

    if status == "success" and session_id:
        # Stripe webhook will handle actual provisioning.
        # This page just shows a friendly confirmation.
        return redirect(f"{ONBOARDING_BASE_URL}/?signup=success")

    return redirect(ONBOARDING_BASE_URL)


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook() -> Response:
    """
    Stripe webhook handler.
    Listens for checkout.session.completed and customer.subscription.deleted events.
    """
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        logger.warning("STRIPE_WEBHOOK_SECRET not set — skipping signature verification")
        event_data = json.loads(payload)
    else:
        try:
            event_data = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except stripe.SignatureVerificationError as exc:
            logger.error("Stripe webhook signature verification failed: %s", exc)
            return Response("Invalid signature", status=400)

    event_type = event_data.get("type", "")
    logger.info("Stripe webhook received: %s", event_type)

    if event_type == "checkout.session.completed":
        _handle_checkout_completed(event_data["data"]["object"])
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_cancelled(event_data["data"]["object"])

    return Response("ok", status=200)


def _handle_checkout_completed(session: dict[str, Any]) -> None:
    """Provision the family and send welcome WhatsApp after successful payment."""
    metadata = session.get("metadata") or {}
    family_id = metadata.get("family_id", "")
    primary_name = metadata.get("primary_name", "")
    primary_phone = metadata.get("primary_phone", "")
    member_phones_raw = metadata.get("member_phones", "[]")
    plan = metadata.get("plan", "monthly")
    stripe_customer_id = session.get("customer", "")
    stripe_subscription_id = session.get("subscription", "")

    if not family_id or not primary_phone:
        logger.error("checkout.session.completed missing family_id or primary_phone in metadata")
        return

    try:
        member_phones = json.loads(member_phones_raw)
    except Exception:
        member_phones = []

    all_phones = [primary_phone] + [p for p in member_phones if p != primary_phone]
    
    # Enforce 6-adult member cap
    if len(all_phones) > 6:
        logger.warning("Family %s attempted to register with %d members. Capping at 6.", family_id, len(all_phones))
        all_phones = all_phones[:6]
        member_phones = [p for p in all_phones if p != primary_phone]

    try:
        # 1. Provision in Supabase
        _provision_family(
            family_id=family_id,
            primary_name=primary_name,
            primary_phone=primary_phone,
            member_phones=all_phones,
            plan=plan,
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
        )

        # 2. Register phone numbers in the WhatsApp routing table
        _register_phones(family_id=family_id, phones=all_phones)

        # 3. Send welcome WhatsApp
        _send_welcome_whatsapp(
            family_id=family_id,
            primary_name=primary_name,
            primary_phone=primary_phone,
            member_phones=member_phones,
            plan=plan,
        )
    except Exception as exc:
        logger.error("Failed to provision family %s: %s\n%s", family_id, exc, traceback.format_exc())


def _handle_subscription_cancelled(subscription: dict[str, Any]) -> None:
    """Mark a family as inactive when their subscription is cancelled."""
    stripe_subscription_id = subscription.get("id", "")
    if not stripe_subscription_id:
        return
    try:
        sb = _get_supabase()
        sb.table("families").update({"status": "cancelled"}).eq(
            "stripe_subscription_id", stripe_subscription_id
        ).execute()
        logger.info("Family subscription cancelled: %s", stripe_subscription_id)
    except Exception as exc:
        logger.error("Failed to mark subscription cancelled: %s", exc)


def _register_phones(family_id: str, phones: list[str]) -> None:
    """
    Register phone numbers in the whatsapp_members table so the WhatsApp
    capture layer can route messages to the correct family.
    """
    sb = _get_supabase()
    for phone in phones:
        normalised = phone.strip()
        if not normalised:
            continue
        try:
            sb.table("whatsapp_members").upsert({
                "phone": normalised,
                "family_id": family_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            logger.info("Registered phone %s -> family %s", normalised, family_id)
        except Exception as exc:
            logger.error("Failed to register phone %s: %s", normalised, exc)


# ---------------------------------------------------------------------------
# Founding member slot check
# ---------------------------------------------------------------------------
@app.route("/founding-slots", methods=["GET"])
def founding_slots() -> Response:
    """Return the number of founding member slots remaining."""
    try:
        sb = _get_supabase()
        result = sb.table("families").select("id", count="exact").eq("plan", "founding").execute()
        used = result.count or 0
        remaining = max(0, FOUNDING_MEMBER_SLOTS - used)
        return jsonify({"remaining": remaining, "total": FOUNDING_MEMBER_SLOTS})
    except Exception as exc:
        logger.error("Failed to count founding members: %s", exc)
        return jsonify({"remaining": FOUNDING_MEMBER_SLOTS, "total": FOUNDING_MEMBER_SLOTS})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    port = int(os.environ.get("PORT", "8081"))
    logger.info("Starting FamilyBrain onboarding service on port %d", port)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()

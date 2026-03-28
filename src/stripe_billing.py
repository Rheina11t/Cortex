# -*- coding: utf-8 -*-
"""
FamilyBrain — Stripe Billing Integration
=========================================
Provides three Flask routes that bolt onto the existing WhatsApp capture layer:

  GET  /join                     — Pre-checkout landing page: collects the user's
                                   WhatsApp number before redirecting to Stripe.
  POST /stripe/create-checkout   — Creates a Stripe Checkout Session and returns
                                   the checkout URL as JSON.
  GET  /subscribe                — Convenience redirect to the /join page (for use
                                   as a landing-page CTA link).
  POST /stripe/webhook           — Stripe webhook handler (signature-verified).
                                   Handles:
                                     • checkout.session.completed  → provision family
                                     • customer.subscription.deleted → mark inactive

Environment variables:
  STRIPE_SECRET_KEY      — Stripe secret key  (sk_live_... / sk_test_...)
  STRIPE_PUBLISHABLE_KEY — Stripe publishable key (pk_live_... / pk_test_...)
  STRIPE_PRICE_ID        — Monthly subscription Price ID (default: creates a
                           £4.99/month price at runtime if unset — test mode only)
  STRIPE_WEBHOOK_SECRET  — Stripe webhook signing secret (whsec_...)
  FAMILYBRAIN_BASE_URL   — Public base URL (e.g. https://cortex-production.up.railway.app)
  SUPABASE_URL           — Supabase project URL
  SUPABASE_SERVICE_KEY   — Supabase service role key
  TWILIO_ACCOUNT_SID     — Twilio account SID
  TWILIO_AUTH_TOKEN      — Twilio auth token
  TWILIO_WHATSAPP_FROM   — Twilio WhatsApp sender (e.g. whatsapp:+447XXXXXXXXX)

Grandfathered families (exempt from subscription checks):
  family-dan
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

import stripe
from flask import Blueprint, Response, jsonify, redirect, render_template_string, request
from . import security_logger

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger("familybrain.stripe_billing")

# ---------------------------------------------------------------------------
# Allowed Stripe webhook event types (Phase 2 security hardening)
# ---------------------------------------------------------------------------
_ALLOWED_EVENT_TYPES: frozenset[str] = frozenset({
    "checkout.session.completed",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_succeeded",
    "invoice.payment_failed",
})

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY: str = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_PUBLISHABLE_KEY: str = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_PRICE_ID: str = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_WEBHOOK_SECRET: str = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
FAMILYBRAIN_BASE_URL: str = os.environ.get(
    "FAMILYBRAIN_BASE_URL", "https://cortex-production.up.railway.app"
).rstrip("/")
SUPABASE_URL: str = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY: str = os.environ.get("SUPABASE_SERVICE_KEY", "")
TWILIO_ACCOUNT_SID: str = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN: str = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM: str = os.environ.get("TWILIO_WHATSAPP_FROM", "")
MAILGUN_DOMAIN: str = os.environ.get("MAILGUN_DOMAIN", "familybrain.co.uk")

# Families that are grandfathered / exempt from subscription checks
GRANDFATHERED_FAMILY_IDS: frozenset[str] = frozenset({"family-dan"})

stripe.api_key = STRIPE_SECRET_KEY

# ---------------------------------------------------------------------------
# Blueprint — registered onto the main Flask app in whatsapp_capture.py
# ---------------------------------------------------------------------------
billing_bp = Blueprint("stripe_billing", __name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_supabase():
    """Return a Supabase client, raising if credentials are missing."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _send_wa(to: str, body: str) -> None:
    """Send a WhatsApp message via the transport-agnostic layer."""
    try:
        from . import meta_whatsapp as _meta_wa
    except ImportError:
        import meta_whatsapp as _meta_wa  # fallback for standalone execution
    _meta_wa.send_whatsapp_message(to=to, body=body)


def _generate_family_id(primary_phone: str) -> str:
    """Generate a stable, unique family_id from the primary phone number."""
    h = hashlib.sha256(primary_phone.encode()).hexdigest()[:12]
    return f"family_{h}"


def _normalise_phone(phone: str) -> str:
    """Strip whitespace and ensure E.164 format (leading +)."""
    return phone.strip().replace(" ", "")


# ---------------------------------------------------------------------------
# /join — pre-checkout page
# ---------------------------------------------------------------------------

_JOIN_PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Join FamilyBrain — Enter your WhatsApp number</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,600;0,9..144,700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
    html { font-size: 16px; }
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
      background: #FAF8F4;
      color: #2C2C2C;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 24px;
      -webkit-font-smoothing: antialiased;
    }
    .card {
      background: #fff;
      border-radius: 20px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.08), 0 1px 3px rgba(0,0,0,0.04);
      padding: 48px 40px 40px;
      width: 100%;
      max-width: 440px;
    }
    .logo {
      font-family: 'Fraunces', Georgia, serif;
      font-size: 1.5rem;
      font-weight: 700;
      color: #4A8C7A;
      margin-bottom: 32px;
      display: block;
      text-decoration: none;
    }
    h1 {
      font-family: 'Fraunces', Georgia, serif;
      font-size: 1.75rem;
      font-weight: 700;
      line-height: 1.25;
      color: #2C2C2C;
      margin-bottom: 10px;
    }
    .subtitle {
      font-size: 0.95rem;
      color: #7A7A7A;
      line-height: 1.6;
      margin-bottom: 32px;
    }
    label {
      display: block;
      font-size: 0.85rem;
      font-weight: 600;
      color: #2C2C2C;
      margin-bottom: 6px;
    }
    .hint {
      font-weight: 400;
      color: #9A9A9A;
      font-size: 0.8rem;
      margin-left: 6px;
    }
    input[type="tel"] {
      width: 100%;
      padding: 14px 16px;
      border: 1.5px solid #E0DDD8;
      border-radius: 10px;
      font-size: 1rem;
      font-family: inherit;
      color: #2C2C2C;
      background: #FAFAF8;
      transition: border-color 0.2s, box-shadow 0.2s;
      outline: none;
      margin-bottom: 6px;
    }
    input[type="tel"]:focus {
      border-color: #4A8C7A;
      box-shadow: 0 0 0 3px rgba(74,140,122,0.12);
      background: #fff;
    }
    input[type="tel"].error { border-color: #E8714A; }
    .field-error {
      font-size: 0.8rem;
      color: #E8714A;
      margin-bottom: 20px;
      display: none;
    }
    .field-error.visible { display: block; }
    .btn-submit {
      width: 100%;
      padding: 16px;
      background: #E8714A;
      color: #fff;
      border: none;
      border-radius: 12px;
      font-size: 1rem;
      font-weight: 600;
      font-family: inherit;
      cursor: pointer;
      transition: background 0.2s, transform 0.15s, box-shadow 0.2s;
      box-shadow: 0 4px 14px rgba(232,113,74,0.35);
      margin-top: 8px;
    }
    .btn-submit:hover:not(:disabled) {
      background: #d4613c;
      transform: translateY(-1px);
      box-shadow: 0 6px 20px rgba(232,113,74,0.45);
    }
    .btn-submit:disabled { opacity: 0.65; cursor: not-allowed; transform: none; }
    .small-note {
      font-size: 0.78rem;
      color: #9A9A9A;
      text-align: center;
      margin-top: 14px;
      line-height: 1.5;
    }
    .price-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: #F0F7F4;
      color: #4A8C7A;
      border: 1px solid #C8E0D8;
      border-radius: 8px;
      padding: 6px 12px;
      font-size: 0.82rem;
      font-weight: 600;
      margin-bottom: 24px;
    }
    .error-banner {
      background: #FEF2EE;
      border: 1px solid #F5C4A8;
      border-radius: 10px;
      padding: 12px 16px;
      font-size: 0.85rem;
      color: #C0392B;
      margin-bottom: 16px;
      display: none;
    }
    .error-banner.visible { display: block; }
    .back-link {
      display: block;
      text-align: center;
      margin-top: 20px;
      font-size: 0.85rem;
      color: #9A9A9A;
      text-decoration: none;
      transition: color 0.2s;
    }
    .back-link:hover { color: #4A8C7A; }
    @media (max-width: 480px) {
      .card { padding: 36px 24px 32px; }
      h1 { font-size: 1.5rem; }
    }
  </style>
</head>
<body>
  <div class="card">
    <a class="logo" href="https://familybrain.co.uk">FamilyBrain</a>
    <h1>One last step before you start</h1>
    <p class="subtitle">Enter your WhatsApp number and we'll take you to secure checkout. Your account activates the moment payment confirms.</p>
    <div class="price-badge">
      <span>&#x1F4B3;</span>
      <span>{{ price_display }} / month &mdash; cancel anytime</span>
    </div>
    <form id="joinForm" novalidate>
      <label for="phone">Your WhatsApp number <span class="hint">Include country code</span></label>
      <input type="tel" id="phone" name="phone" placeholder="+44 7700 900000" autocomplete="tel" inputmode="tel">
      <div class="field-error" id="phoneError">Please enter a valid number starting with + (e.g. +44 7700 900000).</div>
      <div class="error-banner" id="errorBanner">Something went wrong — please try again or email <a href="mailto:hello@familybrain.co.uk" style="color:#c0392b;text-decoration:underline;">hello@familybrain.co.uk</a></div>
      <button type="submit" class="btn-submit" id="submitBtn">Continue to secure checkout &rarr;</button>
    </form>
    <p class="small-note">&#x1F512; Payments processed securely by Stripe. We never store your card details.</p>
    <a class="back-link" href="https://familybrain.co.uk">&#x2190; Back to FamilyBrain</a>
  </div>

  <script>
    function isValidPhone(val) {
      return /^\\+[0-9]{9,}$/.test(val.replace(/\\s/g, ''));
    }

    // Capture referral code from URL query parameter (?ref=XXXXXX)
    // and persist it in sessionStorage so it survives any redirects on this domain.
    (function captureRef() {
      const params = new URLSearchParams(window.location.search);
      const ref = params.get('ref') || params.get('REF');
      if (ref) {
        sessionStorage.setItem('fb_ref', ref.trim().toUpperCase());
      }
    })();

    document.getElementById('joinForm').addEventListener('submit', async function(e) {
      e.preventDefault();
      const phoneInput = document.getElementById('phone');
      const phoneError = document.getElementById('phoneError');
      const errorBanner = document.getElementById('errorBanner');
      const btn = document.getElementById('submitBtn');

      // Reset errors
      phoneInput.classList.remove('error');
      phoneError.classList.remove('visible');
      errorBanner.classList.remove('visible');

      const phone = phoneInput.value.trim().replace(/\\s/g, '');

      if (!isValidPhone(phone)) {
        phoneInput.classList.add('error');
        phoneError.classList.add('visible');
        return;
      }

      btn.textContent = 'Preparing your checkout\u2026';
      btn.disabled = true;

      // Read referral code from sessionStorage (set above or by the landing page)
      const refCode = sessionStorage.getItem('fb_ref') || '';

      try {
        const payload = { phone: phone };
        if (refCode) { payload.ref = refCode; }
        const res = await fetch('/stripe/create-checkout', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        if (!res.ok) throw new Error('API error ' + res.status);
        const data = await res.json();
        if (data.checkout_url) {
          window.location.href = data.checkout_url;
        } else {
          throw new Error('No checkout_url in response');
        }
      } catch (err) {
        btn.textContent = 'Continue to secure checkout \u2192';
        btn.disabled = false;
        errorBanner.classList.add('visible');
      }
    });
  </script>
</body>
</html>
"""


@billing_bp.route("/join", methods=["GET"])
def join_page() -> Response:
    """Pre-checkout page: collects the user's WhatsApp number."""
    # Determine the display price from the env var or fall back to £4.99
    price_display = "£4.99"
    if STRIPE_PRICE_ID and STRIPE_SECRET_KEY:
        try:
            price_obj = stripe.Price.retrieve(STRIPE_PRICE_ID)
            unit_amount = price_obj.get("unit_amount") or 499
            currency = (price_obj.get("currency") or "gbp").upper()
            symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, currency + " ")
            price_display = f"{symbol}{unit_amount / 100:.2f}"
        except Exception as exc:
            logger.debug("Could not retrieve Stripe price for display: %s", exc)

    html = render_template_string(_JOIN_PAGE_HTML, price_display=price_display)
    return Response(html, mimetype="text/html")


@billing_bp.route("/subscribe", methods=["GET"])
def subscribe_redirect() -> Response:
    """Convenience redirect to /join — for use as a landing page CTA."""
    return redirect("/join", code=302)


# ---------------------------------------------------------------------------
# POST /stripe/create-checkout
# ---------------------------------------------------------------------------

@billing_bp.route("/stripe/create-checkout", methods=["POST"])
def create_checkout() -> Response:
    """
    Create a Stripe Checkout Session for a monthly subscription.

    Expected JSON body:
      { "phone": "+447700900000", "ref": "FAMXXXXX" }  (ref is optional)

    Returns:
      { "checkout_url": "https://checkout.stripe.com/..." }
    """
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Stripe is not configured on this server"}), 500

    data = request.get_json(force=True, silent=True) or {}
    phone_raw: str = (data.get("phone") or "").strip()

    if not phone_raw:
        return jsonify({"error": "phone is required"}), 400

    phone = _normalise_phone(phone_raw)
    if not phone.startswith("+"):
        return jsonify({"error": "phone must be in E.164 format (e.g. +447700900000)"}), 400

    # Optional referral code passed from the landing page (?ref=XXXXXX)
    ref_code: str = (data.get("ref") or "").strip().upper()

    # Resolve price ID — fall back to a default £4.99 label if not set
    price_id = STRIPE_PRICE_ID
    if not price_id:
        logger.warning(
            "STRIPE_PRICE_ID not set — Stripe checkout will fail unless a valid price_id is provided"
        )
        return jsonify({"error": "STRIPE_PRICE_ID is not configured on this server"}), 500

    family_id = _generate_family_id(phone)

    # Build Stripe metadata — include ref_code if present
    session_metadata: dict[str, str] = {
        "family_id": family_id,
        "primary_phone": phone,
    }
    if ref_code:
        session_metadata["ref_code"] = ref_code
        logger.info("Checkout session for family %s includes ref_code=%s", family_id, ref_code)

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=(
                f"{FAMILYBRAIN_BASE_URL}/stripe/success"
                "?session_id={CHECKOUT_SESSION_ID}"
            ),
            cancel_url=f"{FAMILYBRAIN_BASE_URL}/join?cancelled=1",
            metadata=session_metadata,
            phone_number_collection={"enabled": False},
        )
        logger.info(
            "Stripe checkout session created: %s for family %s (phone %s)",
            session.id, family_id, phone,
        )
        return jsonify({"checkout_url": session.url, "family_id": family_id})
    except stripe.StripeError as exc:
        logger.error("Stripe error creating checkout session: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /stripe/webhook
# ---------------------------------------------------------------------------

@billing_bp.route("/stripe/webhook", methods=["POST"])
def stripe_webhook() -> Response:
    """
    Stripe webhook handler.

    Verifies the Stripe-Signature header using STRIPE_WEBHOOK_SECRET.
    Phase 2 hardening: event type allowlisting + idempotency.
    Handles:
      • checkout.session.completed        → provision family in Supabase + send welcome WhatsApp
      • customer.subscription.updated     → (logged, future use)
      • customer.subscription.deleted     → mark family as inactive
      • invoice.payment_succeeded         → (logged, future use)
      • invoice.payment_failed            → (logged, future use)
    """
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        logger.error(
            "STRIPE_WEBHOOK_SECRET not configured — rejecting webhook. "
            "Set this env var to your Stripe webhook signing secret (whsec_...)."
        )
        return Response("Webhook secret not configured", status=500)

    try:
        event_data = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except stripe.SignatureVerificationError as exc:
        logger.error("Stripe webhook signature verification failed: %s", exc)
        security_logger.security_log(
            "stripe_webhook_failed",
            {"reason": "signature_verification_failed", "error": str(exc)[:200]},
        )
        return Response("Invalid signature", status=400)
    except Exception as exc:
        logger.error("Stripe webhook parse error: %s", exc)
        security_logger.security_log(
            "stripe_webhook_failed",
            {"reason": "parse_error", "error": str(exc)[:200]},
        )
        return Response("Bad request", status=400)

    event_type: str = event_data.get("type", "")
    event_id: str = event_data.get("id", "")
    logger.info("Stripe webhook received: %s (id=%s)", event_type, event_id)

    # --- Event type allowlisting (Phase 2) ---
    if event_type not in _ALLOWED_EVENT_TYPES:
        logger.info("Ignoring unhandled Stripe event type: %s", event_type)
        return Response("ok", status=200)

    # --- Idempotency check (Phase 2) ---
    if event_id and _is_event_already_processed(event_id):
        logger.info("Skipping already-processed Stripe event: %s", event_id)
        return Response("ok", status=200)

    # --- Route to handler ---
    if event_type == "checkout.session.completed":
        _handle_checkout_completed(event_data["data"]["object"])
    elif event_type == "customer.subscription.updated":
        logger.info("Subscription updated event received (id=%s)", event_id)
    elif event_type == "customer.subscription.deleted":
        _handle_subscription_deleted(event_data["data"]["object"])
    elif event_type == "invoice.payment_succeeded":
        logger.info("Invoice payment succeeded (id=%s)", event_id)
    elif event_type == "invoice.payment_failed":
        logger.info("Invoice payment failed (id=%s)", event_id)

    # --- Mark event as processed ---
    if event_id:
        _mark_event_processed(event_id, event_type)

    return Response("ok", status=200)


def _is_event_already_processed(event_id: str) -> bool:
    """Check if a Stripe event has already been processed (idempotency guard)."""
    try:
        sb = _get_supabase()
        result = (
            sb.table("processed_stripe_events")
            .select("id")
            .eq("event_id", event_id)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.warning("Idempotency check failed for event %s: %s", event_id, exc)
        return False  # Fail open: process the event rather than silently dropping it


def _mark_event_processed(event_id: str, event_type: str) -> None:
    """Record a Stripe event as processed in the idempotency table."""
    try:
        sb = _get_supabase()
        sb.table("processed_stripe_events").insert({
            "event_id": event_id,
            "event_type": event_type,
        }).execute()
    except Exception as exc:
        logger.warning("Failed to record processed event %s: %s", event_id, exc)


# ---------------------------------------------------------------------------
# Success page (after Stripe redirect)
# ---------------------------------------------------------------------------

_SUCCESS_PAGE_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Payment confirmed — FamilyBrain</title>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: 'Inter', sans-serif;
      background: #FAF8F4;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
      -webkit-font-smoothing: antialiased;
    }
    .card {
      background: #fff;
      border-radius: 20px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.08);
      padding: 48px 40px;
      max-width: 440px;
      width: 100%;
      text-align: center;
    }
    .icon { font-size: 3rem; margin-bottom: 20px; }
    h1 {
      font-family: 'Fraunces', Georgia, serif;
      font-size: 1.75rem;
      color: #2C2C2C;
      margin-bottom: 12px;
    }
    p { color: #7A7A7A; line-height: 1.65; font-size: 0.95rem; }
    .back {
      display: inline-block;
      margin-top: 28px;
      color: #4A8C7A;
      font-weight: 600;
      font-size: 0.9rem;
      text-decoration: none;
    }
    .back:hover { color: #3a7264; }
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">&#x1F389;</div>
    <h1>You're in!</h1>
    <p>Payment confirmed. Check your WhatsApp — your welcome message is on its way. FamilyBrain is ready whenever you are.</p>
    <a class="back" href="https://familybrain.co.uk">Back to FamilyBrain &rarr;</a>
  </div>
</body>
</html>
"""


@billing_bp.route("/stripe/success", methods=["GET"])
def stripe_success() -> Response:
    """Friendly confirmation page shown after a successful Stripe checkout."""
    return Response(_SUCCESS_PAGE_HTML, mimetype="text/html")


# ---------------------------------------------------------------------------
# Webhook event handlers
# ---------------------------------------------------------------------------

def _handle_checkout_completed(session: dict[str, Any]) -> None:
    """Provision the family and send a welcome WhatsApp after successful payment."""
    metadata: dict = session.get("metadata") or {}
    family_id: str = metadata.get("family_id", "")
    primary_phone: str = metadata.get("primary_phone", "")
    ref_code: str = metadata.get("ref_code", "").strip().upper()
    stripe_customer_id: str = session.get("customer", "") or ""
    stripe_subscription_id: str = session.get("subscription", "") or ""

    if not family_id or not primary_phone:
        logger.error(
            "checkout.session.completed missing family_id or primary_phone in metadata. "
            "session_id=%s", session.get("id", "unknown")
        )
        return

    logger.info(
        "Provisioning family %s (phone=%s, customer=%s, subscription=%s, ref=%s)",
        family_id, primary_phone, stripe_customer_id, stripe_subscription_id,
        ref_code or "none",
    )

    try:
        sb = _get_supabase()

        # 1. Upsert the families record (include referred_by if a ref code was used)
        now = datetime.now(timezone.utc).isoformat()
        families_row: dict[str, Any] = {
            "family_id": family_id,
            "primary_name": family_id,   # will be updated when user introduces themselves
            "primary_phone": primary_phone,
            "member_phones": [primary_phone],
            "plan": "monthly",
            "stripe_customer_id": stripe_customer_id,
            "stripe_subscription_id": stripe_subscription_id,
            "subscription_status": "active",
            "subscription_started_at": now,
            "status": "active",
            "created_at": now,
        }
        if ref_code:
            families_row["referred_by"] = ref_code

        sb.table("families").upsert(families_row).execute()
        logger.info("Families record upserted for %s", family_id)

        # 2. Register phone in whatsapp_members
        normalised_phone = primary_phone.replace("whatsapp:", "").strip()
        sb.table("whatsapp_members").upsert({
            "phone": normalised_phone,
            "family_id": family_id,
            "name": "Family Member",
            "created_at": now,
        }).execute()
        logger.info("Registered phone %s -> family %s", normalised_phone, family_id)

        # 3. Handle referral conversion tracking
        if ref_code:
            _handle_referral_conversion(sb, ref_code, family_id, now)

        # 4. Send welcome WhatsApp
        _send_welcome_whatsapp(primary_phone)

    except Exception as exc:
        import traceback
        logger.error(
            "Failed to provision family %s: %s\n%s",
            family_id, exc, traceback.format_exc(),
        )


def _handle_referral_conversion(
    sb: Any,
    ref_code: str,
    referred_family_id: str,
    now: str,
) -> None:
    """
    Record a referral conversion when a referred user completes their first payment.

    Steps:
      1. Look up the canonical referral row (owner's row: referred_family_id IS NULL)
      2. Increment uses_count on that row
      3. Insert a new conversion row linking the referring family to the new family
      4. Flag credit_issued=True on the canonical row once the first conversion lands
         (the free-month credit itself is applied manually / via a future automation)
      5. Send a proactive WhatsApp to the referrer notifying them of the conversion
    """
    try:
        # Find the canonical referral row for this code
        owner_res = (
            sb.table("referrals")
            .select("id, family_id, uses_count, user_phone, credit_issued")
            .eq("ref_code", ref_code)
            .is_("referred_family_id", "null")
            .limit(1)
            .execute()
        )
        if not owner_res.data:
            logger.warning(
                "Referral conversion: no canonical row found for ref_code=%s", ref_code
            )
            return

        owner_row = owner_res.data[0]
        owner_family_id: str = owner_row["family_id"]
        current_uses: int = owner_row.get("uses_count") or 0
        referrer_phone: str = owner_row.get("user_phone") or ""
        already_credited: bool = bool(owner_row.get("credit_issued"))

        new_uses = current_uses + 1

        # Increment uses_count on the canonical row; flag credit_issued on first conversion
        update_payload: dict[str, Any] = {"uses_count": new_uses}
        if not already_credited:
            update_payload["credit_issued"] = True  # flag for manual / automated credit

        sb.table("referrals").update(update_payload).eq("id", owner_row["id"]).execute()
        logger.info(
            "Referral conversion: ref_code=%s, referring_family=%s, new_uses=%d",
            ref_code, owner_family_id, new_uses,
        )

        # Insert a conversion row to record which family was referred
        sb.table("referrals").insert({
            "family_id": owner_family_id,
            "ref_code": ref_code,
            "referred_family_id": referred_family_id,
            "converted_at": now,
            "uses_count": 0,  # conversion rows don't track further uses
        }).execute()

        # Notify the referrer via WhatsApp if we have their phone number
        if referrer_phone:
            _notify_referrer(referrer_phone, new_uses)

    except Exception as exc:
        import traceback
        logger.error(
            "Referral conversion tracking failed for ref_code=%s: %s\n%s",
            ref_code, exc, traceback.format_exc(),
        )


def _notify_referrer(referrer_phone: str, total_conversions: int) -> None:
    """Send a WhatsApp notification to the referrer when someone converts."""
    to_number = (
        referrer_phone
        if referrer_phone.startswith("whatsapp:")
        else f"whatsapp:{referrer_phone}"
    )
    if total_conversions == 1:
        msg = (
            "\U0001f389 Great news! Someone just signed up to FamilyBrain using your referral link. "
            "You've earned a *free month* on your next billing cycle — we'll apply it automatically. "
            "Thanks for spreading the word!"
        )
    else:
        msg = (
            f"\U0001f389 Another referral converted! You've now referred {total_conversions} families to FamilyBrain. "
            "Your free month credit has been noted. Keep sharing!"
        )
    try:
        _send_wa(to_number, msg)
        logger.info("Referral conversion notification sent to %s", referrer_phone)
    except Exception as exc:
        logger.warning("Failed to send referral notification to %s: %s", referrer_phone, exc)


def _handle_subscription_deleted(subscription: dict[str, Any]) -> None:
    """Mark a family as inactive when their Stripe subscription is cancelled."""
    stripe_subscription_id: str = subscription.get("id", "")
    if not stripe_subscription_id:
        logger.warning("customer.subscription.deleted event missing subscription id")
        return
    try:
        sb = _get_supabase()
        sb.table("families").update({
            "subscription_status": "inactive",
            "status": "cancelled",
        }).eq("stripe_subscription_id", stripe_subscription_id).execute()
        logger.info("Marked subscription inactive: %s", stripe_subscription_id)
    except Exception as exc:
        logger.error("Failed to mark subscription inactive (%s): %s", stripe_subscription_id, exc)


def _send_welcome_whatsapp(primary_phone: str) -> None:
    """Send the post-payment welcome message via WhatsApp."""
    to_number = (
        primary_phone
        if primary_phone.startswith("whatsapp:")
        else f"whatsapp:{primary_phone}"
    )
    welcome_msg = (
        "\U0001f389 Payment confirmed! Welcome to FamilyBrain. "
        "Send me a message on WhatsApp to get started."
    )
    try:
        _send_wa(to_number, welcome_msg)
        logger.info("Welcome WhatsApp sent to %s", primary_phone)
    except Exception as exc:
        logger.error("Failed to send welcome WhatsApp to %s: %s", primary_phone, exc)


# ---------------------------------------------------------------------------
# Subscription status helper (used by whatsapp_capture.py)
# ---------------------------------------------------------------------------

def get_subscription_status(family_id: str) -> Optional[str]:
    """
    Return the subscription_status for a family_id, or None if not found.

    Returns one of: 'active', 'inactive', 'trialing', or None.
    Grandfathered families always return 'active'.

    Returns None when:
      - The family has no record in the database
      - A database error occurs
    Callers MUST treat None as "unknown / not subscribed" (fail closed).
    """
    if family_id in GRANDFATHERED_FAMILY_IDS:
        return "active"
    try:
        sb = _get_supabase()
        result = (
            sb.table("families")
            .select("subscription_status, status")
            .eq("family_id", family_id)
            .limit(1)
            .execute()
        )
        if result.data:
            row = result.data[0]
            # Prefer the new subscription_status column; fall back to legacy status
            sub_status = row.get("subscription_status")
            if sub_status:
                return sub_status
            # Legacy: map old status values
            legacy = row.get("status", "active")
            if legacy in ("active",):
                return "active"
            if legacy in ("cancelled", "paused"):
                return "inactive"
        return None
    except Exception as exc:
        logger.warning("Could not check subscription status for %s: %s", family_id, exc)
        return None  # Caller treats None as inactive (fail closed)


def is_subscription_active(family_id: str) -> bool:
    """
    Return True if the family has an active or trialing subscription.

    Grandfathered family-dan is always active.
    Fails CLOSED: returns False on DB errors or unknown families to prevent
    unauthorised access. This was changed from fail-open as a security fix.
    """
    if family_id in GRANDFATHERED_FAMILY_IDS:
        return True
    status = get_subscription_status(family_id)
    if status is None:
        # Security fix: unknown families are denied access (fail closed).
        # Previously this returned True, allowing unregistered phone numbers
        # to bypass the paywall entirely.
        logger.warning(
            "Subscription check: family_id=%s not found or DB error — denying access (fail-closed)",
            family_id,
        )
        return False
    return status in ("active", "trialing")

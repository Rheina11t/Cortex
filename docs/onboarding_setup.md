# FamilyBrain Onboarding Backend — Setup Guide

## Overview

The onboarding backend (`src/onboarding.py`) is a separate Flask service that handles:
1. New family sign-ups via the FamilyBrain.co.uk landing page
2. Stripe checkout and subscription management
3. Automatic family provisioning in Supabase
4. Welcome WhatsApp messages sent on payment confirmation

It runs alongside the WhatsApp capture layer as a second Railway service.

---

## Step 1: Run the Supabase Migration

In the Supabase SQL editor, run `migrations/010_onboarding.sql`.

This creates two tables:
- `families` — one row per paying customer
- `whatsapp_members` — maps phone numbers to family_id for message routing

---

## Step 2: Set Up Stripe

1. Create a Stripe account at [stripe.com](https://stripe.com)
2. Create three subscription products in the Stripe dashboard:
   - **Founding Member**: £9.99/month (recurring)
   - **Monthly**: £19.99/month (recurring)
   - **Annual**: £179/year (recurring)
3. Copy the Price IDs (start with `price_...`) for each plan
4. Set up a webhook in Stripe pointing to `https://your-onboarding-service.railway.app/stripe/webhook`
   - Events to listen for: `checkout.session.completed`, `customer.subscription.deleted`
5. Copy the webhook signing secret (starts with `whsec_...`)

---

## Step 3: Deploy to Railway

Create a **second Railway service** in the same project (or a new project):

```
Start command: python -m src.onboarding
Port: 8081
```

Set these environment variables:

| Variable | Value |
|----------|-------|
| `STRIPE_SECRET_KEY` | `sk_live_...` or `sk_test_...` |
| `STRIPE_WEBHOOK_SECRET` | `whsec_...` |
| `STRIPE_PRICE_ID_FOUNDING` | `price_...` |
| `STRIPE_PRICE_ID_MONTHLY` | `price_...` |
| `STRIPE_PRICE_ID_ANNUAL` | `price_...` |
| `SUPABASE_URL` | Same as main service |
| `SUPABASE_SERVICE_KEY` | Same as main service |
| `TWILIO_ACCOUNT_SID` | Same as main service |
| `TWILIO_AUTH_TOKEN` | Same as main service |
| `TWILIO_WHATSAPP_FROM` | `whatsapp:+447XXXXXXXXX` |
| `ONBOARDING_BASE_URL` | `https://familybrain.co.uk` |
| `GOOGLE_CALENDAR_OAUTH_URL` | Optional — URL of your Google Calendar OAuth flow |
| `FOUNDING_MEMBER_SLOTS` | `100` (default) |

---

## Step 4: Connect the Landing Page

The landing page sign-up form should POST to:
```
POST https://your-onboarding-service.railway.app/signup
Content-Type: application/json

{
  "primary_name": "Dan Smith",
  "primary_phone": "+447700900000",
  "member_phones": ["+447700900001"],
  "plan": "founding"
}
```

The response will include a `checkout_url` — redirect the user there.

After payment, Stripe redirects to `ONBOARDING_BASE_URL/signup/return?status=success`.

---

## Step 5: Update the WhatsApp Capture Layer

The WhatsApp capture layer (`src/whatsapp_capture.py`) has already been updated to:
- Look up incoming phone numbers in the `whatsapp_members` table
- Route each message to the correct family's data (using `family_id`)
- Fall back to env-var config for existing single-family deployments

No additional changes needed.

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/signup` | POST | Create Stripe checkout session |
| `/signup/return` | GET | Stripe redirect after payment |
| `/stripe/webhook` | POST | Stripe webhook handler |
| `/founding-slots` | GET | Remaining founding member slots |

---

## Testing

Use Stripe test mode (`sk_test_...`) and test card `4242 4242 4242 4242` to test the full flow without real payments.

For WhatsApp testing, use the Twilio sandbox (see main README).

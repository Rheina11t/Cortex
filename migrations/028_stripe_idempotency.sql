-- ============================================================================
-- Migration 028: Stripe Webhook Idempotency
-- ============================================================================
-- Phase 2 security hardening:
--   Adds a table to track processed Stripe webhook events and prevent
--   duplicate processing.  Stripe retries failed webhooks for up to 3 days,
--   so this table acts as a deduplication guard.
-- ============================================================================

CREATE TABLE IF NOT EXISTS processed_stripe_events (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id      TEXT NOT NULL UNIQUE,
    event_type    TEXT NOT NULL,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fast lookup by Stripe event ID (hot path on every webhook)
CREATE INDEX IF NOT EXISTS idx_processed_stripe_events_event_id
    ON processed_stripe_events (event_id);

-- RLS: service role only (consistent with other tables)
ALTER TABLE processed_stripe_events ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Service role has full access to processed_stripe_events"
    ON processed_stripe_events FOR ALL USING (true) WITH CHECK (true);

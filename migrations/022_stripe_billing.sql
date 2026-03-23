-- ==========================================================================
-- Migration 022: Stripe Billing Integration
-- ==========================================================================
-- Adds columns to the families table to support Stripe subscriptions.
-- ==========================================================================

-- Add Stripe billing columns to families table
ALTER TABLE families
ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT,
ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT,
ADD COLUMN IF NOT EXISTS subscription_status TEXT DEFAULT 'active',
ADD COLUMN IF NOT EXISTS subscription_started_at TIMESTAMPTZ;

-- Index for fast lookups by Stripe subscription ID (if not already created in 010)
CREATE INDEX IF NOT EXISTS idx_families_stripe_subscription_id
    ON families (stripe_subscription_id);

-- Index for fast lookups by Stripe customer ID
CREATE INDEX IF NOT EXISTS idx_families_stripe_customer_id
    ON families (stripe_customer_id);

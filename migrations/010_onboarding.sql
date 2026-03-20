-- ---------------------------------------------------------------------------
-- Migration 010: FamilyBrain Onboarding Tables
-- ---------------------------------------------------------------------------
-- Creates two tables needed for the onboarding backend:
--   families         — one row per paying customer family
--   whatsapp_members — maps WhatsApp phone numbers to family_id for routing
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- 1. families table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS families (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id               TEXT NOT NULL UNIQUE,          -- e.g. family_a1b2c3d4e5f6
    primary_name            TEXT NOT NULL,
    primary_phone           TEXT NOT NULL,
    member_phones           TEXT[] DEFAULT '{}',
    plan                    TEXT NOT NULL DEFAULT 'monthly', -- founding | monthly | annual
    stripe_customer_id      TEXT,
    stripe_subscription_id  TEXT,
    status                  TEXT NOT NULL DEFAULT 'active', -- active | cancelled | paused
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for Stripe lookups
CREATE INDEX IF NOT EXISTS idx_families_stripe_subscription
    ON families (stripe_subscription_id);

-- Index for phone lookups
CREATE INDEX IF NOT EXISTS idx_families_primary_phone
    ON families (primary_phone);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_families_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS families_updated_at ON families;
CREATE TRIGGER families_updated_at
    BEFORE UPDATE ON families
    FOR EACH ROW EXECUTE FUNCTION update_families_updated_at();

-- RLS
ALTER TABLE families ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role has full access to families"
    ON families FOR ALL USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- 2. whatsapp_members table
-- ---------------------------------------------------------------------------
-- Maps each WhatsApp phone number to a family_id.
-- The WhatsApp capture layer queries this table to route incoming messages.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS whatsapp_members (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone       TEXT NOT NULL UNIQUE,   -- E.164 format, e.g. +447700900000
    family_id   TEXT NOT NULL,          -- references families.family_id
    name        TEXT,                   -- optional display name
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Index for fast phone lookups (the hot path on every incoming message)
CREATE INDEX IF NOT EXISTS idx_whatsapp_members_phone
    ON whatsapp_members (phone);

CREATE INDEX IF NOT EXISTS idx_whatsapp_members_family_id
    ON whatsapp_members (family_id);

-- RLS
ALTER TABLE whatsapp_members ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role has full access to whatsapp_members"
    ON whatsapp_members FOR ALL USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
-- Done. Run this migration in the Supabase SQL editor.
-- ---------------------------------------------------------------------------

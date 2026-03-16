-- ==========================================================================
-- Migration 003: Household Knowledge Schema
-- ==========================================================================
-- Adds tables for tracking household items (paint colours, appliances,
-- measurements, warranties) and trusted vendors/tradespeople.
--
-- Run this in the Supabase SQL Editor:
--   1. Go to your project dashboard → SQL Editor → New Query
--   2. Paste this entire file and click RUN
-- ==========================================================================

-- ── household_items ────────────────────────────────────────────────────────
-- Stores facts about the home: paint colours, appliance models, room
-- measurements, warranty info, and anything else worth remembering.

CREATE TABLE IF NOT EXISTS household_items (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     TEXT NOT NULL,                       -- family member identifier (e.g. "Dan", "Sarah")
    name        TEXT NOT NULL,                       -- item name (e.g. "Living room paint")
    category    TEXT NOT NULL DEFAULT 'other',       -- paint | appliance | measurement | warranty | other
    location    TEXT DEFAULT '',                     -- room or area (e.g. "Kitchen", "Master bedroom")
    details     JSONB DEFAULT '{}'::JSONB,           -- flexible metadata (colour code, model, dimensions…)
    notes       TEXT DEFAULT '',                     -- free-form notes
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for fast lookups by category and location
CREATE INDEX IF NOT EXISTS idx_household_items_category ON household_items (category);
CREATE INDEX IF NOT EXISTS idx_household_items_location ON household_items (location);
CREATE INDEX IF NOT EXISTS idx_household_items_user_id  ON household_items (user_id);

-- Full-text search index on name and notes
CREATE INDEX IF NOT EXISTS idx_household_items_name_trgm
    ON household_items USING gin (name gin_trgm_ops);

-- Auto-update updated_at on row changes
CREATE OR REPLACE FUNCTION update_household_items_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_household_items_updated_at ON household_items;
CREATE TRIGGER trg_household_items_updated_at
    BEFORE UPDATE ON household_items
    FOR EACH ROW
    EXECUTE FUNCTION update_household_items_updated_at();

-- Enable RLS (service_role key bypasses RLS)
ALTER TABLE household_items ENABLE ROW LEVEL SECURITY;


-- ── household_vendors ──────────────────────────────────────────────────────
-- Stores contact details for trusted tradespeople and service providers.

CREATE TABLE IF NOT EXISTS household_vendors (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         TEXT NOT NULL,                   -- who added this vendor
    name            TEXT NOT NULL,                   -- vendor/person name
    trade           TEXT NOT NULL DEFAULT 'other',   -- plumber | electrician | painter | builder | gardener | cleaner | other
    phone           TEXT DEFAULT '',
    email           TEXT DEFAULT '',
    last_used_date  DATE,                            -- when they were last used
    rating          INTEGER CHECK (rating >= 1 AND rating <= 5),  -- 1-5 star rating
    notes           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_household_vendors_trade   ON household_vendors (trade);
CREATE INDEX IF NOT EXISTS idx_household_vendors_user_id ON household_vendors (user_id);

CREATE INDEX IF NOT EXISTS idx_household_vendors_name_trgm
    ON household_vendors USING gin (name gin_trgm_ops);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_household_vendors_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_household_vendors_updated_at ON household_vendors;
CREATE TRIGGER trg_household_vendors_updated_at
    BEFORE UPDATE ON household_vendors
    FOR EACH ROW
    EXECUTE FUNCTION update_household_vendors_updated_at();

-- Enable RLS
ALTER TABLE household_vendors ENABLE ROW LEVEL SECURITY;


-- ── Enable pg_trgm extension (required for gin_trgm_ops indexes) ──────────
-- This must be run before the indexes above will work.  If the extension
-- is already enabled, this is a no-op.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ==========================================================================
-- Done.  Verify with:
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public'
--     AND table_name IN ('household_items', 'household_vendors');
-- ==========================================================================

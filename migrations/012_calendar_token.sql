-- ==========================================================================
-- Migration 012: Calendar Token & Family Scoping for Events
-- ==========================================================================
-- Adds calendar_token to the families table so each family gets a unique,
-- unguessable URL for their read-only kitchen calendar page.
-- Also adds family_id to family_events to support multi-tenant event storage.
-- ==========================================================================

-- 1. Add calendar_token column to families table
ALTER TABLE families ADD COLUMN IF NOT EXISTS calendar_token TEXT UNIQUE;

-- 2. Add family_id column to family_events table (multi-tenant scoping)
ALTER TABLE family_events ADD COLUMN IF NOT EXISTS family_id TEXT DEFAULT 'family-dan';

-- 3. Indexes for fast lookups
CREATE INDEX IF NOT EXISTS idx_families_calendar_token
    ON families (calendar_token);

CREATE INDEX IF NOT EXISTS idx_family_events_family_id
    ON family_events (family_id);

-- 4. Backfill existing family_events rows with the default family_id
UPDATE family_events
SET family_id = 'family-dan'
WHERE family_id IS NULL;

-- ==========================================================================
-- Verify with:
--   SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'families' AND column_name = 'calendar_token';
--
--   SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'family_events' AND column_name = 'family_id';
-- ==========================================================================

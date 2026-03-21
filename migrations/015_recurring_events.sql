-- ==========================================================================
-- Migration 015: Recurring Events Support
-- Adds recurrence metadata columns to family_events and a google_event_id
-- column so we can update/delete Google Calendar recurring events later.
-- ==========================================================================

ALTER TABLE family_events ADD COLUMN IF NOT EXISTS is_recurring BOOLEAN DEFAULT FALSE;
ALTER TABLE family_events ADD COLUMN IF NOT EXISTS recurrence_rule TEXT;
ALTER TABLE family_events ADD COLUMN IF NOT EXISTS recurrence_end DATE;
ALTER TABLE family_events ADD COLUMN IF NOT EXISTS google_event_id TEXT;

-- Index to quickly find recurring events for a family
CREATE INDEX IF NOT EXISTS idx_family_events_is_recurring
    ON family_events (family_id, is_recurring)
    WHERE is_recurring = TRUE;

-- ==========================================================================
-- Done.  Verify with:
--   SELECT column_name FROM information_schema.columns
--   WHERE table_name = 'family_events'
--   AND column_name IN ('is_recurring','recurrence_rule','recurrence_end','google_event_id');
-- ==========================================================================

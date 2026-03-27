-- ==========================================================================
-- Migration 025: Binder Progress Cache
-- ==========================================================================
-- Caches the per-item checklist state for each family's death binder.
-- Computed on-the-fly when /binder is called, then written here so that:
--   (a) proactive nudges can cheaply read last-known state
--   (b) we can detect when a new item tips a category from incomplete → complete
--
-- Run in Supabase SQL Editor → New Query → Paste → RUN
-- ==========================================================================

-- ── binder_progress ──────────────────────────────────────────────────────────
-- One row per family. Stores the full checklist state as JSONB so we never
-- need to alter columns when the checklist definition changes.
CREATE TABLE IF NOT EXISTS binder_progress (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id           TEXT NOT NULL UNIQUE,

    -- Snapshot of item-level completion: {"1": {"will": true, "lpa_health": false, ...}, ...}
    item_state          JSONB NOT NULL DEFAULT '{}',

    -- Derived summary fields (denormalised for cheap reads)
    items_complete      INTEGER NOT NULL DEFAULT 0,   -- total checked items
    items_total         INTEGER NOT NULL DEFAULT 0,   -- total checklist items
    cats_complete       INTEGER NOT NULL DEFAULT 0,   -- categories where ALL items are ✅
    cats_total          INTEGER NOT NULL DEFAULT 10,

    -- Percentage 0-100 (items_complete / items_total * 100)
    pct_complete        INTEGER NOT NULL DEFAULT 0,

    -- ISO timestamp of the last time we sent a proactive nudge
    last_nudge_at       TIMESTAMPTZ,

    -- The pct_complete value at the time of the last nudge
    last_nudge_pct      INTEGER NOT NULL DEFAULT 0,

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_binder_progress_family ON binder_progress (family_id);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_binder_progress_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_binder_progress_updated_at ON binder_progress;
CREATE TRIGGER trg_binder_progress_updated_at
    BEFORE UPDATE ON binder_progress
    FOR EACH ROW
    EXECUTE FUNCTION update_binder_progress_updated_at();

ALTER TABLE binder_progress ENABLE ROW LEVEL SECURITY;

-- Service role has full access to binder_progress
CREATE POLICY "Service role has full access to binder_progress"
    ON binder_progress
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ==========================================================================
-- Done.  Verify with:
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public' AND table_name = 'binder_progress';
-- ==========================================================================

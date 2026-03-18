-- ==========================================================================
-- Migration 004: Family Events / Scheduling Schema
-- ==========================================================================

CREATE TABLE IF NOT EXISTS family_events (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_member       TEXT NOT NULL,
    event_name          TEXT NOT NULL,
    event_date          DATE NOT NULL,
    event_time          TIME,
    end_date            DATE,
    location            TEXT DEFAULT '',
    recurring           BOOLEAN NOT NULL DEFAULT FALSE,
    recurrence_pattern  TEXT DEFAULT '',
    requirements        TEXT[] DEFAULT '{}',
    notes               TEXT DEFAULT '',
    source              TEXT DEFAULT 'manual',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes for fast date-range and member queries
CREATE INDEX IF NOT EXISTS idx_family_events_date   ON family_events (event_date);
CREATE INDEX IF NOT EXISTS idx_family_events_member ON family_events (family_member);
CREATE INDEX IF NOT EXISTS idx_family_events_date_member
    ON family_events (event_date, family_member);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_family_events_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_family_events_updated_at ON family_events;
CREATE TRIGGER trg_family_events_updated_at
    BEFORE UPDATE ON family_events
    FOR EACH ROW
    EXECUTE FUNCTION update_family_events_updated_at();

-- ── Conflict detection function ────────────────────────────────────────────
CREATE OR REPLACE FUNCTION check_schedule_conflicts(
    check_date DATE,
    check_member TEXT DEFAULT NULL
)
RETURNS TABLE (
    event_id        UUID,
    member          TEXT,
    name            TEXT,
    edate           DATE,
    etime           TIME,
    elocation       TEXT,
    enotes          TEXT
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    SELECT
        fe.id,
        fe.family_member,
        fe.event_name,
        fe.event_date,
        fe.event_time,
        fe.location,
        fe.notes
    FROM family_events fe
    WHERE fe.event_date = check_date
      AND (check_member IS NULL OR fe.family_member = check_member
           OR fe.family_member = 'family')
    ORDER BY fe.event_time NULLS FIRST;
END;
$$;

-- Enable RLS
ALTER TABLE family_events ENABLE ROW LEVEL SECURITY;

-- ==========================================================================
-- Done.  Verify with:
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public' AND table_name = 'family_events';
-- ==========================================================================

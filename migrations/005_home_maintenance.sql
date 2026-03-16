-- ==========================================================================
-- Migration 005: Home Maintenance Tracker
-- ==========================================================================
-- Tables for tracking recurring home maintenance tasks and logging
-- completed work with costs.
--
-- Run in Supabase SQL Editor → New Query → Paste → RUN
-- ==========================================================================

-- ── maintenance_tasks ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS maintenance_tasks (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         TEXT NOT NULL DEFAULT 'family',
    title           TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'other'
                    CHECK (category IN ('hvac','plumbing','electrical','garden','appliance','structural','cleaning','other')),
    location        TEXT DEFAULT '',
    frequency_days  INTEGER,                            -- NULL = one-off task
    last_completed  DATE,
    next_due        DATE,
    notes           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_maintenance_tasks_category  ON maintenance_tasks (category);
CREATE INDEX IF NOT EXISTS idx_maintenance_tasks_next_due  ON maintenance_tasks (next_due);
CREATE INDEX IF NOT EXISTS idx_maintenance_tasks_user_id   ON maintenance_tasks (user_id);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_maintenance_tasks_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_maintenance_tasks_updated_at ON maintenance_tasks;
CREATE TRIGGER trg_maintenance_tasks_updated_at
    BEFORE UPDATE ON maintenance_tasks
    FOR EACH ROW
    EXECUTE FUNCTION update_maintenance_tasks_updated_at();

ALTER TABLE maintenance_tasks ENABLE ROW LEVEL SECURITY;


-- ── maintenance_logs ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS maintenance_logs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id         UUID NOT NULL REFERENCES maintenance_tasks(id) ON DELETE CASCADE,
    completed_date  DATE NOT NULL DEFAULT CURRENT_DATE,
    performed_by    TEXT DEFAULT '',
    cost_gbp        DECIMAL(10,2) DEFAULT 0.00,
    notes           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_maintenance_logs_task_id ON maintenance_logs (task_id);
CREATE INDEX IF NOT EXISTS idx_maintenance_logs_date    ON maintenance_logs (completed_date);

ALTER TABLE maintenance_logs ENABLE ROW LEVEL SECURITY;


-- ── Helper function: auto-update task after logging maintenance ──────────
-- Call after inserting a maintenance_log to update the parent task's
-- last_completed and next_due fields.
CREATE OR REPLACE FUNCTION update_task_after_log()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE maintenance_tasks
    SET last_completed = NEW.completed_date,
        next_due = CASE
            WHEN frequency_days IS NOT NULL THEN NEW.completed_date + frequency_days
            ELSE NULL
        END
    WHERE id = NEW.task_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_update_task_after_log ON maintenance_logs;
CREATE TRIGGER trg_update_task_after_log
    AFTER INSERT ON maintenance_logs
    FOR EACH ROW
    EXECUTE FUNCTION update_task_after_log();

-- ==========================================================================
-- Done.  Verify with:
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public'
--     AND table_name IN ('maintenance_tasks', 'maintenance_logs');
-- ==========================================================================

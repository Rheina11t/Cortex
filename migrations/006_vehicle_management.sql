-- ==========================================================================
-- Migration 006: Vehicle Management
-- ==========================================================================
-- Tables for tracking family vehicles, service history, and MOT/insurance
-- reminders.
--
-- Run in Supabase SQL Editor → New Query → Paste → RUN
-- ==========================================================================

-- ── vehicles ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vehicles (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id         TEXT NOT NULL DEFAULT 'family',
    nickname        TEXT NOT NULL,                       -- e.g. "Dan's Golf", "Emma's Fiat"
    make            TEXT DEFAULT '',
    model           TEXT DEFAULT '',
    year            INTEGER,
    registration    TEXT DEFAULT '',                     -- UK reg plate
    colour          TEXT DEFAULT '',
    mot_due         DATE,
    insurance_due   DATE,
    tax_due         DATE,
    mileage         INTEGER,
    notes           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vehicles_user_id ON vehicles (user_id);
CREATE INDEX IF NOT EXISTS idx_vehicles_mot_due ON vehicles (mot_due);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_vehicles_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_vehicles_updated_at ON vehicles;
CREATE TRIGGER trg_vehicles_updated_at
    BEFORE UPDATE ON vehicles
    FOR EACH ROW
    EXECUTE FUNCTION update_vehicles_updated_at();

ALTER TABLE vehicles ENABLE ROW LEVEL SECURITY;


-- ── vehicle_service_logs ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vehicle_service_logs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    vehicle_id      UUID NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
    service_date    DATE NOT NULL DEFAULT CURRENT_DATE,
    service_type    TEXT NOT NULL DEFAULT 'other'
                    CHECK (service_type IN ('mot','service','repair','tyre','insurance','tax','fuel','other')),
    description     TEXT DEFAULT '',
    mileage_at      INTEGER,
    cost_gbp        DECIMAL(10,2) DEFAULT 0.00,
    garage          TEXT DEFAULT '',                     -- who did the work
    notes           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_vehicle_service_logs_vehicle_id ON vehicle_service_logs (vehicle_id);
CREATE INDEX IF NOT EXISTS idx_vehicle_service_logs_date       ON vehicle_service_logs (service_date);
CREATE INDEX IF NOT EXISTS idx_vehicle_service_logs_type       ON vehicle_service_logs (service_type);

ALTER TABLE vehicle_service_logs ENABLE ROW LEVEL SECURITY;

-- ==========================================================================
-- Done.  Verify with:
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public'
--     AND table_name IN ('vehicles', 'vehicle_service_logs');
-- ==========================================================================

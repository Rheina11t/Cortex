-- ==========================================================================
-- Migration 007: Health & Wellness Tracker
-- ==========================================================================
-- Tables for tracking family health metrics, medications, and medical
-- appointments.
--
-- Run in Supabase SQL Editor → New Query → Paste → RUN
-- ==========================================================================

-- ── health_metrics ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS health_metrics (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_member   TEXT NOT NULL,                       -- "Dan", "Emma"
    metric_type     TEXT NOT NULL
                    CHECK (metric_type IN ('weight','blood_pressure','heart_rate','sleep_hours',
                                           'steps','exercise_minutes','water_litres','mood','other')),
    value           DECIMAL(10,2) NOT NULL,              -- primary value
    unit            TEXT DEFAULT '',                     -- e.g. "kg", "mmHg", "bpm"
    secondary_value DECIMAL(10,2),                       -- e.g. diastolic for BP
    notes           TEXT DEFAULT '',
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_health_metrics_member ON health_metrics (family_member);
CREATE INDEX IF NOT EXISTS idx_health_metrics_type   ON health_metrics (metric_type);
CREATE INDEX IF NOT EXISTS idx_health_metrics_date   ON health_metrics (recorded_at);

ALTER TABLE health_metrics ENABLE ROW LEVEL SECURITY;


-- ── medications ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS medications (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_member   TEXT NOT NULL,
    name            TEXT NOT NULL,                       -- medication name
    dosage          TEXT DEFAULT '',                     -- e.g. "10mg"
    frequency       TEXT DEFAULT '',                     -- e.g. "twice daily", "as needed"
    prescriber      TEXT DEFAULT '',                     -- doctor name
    pharmacy        TEXT DEFAULT '',
    start_date      DATE,
    end_date        DATE,                                -- NULL = ongoing
    refill_due      DATE,
    notes           TEXT DEFAULT '',
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_medications_member ON medications (family_member);
CREATE INDEX IF NOT EXISTS idx_medications_active ON medications (is_active);
CREATE INDEX IF NOT EXISTS idx_medications_refill ON medications (refill_due);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_medications_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_medications_updated_at ON medications;
CREATE TRIGGER trg_medications_updated_at
    BEFORE UPDATE ON medications
    FOR EACH ROW
    EXECUTE FUNCTION update_medications_updated_at();

ALTER TABLE medications ENABLE ROW LEVEL SECURITY;


-- ── medical_appointments ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS medical_appointments (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_member   TEXT NOT NULL,
    appointment_type TEXT NOT NULL DEFAULT 'general'
                    CHECK (appointment_type IN ('gp','dentist','optician','specialist','hospital',
                                                'physio','mental_health','vaccination','screening','general','other')),
    provider        TEXT DEFAULT '',                     -- doctor/clinic name
    location        TEXT DEFAULT '',
    appointment_date DATE NOT NULL,
    appointment_time TIME,
    notes           TEXT DEFAULT '',
    outcome         TEXT DEFAULT '',                     -- filled in after the appointment
    follow_up_date  DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_medical_appts_member ON medical_appointments (family_member);
CREATE INDEX IF NOT EXISTS idx_medical_appts_date   ON medical_appointments (appointment_date);
CREATE INDEX IF NOT EXISTS idx_medical_appts_type   ON medical_appointments (appointment_type);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_medical_appointments_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_medical_appointments_updated_at ON medical_appointments;
CREATE TRIGGER trg_medical_appointments_updated_at
    BEFORE UPDATE ON medical_appointments
    FOR EACH ROW
    EXECUTE FUNCTION update_medical_appointments_updated_at();

ALTER TABLE medical_appointments ENABLE ROW LEVEL SECURITY;

-- ==========================================================================
-- Done.  Verify with:
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public'
--     AND table_name IN ('health_metrics', 'medications', 'medical_appointments');
-- ==========================================================================

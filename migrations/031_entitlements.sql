-- Migration 031: Feature entitlements table (Phase 5 gap analysis)
-- Maps subscription plans to feature access levels.
-- Used by check_entitlement() to enforce plan-based feature gates.

CREATE TABLE IF NOT EXISTS entitlements (
    id              BIGSERIAL PRIMARY KEY,
    plan            TEXT NOT NULL,                -- 'founding', 'monthly', 'annual', 'free_trial'
    feature         TEXT NOT NULL,                -- e.g. 'sos_pdf', 'death_binder', 'web_search', 'gcal_sync'
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    max_per_day     INTEGER DEFAULT NULL,         -- NULL = unlimited
    max_per_month   INTEGER DEFAULT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (plan, feature)
);

-- Seed default entitlements for each plan
INSERT INTO entitlements (plan, feature, enabled, max_per_day, max_per_month) VALUES
    -- Founding members (full access)
    ('founding', 'sos_pdf',         TRUE, NULL, NULL),
    ('founding', 'death_binder',    TRUE, NULL, NULL),
    ('founding', 'web_search',      TRUE, 50,   1000),
    ('founding', 'gcal_sync',       TRUE, NULL, NULL),
    ('founding', 'query',           TRUE, 100,  3000),
    ('founding', 'memory_store',    TRUE, 200,  5000),
    ('founding', 'family_invites',  TRUE, NULL, NULL),
    ('founding', 'briefings',       TRUE, NULL, NULL),
    -- Monthly plan
    ('monthly', 'sos_pdf',          TRUE, NULL, NULL),
    ('monthly', 'death_binder',     TRUE, NULL, NULL),
    ('monthly', 'web_search',       TRUE, 30,   500),
    ('monthly', 'gcal_sync',        TRUE, NULL, NULL),
    ('monthly', 'query',            TRUE, 75,   2000),
    ('monthly', 'memory_store',     TRUE, 150,  3000),
    ('monthly', 'family_invites',   TRUE, NULL, NULL),
    ('monthly', 'briefings',        TRUE, NULL, NULL),
    -- Annual plan (same as monthly but higher limits)
    ('annual', 'sos_pdf',           TRUE, NULL, NULL),
    ('annual', 'death_binder',      TRUE, NULL, NULL),
    ('annual', 'web_search',        TRUE, 50,   1000),
    ('annual', 'gcal_sync',         TRUE, NULL, NULL),
    ('annual', 'query',             TRUE, 100,  3000),
    ('annual', 'memory_store',      TRUE, 200,  5000),
    ('annual', 'family_invites',    TRUE, NULL, NULL),
    ('annual', 'briefings',         TRUE, NULL, NULL)
ON CONFLICT (plan, feature) DO NOTHING;

-- Index for fast lookups
CREATE INDEX IF NOT EXISTS idx_entitlements_plan ON entitlements (plan);

-- RLS: entitlements are read-only for authenticated users
ALTER TABLE entitlements ENABLE ROW LEVEL SECURITY;
CREATE POLICY entitlements_read_all ON entitlements FOR SELECT USING (TRUE);

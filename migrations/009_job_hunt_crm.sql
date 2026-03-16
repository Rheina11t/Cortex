-- ==========================================================================
-- Migration 009: Job Hunt CRM
-- ==========================================================================
-- Tables for tracking job applications, interviews, contacts, and the
-- full recruitment pipeline.
--
-- Run in Supabase SQL Editor → New Query → Paste → RUN
-- ==========================================================================

-- ── jh_contacts ───────────────────────────────────────────────────────────
-- Recruiters, hiring managers, referrals, and other professional contacts.
CREATE TABLE IF NOT EXISTS jh_contacts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,
    company         TEXT DEFAULT '',
    role            TEXT DEFAULT '',                     -- their role (e.g. "Recruiter", "VP Engineering")
    email           TEXT DEFAULT '',
    phone           TEXT DEFAULT '',
    linkedin_url    TEXT DEFAULT '',
    relationship    TEXT DEFAULT 'recruiter'
                    CHECK (relationship IN ('recruiter','hiring_manager','referral','peer','mentor','other')),
    notes           TEXT DEFAULT '',
    last_contact    DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jh_contacts_company ON jh_contacts (company);
CREATE INDEX IF NOT EXISTS idx_jh_contacts_relationship ON jh_contacts (relationship);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_jh_contacts_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jh_contacts_updated_at ON jh_contacts;
CREATE TRIGGER trg_jh_contacts_updated_at
    BEFORE UPDATE ON jh_contacts
    FOR EACH ROW
    EXECUTE FUNCTION update_jh_contacts_updated_at();

ALTER TABLE jh_contacts ENABLE ROW LEVEL SECURITY;


-- ── jh_applications ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jh_applications (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company         TEXT NOT NULL,
    job_title       TEXT NOT NULL,
    url             TEXT DEFAULT '',                     -- job listing URL
    salary_min      INTEGER,                             -- annual, GBP
    salary_max      INTEGER,
    requirements    TEXT DEFAULT '',                     -- key requirements
    source          TEXT DEFAULT '',                     -- where the job was found
    status          TEXT NOT NULL DEFAULT 'identified'
                    CHECK (status IN ('identified','applied','screening','interviewing',
                                      'offer','accepted','rejected','withdrawn','ghosted')),
    applied_date    DATE,
    resume_version  TEXT DEFAULT '',
    cover_letter_notes TEXT DEFAULT '',
    contact_id      UUID REFERENCES jh_contacts(id),    -- primary contact
    notes           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jh_applications_status  ON jh_applications (status);
CREATE INDEX IF NOT EXISTS idx_jh_applications_company ON jh_applications (company);
CREATE INDEX IF NOT EXISTS idx_jh_applications_date    ON jh_applications (applied_date);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_jh_applications_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jh_applications_updated_at ON jh_applications;
CREATE TRIGGER trg_jh_applications_updated_at
    BEFORE UPDATE ON jh_applications
    FOR EACH ROW
    EXECUTE FUNCTION update_jh_applications_updated_at();

ALTER TABLE jh_applications ENABLE ROW LEVEL SECURITY;


-- ── jh_interviews ─────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS jh_interviews (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    application_id  UUID NOT NULL REFERENCES jh_applications(id) ON DELETE CASCADE,
    interview_type  TEXT NOT NULL DEFAULT 'phone'
                    CHECK (interview_type IN ('phone','video','onsite','technical','panel',
                                              'case_study','presentation','final','other')),
    scheduled_at    TIMESTAMPTZ NOT NULL,
    duration_minutes INTEGER DEFAULT 60,
    interviewer_name TEXT DEFAULT '',
    interviewer_role TEXT DEFAULT '',
    location        TEXT DEFAULT '',                     -- URL for video, address for onsite
    status          TEXT NOT NULL DEFAULT 'scheduled'
                    CHECK (status IN ('scheduled','completed','cancelled','rescheduled','no_show')),
    feedback        TEXT DEFAULT '',
    rating          INTEGER CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5)),
    notes           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jh_interviews_app_id   ON jh_interviews (application_id);
CREATE INDEX IF NOT EXISTS idx_jh_interviews_date     ON jh_interviews (scheduled_at);
CREATE INDEX IF NOT EXISTS idx_jh_interviews_status   ON jh_interviews (status);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_jh_interviews_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_jh_interviews_updated_at ON jh_interviews;
CREATE TRIGGER trg_jh_interviews_updated_at
    BEFORE UPDATE ON jh_interviews
    FOR EACH ROW
    EXECUTE FUNCTION update_jh_interviews_updated_at();

ALTER TABLE jh_interviews ENABLE ROW LEVEL SECURITY;


-- ── professional_contacts (linked from jh_contacts for long-term CRM) ────
CREATE TABLE IF NOT EXISTS professional_contacts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    jh_contact_id   UUID REFERENCES jh_contacts(id),    -- optional link back
    name            TEXT NOT NULL,
    company         TEXT DEFAULT '',
    role            TEXT DEFAULT '',
    email           TEXT DEFAULT '',
    phone           TEXT DEFAULT '',
    linkedin_url    TEXT DEFAULT '',
    category        TEXT DEFAULT 'professional'
                    CHECK (category IN ('professional','mentor','peer','client','vendor','other')),
    notes           TEXT DEFAULT '',
    last_contact    DATE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_prof_contacts_company ON professional_contacts (company);
CREATE INDEX IF NOT EXISTS idx_prof_contacts_category ON professional_contacts (category);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_professional_contacts_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_professional_contacts_updated_at ON professional_contacts;
CREATE TRIGGER trg_professional_contacts_updated_at
    BEFORE UPDATE ON professional_contacts
    FOR EACH ROW
    EXECUTE FUNCTION update_professional_contacts_updated_at();

ALTER TABLE professional_contacts ENABLE ROW LEVEL SECURITY;


-- ── Helper view: pipeline overview ────────────────────────────────────────
CREATE OR REPLACE VIEW jh_pipeline_overview AS
SELECT
    status,
    COUNT(*)                                         AS count,
    ARRAY_AGG(company || ' — ' || job_title)         AS roles
FROM jh_applications
GROUP BY status
ORDER BY
    CASE status
        WHEN 'offer'        THEN 1
        WHEN 'interviewing' THEN 2
        WHEN 'screening'    THEN 3
        WHEN 'applied'      THEN 4
        WHEN 'identified'   THEN 5
        WHEN 'accepted'     THEN 6
        WHEN 'rejected'     THEN 7
        WHEN 'withdrawn'    THEN 8
        WHEN 'ghosted'      THEN 9
    END;

-- ==========================================================================
-- Done.  Verify with:
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public'
--     AND table_name IN ('jh_contacts', 'jh_applications', 'jh_interviews', 'professional_contacts');
-- ==========================================================================

-- Migration 024: Death Binder — structured storage for /sos emergency file categories
-- Adds family_id to existing tables that lack it, creates death_binder_entries for
-- free-text category data, and adds a recurring_bills table for bills/debts.

-- ============================================================
-- 1. Add family_id to tables that currently lack it
-- ============================================================

ALTER TABLE financial_accounts ADD COLUMN IF NOT EXISTS family_id TEXT NOT NULL DEFAULT 'family-dan';
ALTER TABLE bills ADD COLUMN IF NOT EXISTS family_id TEXT NOT NULL DEFAULT 'family-dan';
ALTER TABLE medications ADD COLUMN IF NOT EXISTS family_id TEXT NOT NULL DEFAULT 'family-dan';
ALTER TABLE medical_appointments ADD COLUMN IF NOT EXISTS family_id TEXT NOT NULL DEFAULT 'family-dan';
ALTER TABLE health_records ADD COLUMN IF NOT EXISTS family_id TEXT NOT NULL DEFAULT 'family-dan';
ALTER TABLE vehicles ADD COLUMN IF NOT EXISTS family_id TEXT NOT NULL DEFAULT 'family-dan';
ALTER TABLE professional_contacts ADD COLUMN IF NOT EXISTS family_id TEXT NOT NULL DEFAULT 'family-dan';

-- ============================================================
-- 2. death_binder_entries — structured free-text entries per category
--    Used by /funeral, /digital, /legal, /contacts, /binder commands
-- ============================================================

CREATE TABLE IF NOT EXISTS death_binder_entries (
    id              UUID PRIMARY KEY DEFAULT extensions.uuid_generate_v4(),
    family_id       TEXT NOT NULL,
    category        TEXT NOT NULL,   -- '1'..'10' matching CATEGORIES dict
    subcategory     TEXT NOT NULL DEFAULT '',  -- e.g. 'funeral_wishes', 'crypto', 'will'
    label           TEXT NOT NULL DEFAULT '',  -- human label, e.g. 'Barclays Current Account'
    value           TEXT NOT NULL DEFAULT '',  -- free-text value stored by user
    notes           TEXT NOT NULL DEFAULT '',
    source_phone    TEXT NOT NULL DEFAULT '',  -- WhatsApp number that stored this
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS death_binder_entries_family_id_idx ON death_binder_entries (family_id);
CREATE INDEX IF NOT EXISTS death_binder_entries_category_idx  ON death_binder_entries (family_id, category);

-- Update trigger
CREATE OR REPLACE FUNCTION update_death_binder_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS death_binder_entries_updated_at ON death_binder_entries;
CREATE TRIGGER death_binder_entries_updated_at
    BEFORE UPDATE ON death_binder_entries
    FOR EACH ROW EXECUTE FUNCTION update_death_binder_updated_at();

-- RLS: service role full access
ALTER TABLE death_binder_entries ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename = 'death_binder_entries' AND policyname = 'service_role_death_binder'
  ) THEN
    EXECUTE 'CREATE POLICY "service_role_death_binder" ON death_binder_entries FOR ALL TO service_role USING (true) WITH CHECK (true)';
  END IF;
END $$;

-- ============================================================
-- 3. recurring_bills — structured bills/debts/subscriptions
--    (mirrors migration 008 but with family_id and broader categories)
-- ============================================================

CREATE TABLE IF NOT EXISTS recurring_bills (
    id              UUID PRIMARY KEY DEFAULT extensions.uuid_generate_v4(),
    family_id       TEXT NOT NULL DEFAULT 'family-dan',
    name            TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'other'
                    CHECK (category IN (
                        'mortgage','rent','council_tax','energy','water','broadband',
                        'mobile','insurance','subscription','membership','loan',
                        'pension','investment','other'
                    )),
    amount_gbp      NUMERIC(10,2),
    frequency       TEXT NOT NULL DEFAULT 'monthly'
                    CHECK (frequency IN ('weekly','monthly','quarterly','annually','other')),
    due_day         INTEGER CHECK (due_day BETWEEN 1 AND 31),
    provider        TEXT NOT NULL DEFAULT '',
    account_ref     TEXT NOT NULL DEFAULT '',
    payment_method  TEXT NOT NULL DEFAULT '',
    auto_pay        BOOLEAN NOT NULL DEFAULT false,
    renewal_date    DATE,
    notes           TEXT NOT NULL DEFAULT '',
    active          BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS recurring_bills_family_id_idx ON recurring_bills (family_id);

CREATE OR REPLACE FUNCTION update_recurring_bills_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END;
$$;

DROP TRIGGER IF EXISTS recurring_bills_updated_at ON recurring_bills;
CREATE TRIGGER recurring_bills_updated_at
    BEFORE UPDATE ON recurring_bills
    FOR EACH ROW EXECUTE FUNCTION update_recurring_bills_updated_at();

ALTER TABLE recurring_bills ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies WHERE tablename = 'recurring_bills' AND policyname = 'service_role_recurring_bills'
  ) THEN
    EXECUTE 'CREATE POLICY "service_role_recurring_bills" ON recurring_bills FOR ALL TO service_role USING (true) WITH CHECK (true)';
  END IF;
END $$;

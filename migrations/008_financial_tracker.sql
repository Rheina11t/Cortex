-- ==========================================================================
-- Migration 008: Financial Tracker
-- ==========================================================================
-- Tables for tracking recurring bills, subscriptions, and one-off expenses.
--
-- Run in Supabase SQL Editor → New Query → Paste → RUN
-- ==========================================================================

-- ── recurring_bills ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS recurring_bills (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT NOT NULL,                       -- e.g. "Council Tax", "Netflix"
    category        TEXT NOT NULL DEFAULT 'other'
                    CHECK (category IN ('mortgage','rent','council_tax','energy','water','broadband',
                                        'mobile','insurance','subscription','membership','loan','other')),
    amount_gbp      DECIMAL(10,2) NOT NULL,
    frequency       TEXT NOT NULL DEFAULT 'monthly'
                    CHECK (frequency IN ('weekly','fortnightly','monthly','quarterly','annually','other')),
    due_day         INTEGER,                             -- day of month (1-31)
    provider        TEXT DEFAULT '',
    account_ref     TEXT DEFAULT '',                     -- account/reference number
    payment_method  TEXT DEFAULT '',                     -- "direct debit", "credit card", etc.
    auto_pay        BOOLEAN DEFAULT TRUE,
    notes           TEXT DEFAULT '',
    is_active       BOOLEAN DEFAULT TRUE,
    start_date      DATE,
    end_date        DATE,                                -- NULL = ongoing
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_recurring_bills_category ON recurring_bills (category);
CREATE INDEX IF NOT EXISTS idx_recurring_bills_active   ON recurring_bills (is_active);

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_recurring_bills_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_recurring_bills_updated_at ON recurring_bills;
CREATE TRIGGER trg_recurring_bills_updated_at
    BEFORE UPDATE ON recurring_bills
    FOR EACH ROW
    EXECUTE FUNCTION update_recurring_bills_updated_at();

ALTER TABLE recurring_bills ENABLE ROW LEVEL SECURITY;


-- ── expenses ──────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS expenses (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_member   TEXT NOT NULL DEFAULT 'family',
    description     TEXT NOT NULL,
    amount_gbp      DECIMAL(10,2) NOT NULL,
    category        TEXT NOT NULL DEFAULT 'other'
                    CHECK (category IN ('groceries','dining','transport','fuel','entertainment',
                                        'clothing','health','education','home','gifts','travel',
                                        'pets','children','other')),
    payment_method  TEXT DEFAULT '',
    vendor          TEXT DEFAULT '',
    expense_date    DATE NOT NULL DEFAULT CURRENT_DATE,
    receipt_ref     TEXT DEFAULT '',                     -- link to stored receipt
    notes           TEXT DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_expenses_member   ON expenses (family_member);
CREATE INDEX IF NOT EXISTS idx_expenses_category ON expenses (category);
CREATE INDEX IF NOT EXISTS idx_expenses_date     ON expenses (expense_date);

ALTER TABLE expenses ENABLE ROW LEVEL SECURITY;


-- ── Helper view: monthly spending summary ─────────────────────────────────
CREATE OR REPLACE VIEW monthly_spending_summary AS
SELECT
    date_trunc('month', expense_date)::DATE AS month,
    category,
    family_member,
    COUNT(*)                                AS transaction_count,
    SUM(amount_gbp)                         AS total_gbp,
    ROUND(AVG(amount_gbp), 2)              AS avg_gbp
FROM expenses
GROUP BY date_trunc('month', expense_date), category, family_member
ORDER BY month DESC, total_gbp DESC;

-- ==========================================================================
-- Done.  Verify with:
--   SELECT table_name FROM information_schema.tables
--   WHERE table_schema = 'public'
--     AND table_name IN ('recurring_bills', 'expenses');
-- ==========================================================================

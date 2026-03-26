-- Migration 023: Extend referrals table and families table for full referral tracking
--
-- Adds:
--   referrals.user_phone       — WhatsApp phone of the referring user (E.164, no whatsapp: prefix)
--   referrals.uses_count       — number of referred users who completed their first payment
--   referrals.credit_issued    — flag: free-month credit has been noted for this referral
--   families.referred_by       — ref_code used when this family signed up
--
-- The referrals table uses a two-row-per-referral pattern:
--   • Canonical row  (referred_family_id IS NULL)  — the owner's personal code; uses_count is incremented here
--   • Conversion row (referred_family_id IS NOT NULL) — one row per successful referral, records converted_at

ALTER TABLE referrals ADD COLUMN IF NOT EXISTS user_phone TEXT;
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS uses_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE referrals ADD COLUMN IF NOT EXISTS credit_issued BOOLEAN NOT NULL DEFAULT FALSE;

-- Index for phone-based lookups (e.g. "what is my referral code?")
CREATE INDEX IF NOT EXISTS idx_referrals_user_phone ON referrals(user_phone);

-- Track which ref code was used when a family signed up
ALTER TABLE families ADD COLUMN IF NOT EXISTS referred_by TEXT;
CREATE INDEX IF NOT EXISTS idx_families_referred_by ON families(referred_by);

COMMENT ON COLUMN referrals.user_phone IS 'WhatsApp phone number of the referring user (E.164, no whatsapp: prefix)';
COMMENT ON COLUMN referrals.uses_count IS 'Number of referred users who have completed their first payment';
COMMENT ON COLUMN referrals.credit_issued IS 'Whether a free-month credit has been flagged for this referral row';
COMMENT ON COLUMN families.referred_by IS 'ref_code used when this family signed up (from ?ref= query param)';

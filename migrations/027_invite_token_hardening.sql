-- ============================================================================
-- Migration 027: Invite Token Hardening
-- ============================================================================
-- Phase 2 security hardening:
--   1. Adds expires_at column to family_invites for 7-day token expiry.
--   2. Backfills existing tokens with created_at + 7 days.
--   3. Sets a default so future inserts auto-populate expires_at.
--
-- The invite_token column already supports TEXT so no width change is needed
-- for the upgrade from 8-char to 32-char tokens (secrets.token_urlsafe).
-- ============================================================================

-- 1. Add expires_at column (nullable initially for safe backfill)
ALTER TABLE family_invites
    ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

-- 2. Backfill: set expires_at = created_at + 7 days for existing rows
UPDATE family_invites
   SET expires_at = created_at + INTERVAL '7 days'
 WHERE expires_at IS NULL;

-- 3. Set default for future inserts
ALTER TABLE family_invites
    ALTER COLUMN expires_at SET DEFAULT NOW() + INTERVAL '7 days';

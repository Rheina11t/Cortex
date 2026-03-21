-- Migration 011: Google Calendar OAuth web flow
-- Adds:
--   1. gcal_connect_tokens table (one-time tokens for the OAuth link flow)
--   2. google_refresh_token column on families table
--   3. google_refresh_token column on whatsapp_members table (optional per-member override)

-- -----------------------------------------------------------------------
-- 1. gcal_connect_tokens — stores short-lived one-time OAuth link tokens
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS gcal_connect_tokens (
  token       TEXT PRIMARY KEY,
  phone       TEXT NOT NULL,
  family_id   TEXT NOT NULL,
  created_at  TIMESTAMPTZ DEFAULT now(),
  expires_at  TIMESTAMPTZ NOT NULL
);

-- Index for fast lookup by phone (used when checking existing pending tokens)
CREATE INDEX IF NOT EXISTS idx_gcal_connect_tokens_phone
  ON gcal_connect_tokens (phone);

-- Index for cleanup of expired tokens
CREATE INDEX IF NOT EXISTS idx_gcal_connect_tokens_expires_at
  ON gcal_connect_tokens (expires_at);

-- RLS: service role only (same pattern as other tables in this project)
ALTER TABLE gcal_connect_tokens ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'gcal_connect_tokens'
      AND policyname = 'service_role_all'
  ) THEN
    CREATE POLICY service_role_all ON gcal_connect_tokens
      FOR ALL
      TO service_role
      USING (true)
      WITH CHECK (true);
  END IF;
END$$;

-- -----------------------------------------------------------------------
-- 2. Add google_refresh_token to families (one token per family)
-- -----------------------------------------------------------------------
ALTER TABLE families
  ADD COLUMN IF NOT EXISTS google_refresh_token TEXT;

-- -----------------------------------------------------------------------
-- 3. Add google_refresh_token to whatsapp_members (optional per-member)
-- -----------------------------------------------------------------------
ALTER TABLE whatsapp_members
  ADD COLUMN IF NOT EXISTS google_refresh_token TEXT;

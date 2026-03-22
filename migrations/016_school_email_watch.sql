-- ==========================================================================
-- Migration 016: Gmail School Email Watcher
-- ==========================================================================
-- Adds:
--   1. school_email_watch flag on families table (opt-in per family)
--   2. school_emails_processed table (tracks processed Gmail message IDs
--      and extracted structured data to prevent duplicate processing)
-- ==========================================================================

-- -----------------------------------------------------------------------
-- 1. Add school_email_watch opt-in flag to families
-- -----------------------------------------------------------------------
ALTER TABLE families
  ADD COLUMN IF NOT EXISTS school_email_watch BOOLEAN DEFAULT FALSE;

-- -----------------------------------------------------------------------
-- 2. school_emails_processed — deduplication + audit log for Gmail watcher
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS school_emails_processed (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id          TEXT NOT NULL,
  gmail_message_id   TEXT NOT NULL,
  processed_at       TIMESTAMPTZ DEFAULT now(),
  extracted_events   JSONB,
  extracted_info     JSONB,
  UNIQUE (family_id, gmail_message_id)
);

-- Index for fast deduplication lookups by family + message ID
CREATE INDEX IF NOT EXISTS idx_school_emails_family_message
  ON school_emails_processed (family_id, gmail_message_id);

-- Index for time-based queries / cleanup
CREATE INDEX IF NOT EXISTS idx_school_emails_processed_at
  ON school_emails_processed (processed_at);

-- RLS: service role only (consistent with other tables in this project)
ALTER TABLE school_emails_processed ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'school_emails_processed'
      AND policyname = 'service_role_all'
  ) THEN
    CREATE POLICY service_role_all ON school_emails_processed
      FOR ALL
      TO service_role
      USING (true)
      WITH CHECK (true);
  END IF;
END$$;

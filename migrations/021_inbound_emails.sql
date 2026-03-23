-- ==========================================================================
-- Migration 021: Mailgun Inbound Email Processing
-- ==========================================================================
-- Adds:
--   inbound_emails_processed table — tracks Mailgun inbound emails that have
--   been processed by the /webhook/email-inbound endpoint. Used for
--   deduplication (Mailgun may deliver the same message more than once) and
--   as an audit log for inbound email activity per family.
--
-- Each family receives a unique inbound address: {family_id}@familybrain.co
-- e.g. family-dan@familybrain.co
--
-- Related: migrations/016_school_email_watch.sql (school_emails_processed)
-- ==========================================================================

-- -----------------------------------------------------------------------
-- inbound_emails_processed — deduplication + audit log for Mailgun webhook
-- -----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS inbound_emails_processed (
  id               UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id        TEXT        NOT NULL,
  message_id       TEXT        NOT NULL,   -- Mailgun Message-Id header (deduplication key)
  sender           TEXT,                   -- From address of the inbound email
  subject          TEXT,                   -- Subject line
  processed_at     TIMESTAMPTZ DEFAULT now(),
  attachment_count INTEGER     DEFAULT 0,  -- Number of attachments that were processed
  UNIQUE (family_id, message_id)
);

-- Index for fast deduplication lookups by family + Mailgun message ID
CREATE INDEX IF NOT EXISTS idx_inbound_emails_family_message
  ON inbound_emails_processed (family_id, message_id);

-- Index for time-based queries and data retention cleanup
CREATE INDEX IF NOT EXISTS idx_inbound_emails_processed_at
  ON inbound_emails_processed (processed_at);

-- Index for per-family audit queries
CREATE INDEX IF NOT EXISTS idx_inbound_emails_family_id
  ON inbound_emails_processed (family_id);

-- RLS: service role only (consistent with all other tables in this project)
ALTER TABLE inbound_emails_processed ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'inbound_emails_processed'
      AND policyname = 'service_role_all'
  ) THEN
    CREATE POLICY service_role_all ON inbound_emails_processed
      FOR ALL
      TO service_role
      USING (true)
      WITH CHECK (true);
  END IF;
END$$;

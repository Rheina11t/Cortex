-- Migration 013: cortex_briefings
-- Logs every proactive WhatsApp message sent by the bot so we can
-- deduplicate identical messages within a rolling time window.

CREATE TABLE IF NOT EXISTS cortex_briefings (
  id            UUID        DEFAULT gen_random_uuid() PRIMARY KEY,
  family_id     TEXT        NOT NULL,
  briefing_type TEXT        NOT NULL,
  content_hash  TEXT        NOT NULL,
  delivered_at  TIMESTAMPTZ DEFAULT now(),
  created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cortex_briefings_family_type
  ON cortex_briefings(family_id, briefing_type, delivered_at DESC);

-- Enable Row Level Security (consistent with other tables in this project)
ALTER TABLE cortex_briefings ENABLE ROW LEVEL SECURITY;

-- Allow the service role unrestricted access (used by the bot backend)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE tablename = 'cortex_briefings'
      AND policyname = 'service_role_all'
  ) THEN
    CREATE POLICY service_role_all ON cortex_briefings
      FOR ALL
      TO service_role
      USING (true)
      WITH CHECK (true);
  END IF;
END $$;

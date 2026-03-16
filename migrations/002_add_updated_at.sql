-- ============================================================================
-- Migration 002: Add updated_at column and auto-update trigger
-- Run this in the Supabase SQL Editor:
--   https://supabase.com/dashboard/project/lmwnozlqjaggdpaoossy/sql/new
-- ============================================================================

-- 1. Add the updated_at column (idempotent: safe to run multiple times)
ALTER TABLE memories
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();

-- 2. Create the trigger function that sets updated_at on every UPDATE
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 3. Attach the trigger to the memories table
DROP TRIGGER IF EXISTS update_memories_updated_at ON memories;
CREATE TRIGGER update_memories_updated_at
  BEFORE UPDATE ON memories
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at_column();

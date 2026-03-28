-- Phase 3: GDPR Compliance Schema Changes

-- 1. Add consent tracking to families table
ALTER TABLE families ADD COLUMN IF NOT EXISTS consent_given_at TIMESTAMPTZ;

-- 2. Add data export tracking (optional but good for audit)
CREATE TABLE IF NOT EXISTS data_exports (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    family_id TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    requested_at TIMESTAMPTZ DEFAULT NOW(),
    status TEXT DEFAULT 'completed'
);

-- 3. Add index for faster deletion lookups
CREATE INDEX IF NOT EXISTS idx_memories_family_id ON memories USING gin (metadata);
CREATE INDEX IF NOT EXISTS idx_family_events_family_id ON family_events (family_id);
CREATE INDEX IF NOT EXISTS idx_cortex_briefings_family_id ON cortex_briefings (family_id);
CREATE INDEX IF NOT EXISTS idx_cortex_actions_family_id ON cortex_actions (family_id);

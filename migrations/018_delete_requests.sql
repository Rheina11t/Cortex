-- ---------------------------------------------------------------------------
-- Migration 018: Create delete_requests table for Tier 2 family wipe consensus
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS delete_requests (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    family_id TEXT NOT NULL,
    requested_by TEXT NOT NULL, -- phone number of the requester
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmations TEXT[] DEFAULT '{}', -- array of phone numbers that have confirmed
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'expired'))
);

CREATE INDEX IF NOT EXISTS idx_delete_requests_family_id ON delete_requests(family_id);
CREATE INDEX IF NOT EXISTS idx_delete_requests_status ON delete_requests(status);

ALTER TABLE delete_requests ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role has full access to delete_requests"
    ON delete_requests FOR ALL USING (true) WITH CHECK (true);

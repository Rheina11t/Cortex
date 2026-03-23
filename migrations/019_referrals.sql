-- Create referrals table
CREATE TABLE IF NOT EXISTS referrals (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    family_id TEXT NOT NULL,
    ref_code TEXT UNIQUE NOT NULL,
    referred_family_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    converted_at TIMESTAMPTZ
);

-- Indexes for fast lookup
CREATE INDEX IF NOT EXISTS idx_referrals_family_id ON referrals(family_id);
CREATE INDEX IF NOT EXISTS idx_referrals_ref_code ON referrals(ref_code);
CREATE INDEX IF NOT EXISTS idx_referrals_referred_family_id ON referrals(referred_family_id);

-- Enable RLS
ALTER TABLE referrals ENABLE ROW LEVEL SECURITY;

-- Service role has full access
CREATE POLICY "Service role has full access to referrals"
    ON referrals
    FOR ALL
    TO service_role
    USING (true)
    WITH CHECK (true);

-- ==========================================================================
-- Migration 023: Family Invites
-- ==========================================================================
-- Adds the family_invites table to support the "add family member without
-- phone number" feature.  A family member can generate a shareable invite
-- link (familybrain.co.uk/join/<token>) that the invitee taps to open
-- WhatsApp with a pre-filled "join <token>" message.
-- ==========================================================================

CREATE TABLE IF NOT EXISTS family_invites (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invite_token     TEXT NOT NULL UNIQUE,          -- short URL-safe token, e.g. "abc12345"
    family_id        TEXT NOT NULL,                 -- references families.family_id
    invited_name     TEXT NOT NULL,                 -- name the inviter gave, e.g. "Sarah"
    invited_by_phone TEXT NOT NULL,                 -- E.164 phone of the inviting member
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    used_at          TIMESTAMPTZ,                   -- NULL until the invite is accepted
    used_by_phone    TEXT                           -- E.164 phone of the new member
);

-- Fast token lookup (hot path on every /join/<token> request)
CREATE INDEX IF NOT EXISTS idx_family_invites_token
    ON family_invites (invite_token);

-- Lookup by family for admin / listing purposes
CREATE INDEX IF NOT EXISTS idx_family_invites_family_id
    ON family_invites (family_id);

-- RLS: service role has full access (consistent with other tables)
ALTER TABLE family_invites ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role has full access to family_invites"
    ON family_invites FOR ALL USING (true) WITH CHECK (true);

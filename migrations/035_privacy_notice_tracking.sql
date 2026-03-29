-- ==========================================================================
-- Migration 035: Privacy Notice Tracking
-- ==========================================================================
-- Adds a privacy_notice_sent_at timestamp column to whatsapp_members so that
-- the first-message GDPR privacy notice is sent exactly once per phone number.
--
-- UK GDPR Article 13 requires that data subjects are informed of processing
-- at the point of first data collection. This column tracks compliance.
-- ==========================================================================

ALTER TABLE whatsapp_members
    ADD COLUMN IF NOT EXISTS privacy_notice_sent_at TIMESTAMPTZ;

-- Index for fast lookup: "has this number received the notice yet?"
-- Used on every incoming message from an existing member.
CREATE INDEX IF NOT EXISTS idx_whatsapp_members_privacy_notice
    ON whatsapp_members (phone)
    WHERE privacy_notice_sent_at IS NULL;

COMMENT ON COLUMN whatsapp_members.privacy_notice_sent_at IS
    'Timestamp when the UK GDPR Article 13 first-message privacy notice was sent '
    'to this phone number. NULL means the notice has not yet been sent. '
    'Once set, the notice is never re-sent to the same number.';

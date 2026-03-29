-- ==========================================================================
-- Migration 036: Data Retention Policy Schema
-- ==========================================================================
-- Implements the FamilyBrain data retention policy:
--
--   • Data is retained indefinitely while the account is active.
--     This is proportionate — FamilyBrain IS the emergency vault.
--   • Data is deleted when:
--       (a) the user explicitly requests it via /delete, OR
--       (b) the subscription is cancelled and not renewed within 90 days.
--   • The 90-day post-cancellation grace period gives users time to export
--     their data before permanent deletion.
--   • NO inactivity-based deletion. Families store wills, insurance policies,
--     and critical documents that must survive long periods of non-use.
--
-- UK GDPR Article 5(1)(e) — storage limitation: data kept "no longer than
-- necessary for the purposes for which the personal data are processed."
-- For an emergency vault, necessity is coextensive with account existence.
-- ==========================================================================

-- 1. Add subscription_cancelled_at to families table
--    Populated by the Stripe webhook handler when a subscription is deleted.
ALTER TABLE families
    ADD COLUMN IF NOT EXISTS subscription_cancelled_at TIMESTAMPTZ;

COMMENT ON COLUMN families.subscription_cancelled_at IS
    'Timestamp when the Stripe subscription was cancelled. NULL means the '
    'subscription is active or has never been cancelled. The data retention '
    'job uses this to enforce the 90-day post-cancellation deletion policy.';

-- 2. Add deletion_scheduled_at to families table
--    Set by the retention job when a family enters the 90-day countdown.
--    Cleared if the family reactivates their subscription.
ALTER TABLE families
    ADD COLUMN IF NOT EXISTS deletion_scheduled_at TIMESTAMPTZ;

COMMENT ON COLUMN families.deletion_scheduled_at IS
    'Timestamp when permanent data deletion is scheduled (90 days after '
    'subscription_cancelled_at). Set by the monthly retention job. '
    'Cleared to NULL if the family reactivates before deletion occurs.';

-- 3. Add retention_warning_sent_at to families table
--    Tracks when the 30-day warning WhatsApp message was sent.
ALTER TABLE families
    ADD COLUMN IF NOT EXISTS retention_warning_sent_at TIMESTAMPTZ;

COMMENT ON COLUMN families.retention_warning_sent_at IS
    'Timestamp when the 30-day pre-deletion warning WhatsApp message was sent. '
    'Prevents duplicate warnings from being sent on subsequent job runs.';

-- 4. Index for efficient retention job queries
--    The job queries: WHERE subscription_cancelled_at IS NOT NULL AND status = ''cancelled''
CREATE INDEX IF NOT EXISTS idx_families_subscription_cancelled
    ON families (subscription_cancelled_at)
    WHERE subscription_cancelled_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_families_deletion_scheduled
    ON families (deletion_scheduled_at)
    WHERE deletion_scheduled_at IS NOT NULL;

-- 5. Update the Stripe webhook handler's cancellation path
--    (handled in code — this migration just ensures the column exists)

-- 6. Stripe events table — add processed_at for purge tracking
--    The retention job purges processed Stripe events older than 90 days.
ALTER TABLE stripe_events
    ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ DEFAULT NOW();

COMMENT ON COLUMN stripe_events.processed_at IS
    'Timestamp when this Stripe event was processed. Used by the monthly '
    'retention job to purge events older than 90 days.';

-- 7. Index for Stripe events purge query
CREATE INDEX IF NOT EXISTS idx_stripe_events_processed_at
    ON stripe_events (processed_at)
    WHERE processed_at IS NOT NULL;

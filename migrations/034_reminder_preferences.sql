-- ==========================================================================
-- Migration 034: Reminder Preferences
-- ==========================================================================
-- Adds per-family reminder preferences to the families table:
--   reminders_enabled  — whether proactive WhatsApp reminders are on (default: true)
--   reminder_time      — preferred morning reminder time in HH:MM format (default: '08:00')
--
-- Commands: /reminders on | off | time HH:MM
-- ==========================================================================

ALTER TABLE families
    ADD COLUMN IF NOT EXISTS reminders_enabled BOOLEAN NOT NULL DEFAULT TRUE;

ALTER TABLE families
    ADD COLUMN IF NOT EXISTS reminder_time VARCHAR(5) NOT NULL DEFAULT '08:00';

COMMENT ON COLUMN families.reminders_enabled IS
    'Whether proactive WhatsApp reminders are enabled for this family. Controlled via /reminders on|off.';

COMMENT ON COLUMN families.reminder_time IS
    'Preferred morning reminder time in HH:MM format (Europe/London). Controlled via /reminders time HH:MM.';

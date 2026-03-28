-- Add sos_pin column to families table for PIN protection of sensitive commands (Phase 4)
ALTER TABLE families ADD COLUMN IF NOT EXISTS sos_pin TEXT;

-- Add comment for clarity
COMMENT ON COLUMN families.sos_pin IS 'Bcrypt hashed PIN (4-6 digits) for protecting /sos, /delete, and /mydata commands.';

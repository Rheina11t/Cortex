-- Migration 014: Emergency PDF feature
-- Creates the emergency-pdfs storage bucket and associated RLS policy.
-- The emergency_category field is stored in metadata JSONB on the memories table
-- (no schema change needed for memories).

-- Create the emergency-pdfs storage bucket (private, max 50 MB, PDF only)
INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
VALUES ('emergency-pdfs', 'emergency-pdfs', false, 52428800, '{"application/pdf"}')
ON CONFLICT (id) DO NOTHING;

-- RLS policy: service role has full access to emergency-pdfs objects
-- (already applied via apply_migration in deployment, idempotent here)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'storage'
      AND tablename = 'objects'
      AND policyname = 'service_role_emergency_pdfs'
  ) THEN
    EXECUTE 'CREATE POLICY "service_role_emergency_pdfs" ON storage.objects
             FOR ALL TO service_role
             USING (bucket_id = ''emergency-pdfs'')
             WITH CHECK (bucket_id = ''emergency-pdfs'')';
  END IF;
END $$;

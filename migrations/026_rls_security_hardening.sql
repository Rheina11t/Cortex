-- ============================================================================
-- Migration 026: RLS Security Hardening
-- ============================================================================
-- Fixes identified in the FamilyBrain security audit (27 March 2026):
--
--   1. Seven tables had RLS policies granting full access to the {public} role
--      (i.e. the Supabase anon key). These are replaced with service_role-only
--      policies.
--
--   2. Sixteen tables had RLS enabled but zero policies defined. Service-role-
--      only policies are added so the backend (which uses the service_role key)
--      can still operate, while the anon key is completely locked out.
--
--   3. All privileges are explicitly revoked from the anon role on every table
--      in the public schema as a defence-in-depth measure.
--
-- The backend uses the Supabase service_role key for all database operations,
-- which bypasses RLS. These policies therefore serve as a safety net against
-- direct PostgREST access via the anon key.
-- ============================================================================

BEGIN;

-- ────────────────────────────────────────────────────────────────────────────
-- PART 1: Fix the 7 tables with permissive {public} role policies
-- ────────────────────────────────────────────────────────────────────────────

-- 1. families
DROP POLICY IF EXISTS "Service role has full access to families" ON families;
CREATE POLICY "service_role_only_families"
  ON families FOR ALL TO service_role
  USING (true) WITH CHECK (true);

-- 2. whatsapp_members
DROP POLICY IF EXISTS "Service role has full access to whatsapp_members" ON whatsapp_members;
CREATE POLICY "service_role_only_whatsapp_members"
  ON whatsapp_members FOR ALL TO service_role
  USING (true) WITH CHECK (true);

-- 3. family_entities
DROP POLICY IF EXISTS "Service role has full access" ON family_entities;
CREATE POLICY "service_role_only_family_entities"
  ON family_entities FOR ALL TO service_role
  USING (true) WITH CHECK (true);

-- 4. family_entity_relations
DROP POLICY IF EXISTS "Service role has full access" ON family_entity_relations;
CREATE POLICY "service_role_only_family_entity_relations"
  ON family_entity_relations FOR ALL TO service_role
  USING (true) WITH CHECK (true);

-- 5. family_invites
DROP POLICY IF EXISTS "Service role has full access to family_invites" ON family_invites;
CREATE POLICY "service_role_only_family_invites"
  ON family_invites FOR ALL TO service_role
  USING (true) WITH CHECK (true);

-- 6. memory_entity_links
DROP POLICY IF EXISTS "Service role has full access" ON memory_entity_links;
CREATE POLICY "service_role_only_memory_entity_links"
  ON memory_entity_links FOR ALL TO service_role
  USING (true) WITH CHECK (true);

-- 7. delete_requests
DROP POLICY IF EXISTS "Service role has full access to delete_requests" ON delete_requests;
CREATE POLICY "service_role_only_delete_requests"
  ON delete_requests FOR ALL TO service_role
  USING (true) WITH CHECK (true);


-- ────────────────────────────────────────────────────────────────────────────
-- PART 2: Add service_role-only policies to the 16 tables that have RLS
--         enabled but zero policies (currently fail-closed, which is safe,
--         but adding explicit policies makes the intent clear and prevents
--         accidental breakage if someone adds a permissive policy later).
-- ────────────────────────────────────────────────────────────────────────────

-- Tables WITH family_id column:
CREATE POLICY IF NOT EXISTS "service_role_only_bills"
  ON bills FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_family_events"
  ON family_events FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_financial_accounts"
  ON financial_accounts FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_health_records"
  ON health_records FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_medical_appointments"
  ON medical_appointments FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_medications"
  ON medications FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_professional_contacts"
  ON professional_contacts FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_vehicles"
  ON vehicles FOR ALL TO service_role
  USING (true) WITH CHECK (true);

-- Tables WITHOUT family_id column:
CREATE POLICY IF NOT EXISTS "service_role_only_contact_interactions"
  ON contact_interactions FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_family_members"
  ON family_members FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_financial_transactions"
  ON financial_transactions FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_household_vendors"
  ON household_vendors FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_maintenance_logs"
  ON maintenance_logs FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_maintenance_tasks"
  ON maintenance_tasks FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_opportunities"
  ON opportunities FOR ALL TO service_role
  USING (true) WITH CHECK (true);

CREATE POLICY IF NOT EXISTS "service_role_only_vehicle_service_logs"
  ON vehicle_service_logs FOR ALL TO service_role
  USING (true) WITH CHECK (true);


-- ────────────────────────────────────────────────────────────────────────────
-- PART 3: Revoke all privileges from the anon role on every table.
--         This is a defence-in-depth measure. Even if someone accidentally
--         creates a permissive RLS policy, the anon role will have no
--         underlying table privileges to exploit.
-- ────────────────────────────────────────────────────────────────────────────

REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM anon;

-- Also revoke from the authenticated role (not currently used, but prevents
-- future misconfiguration if Supabase Auth is enabled later).
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM authenticated;


-- ────────────────────────────────────────────────────────────────────────────
-- VERIFICATION: This query should return zero rows with roles containing
-- {public} or {anon}. Run it after the migration to confirm.
-- ────────────────────────────────────────────────────────────────────────────
-- SELECT tablename, policyname, roles
-- FROM pg_policies
-- WHERE schemaname = 'public'
--   AND (roles::text LIKE '%public%' OR roles::text LIKE '%anon%');

COMMIT;

-- Migration 037: Memory Profiles (Entity Consolidation)
--
-- Stores LLM-generated consolidated profiles for entities that have 3+
-- linked memories.  Used by the memory_consolidation background job to
-- reduce context-window bloat when answering queries — instead of
-- injecting 15 raw memories about "Izzy", we inject a single concise
-- profile paragraph.
--
-- Depends on: 020_entity_graph.sql (family_entities table)

-- 1. Create the table --------------------------------------------------------

CREATE TABLE IF NOT EXISTS memory_profiles (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id       TEXT        NOT NULL,
    entity_id       UUID        NOT NULL REFERENCES family_entities(id) ON DELETE CASCADE,
    profile_text    TEXT        NOT NULL,
    memory_count    INT         NOT NULL DEFAULT 0,
    source_memory_ids UUID[]    DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_memory_profiles_family_entity UNIQUE (family_id, entity_id)
);

-- 2. Indexes -----------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_memory_profiles_family
    ON memory_profiles (family_id);

CREATE INDEX IF NOT EXISTS idx_memory_profiles_entity
    ON memory_profiles (entity_id);

-- 3. Auto-update trigger (reuse existing function) ---------------------------

CREATE TRIGGER set_memory_profiles_updated_at
    BEFORE UPDATE ON memory_profiles
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- 4. Row-Level Security ------------------------------------------------------

ALTER TABLE memory_profiles ENABLE ROW LEVEL SECURITY;

-- Service-role bypass (matches pattern from 020_entity_graph.sql)
CREATE POLICY memory_profiles_service_all
    ON memory_profiles
    FOR ALL
    USING (true)
    WITH CHECK (true);

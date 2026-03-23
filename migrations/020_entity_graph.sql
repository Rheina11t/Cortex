-- =============================================================================
-- FamilyBrain: Database Migration 020
-- Entity Relationship Graph Layer (GraphRAG pattern)
--
-- Adds a lightweight graph layer on top of the existing pgvector memories
-- table, enabling relational queries like "what's everything connected to
-- Izzy's school this term?" without requiring an external graph database.
--
-- Tables created:
--   family_entities          — nodes (people, places, events, organisations…)
--   family_entity_relations  — edges (attends, parent_of, part_of…)
--   memory_entity_links      — joins memories ↔ entities
-- =============================================================================
-- Target: Supabase (PostgreSQL 15+ with existing uuid-ossp extension)
-- =============================================================================


-- ---------------------------------------------------------------------------
-- 1. family_entities — graph nodes
-- ---------------------------------------------------------------------------
-- Each row represents a named entity belonging to a family: a person, place,
-- event, organisation, document, or date range.  The aliases array allows
-- fuzzy matching on alternative names (e.g. "Izzy" / "Isabella").
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS family_entities (
  id           UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id    TEXT            NOT NULL,
  entity_type  TEXT            NOT NULL,          -- person, place, event, document, organisation, date_range
  name         TEXT            NOT NULL,
  aliases      TEXT[]          DEFAULT '{}',       -- alternative names / spellings
  metadata     JSONB           NOT NULL DEFAULT '{}'::jsonb,
  created_at   TIMESTAMPTZ     NOT NULL DEFAULT now(),
  updated_at   TIMESTAMPTZ     NOT NULL DEFAULT now()
);

COMMENT ON TABLE family_entities IS
  'Entity graph nodes — named entities (people, places, events, etc.) belonging to a family.';

-- Auto-update trigger for updated_at (reuses existing function from migration 001)
DROP TRIGGER IF EXISTS update_family_entities_updated_at ON family_entities;
CREATE TRIGGER update_family_entities_updated_at
  BEFORE UPDATE ON family_entities
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at_column();


-- ---------------------------------------------------------------------------
-- 2. family_entity_relations — graph edges
-- ---------------------------------------------------------------------------
-- Directed edges between two entities.  relation_type describes the
-- relationship (e.g. 'attends', 'parent_of', 'scheduled_for').
-- confidence < 1.0 indicates an LLM-inferred relationship.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS family_entity_relations (
  id              UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
  family_id       TEXT            NOT NULL,
  from_entity_id  UUID            NOT NULL REFERENCES family_entities(id) ON DELETE CASCADE,
  to_entity_id    UUID            NOT NULL REFERENCES family_entities(id) ON DELETE CASCADE,
  relation_type   TEXT            NOT NULL,        -- attends, owns, relates_to, scheduled_for, parent_of, part_of
  confidence      FLOAT           NOT NULL DEFAULT 1.0,
  source          TEXT            NOT NULL DEFAULT 'user',  -- user, llm_inferred, calendar, email
  created_at      TIMESTAMPTZ     NOT NULL DEFAULT now()
);

COMMENT ON TABLE family_entity_relations IS
  'Entity graph edges — directed relationships between family entities.';


-- ---------------------------------------------------------------------------
-- 3. memory_entity_links — join table: memories ↔ entities
-- ---------------------------------------------------------------------------
-- Links existing memory rows to the entities mentioned within them.
-- This is the bridge between the flat vector store and the graph layer.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memory_entity_links (
  memory_id   UUID   NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
  entity_id   UUID   NOT NULL REFERENCES family_entities(id) ON DELETE CASCADE,
  family_id   TEXT   NOT NULL,
  PRIMARY KEY (memory_id, entity_id)
);

COMMENT ON TABLE memory_entity_links IS
  'Join table linking memories to the entities they mention.';


-- ---------------------------------------------------------------------------
-- 4. Indexes
-- ---------------------------------------------------------------------------

-- family_entities indexes
CREATE INDEX IF NOT EXISTS idx_family_entities_family_id
  ON family_entities (family_id);

CREATE INDEX IF NOT EXISTS idx_family_entities_entity_type
  ON family_entities (family_id, entity_type);

CREATE INDEX IF NOT EXISTS idx_family_entities_name
  ON family_entities (family_id, lower(name));

CREATE INDEX IF NOT EXISTS idx_family_entities_aliases
  ON family_entities USING gin (aliases);

-- family_entity_relations indexes
CREATE INDEX IF NOT EXISTS idx_entity_relations_family_id
  ON family_entity_relations (family_id);

CREATE INDEX IF NOT EXISTS idx_entity_relations_from
  ON family_entity_relations (from_entity_id);

CREATE INDEX IF NOT EXISTS idx_entity_relations_to
  ON family_entity_relations (to_entity_id);

CREATE INDEX IF NOT EXISTS idx_entity_relations_type
  ON family_entity_relations (family_id, relation_type);

-- memory_entity_links indexes
CREATE INDEX IF NOT EXISTS idx_memory_entity_links_memory
  ON memory_entity_links (memory_id);

CREATE INDEX IF NOT EXISTS idx_memory_entity_links_entity
  ON memory_entity_links (entity_id);

CREATE INDEX IF NOT EXISTS idx_memory_entity_links_family
  ON memory_entity_links (family_id);


-- ---------------------------------------------------------------------------
-- 5. Row-Level Security (RLS)
-- ---------------------------------------------------------------------------
-- Enable RLS on all three tables with a permissive service-role policy,
-- matching the pattern used by the rest of the FamilyBrain schema.
-- ---------------------------------------------------------------------------

ALTER TABLE family_entities ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role has full access"
  ON family_entities FOR ALL
  USING (true) WITH CHECK (true);

ALTER TABLE family_entity_relations ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role has full access"
  ON family_entity_relations FOR ALL
  USING (true) WITH CHECK (true);

ALTER TABLE memory_entity_links ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Service role has full access"
  ON memory_entity_links FOR ALL
  USING (true) WITH CHECK (true);


-- ---------------------------------------------------------------------------
-- Done!  Verify with:
--   SELECT * FROM family_entities LIMIT 1;
--   SELECT * FROM family_entity_relations LIMIT 1;
--   SELECT * FROM memory_entity_links LIMIT 1;
-- ---------------------------------------------------------------------------

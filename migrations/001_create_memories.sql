-- =============================================================================
-- Open Brain: Database Migration 001
-- Creates the memories table with pgvector support for semantic search
-- =============================================================================
-- Target: Supabase (PostgreSQL 15+ with pgvector)
-- Run this in the Supabase SQL Editor (Dashboard → SQL Editor → New Query)
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Enable required extensions
-- ---------------------------------------------------------------------------
-- pgvector provides the VECTOR data type and similarity operators.
-- On Supabase, extensions live in the "extensions" schema by default.
-- The uuid-ossp extension provides uuid_generate_v4() for primary keys.
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS vector
  WITH SCHEMA extensions;

CREATE EXTENSION IF NOT EXISTS "uuid-ossp"
  WITH SCHEMA extensions;


-- ---------------------------------------------------------------------------
-- 2. Create the memories table
-- ---------------------------------------------------------------------------
-- Each row represents a single captured thought, note, or piece of knowledge.
--
--   id         – Unique identifier (UUID v4, auto-generated).
--   content    – The raw text of the memory.
--   embedding  – A 1536-dimensional vector produced by OpenAI
--                text-embedding-3-small (or any compatible model).
--   metadata   – Flexible JSONB column for tags, people, source, category,
--                action items, and any future fields.
--   created_at – Timestamp of when the memory was captured.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS memories (
  id         UUID            PRIMARY KEY DEFAULT extensions.uuid_generate_v4(),
  content    TEXT            NOT NULL,
  embedding  vector(1536),
  metadata   JSONB           NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ     NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ     NOT NULL DEFAULT now()
);

-- Add a table comment for documentation
COMMENT ON TABLE memories IS
  'Open Brain knowledge base – stores captured thoughts with vector embeddings for semantic search.';


-- ---------------------------------------------------------------------------
-- 3a. Auto-update trigger for updated_at
-- ---------------------------------------------------------------------------
-- Automatically sets updated_at = now() whenever a row is modified.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS update_memories_updated_at ON memories;
CREATE TRIGGER update_memories_updated_at
  BEFORE UPDATE ON memories
  FOR EACH ROW
  EXECUTE FUNCTION update_updated_at_column();


-- ---------------------------------------------------------------------------
-- 3. Create indexes
-- ---------------------------------------------------------------------------
-- a) IVFFlat index on the embedding column for fast approximate nearest-
--    neighbour search.  lists = 100 is a good starting point for up to
--    ~100 000 rows.  Rebuild with more lists as the table grows.
--
--    IMPORTANT: IVFFlat requires at least some data before the index can
--    be built.  If you are starting from an empty table, you may choose to
--    create this index later (after inserting initial data) or switch to
--    HNSW which does not have this limitation.
--
-- b) GIN index on the metadata JSONB column so that tag / people / category
--    filters remain fast even as the table grows.
--
-- c) B-tree index on created_at for efficient "recent memories" queries.
-- ---------------------------------------------------------------------------

-- Option A (recommended for most users): HNSW index – works on empty tables
CREATE INDEX IF NOT EXISTS idx_memories_embedding_hnsw
  ON memories
  USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);

-- Option B (alternative): IVFFlat index – requires data before creation
-- Uncomment the line below and comment out the HNSW index above if preferred.
-- CREATE INDEX IF NOT EXISTS idx_memories_embedding_ivfflat
--   ON memories
--   USING ivfflat (embedding vector_cosine_ops)
--   WITH (lists = 100);

-- GIN index for fast JSONB containment queries (e.g. metadata @> '{"tags":["project"]}')
CREATE INDEX IF NOT EXISTS idx_memories_metadata
  ON memories
  USING gin (metadata jsonb_path_ops);

-- B-tree index for ordering by recency
CREATE INDEX IF NOT EXISTS idx_memories_created_at
  ON memories (created_at DESC);


-- ---------------------------------------------------------------------------
-- 4. Similarity search function
-- ---------------------------------------------------------------------------
-- This function is called via Supabase's .rpc() method from the application
-- layer.  It accepts a query embedding and returns the closest matches
-- ranked by cosine similarity.
--
-- Parameters:
--   query_embedding  – The 1536-dim vector of the search query.
--   match_threshold  – Minimum cosine similarity (0.0 – 1.0).  Rows below
--                      this threshold are excluded.  Start with 0.5 – 0.7.
--   match_count      – Maximum number of results to return (capped at 200).
--
-- Returns: rows from memories plus a computed "similarity" score.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION match_memories(
  query_embedding vector(1536),
  match_threshold float DEFAULT 0.5,
  match_count     int   DEFAULT 10
)
RETURNS TABLE (
  id         UUID,
  content    TEXT,
  metadata   JSONB,
  similarity float,
  created_at TIMESTAMPTZ
)
LANGUAGE sql STABLE
AS $$
  SELECT
    m.id,
    m.content,
    m.metadata,
    1 - (m.embedding <=> query_embedding) AS similarity,
    m.created_at
  FROM memories m
  WHERE 1 - (m.embedding <=> query_embedding) >= match_threshold
  ORDER BY m.embedding <=> query_embedding ASC
  LIMIT LEAST(match_count, 200);
$$;

COMMENT ON FUNCTION match_memories IS
  'Performs cosine-similarity search against the memories table and returns the top matches above the given threshold.';


-- ---------------------------------------------------------------------------
-- 5. Metadata-filtered similarity search function
-- ---------------------------------------------------------------------------
-- Extends match_memories with an optional JSONB filter so that callers can
-- restrict results to specific tags, people, categories, etc.
--
-- Example filter values:
--   '{"tags": ["project-x"]}'       – memories tagged "project-x"
--   '{"people": ["Alice"]}'         – memories mentioning Alice
--   '{"category": "meeting-notes"}' – memories in the meeting-notes category
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION match_memories_by_metadata(
  query_embedding  vector(1536),
  filter           JSONB       DEFAULT '{}'::jsonb,
  match_threshold  float       DEFAULT 0.5,
  match_count      int         DEFAULT 10
)
RETURNS TABLE (
  id         UUID,
  content    TEXT,
  metadata   JSONB,
  similarity float,
  created_at TIMESTAMPTZ
)
LANGUAGE sql STABLE
AS $$
  SELECT
    m.id,
    m.content,
    m.metadata,
    1 - (m.embedding <=> query_embedding) AS similarity,
    m.created_at
  FROM memories m
  WHERE m.metadata @> filter
    AND 1 - (m.embedding <=> query_embedding) >= match_threshold
  ORDER BY m.embedding <=> query_embedding ASC
  LIMIT LEAST(match_count, 200);
$$;

COMMENT ON FUNCTION match_memories_by_metadata IS
  'Performs cosine-similarity search with an additional JSONB containment filter on the metadata column.';


-- ---------------------------------------------------------------------------
-- 6. Row-Level Security (RLS) – optional but recommended
-- ---------------------------------------------------------------------------
-- Enable RLS so that the table is protected by default.  The policy below
-- allows full access when using the service_role key (which is what the
-- backend application should use).  Adjust as needed for multi-tenant or
-- user-facing scenarios.
-- ---------------------------------------------------------------------------

ALTER TABLE memories ENABLE ROW LEVEL SECURITY;

-- Allow the service role unrestricted access
CREATE POLICY "Service role has full access"
  ON memories
  FOR ALL
  USING (true)
  WITH CHECK (true);


-- ---------------------------------------------------------------------------
-- Done!  You can verify the setup by running:
--   SELECT * FROM memories LIMIT 1;
--   SELECT match_memories(ARRAY_FILL(0, ARRAY[1536])::vector, 0.0, 5);
--
-- For existing deployments, run migrations/002_add_updated_at.sql to add
-- the updated_at column and trigger to an existing memories table.
-- ---------------------------------------------------------------------------

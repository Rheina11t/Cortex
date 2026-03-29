-- Migration 038: 2-Hop Graph Traversal RPC Function
--
-- Creates a PostgreSQL function that performs recursive graph traversal
-- on the family_entity_relations table.  Given a set of seed entity IDs,
-- it walks outgoing AND incoming edges up to N hops (default 2) and
-- returns a flat result set with entity names, types, and hop distances.
--
-- This replaces the Python-side 1-hop traversal in entity_graph.py with
-- a single efficient database round-trip.
--
-- Depends on: 020_entity_graph.sql (family_entities, family_entity_relations)

CREATE OR REPLACE FUNCTION get_entity_subgraph(
    p_family_id  TEXT,
    p_entity_ids UUID[],
    p_max_hops   INT DEFAULT 2
)
RETURNS TABLE (
    from_entity_id UUID,
    from_name      TEXT,
    from_type      TEXT,
    relation_type  TEXT,
    to_entity_id   UUID,
    to_name        TEXT,
    to_type        TEXT,
    hop_distance   INT,
    confidence     FLOAT
)
LANGUAGE sql
STABLE
AS $$
    WITH RECURSIVE traversal AS (
        -- Base case: all relations where a seed entity is the source
        SELECT
            r.from_entity_id,
            r.to_entity_id,
            r.relation_type,
            r.confidence,
            1 AS hop,
            ARRAY[r.from_entity_id, r.to_entity_id] AS visited
        FROM family_entity_relations r
        WHERE r.family_id = p_family_id
          AND r.from_entity_id = ANY(p_entity_ids)

        UNION ALL

        -- Base case: all relations where a seed entity is the target (reverse)
        SELECT
            r.from_entity_id,
            r.to_entity_id,
            r.relation_type,
            r.confidence,
            1 AS hop,
            ARRAY[r.from_entity_id, r.to_entity_id] AS visited
        FROM family_entity_relations r
        WHERE r.family_id = p_family_id
          AND r.to_entity_id = ANY(p_entity_ids)

        UNION ALL

        -- Recursive step: follow outgoing edges from discovered nodes
        SELECT
            r.from_entity_id,
            r.to_entity_id,
            r.relation_type,
            r.confidence,
            t.hop + 1 AS hop,
            t.visited || r.to_entity_id
        FROM family_entity_relations r
        JOIN traversal t
            ON r.from_entity_id = t.to_entity_id
           OR r.from_entity_id = t.from_entity_id
        WHERE r.family_id = p_family_id
          AND t.hop < p_max_hops
          -- Prevent cycles: don't revisit nodes already in the path
          AND NOT (r.to_entity_id = ANY(t.visited))
          AND NOT (r.from_entity_id = ANY(t.visited) AND r.to_entity_id = ANY(t.visited))
    ),
    -- Deduplicate: keep the shortest hop distance for each unique edge
    deduped AS (
        SELECT DISTINCT ON (t.from_entity_id, t.to_entity_id, t.relation_type)
            t.from_entity_id,
            t.to_entity_id,
            t.relation_type,
            t.confidence,
            t.hop
        FROM traversal t
        ORDER BY t.from_entity_id, t.to_entity_id, t.relation_type, t.hop ASC
    )
    SELECT
        d.from_entity_id,
        fe_from.name  AS from_name,
        fe_from.entity_type AS from_type,
        d.relation_type,
        d.to_entity_id,
        fe_to.name    AS to_name,
        fe_to.entity_type AS to_type,
        d.hop         AS hop_distance,
        d.confidence
    FROM deduped d
    JOIN family_entities fe_from ON fe_from.id = d.from_entity_id
    JOIN family_entities fe_to   ON fe_to.id   = d.to_entity_id
    ORDER BY d.hop ASC, d.confidence DESC
    LIMIT 50;
$$;

COMMENT ON FUNCTION get_entity_subgraph IS
    'Traverse the family entity graph up to N hops from seed entities. '
    'Returns edges with entity names, types, hop distance, and confidence. '
    'Used by entity_graph.get_entity_context() for 2-hop context retrieval.';

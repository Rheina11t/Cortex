"""
FamilyBrain – Entity Relationship Graph Layer.

Implements a lightweight GraphRAG pattern on top of the existing Postgres/
Supabase setup.  Entities (people, places, events, organisations, date
ranges) are extracted from text via the LLM and stored as graph nodes in
``family_entities``.  Relationships between entities are stored as directed
edges in ``family_entity_relations``.  Memories are linked to entities via
``memory_entity_links``.

Public API
----------
- extract_and_store_entities(text, family_id, memory_id=None)
- infer_relations(family_id)
- get_entity_context(query, family_id)
- get_entity_graph_summary(family_id)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from . import brain

logger = logging.getLogger("open_brain.entity_graph")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Valid entity types the LLM is allowed to produce
_VALID_ENTITY_TYPES = frozenset(
    {"person", "place", "event", "document", "organisation", "date_range"}
)

# LLM prompt for entity extraction
_ENTITY_EXTRACTION_PROMPT = """\
You are an entity extraction assistant for a family knowledge base called FamilyBrain.

Given a raw message, extract all named entities and return a JSON object with exactly this structure:

{
  "entities": [
    {
      "name": "<canonical name, e.g. 'Izzy', 'St Joseph's School'>",
      "entity_type": "<one of: person, place, event, document, organisation, date_range>",
      "aliases": ["<alternative names or spellings, if any>"],
      "metadata": {}
    }
  ]
}

Rules:
- Return ONLY valid JSON. No markdown fences, no commentary.
- Extract people (first names are fine), places, organisations (schools, companies),
  events (sports day, holidays, appointments), and date ranges (Easter holidays 2026,
  summer term).
- Use lowercase entity_type values from the list above.
- If no entities are found, return {"entities": []}.
- Do NOT extract generic words — only proper nouns or specific named things.
- Keep names concise and capitalised naturally.
- For date ranges, include relevant dates in the metadata if mentioned
  (e.g. {"start": "2026-04-03", "end": "2026-04-17"}).
"""

# LLM prompt for relation inference
_RELATION_INFERENCE_PROMPT = """\
You are a relationship inference assistant for a family knowledge base called FamilyBrain.

Given a list of entities and the memory texts where they co-occur, infer directed
relationships between them.  Return a JSON object:

{
  "relations": [
    {
      "from_entity": "<exact entity name>",
      "to_entity": "<exact entity name>",
      "relation_type": "<one of: attends, owns, relates_to, scheduled_for, parent_of, part_of, sibling_of, lives_at, works_at, member_of>",
      "confidence": <float 0.0-1.0>
    }
  ]
}

Rules:
- Return ONLY valid JSON. No markdown fences, no commentary.
- Only infer relationships that are clearly supported by the memory text.
- Use confidence < 0.8 for uncertain inferences.
- Do NOT infer trivial or obvious relationships (e.g. a person "relates_to" themselves).
- Prefer specific relation types (attends, parent_of) over generic ones (relates_to).
"""


# ---------------------------------------------------------------------------
# Helper: get Supabase client
# ---------------------------------------------------------------------------

def _get_db():
    """Return the initialised Supabase client from the brain module."""
    db = brain._supabase
    if db is None:
        raise RuntimeError(
            "Supabase client not initialised. Call brain.init(settings) first."
        )
    return db


# ---------------------------------------------------------------------------
# Helper: call LLM for JSON extraction
# ---------------------------------------------------------------------------

def _llm_json(system_prompt: str, user_text: str, max_tokens: int = 1024) -> dict:
    """Call the LLM with a system prompt and return parsed JSON."""
    try:
        result = brain.get_llm_reply(
            system_message=system_prompt,
            user_message=user_text,
            max_tokens=max_tokens,
            json_schema={"type": "object"},
        )
        if isinstance(result, dict):
            return result
        return json.loads(result)
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning("LLM JSON extraction failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# 1. extract_and_store_entities
# ---------------------------------------------------------------------------

def extract_and_store_entities(
    text: str,
    family_id: str,
    memory_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Extract named entities from *text* via the LLM and upsert into the graph.

    For each extracted entity the function:
      1. Checks whether an entity with the same (name, family_id) already exists.
      2. If it exists, updates aliases/metadata if new information is found.
      3. If it does not exist, inserts a new row.
      4. If *memory_id* is provided, creates a link in memory_entity_links.

    Returns the list of entity dicts (id, name, entity_type) that were stored.
    """
    db = _get_db()

    # --- Step 1: Ask the LLM to extract entities ---
    extracted = _llm_json(_ENTITY_EXTRACTION_PROMPT, text)
    entities_raw = extracted.get("entities", [])
    if not entities_raw:
        logger.info("No entities extracted from text (%d chars)", len(text))
        return []

    stored: list[dict[str, Any]] = []

    for ent in entities_raw:
        name = (ent.get("name") or "").strip()
        entity_type = (ent.get("entity_type") or "").strip().lower()
        aliases = ent.get("aliases") or []
        metadata = ent.get("metadata") or {}

        # Validate
        if not name:
            continue
        if entity_type not in _VALID_ENTITY_TYPES:
            entity_type = "person" if entity_type == "" else "event"

        # --- Step 2: Upsert — match on lower(name) + family_id ---
        try:
            existing = (
                db.table("family_entities")
                .select("id, aliases, metadata")
                .eq("family_id", family_id)
                .ilike("name", name)
                .limit(1)
                .execute()
            )

            if existing.data:
                # Entity already exists — merge aliases and metadata
                row = existing.data[0]
                entity_id = row["id"]
                old_aliases = set(row.get("aliases") or [])
                new_aliases = list(old_aliases | set(aliases))
                merged_meta = {**(row.get("metadata") or {}), **metadata}

                db.table("family_entities").update({
                    "aliases": new_aliases,
                    "metadata": merged_meta,
                }).eq("id", entity_id).execute()

                logger.debug("Updated existing entity '%s' (id=%s)", name, entity_id)
            else:
                # Insert new entity
                result = db.table("family_entities").insert({
                    "family_id": family_id,
                    "entity_type": entity_type,
                    "name": name,
                    "aliases": aliases,
                    "metadata": metadata,
                }).execute()
                entity_id = result.data[0]["id"] if result.data else None

                if entity_id:
                    logger.info(
                        "New entity created: '%s' (%s) for family %s",
                        name, entity_type, family_id,
                    )
                else:
                    logger.warning("Entity insert returned no data for '%s'", name)
                    continue

            stored.append({
                "id": entity_id,
                "name": name,
                "entity_type": entity_type,
            })

            # --- Step 3: Link to memory if memory_id provided ---
            if memory_id and entity_id:
                try:
                    db.table("memory_entity_links").upsert({
                        "memory_id": memory_id,
                        "entity_id": entity_id,
                        "family_id": family_id,
                    }).execute()
                except Exception as link_exc:
                    logger.warning(
                        "Failed to link memory %s to entity %s: %s",
                        memory_id, entity_id, link_exc,
                    )

        except Exception as exc:
            logger.warning("Entity upsert failed for '%s': %s", name, exc)
            continue

    logger.info(
        "Entity extraction complete: %d entities from %d-char text (family=%s)",
        len(stored), len(text), family_id,
    )
    return stored


# ---------------------------------------------------------------------------
# 2. infer_relations
# ---------------------------------------------------------------------------

def infer_relations(family_id: str) -> int:
    """Infer relationships between entities from co-occurrence in memories.

    Finds pairs of entities that appear in the same memory, sends them to
    the LLM with the memory text for context, and stores inferred relations
    with source='llm_inferred'.

    Returns the number of new relations created.
    """
    db = _get_db()

    # --- Step 1: Get all entities for this family ---
    entities_res = db.table("family_entities").select(
        "id, name, entity_type"
    ).eq("family_id", family_id).execute()
    entities = entities_res.data or []

    if len(entities) < 2:
        logger.info("Not enough entities (%d) to infer relations", len(entities))
        return 0

    entity_map = {e["id"]: e for e in entities}

    # --- Step 2: Find co-occurring entity pairs via memory_entity_links ---
    links_res = db.table("memory_entity_links").select(
        "memory_id, entity_id"
    ).eq("family_id", family_id).execute()
    links = links_res.data or []

    # Group entity IDs by memory_id
    memory_to_entities: dict[str, list[str]] = {}
    for link in links:
        mid = link["memory_id"]
        eid = link["entity_id"]
        memory_to_entities.setdefault(mid, []).append(eid)

    # Collect co-occurring pairs and the memories they share
    from collections import defaultdict
    pair_memories: dict[tuple[str, str], list[str]] = defaultdict(list)
    for mid, eids in memory_to_entities.items():
        if len(eids) < 2:
            continue
        for i, eid_a in enumerate(eids):
            for eid_b in eids[i + 1:]:
                # Canonical ordering to avoid duplicates
                pair = tuple(sorted([eid_a, eid_b]))
                pair_memories[pair].append(mid)

    if not pair_memories:
        logger.info("No co-occurring entity pairs found for family %s", family_id)
        return 0

    # --- Step 3: Get existing relations to avoid duplicates ---
    existing_rels_res = db.table("family_entity_relations").select(
        "from_entity_id, to_entity_id, relation_type"
    ).eq("family_id", family_id).execute()
    existing_rels = set()
    for r in (existing_rels_res.data or []):
        existing_rels.add(
            (r["from_entity_id"], r["to_entity_id"], r["relation_type"])
        )

    # --- Step 4: For each pair, fetch memory content and ask LLM ---
    new_relations = 0

    # Process in batches to avoid overwhelming the LLM
    pairs_list = list(pair_memories.items())[:50]  # Cap at 50 pairs per run

    for (eid_a, eid_b), memory_ids in pairs_list:
        ent_a = entity_map.get(eid_a)
        ent_b = entity_map.get(eid_b)
        if not ent_a or not ent_b:
            continue

        # Fetch memory content for context (limit to 3 memories per pair)
        sample_mids = memory_ids[:3]
        memory_texts = []
        for mid in sample_mids:
            try:
                mem_res = db.table("memories").select("content").eq("id", mid).limit(1).execute()
                if mem_res.data:
                    memory_texts.append(mem_res.data[0]["content"][:500])
            except Exception:
                pass

        if not memory_texts:
            continue

        context = (
            f"Entity A: {ent_a['name']} ({ent_a['entity_type']})\n"
            f"Entity B: {ent_b['name']} ({ent_b['entity_type']})\n\n"
            f"Memories where both appear:\n"
            + "\n---\n".join(memory_texts)
        )

        result = _llm_json(_RELATION_INFERENCE_PROMPT, context, max_tokens=512)
        relations = result.get("relations", [])

        for rel in relations:
            from_name = (rel.get("from_entity") or "").strip()
            to_name = (rel.get("to_entity") or "").strip()
            rel_type = (rel.get("relation_type") or "relates_to").strip().lower()
            confidence = float(rel.get("confidence", 0.7))

            # Resolve names back to entity IDs
            from_id = (
                eid_a if ent_a["name"].lower() == from_name.lower()
                else eid_b if ent_b["name"].lower() == from_name.lower()
                else None
            )
            to_id = (
                eid_a if ent_a["name"].lower() == to_name.lower()
                else eid_b if ent_b["name"].lower() == to_name.lower()
                else None
            )

            if not from_id or not to_id or from_id == to_id:
                continue

            # Skip if relation already exists
            if (from_id, to_id, rel_type) in existing_rels:
                continue

            try:
                db.table("family_entity_relations").insert({
                    "family_id": family_id,
                    "from_entity_id": from_id,
                    "to_entity_id": to_id,
                    "relation_type": rel_type,
                    "confidence": confidence,
                    "source": "llm_inferred",
                }).execute()
                existing_rels.add((from_id, to_id, rel_type))
                new_relations += 1
                logger.info(
                    "Inferred relation: %s -[%s]-> %s (confidence=%.2f)",
                    ent_a["name"] if from_id == eid_a else ent_b["name"],
                    rel_type,
                    ent_b["name"] if to_id == eid_b else ent_a["name"],
                    confidence,
                )
            except Exception as exc:
                logger.warning("Failed to store inferred relation: %s", exc)

    logger.info(
        "Relation inference complete: %d new relations for family %s",
        new_relations, family_id,
    )
    return new_relations


# ---------------------------------------------------------------------------
# 3. get_entity_context
# ---------------------------------------------------------------------------

def get_entity_context(query: str, family_id: str) -> str:
    """Find entities relevant to *query* and traverse their relations.

    Searches family_entities by name (case-insensitive substring match) and
    aliases, then follows one hop of relations to build a context string like:

        Izzy (person) -> attends -> St Joseph's School (organisation)
        St Joseph's School (organisation) -> has event -> Sports Day (event)

    Returns the context string to be injected into LLM prompts, or an empty
    string if no relevant entities are found.
    """
    db = _get_db()

    # --- Step 1: Find matching entities ---
    # Split query into significant words (3+ chars) for matching
    query_words = [w.strip(".,!?;:'\"") for w in query.split() if len(w) >= 3]
    if not query_words:
        return ""

    matched_entity_ids: set[str] = set()
    matched_entities: dict[str, dict] = {}

    # Fetch all entities for this family (typically a small set per family)
    all_entities_res = db.table("family_entities").select(
        "id, name, entity_type, aliases"
    ).eq("family_id", family_id).execute()
    all_entities = all_entities_res.data or []

    for ent in all_entities:
        ent_name_lower = ent["name"].lower()
        ent_aliases = [a.lower() for a in (ent.get("aliases") or [])]
        all_names = [ent_name_lower] + ent_aliases

        for word in query_words:
            word_lower = word.lower()
            if any(word_lower in n or n in word_lower for n in all_names):
                matched_entity_ids.add(ent["id"])
                matched_entities[ent["id"]] = ent
                break

    if not matched_entity_ids:
        return ""

    # --- Step 2: Traverse one hop of relations ---
    context_lines: list[str] = []
    visited_relations: set[str] = set()

    for eid in list(matched_entity_ids):
        # Outgoing relations
        out_res = db.table("family_entity_relations").select(
            "id, to_entity_id, relation_type, confidence"
        ).eq("from_entity_id", eid).eq("family_id", family_id).execute()

        for rel in (out_res.data or []):
            rel_key = rel["id"]
            if rel_key in visited_relations:
                continue
            visited_relations.add(rel_key)

            to_ent = matched_entities.get(rel["to_entity_id"])
            if not to_ent:
                # Fetch the target entity
                to_res = db.table("family_entities").select(
                    "id, name, entity_type"
                ).eq("id", rel["to_entity_id"]).limit(1).execute()
                if to_res.data:
                    to_ent = to_res.data[0]
                    matched_entities[to_ent["id"]] = to_ent

            if to_ent:
                from_ent = matched_entities.get(eid, {})
                conf_str = f" [{rel['confidence']:.0%}]" if rel["confidence"] < 1.0 else ""
                context_lines.append(
                    f"{from_ent.get('name', '?')} ({from_ent.get('entity_type', '?')}) "
                    f"-> {rel['relation_type']} -> "
                    f"{to_ent['name']} ({to_ent.get('entity_type', '?')}){conf_str}"
                )

        # Incoming relations
        in_res = db.table("family_entity_relations").select(
            "id, from_entity_id, relation_type, confidence"
        ).eq("to_entity_id", eid).eq("family_id", family_id).execute()

        for rel in (in_res.data or []):
            rel_key = rel["id"]
            if rel_key in visited_relations:
                continue
            visited_relations.add(rel_key)

            from_ent = matched_entities.get(rel["from_entity_id"])
            if not from_ent:
                from_res = db.table("family_entities").select(
                    "id, name, entity_type"
                ).eq("id", rel["from_entity_id"]).limit(1).execute()
                if from_res.data:
                    from_ent = from_res.data[0]
                    matched_entities[from_ent["id"]] = from_ent

            if from_ent:
                to_ent = matched_entities.get(eid, {})
                conf_str = f" [{rel['confidence']:.0%}]" if rel["confidence"] < 1.0 else ""
                context_lines.append(
                    f"{from_ent['name']} ({from_ent.get('entity_type', '?')}) "
                    f"-> {rel['relation_type']} -> "
                    f"{to_ent.get('name', '?')} ({to_ent.get('entity_type', '?')}){conf_str}"
                )

    if not context_lines:
        # Still return entity names even without relations
        entity_strs = [
            f"{e['name']} ({e['entity_type']})"
            for e in matched_entities.values()
        ]
        return "Known entities: " + ", ".join(entity_strs)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_lines: list[str] = []
    for line in context_lines:
        if line not in seen:
            seen.add(line)
            unique_lines.append(line)

    return "\n".join(unique_lines)


# ---------------------------------------------------------------------------
# 4. get_entity_graph_summary
# ---------------------------------------------------------------------------

# Human-friendly labels for relation types
_RELATION_LABELS: dict[str, str] = {
    "parent_of":     "parent of",
    "sibling_of":    "sibling of",
    "attends":       "attends",
    "works_at":      "works at",
    "lives_at":      "lives at",
    "member_of":     "member of",
    "owns":          "owns",
    "part_of":       "part of",
    "scheduled_for": "scheduled for",
    "relates_to":    "connected to",
}

# Relation types that indicate a person is connected to another entity
_PERSON_CONNECTIONS = frozenset({
    "parent_of", "sibling_of", "attends", "works_at", "lives_at",
    "member_of", "owns", "scheduled_for", "relates_to",
})


def get_entity_graph_summary(family_id: str) -> str:
    """Return a warm, human-readable summary of the family's knowledge graph.

    Output is grouped by person first, then organisations, then places.
    Only entities with at least one relation or linked memory are shown.
    Designed to fit comfortably within WhatsApp's ~4096-char message limit.

    Used by the /graph command.
    """
    db = _get_db()

    # --- Fetch all entities ---
    entities_res = db.table("family_entities").select(
        "id, name, entity_type, aliases, metadata"
    ).eq("family_id", family_id).order("name").execute()
    entities = entities_res.data or []

    if not entities:
        return (
            "\U0001f9e0 I haven't built your family graph yet.\n\n"
            "As you chat with me — sending messages, photos, or documents — "
            "I'll automatically learn about the people, places, and events in your family's life. "
            "Try sending me something to remember!"
        )

    entity_map: dict[str, dict] = {e["id"]: e for e in entities}

    # --- Fetch all relations ---
    relations_res = db.table("family_entity_relations").select(
        "from_entity_id, to_entity_id, relation_type, confidence"
    ).eq("family_id", family_id).execute()
    relations = relations_res.data or []

    # --- Fetch memory link counts per entity ---
    links_res = db.table("memory_entity_links").select(
        "entity_id"
    ).eq("family_id", family_id).execute()
    memory_counts: dict[str, int] = {}
    for lnk in (links_res.data or []):
        eid = lnk["entity_id"]
        memory_counts[eid] = memory_counts.get(eid, 0) + 1

    # --- Build adjacency: entity_id -> list of (relation_type, other_entity_id, direction) ---
    adjacency: dict[str, list[tuple[str, str, str]]] = {}
    for rel in relations:
        fid = rel["from_entity_id"]
        tid = rel["to_entity_id"]
        rtype = rel["relation_type"]
        adjacency.setdefault(fid, []).append((rtype, tid, "out"))
        adjacency.setdefault(tid, []).append((rtype, fid, "in"))

    # --- Determine which entities to show (has relation OR has linked memories) ---
    visible_ids: set[str] = set()
    for eid in entity_map:
        if adjacency.get(eid) or memory_counts.get(eid, 0) > 0:
            visible_ids.add(eid)

    # Always show all people regardless
    for eid, ent in entity_map.items():
        if ent["entity_type"] == "person":
            visible_ids.add(eid)

    if not visible_ids:
        visible_ids = set(entity_map.keys())  # fallback: show everything

    # --- Helper: render a person's section ---
    def _person_section(ent: dict) -> list[str]:
        eid = ent["id"]
        name = ent["name"]
        meta = ent.get("metadata") or {}
        role = meta.get("role", "")

        # Header line with role hint
        if role == "parent":
            header = f"\U0001f464 *{name}* (you)"
        elif role == "child":
            # Pick gender emoji from metadata if available, else generic child
            gender = meta.get("gender", "").lower()
            if gender == "female":
                header = f"\U0001f467 *{name}*"
            elif gender == "male":
                header = f"\U0001f466 *{name}*"
            else:
                header = f"\U0001f9d2 *{name}*"
        else:
            header = f"\U0001f464 *{name}*"

        section = [header]

        # Connections: outgoing relations (e.g. Dan parent_of Izzy)
        connections: list[str] = []
        schools: list[str] = []
        activities: list[str] = []
        upcoming: list[str] = []

        for rtype, other_id, direction in adjacency.get(eid, []):
            other = entity_map.get(other_id)
            if not other:
                continue
            other_name = other["name"]
            other_type = other["entity_type"]
            label = _RELATION_LABELS.get(rtype, rtype.replace("_", " "))

            if direction == "out":
                if rtype == "parent_of" and other_type == "person":
                    # Determine child relationship label
                    child_meta = other.get("metadata") or {}
                    child_role = child_meta.get("role", "child")
                    connections.append(f"{other_name} (child)")
                elif rtype == "sibling_of":
                    connections.append(f"{other_name} (sibling)")
                elif rtype == "attends" and other_type == "organisation":
                    schools.append(other_name)
                elif rtype in ("member_of", "attends") and other_type == "event":
                    activities.append(other_name)
                elif rtype == "scheduled_for":
                    upcoming.append(other_name)
                elif other_type == "person":
                    connections.append(f"{other_name} ({label})")
                elif other_type in ("organisation", "place"):
                    connections.append(f"{other_name} ({label})")
            else:  # incoming
                if rtype == "parent_of" and other_type == "person":
                    connections.append(f"{other_name} (parent)")
                elif rtype == "sibling_of":
                    connections.append(f"{other_name} (sibling)")

        if connections:
            section.append(f"Connected to: {', '.join(connections)}")
        if schools:
            section.append(f"School: {', '.join(schools)}")
        if activities:
            section.append(f"Activities: {', '.join(activities)}")
        if upcoming:
            section.append(f"Upcoming: {', '.join(upcoming)}")

        # Memory count hint
        n_mem = memory_counts.get(eid, 0)
        if n_mem > 0:
            noun = "memory" if n_mem == 1 else "memories"
            section.append(f"I know about: {n_mem} stored {noun}")

        return section

    # --- Helper: render an organisation or place section ---
    def _org_section(ent: dict) -> list[str]:
        eid = ent["id"]
        name = ent["name"]
        etype = ent["entity_type"]
        emoji = "\U0001f3eb" if etype == "organisation" else "\U0001f4cd"
        meta = ent.get("metadata") or {}

        section = [f"{emoji} *{name}*"]

        # Who is connected to this org/place (incoming attends/member_of)
        members: list[str] = []
        for rtype, other_id, direction in adjacency.get(eid, []):
            other = entity_map.get(other_id)
            if not other:
                continue
            if direction == "in" and rtype in ("attends", "member_of", "works_at"):
                members.append(other["name"])

        if members:
            section.append(f"Attended by: {', '.join(members)}")

        # Contact info from metadata
        contact = meta.get("contact") or meta.get("email") or meta.get("phone")
        if contact:
            section.append(f"Contact: {contact}")

        n_mem = memory_counts.get(eid, 0)
        if n_mem > 0:
            noun = "memory" if n_mem == 1 else "memories"
            section.append(f"Linked memories: {n_mem} {noun}")

        return section

    # --- Assemble the full message ---
    lines: list[str] = []
    lines.append("\U0001f9e0 Here's what I know about your family\n")

    # 1. People first
    people = [
        e for e in entities
        if e["entity_type"] == "person" and e["id"] in visible_ids
    ]
    for person in sorted(people, key=lambda e: e["name"]):
        section = _person_section(person)
        lines.extend(section)
        lines.append("")  # blank line between people

    # 2. Organisations (schools, companies)
    orgs = [
        e for e in entities
        if e["entity_type"] == "organisation" and e["id"] in visible_ids
    ]
    if orgs:
        for org in sorted(orgs, key=lambda e: e["name"]):
            section = _org_section(org)
            lines.extend(section)
            lines.append("")

    # 3. Places
    places = [
        e for e in entities
        if e["entity_type"] == "place" and e["id"] in visible_ids
    ]
    if places:
        for place in sorted(places, key=lambda e: e["name"]):
            section = _org_section(place)
            lines.extend(section)
            lines.append("")

    # Footer hint
    lines.append("_Reply /graph [name] for more detail about a specific person._")

    # Trim trailing blank lines before footer
    result = "\n".join(lines)
    return result


# ---------------------------------------------------------------------------
# 5. get_entity_detail  (used by /graph [name])
# ---------------------------------------------------------------------------

def get_entity_detail(name_query: str, family_id: str) -> str:
    """Return a detailed profile for a single named entity.

    Searches by name (case-insensitive) and returns all known relations,
    linked memory count, aliases, and metadata for that entity.

    Used by the /graph [name] variant of the command.
    """
    db = _get_db()

    # --- Find the entity ---
    res = db.table("family_entities").select(
        "id, name, entity_type, aliases, metadata"
    ).eq("family_id", family_id).ilike("name", f"%{name_query.strip()}%").limit(1).execute()

    if not res.data:
        return (
            f"I don't have anyone called \u201c{name_query}\u201d in your family graph yet. "
            "They'll appear automatically as you mention them in messages."
        )

    ent = res.data[0]
    eid = ent["id"]
    name = ent["name"]
    etype = ent["entity_type"]
    meta = ent.get("metadata") or {}

    # --- Fetch all relations for this entity ---
    out_res = db.table("family_entity_relations").select(
        "to_entity_id, relation_type, confidence, source"
    ).eq("from_entity_id", eid).eq("family_id", family_id).execute()

    in_res = db.table("family_entity_relations").select(
        "from_entity_id, relation_type, confidence, source"
    ).eq("to_entity_id", eid).eq("family_id", family_id).execute()

    # --- Fetch linked memory count ---
    links_res = db.table("memory_entity_links").select(
        "memory_id"
    ).eq("entity_id", eid).eq("family_id", family_id).execute()
    n_mem = len(links_res.data or [])

    # --- Collect related entity IDs ---
    related_ids: set[str] = set()
    for r in (out_res.data or []):
        related_ids.add(r["to_entity_id"])
    for r in (in_res.data or []):
        related_ids.add(r["from_entity_id"])

    related_map: dict[str, dict] = {}
    if related_ids:
        rel_ents_res = db.table("family_entities").select(
            "id, name, entity_type"
        ).in_("id", list(related_ids)).execute()
        for re in (rel_ents_res.data or []):
            related_map[re["id"]] = re

    # --- Build the detail card ---
    type_emoji = {
        "person": "\U0001f464",
        "place": "\U0001f4cd",
        "event": "\U0001f4c5",
        "document": "\U0001f4c4",
        "organisation": "\U0001f3eb",
        "date_range": "\U0001f4c6",
    }
    emoji = type_emoji.get(etype, "\u2022")
    lines: list[str] = []
    lines.append(f"{emoji} *{name}*")

    if ent.get("aliases"):
        lines.append(f"Also known as: {', '.join(ent['aliases'])}")

    role = meta.get("role")
    if role:
        lines.append(f"Role: {role}")

    # Outgoing relations
    for r in (out_res.data or []):
        other = related_map.get(r["to_entity_id"])
        if other:
            label = _RELATION_LABELS.get(r["relation_type"], r["relation_type"].replace("_", " "))
            conf_str = f" ({r['confidence']:.0%} confidence)" if r.get("confidence", 1.0) < 1.0 else ""
            lines.append(f"\u2192 {label} {other['name']} ({other['entity_type']}){conf_str}")

    # Incoming relations
    for r in (in_res.data or []):
        other = related_map.get(r["from_entity_id"])
        if other:
            label = _RELATION_LABELS.get(r["relation_type"], r["relation_type"].replace("_", " "))
            conf_str = f" ({r['confidence']:.0%} confidence)" if r.get("confidence", 1.0) < 1.0 else ""
            lines.append(f"\u2190 {other['name']} ({other['entity_type']}) {label} them{conf_str}")

    # Memory count
    if n_mem > 0:
        noun = "memory" if n_mem == 1 else "memories"
        lines.append(f"\U0001f9e0 {n_mem} stored {noun} mention {name}")
    else:
        lines.append(f"No memories linked to {name} yet.")

    # Extra metadata fields (contact, email, phone, etc.)
    for key in ("contact", "email", "phone", "address"):
        val = meta.get(key)
        if val:
            lines.append(f"{key.capitalize()}: {val}")

    return "\n".join(lines)

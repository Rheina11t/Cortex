"""
FamilyBrain -- Memory Consolidation & Entity Profile Generation.

Periodic background job that clusters related memories by entity and
generates concise "Entity Profiles" via the LLM.  These profiles replace
raw memory dumps in the query context, dramatically reducing token usage
while improving answer quality.

Usage
-----
    # Run once for all families
    python -m src.memory_consolidation --now

    # Run once for a specific family
    python -m src.memory_consolidation --now --family <family_id>

    # Schedule weekly (Sunday 03:00)
    python -m src.memory_consolidation --hour 3

Integration
-----------
Call ``get_entity_profiles(family_id, entity_names)`` from the query path
(e.g. ``whatsapp_capture._answer_query``) to retrieve pre-built profiles
instead of raw memories for known entities.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from . import brain

logger = logging.getLogger("open_brain.memory_consolidation")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MIN_MEMORIES_FOR_PROFILE = 3
"""Minimum number of linked memories before we generate a profile."""

_MAX_MEMORIES_PER_PROFILE = 40
"""Cap on memories sent to the LLM per entity to stay within token limits."""

# ---------------------------------------------------------------------------
# LLM Prompt
# ---------------------------------------------------------------------------

_CONSOLIDATION_PROMPT = """\
You are a memory consolidation assistant for a family knowledge base called FamilyBrain.

Given a set of raw memory snippets about a specific entity (person, place, organisation, \
or event), produce a single concise factual profile.

Rules:
- Combine all facts into a coherent paragraph. Do NOT use bullet points.
- Include: key relationships, regular schedules/routines, preferences, allergies, \
  important dates, locations, and any recurring commitments.
- If memories contradict each other, prefer the most recent one and note the discrepancy.
- Do NOT invent information. Only include facts present in the memories.
- Keep the profile under 200 words.
- Start with the entity name and type in parentheses, e.g. "Izzy (person): ..."
- Use present tense for ongoing facts, past tense for completed events.

Example output:
"Izzy (person): Attends St Joseph's School, Year 3. Has swimming lessons every \
Tuesday at 4pm at Riverside Pool. Allergic to nuts — carries an EpiPen. Birthday: \
15 March. Dad usually does school pickup on weekdays. Mum handles weekend activities. \
Best friend is Amara."
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
# Core: build profile for a single entity
# ---------------------------------------------------------------------------

def _build_entity_profile(
    entity: dict[str, Any],
    memories: list[dict[str, Any]],
) -> Optional[str]:
    """Generate a consolidated profile for *entity* from its linked *memories*.

    Returns the profile text, or None if generation fails.
    """
    if len(memories) < _MIN_MEMORIES_FOR_PROFILE:
        return None

    # Sort by created_at descending so the LLM sees newest first
    memories_sorted = sorted(
        memories,
        key=lambda m: m.get("created_at", ""),
        reverse=True,
    )[:_MAX_MEMORIES_PER_PROFILE]

    # Format memories for the prompt
    memory_lines = []
    for m in memories_sorted:
        content = m.get("content", "").strip()
        created = m.get("created_at", "")
        if created:
            try:
                ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                ts_label = ts.strftime("%d %b %Y")
            except Exception:
                ts_label = str(created)[:10]
            memory_lines.append(f"[{ts_label}] {content}")
        else:
            memory_lines.append(content)

    entity_name = entity.get("name", "Unknown")
    entity_type = entity.get("entity_type", "unknown")
    memory_block = "\n".join(memory_lines)

    user_message = (
        f"Entity: {entity_name} ({entity_type})\n"
        f"Number of memories: {len(memories_sorted)}\n\n"
        f"Raw memories (newest first):\n{memory_block}"
    )

    try:
        profile = brain.get_llm_reply(
            system_message=_CONSOLIDATION_PROMPT,
            user_message=user_message,
            max_tokens=400,
        )
        if isinstance(profile, str) and len(profile.strip()) > 20:
            logger.info(
                "Profile generated for '%s' (%d memories -> %d chars)",
                entity_name, len(memories_sorted), len(profile),
            )
            return profile.strip()
        logger.warning("Profile too short for '%s': %r", entity_name, profile)
        return None
    except Exception as exc:
        logger.warning("Profile generation failed for '%s': %s", entity_name, exc)
        return None


# ---------------------------------------------------------------------------
# Core: consolidate one family
# ---------------------------------------------------------------------------

def consolidate_family(family_id: str) -> dict[str, Any]:
    """Run memory consolidation for a single family.

    Returns a summary dict with counts of entities processed and profiles
    created/updated.
    """
    db = _get_db()
    summary: dict[str, Any] = {
        "family_id": family_id,
        "entities_checked": 0,
        "profiles_created": 0,
        "profiles_updated": 0,
        "profiles_skipped": 0,
    }

    # Step 1: Get all entities for this family
    entities_res = db.table("family_entities").select(
        "id, name, entity_type"
    ).eq("family_id", family_id).execute()
    entities = entities_res.data or []

    if not entities:
        logger.info("No entities found for family %s", family_id)
        return summary

    entity_map = {e["id"]: e for e in entities}

    # Step 2: Get all memory-entity links for this family
    links_res = db.table("memory_entity_links").select(
        "memory_id, entity_id"
    ).eq("family_id", family_id).execute()
    links = links_res.data or []

    # Group memory IDs by entity
    entity_memory_ids: dict[str, list[str]] = defaultdict(list)
    for link in links:
        entity_memory_ids[link["entity_id"]].append(link["memory_id"])

    # Step 3: Get existing profiles to check for updates
    existing_res = db.table("memory_profiles").select(
        "entity_id, memory_count, updated_at"
    ).eq("family_id", family_id).execute()
    existing_profiles: dict[str, dict] = {
        p["entity_id"]: p for p in (existing_res.data or [])
    }

    # Step 4: Process each entity
    for entity_id, memory_ids in entity_memory_ids.items():
        summary["entities_checked"] += 1
        entity = entity_map.get(entity_id)
        if not entity:
            continue

        mem_count = len(memory_ids)
        if mem_count < _MIN_MEMORIES_FOR_PROFILE:
            summary["profiles_skipped"] += 1
            continue

        # Check if profile is already up-to-date
        existing = existing_profiles.get(entity_id)
        if existing and existing.get("memory_count", 0) == mem_count:
            summary["profiles_skipped"] += 1
            logger.debug(
                "Profile for '%s' already up-to-date (%d memories)",
                entity.get("name"), mem_count,
            )
            continue

        # Fetch the actual memory content
        memories = []
        # Batch fetch in chunks of 20 to avoid overly large queries
        for i in range(0, len(memory_ids), 20):
            chunk = memory_ids[i:i + 20]
            try:
                mem_res = db.table("memories").select(
                    "id, content, created_at"
                ).in_("id", chunk).execute()
                memories.extend(mem_res.data or [])
            except Exception as exc:
                logger.warning("Failed to fetch memory batch: %s", exc)

        if len(memories) < _MIN_MEMORIES_FOR_PROFILE:
            summary["profiles_skipped"] += 1
            continue

        # Generate the profile
        profile_text = _build_entity_profile(entity, memories)
        if not profile_text:
            summary["profiles_skipped"] += 1
            continue

        # Upsert the profile
        source_ids = [m["id"] for m in memories]
        row = {
            "family_id": family_id,
            "entity_id": entity_id,
            "profile_text": profile_text,
            "memory_count": mem_count,
            "source_memory_ids": source_ids,
        }

        try:
            if existing:
                db.table("memory_profiles").update({
                    "profile_text": profile_text,
                    "memory_count": mem_count,
                    "source_memory_ids": source_ids,
                }).eq("family_id", family_id).eq(
                    "entity_id", entity_id,
                ).execute()
                summary["profiles_updated"] += 1
                logger.info(
                    "Updated profile for '%s' (%d -> %d memories)",
                    entity.get("name"),
                    existing.get("memory_count", 0),
                    mem_count,
                )
            else:
                db.table("memory_profiles").insert(row).execute()
                summary["profiles_created"] += 1
                logger.info(
                    "Created profile for '%s' (%d memories)",
                    entity.get("name"), mem_count,
                )
        except Exception as exc:
            logger.warning(
                "Failed to upsert profile for '%s': %s",
                entity.get("name"), exc,
            )
            summary["profiles_skipped"] += 1

    logger.info(
        "Consolidation complete for family %s: %d checked, %d created, "
        "%d updated, %d skipped",
        family_id,
        summary["entities_checked"],
        summary["profiles_created"],
        summary["profiles_updated"],
        summary["profiles_skipped"],
    )
    return summary


# ---------------------------------------------------------------------------
# Public API: retrieve profiles for query context
# ---------------------------------------------------------------------------

def get_entity_profiles(
    family_id: str,
    entity_names: list[str],
) -> list[dict[str, Any]]:
    """Retrieve pre-built entity profiles matching *entity_names*.

    Performs case-insensitive matching against entity names and aliases.
    Returns a list of dicts with keys: entity_name, entity_type, profile_text.
    """
    db = _get_db()

    if not entity_names:
        return []

    # Fetch all profiles for this family (typically a small set)
    try:
        profiles_res = db.table("memory_profiles").select(
            "entity_id, profile_text, memory_count"
        ).eq("family_id", family_id).execute()
        profiles = profiles_res.data or []
    except Exception as exc:
        logger.warning("Failed to fetch memory profiles: %s", exc)
        return []

    if not profiles:
        return []

    # Fetch entity details to match names
    entity_ids = [p["entity_id"] for p in profiles]
    try:
        entities_res = db.table("family_entities").select(
            "id, name, entity_type, aliases"
        ).eq("family_id", family_id).in_("id", entity_ids).execute()
        entities = {e["id"]: e for e in (entities_res.data or [])}
    except Exception as exc:
        logger.warning("Failed to fetch entities for profiles: %s", exc)
        return []

    # Match requested names against entity names and aliases
    query_names_lower = [n.lower() for n in entity_names]
    matched: list[dict[str, Any]] = []

    for profile in profiles:
        entity = entities.get(profile["entity_id"])
        if not entity:
            continue

        ent_name_lower = entity["name"].lower()
        ent_aliases = [a.lower() for a in (entity.get("aliases") or [])]
        all_names = [ent_name_lower] + ent_aliases

        for qname in query_names_lower:
            if any(qname in n or n in qname for n in all_names):
                matched.append({
                    "entity_name": entity["name"],
                    "entity_type": entity.get("entity_type", "unknown"),
                    "profile_text": profile["profile_text"],
                    "memory_count": profile.get("memory_count", 0),
                })
                break

    return matched


def format_profiles_for_prompt(profiles: list[dict[str, Any]]) -> str:
    """Format retrieved profiles into a text block for LLM prompt injection.

    Returns an empty string if no profiles are available.
    """
    if not profiles:
        return ""

    lines = ["ENTITY PROFILES (consolidated from stored memories):"]
    for p in profiles:
        lines.append(f"  {p['profile_text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point: run consolidation
# ---------------------------------------------------------------------------

def run_consolidation(family_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Run memory consolidation for one or all families.

    Returns a list of summary dicts, one per family processed.
    """
    db = _get_db()
    summaries: list[dict[str, Any]] = []

    if family_id:
        families = [{"family_id": family_id}]
    else:
        # Fetch all active families
        try:
            fam_res = db.table("families").select("family_id").eq(
                "status", "active"
            ).execute()
            families = fam_res.data or []
        except Exception as exc:
            logger.error("Failed to fetch families: %s", exc)
            return summaries

    for fam in families:
        fid = fam.get("family_id")
        if not fid:
            continue
        try:
            summary = consolidate_family(fid)
            summaries.append(summary)
        except Exception as exc:
            logger.error("Consolidation failed for family %s: %s", fid, exc)
            summaries.append({"family_id": fid, "error": str(exc)})

    return summaries


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="FamilyBrain -- Memory Consolidation & Entity Profile Generation"
    )
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run consolidation immediately and exit.",
    )
    parser.add_argument(
        "--family",
        type=str,
        default="",
        help="Process only this family_id (default: all active families).",
    )
    parser.add_argument(
        "--hour",
        type=int,
        default=3,
        help="Hour of day to run consolidation (24h format, default: 3).",
    )
    parser.add_argument(
        "--minute",
        type=int,
        default=0,
        help="Minute of hour to run consolidation (default: 0).",
    )
    args = parser.parse_args()

    from .config import get_settings
    settings = get_settings()
    brain.init(settings)

    if args.now:
        logger.info("Running memory consolidation immediately (--now flag)…")
        fid = args.family or None
        summaries = run_consolidation(family_id=fid)
        for s in summaries:
            logger.info("Summary: %s", json.dumps(s, default=str))
        return

    # Schedule weekly on Sunday at the specified time
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = BackgroundScheduler()
    scheduler.add_job(
        lambda: run_consolidation(family_id=args.family or None),
        trigger=CronTrigger(day_of_week="sun", hour=args.hour, minute=args.minute),
        id="memory_consolidation",
        name="FamilyBrain Memory Consolidation",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Memory consolidation scheduler started — will run every Sunday at %02d:%02d.",
        args.hour, args.minute,
    )

    # Keep the process alive
    try:
        import signal
        signal.pause()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    main()

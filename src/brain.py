"""
Open Brain – Core logic shared by the Telegram capture layer and MCP server.

Provides:
  * generate_embedding()     – create a vector embedding for a text string.
  * extract_metadata()       – call an LLM to clean text and extract metadata.
  * store_memory()           – insert a memory row into Supabase.
  * semantic_search()        – cosine-similarity search via the DB function.
  * list_recent_memories()   – fetch the N most recent memories.
  * query_by_metadata()      – filter memories by JSONB metadata fields.

LLM backend strategy
--------------------
The module supports two LLM backends for metadata extraction, selected via
the ``LLM_BACKEND`` environment variable:

  openai     (default) – uses OpenAI chat completions API (e.g. gpt-4.1-mini).
                          Supports JSON mode via ``response_format``.

  anthropic            – uses the Anthropic Messages API (e.g. claude-3-5-haiku-20241022).
                          Requires ``ANTHROPIC_API_KEY`` to be set.

Embeddings are always generated via the OpenAI embeddings API, regardless of
which LLM backend is selected for metadata extraction.

Embedding strategy
------------------
The module supports two embedding backends, selected via the
``EMBEDDING_BACKEND`` environment variable:

  openai  (default) – calls the OpenAI embeddings API via
                       ``OPENAI_EMBEDDING_BASE_URL``.  Produces 1536-dim
                       vectors when using ``text-embedding-3-small``.

  local             – uses a local sentence-transformer model
                       (``all-MiniLM-L6-v2``, 384 dims) and pads to 1536
                       dims for compatibility with the pgvector column.
                       Useful for testing without an OpenAI API key.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any

import numpy as np
from openai import OpenAI
from supabase import Client, create_client

from .config import Settings

logger = logging.getLogger("open_brain.brain")

# ---------------------------------------------------------------------------
# Module-level singletons (initialised lazily via init())
# ---------------------------------------------------------------------------
_llm_client: OpenAI | None = None          # OpenAI client (used when LLM_BACKEND=openai)
_anthropic_client: Any | None = None       # anthropic.Anthropic client (LLM_BACKEND=anthropic)
_embedding_client: OpenAI | None = None
_supabase: Client | None = None
_settings: Settings | None = None
_local_model: Any = None                   # SentenceTransformer instance (local embedding backend)
_embedding_backend: str = "openai"
_llm_backend: str = "openai"


def init(settings: Settings) -> None:
    """Initialise all clients from the given settings."""
    global _llm_client, _anthropic_client, _embedding_client  # noqa: PLW0603
    global _supabase, _settings, _local_model                  # noqa: PLW0603
    global _embedding_backend, _llm_backend                    # noqa: PLW0603

    _settings = settings
    _embedding_backend = os.getenv("EMBEDDING_BACKEND", "openai").lower()
    _llm_backend = settings.llm_backend.lower()

    # ── LLM client ──────────────────────────────────────────────────────────
    if _llm_backend == "anthropic":
        if not settings.anthropic_api_key:
            raise EnvironmentError(
                "LLM_BACKEND=anthropic requires ANTHROPIC_API_KEY to be set."
            )
        import anthropic as _anthropic_sdk
        _anthropic_client = _anthropic_sdk.Anthropic(api_key=settings.anthropic_api_key)
        _llm_client = None
        logger.info("LLM backend: Anthropic (model=%s)", settings.llm_model)
    else:
        # Default: OpenAI (may go through a proxy)
        _llm_client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
        _anthropic_client = None
        logger.info("LLM backend: OpenAI (model=%s, base_url=%s)", settings.llm_model, settings.openai_base_url)

    # ── Embedding client ─────────────────────────────────────────────────────
    if _embedding_backend == "local":
        logger.info("Embedding backend: local sentence-transformer (all-MiniLM-L6-v2)")
        from sentence_transformers import SentenceTransformer
        _local_model = SentenceTransformer("all-MiniLM-L6-v2")
        _embedding_client = None
    else:
        logger.info(
            "Embedding backend: OpenAI (model=%s, base_url=%s)",
            settings.embedding_model,
            settings.openai_embedding_base_url,
        )
        _embedding_client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_embedding_base_url,
        )

    # ── Supabase client ──────────────────────────────────────────────────────
    _supabase = create_client(settings.supabase_url, settings.supabase_service_key)

    logger.info(
        "Brain initialised (llm_backend=%s, llm_model=%s, embedding_backend=%s, embedding_model=%s)",
        _llm_backend,
        settings.llm_model,
        _embedding_backend,
        settings.embedding_model if _embedding_backend == "openai" else "all-MiniLM-L6-v2",
    )


def _require_init() -> tuple[Client, Settings]:
    if _supabase is None or _settings is None:
        raise RuntimeError("brain.init(settings) must be called before use.")
    return _supabase, _settings


# ── Embedding generation ──────────────────────────────────────────────────

def generate_embedding(text: str) -> list[float]:
    """Return a 1536-dim embedding vector for *text*.

    Uses either the OpenAI API or a local sentence-transformer model,
    depending on the ``EMBEDDING_BACKEND`` setting.
    Embeddings always use OpenAI regardless of the LLM backend.
    """
    if _embedding_backend == "local":
        return _generate_local_embedding(text)
    return _generate_openai_embedding(text)


def _generate_openai_embedding(text: str) -> list[float]:
    """Generate embedding via the OpenAI API."""
    if _embedding_client is None or _settings is None:
        raise RuntimeError("brain.init(settings) must be called before use.")
    response = _embedding_client.embeddings.create(
        model=_settings.embedding_model,
        input=text,
    )
    embedding = response.data[0].embedding
    logger.debug("Generated OpenAI embedding (%d dims) for %d-char text", len(embedding), len(text))
    return embedding


def _generate_local_embedding(text: str) -> list[float]:
    """Generate embedding via a local sentence-transformer model.

    The model produces 384-dim vectors.  We pad to 1536 dims with zeros
    to match the pgvector column width defined in the migration.
    """
    if _local_model is None:
        raise RuntimeError("Local embedding model not initialised.")
    raw = _local_model.encode(text, normalize_embeddings=True)
    vec_384 = raw.tolist() if hasattr(raw, "tolist") else list(raw)
    padded = vec_384 + [0.0] * (1536 - len(vec_384))
    logger.debug("Generated local embedding (384→1536 dims) for %d-char text", len(text))
    return padded


# ── LLM metadata extraction ──────────────────────────────────────────────

_EXTRACTION_SYSTEM_PROMPT = """\
You are a metadata extraction assistant for a personal knowledge base called "Open Brain".

Given a raw message, you MUST return a JSON object with exactly these keys:

{
  "cleaned_content": "<the message rewritten as a clear, concise note>",
  "tags": ["<relevant topic tags>"],
  "people": ["<names of people mentioned, if any>"],
  "category": "<one of: idea, meeting-notes, decision, action-item, reference, personal, other>",
  "action_items": ["<any action items extracted, if any>"],
  "source": "telegram"
}

Rules:
- Return ONLY valid JSON. No markdown fences, no commentary.
- If a field has no value, use an empty list [] or empty string "".
- Keep cleaned_content faithful to the original meaning.
- For action_items: NEVER include action items for dates that are in the past (before today). Only include action items for future dates or undated items. Today's date is {datetime.now().strftime('%Y-%m-%d')}.
- NEVER include "make the payment" or "pay the direct debit" as an action item if the payment_method is "direct debit" or "DD" — direct debits are automatic and require no action.
- Tags should be lowercase, hyphenated, and concise.
"""


def extract_metadata(raw_text: str) -> dict[str, Any]:
    """Call the configured LLM backend to clean *raw_text* and extract metadata.

    Dispatches to either OpenAI or Anthropic depending on ``LLM_BACKEND``.
    """
    if _settings is None:
        raise RuntimeError("brain.init(settings) must be called before use.")

    if _llm_backend == "anthropic":
        return _extract_metadata_anthropic(raw_text)
    return _extract_metadata_openai(raw_text)


def _extract_metadata_openai(raw_text: str) -> dict[str, Any]:
    """Metadata extraction using the OpenAI chat completions API."""
    if _llm_client is None or _settings is None:
        raise RuntimeError("brain.init(settings) must be called before use.")

    try:
        response = _llm_client.chat.completions.create(
            model=_settings.llm_model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        parsed: dict[str, Any] = json.loads(content)
        logger.info(
            "Metadata extracted via OpenAI – category=%s, tags=%s",
            parsed.get("category"), parsed.get("tags"),
        )
        return parsed

    except Exception as exc:
        logger.warning("OpenAI metadata extraction failed (%s); using fallback.", exc)
        return _fallback_metadata(raw_text)


def _extract_metadata_anthropic(raw_text: str) -> dict[str, Any]:
    """Metadata extraction using the Anthropic Messages API.

    Anthropic does not support a strict JSON mode, so we instruct the model
    to return only JSON and strip any accidental markdown fences.
    """
    if _anthropic_client is None or _settings is None:
        raise RuntimeError("brain.init(settings) must be called before use.")

    # Build the user prompt — combine system instructions + user content
    # because Anthropic's system parameter is separate from the messages list.
    try:
        response = _anthropic_client.messages.create(
            model=_settings.llm_model,
            max_tokens=1024,
            temperature=0.0,
            system=_EXTRACTION_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": raw_text},
            ],
        )
        raw_content: str = response.content[0].text if response.content else "{}"

        # Strip accidental markdown fences (```json ... ```)
        raw_content = raw_content.strip()
        if raw_content.startswith("```"):
            raw_content = raw_content.split("```")[1]
            if raw_content.startswith("json"):
                raw_content = raw_content[4:]
            raw_content = raw_content.strip()

        parsed: dict[str, Any] = json.loads(raw_content)
        logger.info(
            "Metadata extracted via Anthropic – category=%s, tags=%s",
            parsed.get("category"), parsed.get("tags"),
        )
        return parsed

    except Exception as exc:
        logger.warning("Anthropic metadata extraction failed (%s); using fallback.", exc)
        return _fallback_metadata(raw_text)


def _fallback_metadata(raw_text: str) -> dict[str, Any]:
    """Return a minimal metadata dict when LLM extraction fails."""
    return {
        "cleaned_content": raw_text,
        "tags": [],
        "people": [],
        "category": "other",
        "action_items": [],
        "source": "telegram",
    }


# ── Database operations ──────────────────────────────────────────────────

def store_memory(content: str, embedding: list[float], metadata: dict[str, Any]) -> dict[str, Any]:
    """Insert a new memory row and return the created record."""
    db, _ = _require_init()

    row = {
        "content": content,
        "embedding": embedding,
        "metadata": metadata,
    }
    result = db.table("memories").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Memory stored (id=%s)", record.get("id", "unknown"))
    return record


def semantic_search(
    query: str,
    match_threshold: float = 0.5,
    match_count: int = 10,
) -> list[dict[str, Any]]:
    """Generate an embedding for *query* and search for similar memories."""
    db, _ = _require_init()

    query_embedding = generate_embedding(query)
    result = db.rpc(
        "match_memories",
        {
            "query_embedding": query_embedding,
            "match_threshold": match_threshold,
            "match_count": match_count,
        },
    ).execute()
    logger.info("Semantic search returned %d results", len(result.data or []))
    return result.data or []


def list_recent_memories(limit: int = 20) -> list[dict[str, Any]]:
    """Return the *limit* most recent memories ordered by created_at DESC."""
    db, _ = _require_init()

    result = (
        db.table("memories")
        .select("id, content, metadata, created_at")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    logger.info("Listed %d recent memories", len(result.data or []))
    return result.data or []


def query_by_metadata(
    tags: list[str] | None = None,
    people: list[str] | None = None,
    category: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Filter memories by metadata fields using JSONB containment."""
    db, _ = _require_init()

    query = db.table("memories").select("id, content, metadata, created_at")

    if tags:
        query = query.contains("metadata", {"tags": tags})
    if people:
        query = query.contains("metadata", {"people": people})
    if category:
        query = query.contains("metadata", {"category": category})

    result = query.order("created_at", desc=True).limit(limit).execute()
    logger.info(
        "Metadata query (tags=%s, people=%s, category=%s) returned %d results",
        tags, people, category, len(result.data or []),
    )
    return result.data or []


def get_stats() -> dict[str, Any]:
    """Return aggregate statistics about the knowledge base.

    Computes:
    - total memory count
    - oldest and newest created_at timestamps
    - top 10 tags by frequency
    - top 5 categories by frequency
    - top 10 people mentioned by frequency
    """
    db, _ = _require_init()

    result = db.table("memories").select("metadata, created_at").execute()
    rows: list[dict[str, Any]] = result.data or []

    if not rows:
        return {
            "total": 0,
            "oldest": None,
            "newest": None,
            "top_tags": [],
            "top_categories": [],
            "top_people": [],
        }

    # Date range
    dates = [r["created_at"] for r in rows if r.get("created_at")]
    oldest = min(dates) if dates else None
    newest = max(dates) if dates else None

    # Frequency counters
    from collections import Counter
    tag_counter: Counter = Counter()
    cat_counter: Counter = Counter()
    people_counter: Counter = Counter()

    for row in rows:
        meta = row.get("metadata") or {}
        for tag in meta.get("tags", []):
            tag_counter[tag] += 1
        cat = meta.get("category", "")
        if cat:
            cat_counter[cat] += 1
        for person in meta.get("people", []):
            people_counter[person] += 1

    stats = {
        "total": len(rows),
        "oldest": oldest,
        "newest": newest,
        "top_tags": [{"tag": t, "count": c} for t, c in tag_counter.most_common(10)],
        "top_categories": [{"category": cat, "count": c} for cat, c in cat_counter.most_common(5)],
        "top_people": [{"person": p, "count": c} for p, c in people_counter.most_common(10)],
    }
    logger.info("Stats computed: total=%d", stats["total"])
    return stats


# ── Household knowledge operations ──────────────────────────────────────

def add_household_item(
    item_name: str,
    category: str,
    location: str = "",
    brand: str = "",
    model_number: str = "",
    purchase_date: str = "",
    warranty_expiry: str = "",
    notes: str = "",
    added_by: str = "",
) -> dict[str, Any]:
    """Insert a household item and return the created record."""
    db, _ = _require_init()
    row: dict[str, Any] = {
        "item_name": item_name,
        "category": category,
    }
    if location:
        row["location"] = location
    if brand:
        row["brand"] = brand
    if model_number:
        row["model_number"] = model_number
    if purchase_date:
        row["purchase_date"] = purchase_date
    if warranty_expiry:
        row["warranty_expiry"] = warranty_expiry
    if notes:
        row["notes"] = notes
    if added_by:
        row["added_by"] = added_by
    result = db.table("household_items").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Household item stored (id=%s, name=%s)", record.get("id"), item_name)
    return record


def list_household_items(
    category: str | None = None,
    location: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List household items, optionally filtered by category or location."""
    db, _ = _require_init()
    query = db.table("household_items").select("*")
    if category:
        query = query.ilike("category", f"%{category}%")
    if location:
        query = query.ilike("location", f"%{location}%")
    result = query.order("created_at", desc=True).limit(limit).execute()
    logger.info("Listed %d household items", len(result.data or []))
    return result.data or []


def add_household_vendor(
    vendor_name: str,
    service_type: str,
    phone: str = "",
    email: str = "",
    website: str = "",
    rating: int | None = None,
    notes: str = "",
    added_by: str = "",
) -> dict[str, Any]:
    """Insert a household vendor/contractor and return the created record."""
    db, _ = _require_init()
    row: dict[str, Any] = {
        "vendor_name": vendor_name,
        "service_type": service_type,
    }
    if phone:
        row["phone"] = phone
    if email:
        row["email"] = email
    if website:
        row["website"] = website
    if rating is not None:
        row["rating"] = rating
    if notes:
        row["notes"] = notes
    if added_by:
        row["added_by"] = added_by
    result = db.table("household_vendors").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Vendor stored (id=%s, name=%s)", record.get("id"), vendor_name)
    return record


def list_household_vendors(
    service_type: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List household vendors, optionally filtered by service type."""
    db, _ = _require_init()
    query = db.table("household_vendors").select("*")
    if service_type:
        query = query.ilike("service_type", f"%{service_type}%")
    result = query.order("created_at", desc=True).limit(limit).execute()
    logger.info("Listed %d household vendors", len(result.data or []))
    return result.data or []


# ── Family scheduling operations ────────────────────────────────────────

def add_family_event(
    title: str,
    event_date: str,
    family_member: str,
    event_time: str = "",
    end_date: str = "",
    location: str = "",
    notes: str = "",
    recurrence: str = "",
    added_by: str = "",
) -> dict[str, Any]:
    """Insert a family event and return the created record."""
    db, _ = _require_init()
    row: dict[str, Any] = {
        "title": title,
        "event_date": event_date,
        "family_member": family_member,
    }
    if event_time:
        row["event_time"] = event_time
    if end_date:
        row["end_date"] = end_date
    if location:
        row["location"] = location
    if notes:
        row["notes"] = notes
    if recurrence:
        row["recurrence"] = recurrence
    if added_by:
        row["added_by"] = added_by
    result = db.table("family_events").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Family event stored (id=%s, title=%s)", record.get("id"), title)
    return record


def check_family_schedule(
    date_start: str,
    date_end: str,
    family_member: str | None = None,
) -> list[dict[str, Any]]:
    """Return family events in a date range, optionally for a specific member."""
    db, _ = _require_init()
    query = (
        db.table("family_events")
        .select("*")
        .gte("event_date", date_start)
        .lte("event_date", date_end)
    )
    if family_member:
        query = query.ilike("family_member", f"%{family_member}%")
    result = query.order("event_date").order("event_time").execute()
    logger.info(
        "Schedule query (%s to %s, member=%s) returned %d events",
        date_start, date_end, family_member, len(result.data or []),
    )
    return result.data or []


def check_conflicts(
    event_date: str,
    family_member: str | None = None,
) -> list[dict[str, Any]]:
    """Check for scheduling conflicts on a given date."""
    db, _ = _require_init()
    query = (
        db.table("family_events")
        .select("*")
        .eq("event_date", event_date)
    )
    if family_member:
        query = query.ilike("family_member", f"%{family_member}%")
    result = query.execute()
    logger.info(
        "Conflict check on %s (member=%s): %d existing events",
        event_date, family_member, len(result.data or []),
    )
    return result.data or []


# ==========================================================================
# EXTENSION 2: Home Maintenance Tracker
# ==========================================================================

def add_maintenance_task(
    title: str,
    category: str = "other",
    location: str = "",
    frequency_days: int | None = None,
    next_due: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Insert a maintenance task and return the created record."""
    db, _ = _require_init()
    row: dict[str, Any] = {"title": title, "category": category}
    if location:
        row["location"] = location
    if frequency_days is not None:
        row["frequency_days"] = frequency_days
    if next_due:
        row["next_due"] = next_due
    if notes:
        row["notes"] = notes
    result = db.table("maintenance_tasks").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Maintenance task created (id=%s, title=%s)", record.get("id"), title)
    return record


def log_maintenance(
    task_id: str,
    completed_date: str = "",
    performed_by: str = "",
    cost_gbp: float = 0.0,
    notes: str = "",
) -> dict[str, Any]:
    """Log a maintenance completion and auto-update the parent task."""
    db, _ = _require_init()
    row: dict[str, Any] = {"task_id": task_id}
    if completed_date:
        row["completed_date"] = completed_date
    if performed_by:
        row["performed_by"] = performed_by
    if cost_gbp:
        row["cost_gbp"] = cost_gbp
    if notes:
        row["notes"] = notes
    log_result = db.table("maintenance_logs").insert(row).execute()
    log_record = log_result.data[0] if log_result.data else {}

    # Auto-update the parent task's last_completed and next_due
    if completed_date:
        update: dict[str, Any] = {"last_completed": completed_date}
        # Fetch the task to get frequency_days
        task_result = db.table("maintenance_tasks").select("frequency_days").eq("id", task_id).execute()
        if task_result.data and task_result.data[0].get("frequency_days"):
            from datetime import datetime, timedelta
            freq = task_result.data[0]["frequency_days"]
            comp_dt = datetime.strptime(completed_date, "%Y-%m-%d")
            update["next_due"] = (comp_dt + timedelta(days=freq)).strftime("%Y-%m-%d")
        db.table("maintenance_tasks").update(update).eq("id", task_id).execute()

    logger.info("Maintenance logged (log_id=%s, task_id=%s)", log_record.get("id"), task_id)
    return log_record


def get_upcoming_maintenance(days_ahead: int = 30) -> list[dict[str, Any]]:
    """Return maintenance tasks due within the next *days_ahead* days."""
    db, _ = _require_init()
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    result = (
        db.table("maintenance_tasks")
        .select("*")
        .lte("next_due", cutoff)
        .gte("next_due", today)
        .order("next_due")
        .execute()
    )
    logger.info("Upcoming maintenance (%d days): %d tasks", days_ahead, len(result.data or []))
    return result.data or []


def search_maintenance_history(
    query: str = "",
    task_id: str = "",
    date_from: str = "",
    date_to: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search maintenance logs with optional filters."""
    db, _ = _require_init()
    q = db.table("maintenance_logs").select("*, maintenance_tasks(title, category, location)")
    if task_id:
        q = q.eq("task_id", task_id)
    if date_from:
        q = q.gte("completed_date", date_from)
    if date_to:
        q = q.lte("completed_date", date_to)
    if query:
        q = q.ilike("notes", f"%{query}%")
    result = q.order("completed_date", desc=True).limit(limit).execute()
    logger.info("Maintenance history search: %d results", len(result.data or []))
    return result.data or []


# ==========================================================================
# EXTENSION 3: Vehicle Management
# ==========================================================================

def add_vehicle(
    nickname: str,
    make: str = "",
    model: str = "",
    year: int | None = None,
    registration: str = "",
    colour: str = "",
    mot_due: str = "",
    insurance_due: str = "",
    tax_due: str = "",
    mileage: int | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Insert a vehicle and return the created record."""
    db, _ = _require_init()
    row: dict[str, Any] = {"nickname": nickname}
    if make:
        row["make"] = make
    if model:
        row["model"] = model
    if year is not None:
        row["year"] = year
    if registration:
        row["registration"] = registration
    if colour:
        row["colour"] = colour
    if mot_due:
        row["mot_due"] = mot_due
    if insurance_due:
        row["insurance_due"] = insurance_due
    if tax_due:
        row["tax_due"] = tax_due
    if mileage is not None:
        row["mileage"] = mileage
    if notes:
        row["notes"] = notes
    result = db.table("vehicles").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Vehicle added (id=%s, nickname=%s)", record.get("id"), nickname)
    return record


def list_vehicles() -> list[dict[str, Any]]:
    """Return all vehicles."""
    db, _ = _require_init()
    result = db.table("vehicles").select("*").order("created_at").execute()
    return result.data or []


def log_vehicle_service(
    vehicle_id: str,
    service_type: str = "other",
    description: str = "",
    service_date: str = "",
    mileage_at: int | None = None,
    cost_gbp: float = 0.0,
    garage: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Log a vehicle service/repair entry."""
    db, _ = _require_init()
    row: dict[str, Any] = {"vehicle_id": vehicle_id, "service_type": service_type}
    if description:
        row["description"] = description
    if service_date:
        row["service_date"] = service_date
    if mileage_at is not None:
        row["mileage_at"] = mileage_at
    if cost_gbp:
        row["cost_gbp"] = cost_gbp
    if garage:
        row["garage"] = garage
    if notes:
        row["notes"] = notes
    result = db.table("vehicle_service_logs").insert(row).execute()

    # Update vehicle mileage if provided
    if mileage_at is not None:
        db.table("vehicles").update({"mileage": mileage_at}).eq("id", vehicle_id).execute()

    record = result.data[0] if result.data else {}
    logger.info("Vehicle service logged (id=%s, vehicle=%s)", record.get("id"), vehicle_id)
    return record


def get_vehicle_reminders(days_ahead: int = 30) -> list[dict[str, Any]]:
    """Return vehicles with MOT, insurance, or tax due within *days_ahead* days."""
    db, _ = _require_init()
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    today = datetime.utcnow().strftime("%Y-%m-%d")

    vehicles = db.table("vehicles").select("*").execute().data or []
    reminders = []
    for v in vehicles:
        alerts = []
        for field, label in [("mot_due", "MOT"), ("insurance_due", "Insurance"), ("tax_due", "Tax")]:
            due = v.get(field)
            if due and today <= due <= cutoff:
                alerts.append(f"{label} due {due}")
        if alerts:
            v["_alerts"] = alerts
            reminders.append(v)
    logger.info("Vehicle reminders (%d days): %d vehicles", days_ahead, len(reminders))
    return reminders


def get_vehicle_history(vehicle_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return service history for a specific vehicle."""
    db, _ = _require_init()
    result = (
        db.table("vehicle_service_logs")
        .select("*")
        .eq("vehicle_id", vehicle_id)
        .order("service_date", desc=True)
        .limit(limit)
        .execute()
    )
    return result.data or []


# ==========================================================================
# EXTENSION 4: Health & Wellness Tracker
# ==========================================================================

def log_health_metric(
    family_member: str,
    metric_type: str,
    value: float,
    unit: str = "",
    secondary_value: float | None = None,
    notes: str = "",
    recorded_at: str = "",
) -> dict[str, Any]:
    """Log a health metric reading."""
    db, _ = _require_init()
    row: dict[str, Any] = {
        "family_member": family_member,
        "metric_type": metric_type,
        "value": value,
    }
    if unit:
        row["unit"] = unit
    if secondary_value is not None:
        row["secondary_value"] = secondary_value
    if notes:
        row["notes"] = notes
    if recorded_at:
        row["recorded_at"] = recorded_at
    result = db.table("health_metrics").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Health metric logged (%s: %s=%s)", family_member, metric_type, value)
    return record


def get_health_metrics(
    family_member: str = "",
    metric_type: str = "",
    days_back: int = 30,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Retrieve health metrics with optional filters."""
    db, _ = _require_init()
    from datetime import datetime, timedelta
    since = (datetime.utcnow() - timedelta(days=days_back)).isoformat()
    q = db.table("health_metrics").select("*").gte("recorded_at", since)
    if family_member:
        q = q.eq("family_member", family_member)
    if metric_type:
        q = q.eq("metric_type", metric_type)
    result = q.order("recorded_at", desc=True).limit(limit).execute()
    return result.data or []


def add_medication(
    family_member: str,
    name: str,
    dosage: str = "",
    frequency: str = "",
    prescriber: str = "",
    pharmacy: str = "",
    start_date: str = "",
    end_date: str = "",
    refill_due: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Add a medication record."""
    db, _ = _require_init()
    row: dict[str, Any] = {"family_member": family_member, "name": name}
    for k, v in [("dosage", dosage), ("frequency", frequency), ("prescriber", prescriber),
                 ("pharmacy", pharmacy), ("start_date", start_date), ("end_date", end_date),
                 ("refill_due", refill_due), ("notes", notes)]:
        if v:
            row[k] = v
    result = db.table("medications").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Medication added (%s: %s)", family_member, name)
    return record


def get_active_medications(family_member: str = "") -> list[dict[str, Any]]:
    """Return active medications, optionally for a specific family member."""
    db, _ = _require_init()
    q = db.table("medications").select("*").eq("is_active", True)
    if family_member:
        q = q.eq("family_member", family_member)
    result = q.order("family_member").order("name").execute()
    return result.data or []


def add_medical_appointment(
    family_member: str,
    appointment_type: str = "general",
    provider: str = "",
    location: str = "",
    appointment_date: str = "",
    appointment_time: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Add a medical appointment."""
    db, _ = _require_init()
    row: dict[str, Any] = {
        "family_member": family_member,
        "appointment_type": appointment_type,
    }
    for k, v in [("provider", provider), ("location", location),
                 ("appointment_date", appointment_date), ("appointment_time", appointment_time),
                 ("notes", notes)]:
        if v:
            row[k] = v
    result = db.table("medical_appointments").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Medical appointment added (%s: %s on %s)", family_member, appointment_type, appointment_date)
    return record


def get_upcoming_appointments(days_ahead: int = 30, family_member: str = "") -> list[dict[str, Any]]:
    """Return upcoming medical appointments."""
    db, _ = _require_init()
    from datetime import datetime, timedelta
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cutoff = (datetime.utcnow() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    q = (
        db.table("medical_appointments")
        .select("*")
        .gte("appointment_date", today)
        .lte("appointment_date", cutoff)
    )
    if family_member:
        q = q.eq("family_member", family_member)
    result = q.order("appointment_date").order("appointment_time").execute()
    return result.data or []


def get_medication_refills_due(days_ahead: int = 14) -> list[dict[str, Any]]:
    """Return medications with refills due within *days_ahead* days."""
    db, _ = _require_init()
    from datetime import datetime, timedelta
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cutoff = (datetime.utcnow() + timedelta(days=days_ahead)).strftime("%Y-%m-%d")
    result = (
        db.table("medications")
        .select("*")
        .eq("is_active", True)
        .gte("refill_due", today)
        .lte("refill_due", cutoff)
        .order("refill_due")
        .execute()
    )
    return result.data or []


# ==========================================================================
# EXTENSION 5: Financial Tracker
# ==========================================================================

def add_recurring_bill(
    name: str,
    category: str = "other",
    amount_gbp: float = 0.0,
    frequency: str = "monthly",
    due_day: int | None = None,
    provider: str = "",
    account_ref: str = "",
    payment_method: str = "",
    auto_pay: bool = True,
    notes: str = "",
) -> dict[str, Any]:
    """Add a recurring bill/subscription."""
    db, _ = _require_init()
    row: dict[str, Any] = {
        "name": name,
        "category": category,
        "amount_gbp": amount_gbp,
        "frequency": frequency,
        "auto_pay": auto_pay,
    }
    if due_day is not None:
        row["due_day"] = due_day
    for k, v in [("provider", provider), ("account_ref", account_ref),
                 ("payment_method", payment_method), ("notes", notes)]:
        if v:
            row[k] = v
    result = db.table("recurring_bills").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Recurring bill added (id=%s, name=%s)", record.get("id"), name)
    return record


def get_recurring_bills(category: str = "", active_only: bool = True) -> list[dict[str, Any]]:
    """Return recurring bills, optionally filtered by category."""
    db, _ = _require_init()
    q = db.table("recurring_bills").select("*")
    if active_only:
        q = q.eq("is_active", True)
    if category:
        q = q.eq("category", category)
    result = q.order("category").order("name").execute()
    return result.data or []


def get_monthly_bill_total() -> dict[str, Any]:
    """Calculate total monthly outgoings from active recurring bills."""
    db, _ = _require_init()
    result = db.table("recurring_bills").select("*").eq("is_active", True).execute()
    bills = result.data or []

    monthly_total = 0.0
    annual_total = 0.0
    by_category: dict[str, float] = {}

    freq_multipliers = {
        "weekly": 52 / 12,
        "fortnightly": 26 / 12,
        "monthly": 1.0,
        "quarterly": 1 / 3,
        "annually": 1 / 12,
        "other": 1.0,
    }

    for bill in bills:
        amt = float(bill.get("amount_gbp", 0))
        freq = bill.get("frequency", "monthly")
        monthly = amt * freq_multipliers.get(freq, 1.0)
        monthly_total += monthly
        annual_total += monthly * 12
        cat = bill.get("category", "other")
        by_category[cat] = by_category.get(cat, 0) + monthly

    return {
        "monthly_total_gbp": round(monthly_total, 2),
        "annual_total_gbp": round(annual_total, 2),
        "bill_count": len(bills),
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items(), key=lambda x: -x[1])},
    }


def log_expense(
    description: str,
    amount_gbp: float,
    category: str = "other",
    family_member: str = "family",
    payment_method: str = "",
    vendor: str = "",
    expense_date: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Log a one-off expense."""
    db, _ = _require_init()
    row: dict[str, Any] = {
        "description": description,
        "amount_gbp": amount_gbp,
        "category": category,
        "family_member": family_member,
    }
    for k, v in [("payment_method", payment_method), ("vendor", vendor),
                 ("expense_date", expense_date), ("notes", notes)]:
        if v:
            row[k] = v
    result = db.table("expenses").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Expense logged (id=%s, desc=%s, £%.2f)", record.get("id"), description, amount_gbp)
    return record


def get_spending_summary(
    days_back: int = 30,
    family_member: str = "",
) -> dict[str, Any]:
    """Return a spending summary for the last *days_back* days."""
    db, _ = _require_init()
    from datetime import datetime, timedelta
    since = (datetime.utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    q = db.table("expenses").select("*").gte("expense_date", since)
    if family_member:
        q = q.eq("family_member", family_member)
    result = q.order("expense_date", desc=True).execute()
    expenses = result.data or []

    total = sum(float(e.get("amount_gbp", 0)) for e in expenses)
    by_category: dict[str, float] = {}
    for e in expenses:
        cat = e.get("category", "other")
        by_category[cat] = by_category.get(cat, 0) + float(e.get("amount_gbp", 0))

    return {
        "period_days": days_back,
        "total_gbp": round(total, 2),
        "transaction_count": len(expenses),
        "by_category": {k: round(v, 2) for k, v in sorted(by_category.items(), key=lambda x: -x[1])},
        "recent_expenses": expenses[:10],
    }


# ==========================================================================
# EXTENSION 6: Job Hunt CRM
# ==========================================================================

def add_jh_contact(
    name: str,
    company: str = "",
    role: str = "",
    email: str = "",
    phone: str = "",
    linkedin_url: str = "",
    relationship: str = "recruiter",
    notes: str = "",
) -> dict[str, Any]:
    """Add a job hunt contact (recruiter, hiring manager, etc.)."""
    db, _ = _require_init()
    row: dict[str, Any] = {"name": name, "relationship": relationship}
    for k, v in [("company", company), ("role", role), ("email", email),
                 ("phone", phone), ("linkedin_url", linkedin_url), ("notes", notes)]:
        if v:
            row[k] = v
    result = db.table("jh_contacts").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("JH contact added (id=%s, name=%s)", record.get("id"), name)
    return record


def search_jh_contacts(
    query: str = "",
    company: str = "",
    relationship: str = "",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search job hunt contacts."""
    db, _ = _require_init()
    q = db.table("jh_contacts").select("*")
    if query:
        q = q.or_(f"name.ilike.%{query}%,company.ilike.%{query}%,notes.ilike.%{query}%")
    if company:
        q = q.ilike("company", f"%{company}%")
    if relationship:
        q = q.eq("relationship", relationship)
    result = q.order("updated_at", desc=True).limit(limit).execute()
    return result.data or []


def add_job_application(
    company: str,
    job_title: str,
    url: str = "",
    salary_min: int | None = None,
    salary_max: int | None = None,
    requirements: str = "",
    source: str = "",
    status: str = "identified",
    applied_date: str = "",
    resume_version: str = "",
    cover_letter_notes: str = "",
    contact_id: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Add a job application to the pipeline."""
    db, _ = _require_init()
    row: dict[str, Any] = {"company": company, "job_title": job_title, "status": status}
    if url:
        row["url"] = url
    if salary_min is not None:
        row["salary_min"] = salary_min
    if salary_max is not None:
        row["salary_max"] = salary_max
    for k, v in [("requirements", requirements), ("source", source),
                 ("applied_date", applied_date), ("resume_version", resume_version),
                 ("cover_letter_notes", cover_letter_notes), ("notes", notes)]:
        if v:
            row[k] = v
    if contact_id:
        row["contact_id"] = contact_id
    result = db.table("jh_applications").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Job application added (id=%s, %s @ %s)", record.get("id"), job_title, company)
    return record


def update_application_status(
    application_id: str,
    status: str,
    notes: str = "",
) -> dict[str, Any]:
    """Update the status of a job application."""
    db, _ = _require_init()
    update: dict[str, Any] = {"status": status}
    if notes:
        update["notes"] = notes
    result = db.table("jh_applications").update(update).eq("id", application_id).execute()
    record = result.data[0] if result.data else {}
    logger.info("Application %s status updated to %s", application_id, status)
    return record


def schedule_interview(
    application_id: str,
    interview_type: str = "phone",
    scheduled_at: str = "",
    duration_minutes: int = 60,
    interviewer_name: str = "",
    interviewer_role: str = "",
    location: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """Schedule an interview for a job application."""
    db, _ = _require_init()
    row: dict[str, Any] = {
        "application_id": application_id,
        "interview_type": interview_type,
        "duration_minutes": duration_minutes,
    }
    if scheduled_at:
        row["scheduled_at"] = scheduled_at
    for k, v in [("interviewer_name", interviewer_name), ("interviewer_role", interviewer_role),
                 ("location", location), ("notes", notes)]:
        if v:
            row[k] = v
    result = db.table("jh_interviews").insert(row).execute()

    # Auto-update application status to "interviewing"
    db.table("jh_applications").update({"status": "interviewing"}).eq("id", application_id).execute()

    record = result.data[0] if result.data else {}
    logger.info("Interview scheduled (id=%s, app=%s, type=%s)", record.get("id"), application_id, interview_type)
    return record


def log_interview_notes(
    interview_id: str,
    feedback: str = "",
    rating: int | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Log feedback and rating for a completed interview."""
    db, _ = _require_init()
    update: dict[str, Any] = {"status": "completed"}
    if feedback:
        update["feedback"] = feedback
    if rating is not None:
        update["rating"] = rating
    if notes:
        update["notes"] = notes
    result = db.table("jh_interviews").update(update).eq("id", interview_id).execute()
    record = result.data[0] if result.data else {}
    logger.info("Interview notes logged (id=%s, rating=%s)", interview_id, rating)
    return record


def get_pipeline_overview(days_ahead: int = 7) -> dict[str, Any]:
    """Return a summary of the job hunt pipeline."""
    db, _ = _require_init()
    from datetime import datetime, timedelta

    # Application counts by status
    apps_result = db.table("jh_applications").select("id, company, job_title, status, applied_date, updated_at").execute()
    apps = apps_result.data or []
    by_status: dict[str, list] = {}
    for app in apps:
        s = app.get("status", "unknown")
        by_status.setdefault(s, []).append(f"{app.get('company')} — {app.get('job_title')}")

    # Upcoming interviews
    today = datetime.utcnow().isoformat()
    cutoff = (datetime.utcnow() + timedelta(days=days_ahead)).isoformat()
    interviews_result = (
        db.table("jh_interviews")
        .select("*, jh_applications(company, job_title)")
        .gte("scheduled_at", today)
        .lte("scheduled_at", cutoff)
        .eq("status", "scheduled")
        .order("scheduled_at")
        .execute()
    )

    return {
        "total_applications": len(apps),
        "by_status": {k: {"count": len(v), "roles": v} for k, v in by_status.items()},
        "upcoming_interviews": interviews_result.data or [],
        "active_count": sum(len(v) for k, v in by_status.items() if k in ("applied", "screening", "interviewing", "offer")),
    }


def get_upcoming_interviews(days_ahead: int = 14) -> list[dict[str, Any]]:
    """Return interviews scheduled within the next *days_ahead* days."""
    db, _ = _require_init()
    from datetime import datetime, timedelta
    today = datetime.utcnow().isoformat()
    cutoff = (datetime.utcnow() + timedelta(days=days_ahead)).isoformat()
    result = (
        db.table("jh_interviews")
        .select("*, jh_applications(company, job_title)")
        .gte("scheduled_at", today)
        .lte("scheduled_at", cutoff)
        .order("scheduled_at")
        .execute()
    )
    return result.data or []


def link_contact_to_professional_crm(jh_contact_id: str) -> dict[str, Any]:
    """Copy a job hunt contact into the professional_contacts table for long-term CRM."""
    db, _ = _require_init()
    # Fetch the JH contact
    contact_result = db.table("jh_contacts").select("*").eq("id", jh_contact_id).execute()
    if not contact_result.data:
        return {"error": f"Contact {jh_contact_id} not found"}
    contact = contact_result.data[0]

    # Check if already linked
    existing = (
        db.table("professional_contacts")
        .select("id")
        .eq("jh_contact_id", jh_contact_id)
        .execute()
    )
    if existing.data:
        return {"message": "Already linked", "professional_contact_id": existing.data[0]["id"]}

    # Create professional contact
    row = {
        "jh_contact_id": jh_contact_id,
        "name": contact.get("name", ""),
        "company": contact.get("company", ""),
        "role": contact.get("role", ""),
        "email": contact.get("email", ""),
        "phone": contact.get("phone", ""),
        "linkedin_url": contact.get("linkedin_url", ""),
        "category": "professional",
        "notes": contact.get("notes", ""),
        "last_contact": contact.get("last_contact"),
    }
    result = db.table("professional_contacts").insert(row).execute()
    record = result.data[0] if result.data else {}
    logger.info("Contact %s linked to professional CRM (id=%s)", jh_contact_id, record.get("id"))
    return record

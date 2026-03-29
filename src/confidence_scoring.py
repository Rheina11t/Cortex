"""
FamilyBrain -- Deterministic Confidence Scoring.

Calculates a retrieval quality score based on vector cosine similarity,
memory age, and graph connectivity.  The score is injected into the system
prompt so the LLM can ground its confidence assessment in objective data
rather than guessing.

Usage
-----
    from src import confidence_scoring

    score = confidence_scoring.calculate_retrieval_quality(memories, graph_ctx)
    injection = confidence_scoring.format_confidence_prompt_injection(score)
    # ... append `injection` to the system prompt before calling the LLM
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("open_brain.confidence_scoring")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class RetrievalQualityScore:
    """Deterministic assessment of how good the retrieved context is."""

    avg_similarity: float
    """Mean cosine similarity across retrieved memories (0.0-1.0)."""

    max_similarity: float
    """Highest cosine similarity among retrieved memories (0.0-1.0)."""

    memory_count: int
    """Number of memories retrieved."""

    avg_age_days: float
    """Mean age of retrieved memories in days."""

    freshest_memory_days: float
    """Age of the most recent retrieved memory in days."""

    graph_connection_count: int
    """Number of entity-graph connections in the context."""

    overall_quality: str
    """Categorical quality label: HIGH, MEDIUM, or LOW."""

    explanation: str
    """Human-readable explanation of the quality assessment."""


# ---------------------------------------------------------------------------
# Scoring logic
# ---------------------------------------------------------------------------

def _parse_age_days(created_at_raw: Any) -> float | None:
    """Parse a created_at value and return age in days, or None on failure."""
    if not created_at_raw:
        return None
    try:
        ts = datetime.fromisoformat(str(created_at_raw).replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
        return max(delta.total_seconds() / 86400, 0.0)
    except Exception:
        return None


def calculate_retrieval_quality(
    memories: list[dict[str, Any]],
    graph_context: str = "",
) -> RetrievalQualityScore:
    """Calculate a deterministic retrieval quality score.

    Parameters
    ----------
    memories
        List of memory dicts as returned by ``brain.semantic_search``.
        Each dict may contain ``similarity`` (float) and ``created_at``
        (ISO timestamp string) keys.
    graph_context
        The entity graph context string (one relationship per line).
        Used to count graph connections.

    Returns
    -------
    RetrievalQualityScore
        A dataclass with all computed metrics and the overall quality label.
    """
    # --- Similarity metrics ---
    similarities = [
        float(m["similarity"])
        for m in memories
        if m.get("similarity") is not None
    ]
    avg_sim = sum(similarities) / len(similarities) if similarities else 0.0
    max_sim = max(similarities) if similarities else 0.0

    # --- Age metrics ---
    ages = [
        age
        for m in memories
        if (age := _parse_age_days(m.get("created_at"))) is not None
    ]
    avg_age = sum(ages) / len(ages) if ages else 999.0
    freshest = min(ages) if ages else 999.0

    # --- Graph connections ---
    graph_lines = [
        line for line in graph_context.split("\n")
        if line.strip() and "->" in line
    ]
    graph_count = len(graph_lines)

    mem_count = len(memories)

    # --- Overall quality classification ---
    # HIGH: strong similarity, multiple memories, and recent data
    # LOW: weak similarity, very few memories, or very stale data
    # MEDIUM: everything else
    is_high = (
        avg_sim >= 0.6
        and mem_count >= 3
        and freshest <= 30
    )
    is_low = (
        avg_sim < 0.4
        or mem_count <= 1
        or freshest > 180
    )

    if is_high:
        quality = "HIGH"
    elif is_low:
        quality = "LOW"
    else:
        quality = "MEDIUM"

    # --- Explanation ---
    parts: list[str] = []
    parts.append(f"Based on {mem_count} memor{'y' if mem_count == 1 else 'ies'}")
    if similarities:
        parts.append(f"avg similarity {avg_sim:.2f}, best {max_sim:.2f}")
    if ages:
        parts.append(f"freshest {freshest:.0f} days old, avg age {avg_age:.0f} days")
    if graph_count > 0:
        parts.append(f"{graph_count} graph connection{'s' if graph_count != 1 else ''}")

    explanation = " | ".join(parts) + "."

    score = RetrievalQualityScore(
        avg_similarity=round(avg_sim, 3),
        max_similarity=round(max_sim, 3),
        memory_count=mem_count,
        avg_age_days=round(avg_age, 1),
        freshest_memory_days=round(freshest, 1),
        graph_connection_count=graph_count,
        overall_quality=quality,
        explanation=explanation,
    )

    logger.debug(
        "Retrieval quality: %s (sim=%.2f, count=%d, fresh=%.0fd, graph=%d)",
        quality, avg_sim, mem_count, freshest, graph_count,
    )
    return score


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------

def format_confidence_prompt_injection(score: RetrievalQualityScore) -> str:
    """Format the retrieval quality score as a prompt injection string.

    This string should be appended to the system prompt so the LLM can
    calibrate its stated confidence level against objective retrieval metrics.
    """
    guidance = ""
    if score.overall_quality == "HIGH":
        guidance = (
            "The retrieved data is strong -- you can be confident in your answer "
            "if it is well-supported by the memories."
        )
    elif score.overall_quality == "LOW":
        guidance = (
            "The retrieved data is weak -- your confidence should be Low. "
            "Warn the user about potential gaps and suggest forwarding "
            "relevant documents to improve coverage."
        )
    else:
        guidance = (
            "The retrieved data is moderate -- calibrate your confidence "
            "accordingly and note any areas where more data would help."
        )

    return (
        f"RETRIEVAL QUALITY ASSESSMENT: {score.overall_quality}. "
        f"{score.explanation} "
        f"{guidance}"
    )

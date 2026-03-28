"""FamilyBrain — Per-family token budget and cost caps (Phase 5 gap analysis).

Tracks OpenAI token usage per family and enforces daily/monthly spending limits
to prevent runaway costs from prompt injection, loops, or abuse.

Usage::

    from src.token_budget import check_budget, record_usage

    # Before making an LLM call:
    allowed, reason = check_budget(family_id)
    if not allowed:
        return f"Daily AI limit reached. Try again tomorrow. ({reason})"

    # After an LLM call:
    record_usage(family_id, prompt_tokens=150, completion_tokens=300, model="gpt-4.1-mini")

Budget limits (configurable via environment variables):
    TOKEN_BUDGET_DAILY_PER_FAMILY   — max tokens per family per day (default: 100,000)
    TOKEN_BUDGET_MONTHLY_PER_FAMILY — max tokens per family per month (default: 2,000,000)
    TOKEN_BUDGET_DAILY_GLOBAL       — max tokens across all families per day (default: 5,000,000)
"""
from __future__ import annotations

import collections
import logging
import os
import threading
import time as _time_mod
from datetime import datetime, timezone

logger = logging.getLogger("familybrain.token_budget")

# ---------------------------------------------------------------------------
# Configurable limits
# ---------------------------------------------------------------------------
DAILY_PER_FAMILY = int(os.environ.get("TOKEN_BUDGET_DAILY_PER_FAMILY", "100000"))
MONTHLY_PER_FAMILY = int(os.environ.get("TOKEN_BUDGET_MONTHLY_PER_FAMILY", "2000000"))
DAILY_GLOBAL = int(os.environ.get("TOKEN_BUDGET_DAILY_GLOBAL", "5000000"))

# Approximate cost per 1K tokens (GPT-4.1-mini pricing as baseline)
_COST_PER_1K = {
    "gpt-4.1-mini": {"input": 0.0004, "output": 0.0016},
    "gpt-4.1-nano": {"input": 0.0001, "output": 0.0004},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.0006},
    "default": {"input": 0.001, "output": 0.003},
}


# ---------------------------------------------------------------------------
# In-memory token counters (reset daily/monthly)
# ---------------------------------------------------------------------------
class _TokenBudgetTracker:
    """Thread-safe in-memory token usage tracker."""

    def __init__(self):
        self._lock = threading.Lock()
        # {family_id: {"tokens": int, "date": "YYYY-MM-DD"}}
        self._daily: dict[str, dict] = collections.defaultdict(
            lambda: {"tokens": 0, "date": ""}
        )
        # {family_id: {"tokens": int, "month": "YYYY-MM"}}
        self._monthly: dict[str, dict] = collections.defaultdict(
            lambda: {"tokens": 0, "month": ""}
        )
        self._global_daily = {"tokens": 0, "date": ""}

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _this_month(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def record(self, family_id: str, total_tokens: int, model: str = "default") -> None:
        """Record token usage for a family."""
        today = self._today()
        month = self._this_month()

        with self._lock:
            # Daily counter
            entry = self._daily[family_id]
            if entry["date"] != today:
                entry["tokens"] = 0
                entry["date"] = today
            entry["tokens"] += total_tokens

            # Monthly counter
            m_entry = self._monthly[family_id]
            if m_entry["month"] != month:
                m_entry["tokens"] = 0
                m_entry["month"] = month
            m_entry["tokens"] += total_tokens

            # Global daily counter
            if self._global_daily["date"] != today:
                self._global_daily = {"tokens": 0, "date": today}
            self._global_daily["tokens"] += total_tokens

    def check(self, family_id: str) -> tuple[bool, str]:
        """Check if a family is within budget.  Returns (allowed, reason)."""
        today = self._today()
        month = self._this_month()

        with self._lock:
            # Global daily
            if (
                self._global_daily["date"] == today
                and self._global_daily["tokens"] >= DAILY_GLOBAL
            ):
                return False, "global_daily_limit"

            # Per-family daily
            entry = self._daily[family_id]
            if entry["date"] == today and entry["tokens"] >= DAILY_PER_FAMILY:
                return False, "family_daily_limit"

            # Per-family monthly
            m_entry = self._monthly[family_id]
            if m_entry["month"] == month and m_entry["tokens"] >= MONTHLY_PER_FAMILY:
                return False, "family_monthly_limit"

        return True, ""

    def get_usage(self, family_id: str) -> dict:
        """Return current usage stats for a family (for /stats command)."""
        today = self._today()
        month = self._this_month()
        with self._lock:
            daily = self._daily[family_id]
            monthly = self._monthly[family_id]
            return {
                "daily_tokens": daily["tokens"] if daily["date"] == today else 0,
                "daily_limit": DAILY_PER_FAMILY,
                "monthly_tokens": monthly["tokens"] if monthly["month"] == month else 0,
                "monthly_limit": MONTHLY_PER_FAMILY,
            }


_tracker = _TokenBudgetTracker()


def check_budget(family_id: str) -> tuple[bool, str]:
    """Check if a family is within their token budget.

    Returns (allowed, reason).  If not allowed, *reason* describes which
    limit was hit.
    """
    return _tracker.check(family_id)


def record_usage(
    family_id: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    model: str = "default",
) -> None:
    """Record token usage after an LLM call."""
    total = prompt_tokens + completion_tokens
    if total > 0:
        _tracker.record(family_id, total, model)
        logger.debug(
            "Token usage: family=%s model=%s prompt=%d completion=%d total=%d",
            family_id, model, prompt_tokens, completion_tokens, total,
        )


def get_family_usage(family_id: str) -> dict:
    """Return current usage stats for a family."""
    return _tracker.get_usage(family_id)

"""FamilyBrain — Feature entitlements engine (Phase 5 gap analysis).

Enforces plan-based feature access.  Every gated action should call
``check_entitlement()`` before proceeding.

Usage::

    from src.entitlements import check_entitlement

    allowed, msg = check_entitlement(family_id, "sos_pdf")
    if not allowed:
        return _make_response(msg, from_number=from_number)

The entitlements table (migration 031) maps plans to features with
optional daily/monthly usage caps.
"""
from __future__ import annotations

import collections
import logging
import os
import threading
import time as _time_mod
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional

logger = logging.getLogger("familybrain.entitlements")

# ---------------------------------------------------------------------------
# In-memory usage counters for daily/monthly caps
# ---------------------------------------------------------------------------
class _UsageCounter:
    """Thread-safe counter for per-family per-feature usage."""

    def __init__(self):
        self._lock = threading.Lock()
        # {(family_id, feature): {"count": int, "date": str}}
        self._daily: dict[tuple[str, str], dict] = {}
        # {(family_id, feature): {"count": int, "month": str}}
        self._monthly: dict[tuple[str, str], dict] = {}

    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _this_month(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def increment(self, family_id: str, feature: str) -> None:
        """Record one usage of a feature."""
        key = (family_id, feature)
        today = self._today()
        month = self._this_month()
        with self._lock:
            # Daily
            d = self._daily.get(key, {"count": 0, "date": ""})
            if d["date"] != today:
                d = {"count": 0, "date": today}
            d["count"] += 1
            self._daily[key] = d
            # Monthly
            m = self._monthly.get(key, {"count": 0, "month": ""})
            if m["month"] != month:
                m = {"count": 0, "month": month}
            m["count"] += 1
            self._monthly[key] = m

    def get_daily(self, family_id: str, feature: str) -> int:
        key = (family_id, feature)
        today = self._today()
        with self._lock:
            d = self._daily.get(key, {"count": 0, "date": ""})
            return d["count"] if d["date"] == today else 0

    def get_monthly(self, family_id: str, feature: str) -> int:
        key = (family_id, feature)
        month = self._this_month()
        with self._lock:
            m = self._monthly.get(key, {"count": 0, "month": ""})
            return m["count"] if m["month"] == month else 0


_usage = _UsageCounter()


# ---------------------------------------------------------------------------
# Entitlement cache (loaded from DB, refreshed periodically)
# ---------------------------------------------------------------------------
_entitlement_cache: dict[tuple[str, str], dict] = {}
_cache_loaded_at: float = 0.0
_CACHE_TTL = 300  # 5 minutes


def _load_entitlements_from_db() -> None:
    """Load the entitlements table into the in-memory cache."""
    global _entitlement_cache, _cache_loaded_at
    try:
        from . import brain
        db, _ = brain._require_init()
        result = db.table("entitlements").select("*").execute()
        new_cache = {}
        for row in (result.data or []):
            key = (row["plan"], row["feature"])
            new_cache[key] = {
                "enabled": row.get("enabled", True),
                "max_per_day": row.get("max_per_day"),
                "max_per_month": row.get("max_per_month"),
            }
        _entitlement_cache = new_cache
        _cache_loaded_at = _time_mod.time()
        logger.info("Loaded %d entitlement rules from DB", len(new_cache))
    except Exception as exc:
        logger.warning("Failed to load entitlements from DB (will allow all): %s", exc)


def _get_entitlement(plan: str, feature: str) -> Optional[dict]:
    """Look up the entitlement rule for a plan+feature combo."""
    global _cache_loaded_at
    if _time_mod.time() - _cache_loaded_at > _CACHE_TTL:
        _load_entitlements_from_db()
    return _entitlement_cache.get((plan, feature))


def _get_family_plan(family_id: str) -> str:
    """Look up the subscription plan for a family."""
    try:
        from . import brain
        db, _ = brain._require_init()
        result = (
            db.table("families")
            .select("plan")
            .eq("family_id", family_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return result.data[0].get("plan", "monthly")
    except Exception as exc:
        logger.warning("Failed to look up plan for family %s: %s", family_id, exc)
    return "monthly"  # safe default


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_entitlement(family_id: str, feature: str) -> tuple[bool, str]:
    """Check if a family is entitled to use a feature.

    Returns (allowed, message).  If not allowed, *message* is a
    user-friendly explanation.
    """
    if not family_id:
        return True, ""  # no family context = allow (backward compat)

    plan = _get_family_plan(family_id)
    rule = _get_entitlement(plan, feature)

    if rule is None:
        # No rule found — allow by default (fail open for features not yet gated)
        return True, ""

    if not rule["enabled"]:
        return False, (
            f"The '{feature}' feature is not available on your current plan. "
            "Reply /upgrade for details."
        )

    # Check daily cap
    max_daily = rule.get("max_per_day")
    if max_daily is not None:
        current_daily = _usage.get_daily(family_id, feature)
        if current_daily >= max_daily:
            return False, (
                f"You've reached your daily limit for this feature ({max_daily}/day). "
                "It resets at midnight UTC."
            )

    # Check monthly cap
    max_monthly = rule.get("max_per_month")
    if max_monthly is not None:
        current_monthly = _usage.get_monthly(family_id, feature)
        if current_monthly >= max_monthly:
            return False, (
                f"You've reached your monthly limit for this feature ({max_monthly}/month). "
                "It resets on the 1st of next month."
            )

    return True, ""


def record_feature_use(family_id: str, feature: str) -> None:
    """Record one usage of a feature (call after successful execution)."""
    _usage.increment(family_id, feature)

"""FamilyBrain — Scoped Supabase client factory (Phase 5 gap analysis).

Provides two Supabase clients with different privilege levels:

- **anon_client**: Uses the anon/public key.  Suitable for read operations
  that should respect Row-Level Security (RLS).
- **service_client**: Uses the service_role key.  Required for writes that
  bypass RLS (e.g. family provisioning, billing updates, admin operations).

Usage::

    from src.db_client import get_read_client, get_write_client

    # Read (RLS-enforced)
    db = get_read_client()
    db.table("memories").select("*").eq("family_id", fid).execute()

    # Write (service_role — bypasses RLS)
    db = get_write_client()
    db.table("families").insert({...}).execute()

Environment variables required:
    SUPABASE_URL          — Supabase project URL
    SUPABASE_SERVICE_KEY  — service_role key (elevated privileges)
    SUPABASE_ANON_KEY     — anon/public key (RLS-enforced)

If SUPABASE_ANON_KEY is not set, both clients fall back to service_role
for backward compatibility, but a warning is logged.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

from supabase import create_client, Client

logger = logging.getLogger("familybrain.db_client")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")


@lru_cache(maxsize=1)
def get_write_client() -> Client:
    """Return the service_role Supabase client (bypasses RLS).

    Use ONLY for:
    - Creating/updating families (provisioning)
    - Billing/subscription updates
    - Admin operations (data deletion, migrations)
    - Writes that must bypass RLS
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


@lru_cache(maxsize=1)
def get_read_client() -> Client:
    """Return the anon-key Supabase client (respects RLS).

    Use for:
    - Reading memories, events, documents (user-facing queries)
    - Semantic search results
    - Calendar data
    - Any read where family_id scoping via RLS is appropriate
    """
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL must be set")

    if SUPABASE_ANON_KEY:
        return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)

    # Fallback: no anon key configured — use service_role with a warning
    logger.warning(
        "SUPABASE_ANON_KEY not set — falling back to service_role for reads. "
        "Set SUPABASE_ANON_KEY to enforce RLS on read operations."
    )
    return get_write_client()

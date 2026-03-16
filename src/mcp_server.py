#!/usr/bin/env python3
"""
Family Brain – MCP Server.

Exposes the Family Brain knowledge base to any MCP-compatible client
(Claude Desktop, Cursor, VS Code, Manus, ChatGPT, custom agents) via tools:

  Knowledge Base:
  * semantic_search      – cosine-similarity search over memories.
  * list_recent_memories – fetch the N most recent entries.
  * query_by_metadata    – filter by tags, people, or category.
  * thought_stats        – aggregate statistics about the knowledge base.
  * capture_thought      – write a new memory from any MCP client.

  Household Knowledge:
  * add_household_item     – record a household item.
  * search_household_items – search household items by name.
  * add_household_vendor   – record a trusted vendor/tradesperson.
  * search_household_vendors – search vendors by name or trade.

  Family Scheduling:
  * add_family_event         – add a family event with conflict detection.
  * check_family_schedule    – list events in a date range.

Transport modes:
  stdio  – spawned by the MCP client (Claude Desktop, Cursor). Default.
  http   – Streamable HTTP on <host>:<port>/mcp. Recommended for remote access.
  sse    – Legacy SSE transport. Kept for backward compatibility.

Authentication (HTTP / SSE transports):
  Two mechanisms are supported in parallel:

  1. **Static bearer token** (MCP_AUTH_TOKEN) – for Manus, Cursor, Claude Desktop.
     Clients send: Authorization: Bearer <MCP_AUTH_TOKEN>

  2. **OAuth 2.0 authorization code flow with PKCE** – for ChatGPT and other
     MCP clients that speak OAuth.  Discovery endpoints:
       /.well-known/oauth-protected-resource
       /.well-known/oauth-authorization-server
     Authorization: /authorize  (GET = consent form, POST = submit)
     Token exchange: /token     (POST)

  A request is authorized if it carries *either* a valid static token or a
  valid OAuth access token.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .config import get_settings, logger as root_logger
from . import brain
from . import oauth as oauth_module
from . import scheduling_brain

logger = logging.getLogger("open_brain.mcp")

# ---------------------------------------------------------------------------
# Initialise settings and core modules
# ---------------------------------------------------------------------------
settings = get_settings()
brain.init(settings)

# Initialise scheduling brain (graceful — tables may not exist yet)
try:
    scheduling_brain.init()
    _scheduling_available = True
    logger.info("Scheduling brain initialised successfully.")
except Exception as exc:
    _scheduling_available = False
    logger.warning("Scheduling brain not available (tables may not exist): %s", exc)

# ---------------------------------------------------------------------------
# Create the FastMCP server instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "open-brain",
    instructions=(
        "Family Brain is a shared family knowledge base with semantic search. "
        "Use semantic_search to find memories related to a topic, "
        "list_recent_memories for recent context, "
        "query_by_metadata to filter by tags, people, or category, "
        "thought_stats to see an overview of the knowledge base, "
        "capture_thought to store a new memory, "
        "add_household_item / search_household_items for household knowledge, "
        "add_household_vendor / search_household_vendors for trusted tradespeople, "
        "add_family_event to schedule events with conflict detection, and "
        "check_family_schedule to view the family calendar."
    ),
)


# ---------------------------------------------------------------------------
# Combined Auth Middleware (static bearer token + OAuth access tokens)
# ---------------------------------------------------------------------------

# Paths that never require authentication
_PUBLIC_PATHS = frozenset({
    "/health",
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server",
    "/authorize",
    "/token",
})


class CombinedAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate requests using either a static bearer token or an OAuth
    access token.  Public paths (health, OAuth discovery/endpoints) are
    always allowed through.
    """

    def __init__(self, app, *, static_token: str, oauth_enabled: bool) -> None:
        super().__init__(app)
        self._static_token = static_token
        self._oauth_enabled = oauth_enabled
        self._auth_required = bool(static_token) or oauth_enabled

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path

        # Always allow public paths
        if path in _PUBLIC_PATHS:
            return await call_next(request)

        # If no auth mechanism is configured, allow everything
        if not self._auth_required:
            return await call_next(request)

        # Extract bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return self._unauthorized(request, "Missing or malformed Authorization header")

        token = auth_header.removeprefix("Bearer ").strip()

        # Check 1: static bearer token
        if self._static_token and token == self._static_token:
            return await call_next(request)

        # Check 2: OAuth access token
        if self._oauth_enabled and oauth_module.is_valid_oauth_token(token):
            return await call_next(request)

        logger.warning(
            "Rejected request from %s – invalid token",
            request.client.host if request.client else "unknown",
        )
        return self._unauthorized(request, "Invalid bearer token")

    def _unauthorized(self, request: Request, detail: str) -> JSONResponse:
        base_url = oauth_module.get_server_base_url(request, settings)
        headers = {
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{base_url}/.well-known/oauth-protected-resource"'
            ),
        }
        return JSONResponse(
            {"error": "unauthorized", "error_description": detail},
            status_code=401,
            headers=headers,
        )


# ===================================================================
# KNOWLEDGE BASE TOOLS (existing)
# ===================================================================

# ---------------------------------------------------------------------------
# Tool: semantic_search
# ---------------------------------------------------------------------------
@mcp.tool()
async def semantic_search(
    query: str,
    match_threshold: float = 0.5,
    match_count: int = 10,
) -> str:
    """Search the Family Brain knowledge base by meaning.

    Generates a vector embedding for *query* and performs a cosine-similarity
    search against all stored memories.

    Args:
        query: The search query string.
        match_threshold: Minimum cosine similarity score (0-1). Default 0.5.
        match_count: Maximum number of results to return. Default 10.
    """
    logger.info("semantic_search called: query=%r", query)
    try:
        results = brain.semantic_search(
            query=query,
            match_threshold=match_threshold,
            match_count=match_count,
        )
        if not results:
            return "No memories found matching that query."
        return _format_results(results, include_similarity=True)
    except Exception as exc:
        logger.error("semantic_search failed: %s", exc)
        return f"Error performing semantic search: {exc}"


# ---------------------------------------------------------------------------
# Tool: list_recent_memories
# ---------------------------------------------------------------------------
@mcp.tool()
async def list_recent_memories(limit: int = 20) -> str:
    """List the most recent memories stored in Family Brain.

    Args:
        limit: Number of memories to return (1-100). Default 20.
    """
    logger.info("list_recent_memories called: limit=%s", limit)
    try:
        clamped_limit = max(1, min(limit, 100))
        results = brain.list_recent_memories(limit=clamped_limit)
        if not results:
            return "No memories found in the knowledge base yet."
        return _format_results(results, include_similarity=False)
    except Exception as exc:
        logger.error("list_recent_memories failed: %s", exc)
        return f"Error listing recent memories: {exc}"


# ---------------------------------------------------------------------------
# Tool: query_by_metadata
# ---------------------------------------------------------------------------
@mcp.tool()
async def query_by_metadata(
    tags: Optional[list[str]] = None,
    people: Optional[list[str]] = None,
    category: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Filter memories by metadata fields.

    At least one filter must be provided. All supplied filters are combined
    with AND logic (JSONB containment).

    Args:
        tags: List of tags to filter by (e.g. ["ai", "research"]).
        people: List of people to filter by (e.g. ["Alice", "Bob"]).
        category: Category to filter by (e.g. "meeting-notes", "idea").
        limit: Maximum number of results to return (1-100). Default 20.
    """
    logger.info("query_by_metadata called")
    try:
        if not tags and not people and not category:
            return "Please provide at least one filter: tags, people, or category."
        clamped_limit = max(1, min(limit, 100))
        results = brain.query_by_metadata(
            tags=tags,
            people=people,
            category=category,
            limit=clamped_limit,
        )
        if not results:
            return "No memories found matching those filters."
        return _format_results(results, include_similarity=False)
    except Exception as exc:
        logger.error("query_by_metadata failed: %s", exc)
        return f"Error querying by metadata: {exc}"


# ---------------------------------------------------------------------------
# Tool: thought_stats
# ---------------------------------------------------------------------------
@mcp.tool()
async def thought_stats() -> str:
    """Return aggregate statistics about the Family Brain knowledge base.

    Provides:
    - Total number of stored memories.
    - Date range (oldest and newest memory).
    - Top 10 tags by frequency.
    - Top 5 categories by frequency.
    - Top 10 people mentioned by frequency.
    """
    logger.info("thought_stats called")
    try:
        stats = brain.get_stats()

        if stats["total"] == 0:
            return "The knowledge base is empty. No memories have been captured yet."

        lines = [
            f"Family Brain Knowledge Base Statistics",
            f"=====================================",
            f"Total memories: {stats['total']}",
            f"Oldest memory:  {stats.get('oldest', 'n/a')}",
            f"Newest memory:  {stats.get('newest', 'n/a')}",
        ]

        if stats.get("top_tags"):
            lines.append("\nTop Tags:")
            for entry in stats["top_tags"]:
                lines.append(f"  {entry['tag']}: {entry['count']}")

        if stats.get("top_categories"):
            lines.append("\nTop Categories:")
            for entry in stats["top_categories"]:
                lines.append(f"  {entry['category']}: {entry['count']}")

        if stats.get("top_people"):
            lines.append("\nTop People Mentioned:")
            for entry in stats["top_people"]:
                lines.append(f"  {entry['person']}: {entry['count']}")

        return "\n".join(lines)

    except Exception as exc:
        logger.error("thought_stats failed: %s", exc)
        return f"Error retrieving knowledge base statistics: {exc}"


# ---------------------------------------------------------------------------
# Tool: capture_thought
# ---------------------------------------------------------------------------
@mcp.tool()
async def capture_thought(
    text: str,
    source: str = "mcp",
) -> str:
    """Capture a new memory into the Family Brain knowledge base.

    This tool allows AI clients to write memories directly - not just read them.
    The text is processed through the LLM for cleaning and metadata extraction,
    then embedded and stored in Supabase.

    Args:
        text: The raw thought, note, or idea to capture.
        source: Where this thought originated (default: "mcp").
    """
    logger.info("capture_thought called: %d chars, source=%r", len(text), source)
    try:
        if not text.strip():
            return "Error: text cannot be empty."

        # Step 1: Extract metadata via LLM
        metadata = brain.extract_metadata(text)
        metadata["source"] = source
        cleaned_content: str = metadata.pop("cleaned_content", text)

        # Step 2: Generate embedding
        embedding = brain.generate_embedding(cleaned_content)

        # Step 3: Store in Supabase
        record = brain.store_memory(
            content=cleaned_content,
            embedding=embedding,
            metadata=metadata,
        )

        memory_id = record.get("id", "n/a")
        tags = metadata.get("tags", [])
        category = metadata.get("category", "other")
        action_items: list[str] = metadata.get("action_items", [])

        lines = [
            "Memory captured successfully!",
            f"ID:           {memory_id}",
            f"Category:     {category}",
            f"Tags:         {', '.join(tags) if tags else 'none'}",
            f"Action items: {'; '.join(action_items) if action_items else 'none'}",
            f"Content:      {cleaned_content[:200]}{'...' if len(cleaned_content) > 200 else ''}",
        ]
        return "\n".join(lines)

    except Exception as exc:
        logger.error("capture_thought failed: %s", exc)
        return f"Error capturing thought: {exc}"


# ===================================================================
# HOUSEHOLD KNOWLEDGE TOOLS (new)
# ===================================================================

# ---------------------------------------------------------------------------
# Tool: add_household_item
# ---------------------------------------------------------------------------
@mcp.tool()
async def add_household_item(
    name: str,
    category: str = "other",
    location: str = "",
    notes: str = "",
    details: Optional[dict[str, Any]] = None,
) -> str:
    """Record a household item in the Family Brain.

    Use this for appliances, furniture, systems, warranties, or any physical
    item in the household that the family wants to track.

    Args:
        name: Name of the item (e.g. "Bosch dishwasher", "Ring doorbell").
        category: Category (e.g. "appliance", "furniture", "electronics", "system").
        location: Where in the house (e.g. "kitchen", "garage", "master bedroom").
        notes: Free-text notes (e.g. "warranty expires 2027", "model XYZ-123").
        details: Optional structured details as key-value pairs.
    """
    logger.info("add_household_item called: name=%r", name)
    try:
        record = scheduling_brain.add_household_item(
            user_id="family",
            name=name,
            category=category,
            location=location,
            details=details or {},
            notes=notes,
        )
        return (
            f"Household item recorded!\n"
            f"ID:       {record.get('id', 'n/a')}\n"
            f"Name:     {name}\n"
            f"Category: {category}\n"
            f"Location: {location or 'not specified'}\n"
            f"Notes:    {notes or 'none'}"
        )
    except Exception as exc:
        logger.error("add_household_item failed: %s", exc)
        return f"Error adding household item: {exc}"


# ---------------------------------------------------------------------------
# Tool: search_household_items
# ---------------------------------------------------------------------------
@mcp.tool()
async def search_household_items(
    search_term: str = "",
    category: Optional[str] = None,
    location: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Search household items by name, category, or location.

    Args:
        search_term: Text to search for in item names (partial match).
        category: Filter by category (e.g. "appliance", "furniture").
        location: Filter by location (e.g. "kitchen", "garage").
        limit: Maximum number of results (1-50). Default 20.
    """
    logger.info("search_household_items called: term=%r", search_term)
    try:
        clamped = max(1, min(limit, 50))
        if search_term:
            items = scheduling_brain.search_household_items(search_term, limit=clamped)
        else:
            items = scheduling_brain.get_household_items(
                category=category, location=location, limit=clamped,
            )

        if not items:
            return "No household items found matching your search."

        lines = [f"Found {len(items)} household item(s):\n"]
        for i, item in enumerate(items, 1):
            lines.append(
                f"{i}. {item.get('name', 'n/a')} "
                f"[{item.get('category', 'other')}] "
                f"@ {item.get('location', 'unspecified')}"
            )
            if item.get("notes"):
                lines.append(f"   Notes: {item['notes']}")
            lines.append(f"   Added: {item.get('created_at', 'n/a')}")
        return "\n".join(lines)

    except Exception as exc:
        logger.error("search_household_items failed: %s", exc)
        return f"Error searching household items: {exc}"


# ---------------------------------------------------------------------------
# Tool: add_household_vendor
# ---------------------------------------------------------------------------
@mcp.tool()
async def add_household_vendor(
    name: str,
    trade: str = "other",
    phone: str = "",
    vendor_email: str = "",
    rating: Optional[int] = None,
    notes: str = "",
) -> str:
    """Record a trusted household vendor or tradesperson.

    Use this for plumbers, electricians, cleaners, gardeners, or any service
    provider the family wants to remember.

    Args:
        name: Vendor/person name (e.g. "Mike's Plumbing", "Emma the cleaner").
        trade: Type of trade (e.g. "plumber", "electrician", "cleaner", "gardener").
        phone: Phone number.
        vendor_email: Email address.
        rating: Rating out of 5 (1-5).
        notes: Free-text notes (e.g. "Recommended by neighbour, very reliable").
    """
    logger.info("add_household_vendor called: name=%r, trade=%r", name, trade)
    try:
        record = scheduling_brain.add_household_vendor(
            user_id="family",
            name=name,
            trade=trade,
            phone=phone,
            vendor_email=vendor_email,
            rating=rating,
            notes=notes,
        )
        return (
            f"Vendor recorded!\n"
            f"ID:    {record.get('id', 'n/a')}\n"
            f"Name:  {name}\n"
            f"Trade: {trade}\n"
            f"Phone: {phone or 'not provided'}\n"
            f"Email: {vendor_email or 'not provided'}\n"
            f"Rating: {rating}/5" if rating else f"Rating: not rated"
        )
    except Exception as exc:
        logger.error("add_household_vendor failed: %s", exc)
        return f"Error adding household vendor: {exc}"


# ---------------------------------------------------------------------------
# Tool: search_household_vendors
# ---------------------------------------------------------------------------
@mcp.tool()
async def search_household_vendors(
    search_term: str = "",
    trade: Optional[str] = None,
    limit: int = 20,
) -> str:
    """Search household vendors by name or trade.

    Args:
        search_term: Text to search for in vendor names (partial match).
        trade: Filter by trade type (e.g. "plumber", "electrician").
        limit: Maximum number of results (1-50). Default 20.
    """
    logger.info("search_household_vendors called: term=%r, trade=%r", search_term, trade)
    try:
        clamped = max(1, min(limit, 50))
        if search_term:
            vendors = scheduling_brain.search_household_vendors(search_term, limit=clamped)
        else:
            vendors = scheduling_brain.get_household_vendors(trade=trade, limit=clamped)

        if not vendors:
            return "No vendors found matching your search."

        lines = [f"Found {len(vendors)} vendor(s):\n"]
        for i, v in enumerate(vendors, 1):
            rating_str = f" ({v['rating']}/5)" if v.get("rating") else ""
            lines.append(f"{i}. {v.get('name', 'n/a')} [{v.get('trade', 'other')}]{rating_str}")
            if v.get("phone"):
                lines.append(f"   Phone: {v['phone']}")
            if v.get("email"):
                lines.append(f"   Email: {v['email']}")
            if v.get("notes"):
                lines.append(f"   Notes: {v['notes']}")
        return "\n".join(lines)

    except Exception as exc:
        logger.error("search_household_vendors failed: %s", exc)
        return f"Error searching vendors: {exc}"


# ===================================================================
# FAMILY SCHEDULING TOOLS (new)
# ===================================================================

# ---------------------------------------------------------------------------
# Tool: add_family_event
# ---------------------------------------------------------------------------
@mcp.tool()
async def add_family_event(
    family_member: str,
    event_name: str,
    event_date: str,
    event_time: Optional[str] = None,
    end_date: Optional[str] = None,
    location: str = "",
    notes: str = "",
    recurring: bool = False,
    recurrence_pattern: str = "",
) -> str:
    """Add a family event to the calendar with automatic conflict detection.

    If there are existing events on the same date for the same family member,
    a conflict warning will be included in the response.

    Args:
        family_member: Who the event is for (e.g. "Dan", "Emma", "family").
        event_name: Name of the event (e.g. "Dentist appointment", "School play").
        event_date: Date in YYYY-MM-DD format.
        event_time: Optional time in HH:MM format (24-hour).
        end_date: Optional end date for multi-day events (YYYY-MM-DD).
        location: Where the event takes place.
        notes: Additional notes or requirements.
        recurring: Whether this is a recurring event.
        recurrence_pattern: If recurring, the pattern (e.g. "weekly", "monthly", "every Tuesday").
    """
    logger.info("add_family_event called: %s for %s on %s", event_name, family_member, event_date)
    try:
        # Check for conflicts first
        conflicts = scheduling_brain.check_conflicts(event_date, family_member)

        # Add the event
        record = scheduling_brain.add_event(
            family_member=family_member,
            event_name=event_name,
            event_date=event_date,
            event_time=event_time,
            end_date=end_date,
            location=location,
            recurring=recurring,
            recurrence_pattern=recurrence_pattern,
            notes=notes,
            source="mcp",
        )

        lines = [
            "Family event added!",
            f"ID:     {record.get('id', 'n/a')}",
            f"Who:    {family_member}",
            f"What:   {event_name}",
            f"When:   {event_date}" + (f" at {event_time}" if event_time else ""),
            f"Where:  {location or 'not specified'}",
        ]

        if conflicts:
            lines.append("")
            lines.append(f"⚠️ SCHEDULE CONFLICT — {len(conflicts)} existing event(s) on {event_date}:")
            for c in conflicts:
                time_str = f" at {c['event_time']}" if c.get("event_time") else ""
                lines.append(
                    f"  • {c.get('event_name', 'n/a')} for {c.get('family_member', 'n/a')}{time_str}"
                )

        return "\n".join(lines)

    except Exception as exc:
        logger.error("add_family_event failed: %s", exc)
        return f"Error adding family event: {exc}"


# ---------------------------------------------------------------------------
# Tool: check_family_schedule
# ---------------------------------------------------------------------------
@mcp.tool()
async def check_family_schedule(
    date_range_start: str,
    date_range_end: str,
    family_member: Optional[str] = None,
) -> str:
    """Check the family schedule for events in a date range.

    Args:
        date_range_start: Start date in YYYY-MM-DD format.
        date_range_end: End date in YYYY-MM-DD format.
        family_member: Optional — filter to a specific family member.
    """
    logger.info(
        "check_family_schedule called: %s to %s, member=%s",
        date_range_start, date_range_end, family_member,
    )
    try:
        events = scheduling_brain.get_events_in_range(
            start_date=date_range_start,
            end_date=date_range_end,
            family_member=family_member,
        )

        if not events:
            member_str = f" for {family_member}" if family_member else ""
            return f"No events found{member_str} between {date_range_start} and {date_range_end}."

        lines = [f"Found {len(events)} event(s):\n"]
        for i, ev in enumerate(events, 1):
            time_str = f" at {ev['event_time']}" if ev.get("event_time") else ""
            loc_str = f" @ {ev['location']}" if ev.get("location") else ""
            lines.append(
                f"{i}. [{ev.get('event_date', 'n/a')}{time_str}] "
                f"{ev.get('event_name', 'n/a')} — {ev.get('family_member', 'n/a')}"
                f"{loc_str}"
            )
            if ev.get("notes"):
                lines.append(f"   Notes: {ev['notes']}")
            if ev.get("recurring"):
                lines.append(f"   Recurring: {ev.get('recurrence_pattern', 'yes')}")
        return "\n".join(lines)

    except Exception as exc:
        logger.error("check_family_schedule failed: %s", exc)
        return f"Error checking family schedule: {exc}"


# ===================================================================
# EXTENSION 2: HOME MAINTENANCE TOOLS
# ===================================================================

@mcp.tool()
async def add_maintenance_task(
    title: str,
    category: str = "other",
    location: str = "",
    frequency_days: Optional[int] = None,
    next_due: str = "",
    notes: str = "",
) -> str:
    """Add a recurring or one-off home maintenance task.

    Args:
        title: Task name (e.g. "Service boiler", "Clean gutters").
        category: One of hvac, plumbing, electrical, garden, appliance, other.
        location: Where in the house (e.g. "kitchen", "roof").
        frequency_days: Recurrence in days (e.g. 365 for annual). Null for one-off.
        next_due: Next due date in YYYY-MM-DD format.
        notes: Additional notes.
    """
    logger.info("add_maintenance_task called: %s", title)
    try:
        record = brain.add_maintenance_task(
            title=title, category=category, location=location,
            frequency_days=frequency_days, next_due=next_due, notes=notes,
        )
        return (
            f"Maintenance task created!\n"
            f"ID:        {record.get('id', 'n/a')}\n"
            f"Title:     {title}\n"
            f"Category:  {category}\n"
            f"Location:  {location or 'not specified'}\n"
            f"Frequency: {f'{frequency_days} days' if frequency_days else 'one-off'}\n"
            f"Next due:  {next_due or 'not set'}"
        )
    except Exception as exc:
        logger.error("add_maintenance_task failed: %s", exc)
        return f"Error adding maintenance task: {exc}"


@mcp.tool()
async def log_maintenance(
    task_id: str,
    completed_date: str = "",
    performed_by: str = "",
    cost: float = 0.0,
    notes: str = "",
) -> str:
    """Log completion of a maintenance task. Auto-updates next_due if recurring.

    Args:
        task_id: UUID of the maintenance task.
        completed_date: Date completed (YYYY-MM-DD). Defaults to today.
        performed_by: Who did the work (e.g. "Dan", "Plumber").
        cost: Cost in GBP.
        notes: Notes about the work done.
    """
    logger.info("log_maintenance called: task=%s", task_id)
    try:
        record = brain.log_maintenance(
            task_id=task_id, completed_date=completed_date,
            performed_by=performed_by, cost_gbp=cost, notes=notes,
        )
        return (
            f"Maintenance logged!\n"
            f"Log ID:    {record.get('id', 'n/a')}\n"
            f"Task:      {task_id}\n"
            f"Date:      {completed_date or 'today'}\n"
            f"By:        {performed_by or 'not specified'}\n"
            f"Cost:      \u00a3{cost:.2f}"
        )
    except Exception as exc:
        logger.error("log_maintenance failed: %s", exc)
        return f"Error logging maintenance: {exc}"


@mcp.tool()
async def get_upcoming_maintenance(days_ahead: int = 30) -> str:
    """Get maintenance tasks due within the next N days.

    Args:
        days_ahead: Number of days to look ahead. Default 30.
    """
    logger.info("get_upcoming_maintenance called: days=%d", days_ahead)
    try:
        tasks = brain.get_upcoming_maintenance(days_ahead=days_ahead)
        if not tasks:
            return f"No maintenance tasks due in the next {days_ahead} days."
        lines = [f"Upcoming maintenance ({len(tasks)} tasks):\n"]
        for i, t in enumerate(tasks, 1):
            freq = f" (every {t['frequency_days']}d)" if t.get('frequency_days') else ""
            lines.append(
                f"{i}. {t.get('title', 'n/a')} [{t.get('category', 'other')}]\n"
                f"   Due: {t.get('next_due', 'n/a')}{freq}\n"
                f"   Location: {t.get('location', 'n/a')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_upcoming_maintenance failed: %s", exc)
        return f"Error getting upcoming maintenance: {exc}"


@mcp.tool()
async def search_maintenance_history(
    query: str = "",
    task_id: str = "",
    date_from: str = "",
    date_to: str = "",
) -> str:
    """Search maintenance completion history.

    Args:
        query: Text search in notes.
        task_id: Filter by specific task UUID.
        date_from: Start date (YYYY-MM-DD).
        date_to: End date (YYYY-MM-DD).
    """
    logger.info("search_maintenance_history called")
    try:
        logs = brain.search_maintenance_history(
            query=query, task_id=task_id, date_from=date_from, date_to=date_to,
        )
        if not logs:
            return "No maintenance history found."
        lines = [f"Found {len(logs)} maintenance log(s):\n"]
        for i, log in enumerate(logs, 1):
            task_info = log.get('maintenance_tasks', {})
            lines.append(
                f"{i}. {task_info.get('title', 'n/a')} — {log.get('completed_date', 'n/a')}\n"
                f"   By: {log.get('performed_by', 'n/a')} | Cost: \u00a3{log.get('cost_gbp', 0):.2f}\n"
                f"   Notes: {log.get('notes', 'none')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("search_maintenance_history failed: %s", exc)
        return f"Error searching maintenance history: {exc}"


# ===================================================================
# EXTENSION 3: VEHICLE MANAGEMENT TOOLS
# ===================================================================

@mcp.tool()
async def add_vehicle(
    nickname: str,
    make: str = "",
    model: str = "",
    year: Optional[int] = None,
    registration: str = "",
    colour: str = "",
    mot_due: str = "",
    insurance_due: str = "",
    tax_due: str = "",
    mileage: Optional[int] = None,
    notes: str = "",
) -> str:
    """Add a family vehicle to track.

    Args:
        nickname: Short name (e.g. "Dan's Golf", "Emma's Fiat").
        make: Manufacturer (e.g. "Volkswagen").
        model: Model name (e.g. "Golf").
        year: Model year.
        registration: Registration/license plate.
        colour: Vehicle colour.
        mot_due: MOT due date (YYYY-MM-DD).
        insurance_due: Insurance renewal date (YYYY-MM-DD).
        tax_due: Road tax due date (YYYY-MM-DD).
        mileage: Current mileage.
        notes: Additional notes.
    """
    logger.info("add_vehicle called: %s", nickname)
    try:
        record = brain.add_vehicle(
            nickname=nickname, make=make, model=model, year=year,
            registration=registration, colour=colour, mot_due=mot_due,
            insurance_due=insurance_due, tax_due=tax_due, mileage=mileage, notes=notes,
        )
        return (
            f"Vehicle added!\n"
            f"ID:           {record.get('id', 'n/a')}\n"
            f"Nickname:     {nickname}\n"
            f"Vehicle:      {year or ''} {make} {model}\n"
            f"Registration: {registration or 'n/a'}\n"
            f"MOT due:      {mot_due or 'not set'}"
        )
    except Exception as exc:
        logger.error("add_vehicle failed: %s", exc)
        return f"Error adding vehicle: {exc}"


@mcp.tool()
async def list_vehicles() -> str:
    """List all family vehicles."""
    logger.info("list_vehicles called")
    try:
        vehicles = brain.list_vehicles()
        if not vehicles:
            return "No vehicles registered."
        lines = [f"Family vehicles ({len(vehicles)}):\n"]
        for i, v in enumerate(vehicles, 1):
            lines.append(
                f"{i}. {v.get('nickname', 'n/a')} — "
                f"{v.get('year', '')} {v.get('make', '')} {v.get('model', '')}\n"
                f"   Reg: {v.get('registration', 'n/a')} | "
                f"Mileage: {v.get('mileage', 'n/a')} | "
                f"MOT: {v.get('mot_due', 'n/a')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("list_vehicles failed: %s", exc)
        return f"Error listing vehicles: {exc}"


@mcp.tool()
async def log_vehicle_service(
    vehicle_id: str,
    service_type: str = "other",
    description: str = "",
    service_date: str = "",
    mileage_at: Optional[int] = None,
    cost: float = 0.0,
    garage: str = "",
    notes: str = "",
) -> str:
    """Log a vehicle service, repair, or MOT.

    Args:
        vehicle_id: UUID of the vehicle.
        service_type: One of mot, service, repair, tyre, other.
        description: What was done.
        service_date: Date of service (YYYY-MM-DD).
        mileage_at: Mileage at time of service.
        cost: Cost in GBP.
        garage: Name of the garage/mechanic.
        notes: Additional notes.
    """
    logger.info("log_vehicle_service called: vehicle=%s", vehicle_id)
    try:
        record = brain.log_vehicle_service(
            vehicle_id=vehicle_id, service_type=service_type,
            description=description, service_date=service_date,
            mileage_at=mileage_at, cost_gbp=cost, garage=garage, notes=notes,
        )
        return (
            f"Vehicle service logged!\n"
            f"Log ID:  {record.get('id', 'n/a')}\n"
            f"Type:    {service_type}\n"
            f"Date:    {service_date or 'today'}\n"
            f"Cost:    \u00a3{cost:.2f}\n"
            f"Garage:  {garage or 'n/a'}"
        )
    except Exception as exc:
        logger.error("log_vehicle_service failed: %s", exc)
        return f"Error logging vehicle service: {exc}"


@mcp.tool()
async def get_vehicle_reminders(days_ahead: int = 30) -> str:
    """Get vehicle reminders (MOT, insurance, tax) due within N days.

    Args:
        days_ahead: Number of days to look ahead. Default 30.
    """
    logger.info("get_vehicle_reminders called: days=%d", days_ahead)
    try:
        reminders = brain.get_vehicle_reminders(days_ahead=days_ahead)
        if not reminders:
            return f"No vehicle reminders in the next {days_ahead} days."
        lines = [f"Vehicle reminders ({len(reminders)} vehicle(s)):\n"]
        for v in reminders:
            lines.append(f"\u2022 {v.get('nickname', 'n/a')} ({v.get('registration', 'n/a')})")
            for alert in v.get('_alerts', []):
                lines.append(f"  \u26a0\ufe0f {alert}")
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_vehicle_reminders failed: %s", exc)
        return f"Error getting vehicle reminders: {exc}"


@mcp.tool()
async def get_vehicle_history(vehicle_id: str, limit: int = 50) -> str:
    """Get service history for a specific vehicle.

    Args:
        vehicle_id: UUID of the vehicle.
        limit: Maximum number of records. Default 50.
    """
    logger.info("get_vehicle_history called: vehicle=%s", vehicle_id)
    try:
        logs = brain.get_vehicle_history(vehicle_id=vehicle_id, limit=limit)
        if not logs:
            return "No service history found for this vehicle."
        lines = [f"Service history ({len(logs)} records):\n"]
        for i, log in enumerate(logs, 1):
            lines.append(
                f"{i}. [{log.get('service_date', 'n/a')}] {log.get('service_type', 'other')}\n"
                f"   {log.get('description', 'n/a')} | \u00a3{log.get('cost_gbp', 0):.2f}\n"
                f"   Mileage: {log.get('mileage_at', 'n/a')} | Garage: {log.get('garage', 'n/a')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_vehicle_history failed: %s", exc)
        return f"Error getting vehicle history: {exc}"


# ===================================================================
# EXTENSION 4: HEALTH & WELLNESS TOOLS
# ===================================================================

@mcp.tool()
async def log_health_metric(
    family_member: str,
    metric_type: str,
    value: float,
    unit: str = "",
    secondary_value: Optional[float] = None,
    notes: str = "",
) -> str:
    """Log a health metric reading for a family member.

    Args:
        family_member: Who (e.g. "Dan", "Emma").
        metric_type: Type of metric (e.g. "weight", "blood_pressure", "steps", "sleep_hours").
        value: Primary value (e.g. 82.5 for weight, 120 for systolic BP).
        unit: Unit of measurement (e.g. "kg", "mmHg", "steps").
        secondary_value: Secondary value (e.g. 80 for diastolic BP).
        notes: Additional notes.
    """
    logger.info("log_health_metric called: %s %s=%s", family_member, metric_type, value)
    try:
        record = brain.log_health_metric(
            family_member=family_member, metric_type=metric_type,
            value=value, unit=unit, secondary_value=secondary_value, notes=notes,
        )
        sec_str = f"/{secondary_value}" if secondary_value is not None else ""
        return (
            f"Health metric logged!\n"
            f"ID:     {record.get('id', 'n/a')}\n"
            f"Who:    {family_member}\n"
            f"Metric: {metric_type}\n"
            f"Value:  {value}{sec_str} {unit}"
        )
    except Exception as exc:
        logger.error("log_health_metric failed: %s", exc)
        return f"Error logging health metric: {exc}"


@mcp.tool()
async def get_health_metrics(
    family_member: str = "",
    metric_type: str = "",
    days_back: int = 30,
) -> str:
    """Retrieve health metrics with optional filters.

    Args:
        family_member: Filter by family member.
        metric_type: Filter by metric type.
        days_back: Number of days to look back. Default 30.
    """
    logger.info("get_health_metrics called")
    try:
        metrics = brain.get_health_metrics(
            family_member=family_member, metric_type=metric_type, days_back=days_back,
        )
        if not metrics:
            return "No health metrics found."
        lines = [f"Health metrics ({len(metrics)} readings):\n"]
        for i, m in enumerate(metrics, 1):
            sec = f"/{m['secondary_value']}" if m.get('secondary_value') else ""
            lines.append(
                f"{i}. [{m.get('recorded_at', 'n/a')[:10]}] "
                f"{m.get('family_member', 'n/a')}: {m.get('metric_type', 'n/a')} = "
                f"{m.get('value', 'n/a')}{sec} {m.get('unit', '')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_health_metrics failed: %s", exc)
        return f"Error getting health metrics: {exc}"


@mcp.tool()
async def add_medication(
    family_member: str,
    name: str,
    dosage: str = "",
    frequency: str = "",
    prescriber: str = "",
    pharmacy: str = "",
    start_date: str = "",
    refill_due: str = "",
    notes: str = "",
) -> str:
    """Add a medication record for a family member.

    Args:
        family_member: Who takes this medication.
        name: Medication name.
        dosage: Dosage (e.g. "10mg", "2 tablets").
        frequency: How often (e.g. "twice daily", "as needed").
        prescriber: Prescribing doctor.
        pharmacy: Pharmacy name.
        start_date: When started (YYYY-MM-DD).
        refill_due: Next refill date (YYYY-MM-DD).
        notes: Additional notes or side effects.
    """
    logger.info("add_medication called: %s for %s", name, family_member)
    try:
        record = brain.add_medication(
            family_member=family_member, name=name, dosage=dosage,
            frequency=frequency, prescriber=prescriber, pharmacy=pharmacy,
            start_date=start_date, refill_due=refill_due, notes=notes,
        )
        return (
            f"Medication recorded!\n"
            f"ID:        {record.get('id', 'n/a')}\n"
            f"Who:       {family_member}\n"
            f"Name:      {name}\n"
            f"Dosage:    {dosage or 'n/a'}\n"
            f"Frequency: {frequency or 'n/a'}\n"
            f"Refill:    {refill_due or 'not set'}"
        )
    except Exception as exc:
        logger.error("add_medication failed: %s", exc)
        return f"Error adding medication: {exc}"


@mcp.tool()
async def get_active_medications(family_member: str = "") -> str:
    """Get all active medications, optionally for a specific family member.

    Args:
        family_member: Filter by family member. Leave empty for all.
    """
    logger.info("get_active_medications called")
    try:
        meds = brain.get_active_medications(family_member=family_member)
        if not meds:
            return "No active medications found."
        lines = [f"Active medications ({len(meds)}):\n"]
        for i, m in enumerate(meds, 1):
            lines.append(
                f"{i}. {m.get('name', 'n/a')} — {m.get('family_member', 'n/a')}\n"
                f"   Dosage: {m.get('dosage', 'n/a')} | Frequency: {m.get('frequency', 'n/a')}\n"
                f"   Refill due: {m.get('refill_due', 'n/a')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_active_medications failed: %s", exc)
        return f"Error getting medications: {exc}"


@mcp.tool()
async def add_medical_appointment(
    family_member: str,
    appointment_type: str = "general",
    provider: str = "",
    location: str = "",
    appointment_date: str = "",
    appointment_time: str = "",
    notes: str = "",
) -> str:
    """Add a medical appointment for a family member.

    Args:
        family_member: Who the appointment is for.
        appointment_type: Type (e.g. "gp", "dentist", "specialist", "optician").
        provider: Doctor/clinic name.
        location: Address or clinic.
        appointment_date: Date (YYYY-MM-DD).
        appointment_time: Time (HH:MM).
        notes: Reason or preparation notes.
    """
    logger.info("add_medical_appointment called: %s for %s", appointment_type, family_member)
    try:
        record = brain.add_medical_appointment(
            family_member=family_member, appointment_type=appointment_type,
            provider=provider, location=location,
            appointment_date=appointment_date, appointment_time=appointment_time,
            notes=notes,
        )
        return (
            f"Medical appointment added!\n"
            f"ID:     {record.get('id', 'n/a')}\n"
            f"Who:    {family_member}\n"
            f"Type:   {appointment_type}\n"
            f"Date:   {appointment_date or 'not set'} {appointment_time or ''}\n"
            f"Where:  {location or 'not specified'}"
        )
    except Exception as exc:
        logger.error("add_medical_appointment failed: %s", exc)
        return f"Error adding medical appointment: {exc}"


@mcp.tool()
async def get_upcoming_appointments(
    days_ahead: int = 30,
    family_member: str = "",
) -> str:
    """Get upcoming medical appointments.

    Args:
        days_ahead: Number of days to look ahead. Default 30.
        family_member: Filter by family member. Leave empty for all.
    """
    logger.info("get_upcoming_appointments called")
    try:
        appts = brain.get_upcoming_appointments(
            days_ahead=days_ahead, family_member=family_member,
        )
        if not appts:
            return f"No medical appointments in the next {days_ahead} days."
        lines = [f"Upcoming appointments ({len(appts)}):\n"]
        for i, a in enumerate(appts, 1):
            lines.append(
                f"{i}. [{a.get('appointment_date', 'n/a')} {a.get('appointment_time', '')}] "
                f"{a.get('appointment_type', 'general')} — {a.get('family_member', 'n/a')}\n"
                f"   Provider: {a.get('provider', 'n/a')} @ {a.get('location', 'n/a')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_upcoming_appointments failed: %s", exc)
        return f"Error getting upcoming appointments: {exc}"


@mcp.tool()
async def get_medication_refills_due(days_ahead: int = 14) -> str:
    """Get medications with refills due within N days.

    Args:
        days_ahead: Number of days to look ahead. Default 14.
    """
    logger.info("get_medication_refills_due called")
    try:
        meds = brain.get_medication_refills_due(days_ahead=days_ahead)
        if not meds:
            return f"No medication refills due in the next {days_ahead} days."
        lines = [f"Medication refills due ({len(meds)}):\n"]
        for i, m in enumerate(meds, 1):
            lines.append(
                f"{i}. {m.get('name', 'n/a')} — {m.get('family_member', 'n/a')}\n"
                f"   Refill due: {m.get('refill_due', 'n/a')} | Pharmacy: {m.get('pharmacy', 'n/a')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_medication_refills_due failed: %s", exc)
        return f"Error getting medication refills: {exc}"


# ===================================================================
# EXTENSION 5: FINANCIAL TRACKER TOOLS
# ===================================================================

@mcp.tool()
async def add_recurring_bill(
    name: str,
    category: str = "other",
    amount_gbp: float = 0.0,
    frequency: str = "monthly",
    due_day: Optional[int] = None,
    provider: str = "",
    auto_pay: bool = True,
    notes: str = "",
) -> str:
    """Add a recurring bill or subscription.

    Args:
        name: Bill name (e.g. "Netflix", "Council Tax", "Mortgage").
        category: One of mortgage, utilities, insurance, subscription, council_tax, telecom, other.
        amount_gbp: Amount in GBP.
        frequency: One of weekly, fortnightly, monthly, quarterly, annually.
        due_day: Day of month (1-31) when payment is due.
        provider: Service provider name.
        auto_pay: Whether this is on auto-pay/direct debit.
        notes: Additional notes.
    """
    logger.info("add_recurring_bill called: %s", name)
    try:
        record = brain.add_recurring_bill(
            name=name, category=category, amount_gbp=amount_gbp,
            frequency=frequency, due_day=due_day, provider=provider,
            auto_pay=auto_pay, notes=notes,
        )
        return (
            f"Recurring bill added!\n"
            f"ID:        {record.get('id', 'n/a')}\n"
            f"Name:      {name}\n"
            f"Amount:    \u00a3{amount_gbp:.2f} {frequency}\n"
            f"Category:  {category}\n"
            f"Auto-pay:  {'yes' if auto_pay else 'no'}"
        )
    except Exception as exc:
        logger.error("add_recurring_bill failed: %s", exc)
        return f"Error adding recurring bill: {exc}"


@mcp.tool()
async def get_recurring_bills(category: str = "") -> str:
    """List all active recurring bills, optionally filtered by category.

    Args:
        category: Filter by category. Leave empty for all.
    """
    logger.info("get_recurring_bills called")
    try:
        bills = brain.get_recurring_bills(category=category)
        if not bills:
            return "No recurring bills found."
        lines = [f"Recurring bills ({len(bills)}):\n"]
        for i, b in enumerate(bills, 1):
            auto = "DD" if b.get('auto_pay') else "manual"
            lines.append(
                f"{i}. {b.get('name', 'n/a')} — \u00a3{b.get('amount_gbp', 0):.2f} "
                f"{b.get('frequency', 'monthly')} [{b.get('category', 'other')}] ({auto})"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_recurring_bills failed: %s", exc)
        return f"Error getting recurring bills: {exc}"


@mcp.tool()
async def get_monthly_bill_total() -> str:
    """Calculate total monthly outgoings from all active recurring bills."""
    logger.info("get_monthly_bill_total called")
    try:
        summary = brain.get_monthly_bill_total()
        lines = [
            "Monthly Bill Summary",
            "====================",
            f"Total monthly:  \u00a3{summary['monthly_total_gbp']:.2f}",
            f"Total annual:   \u00a3{summary['annual_total_gbp']:.2f}",
            f"Active bills:   {summary['bill_count']}",
            "\nBy category:",
        ]
        for cat, amt in summary.get('by_category', {}).items():
            lines.append(f"  {cat}: \u00a3{amt:.2f}/month")
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_monthly_bill_total failed: %s", exc)
        return f"Error calculating monthly total: {exc}"


@mcp.tool()
async def log_expense(
    description: str,
    amount_gbp: float,
    category: str = "other",
    family_member: str = "family",
    vendor: str = "",
    expense_date: str = "",
    notes: str = "",
) -> str:
    """Log a one-off expense.

    Args:
        description: What was purchased.
        amount_gbp: Amount in GBP.
        category: Expense category (e.g. "groceries", "dining", "transport").
        family_member: Who spent it.
        vendor: Where it was purchased.
        expense_date: Date (YYYY-MM-DD). Defaults to today.
        notes: Additional notes.
    """
    logger.info("log_expense called: %s \u00a3%.2f", description, amount_gbp)
    try:
        record = brain.log_expense(
            description=description, amount_gbp=amount_gbp,
            category=category, family_member=family_member,
            vendor=vendor, expense_date=expense_date, notes=notes,
        )
        return (
            f"Expense logged!\n"
            f"ID:       {record.get('id', 'n/a')}\n"
            f"What:     {description}\n"
            f"Amount:   \u00a3{amount_gbp:.2f}\n"
            f"Category: {category}\n"
            f"Who:      {family_member}"
        )
    except Exception as exc:
        logger.error("log_expense failed: %s", exc)
        return f"Error logging expense: {exc}"


@mcp.tool()
async def get_spending_summary(days_back: int = 30, family_member: str = "") -> str:
    """Get a spending summary for the last N days.

    Args:
        days_back: Number of days to look back. Default 30.
        family_member: Filter by family member. Leave empty for all.
    """
    logger.info("get_spending_summary called")
    try:
        summary = brain.get_spending_summary(
            days_back=days_back, family_member=family_member,
        )
        lines = [
            f"Spending Summary (last {summary['period_days']} days)",
            "=" * 40,
            f"Total spent:    \u00a3{summary['total_gbp']:.2f}",
            f"Transactions:   {summary['transaction_count']}",
            "\nBy category:",
        ]
        for cat, amt in summary.get('by_category', {}).items():
            lines.append(f"  {cat}: \u00a3{amt:.2f}")
        if summary.get('recent_expenses'):
            lines.append("\nRecent expenses:")
            for e in summary['recent_expenses'][:5]:
                lines.append(
                    f"  \u2022 {e.get('expense_date', 'n/a')}: {e.get('description', 'n/a')} "
                    f"\u00a3{e.get('amount_gbp', 0):.2f}"
                )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_spending_summary failed: %s", exc)
        return f"Error getting spending summary: {exc}"


# ===================================================================
# EXTENSION 6: JOB HUNT CRM TOOLS
# ===================================================================

@mcp.tool()
async def add_jh_contact(
    name: str,
    company: str = "",
    role: str = "",
    email: str = "",
    phone: str = "",
    linkedin_url: str = "",
    relationship: str = "recruiter",
    notes: str = "",
) -> str:
    """Add a job hunt contact (recruiter, hiring manager, referral, etc.).

    Args:
        name: Contact name.
        company: Company they work for.
        role: Their job title.
        email: Email address.
        phone: Phone number.
        linkedin_url: LinkedIn profile URL.
        relationship: One of recruiter, hiring_manager, referral, peer, other.
        notes: How you met, context, etc.
    """
    logger.info("add_jh_contact called: %s", name)
    try:
        record = brain.add_jh_contact(
            name=name, company=company, role=role, email=email,
            phone=phone, linkedin_url=linkedin_url,
            relationship=relationship, notes=notes,
        )
        return (
            f"Job hunt contact added!\n"
            f"ID:           {record.get('id', 'n/a')}\n"
            f"Name:         {name}\n"
            f"Company:      {company or 'n/a'}\n"
            f"Relationship: {relationship}"
        )
    except Exception as exc:
        logger.error("add_jh_contact failed: %s", exc)
        return f"Error adding contact: {exc}"


@mcp.tool()
async def search_jh_contacts(
    query: str = "",
    company: str = "",
    relationship: str = "",
) -> str:
    """Search job hunt contacts.

    Args:
        query: Text search across name, company, and notes.
        company: Filter by company.
        relationship: Filter by relationship type.
    """
    logger.info("search_jh_contacts called")
    try:
        contacts = brain.search_jh_contacts(
            query=query, company=company, relationship=relationship,
        )
        if not contacts:
            return "No contacts found."
        lines = [f"Job hunt contacts ({len(contacts)}):\n"]
        for i, c in enumerate(contacts, 1):
            lines.append(
                f"{i}. {c.get('name', 'n/a')} — {c.get('company', 'n/a')}\n"
                f"   Role: {c.get('role', 'n/a')} | {c.get('relationship', 'other')}\n"
                f"   Email: {c.get('email', 'n/a')} | Phone: {c.get('phone', 'n/a')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("search_jh_contacts failed: %s", exc)
        return f"Error searching contacts: {exc}"


@mcp.tool()
async def add_job_application(
    company: str,
    job_title: str,
    url: str = "",
    salary_min: Optional[int] = None,
    salary_max: Optional[int] = None,
    requirements: str = "",
    source: str = "",
) -> str:
    """Add a job application to the pipeline.

    Args:
        company: Company name.
        job_title: Job title.
        url: Job listing URL.
        salary_min: Minimum salary (GBP).
        salary_max: Maximum salary (GBP).
        requirements: Key requirements from the listing.
        source: Where you found it (e.g. "LinkedIn", "referral").
    """
    logger.info("add_job_application called: %s @ %s", job_title, company)
    try:
        record = brain.add_job_application(
            company=company, job_title=job_title, url=url,
            salary_min=salary_min, salary_max=salary_max,
            requirements=requirements, source=source,
        )
        salary_str = ""
        if salary_min and salary_max:
            salary_str = f"\u00a3{salary_min:,}-\u00a3{salary_max:,}"
        elif salary_min:
            salary_str = f"\u00a3{salary_min:,}+"
        return (
            f"Job application added!\n"
            f"ID:      {record.get('id', 'n/a')}\n"
            f"Role:    {job_title}\n"
            f"Company: {company}\n"
            f"Salary:  {salary_str or 'not specified'}\n"
            f"Status:  identified"
        )
    except Exception as exc:
        logger.error("add_job_application failed: %s", exc)
        return f"Error adding job application: {exc}"


@mcp.tool()
async def submit_application(
    application_id: str,
    status: str = "applied",
    applied_date: str = "",
    resume_version: str = "",
    cover_letter_notes: str = "",
) -> str:
    """Submit/update a job application status.

    Args:
        application_id: UUID of the application (or use add_job_application first).
        status: New status (identified, applied, screening, interviewing, offer, rejected, withdrawn).
        applied_date: Date applied (YYYY-MM-DD).
        resume_version: Which resume version was used.
        cover_letter_notes: Notes about the cover letter.
    """
    logger.info("submit_application called: %s -> %s", application_id, status)
    try:
        notes_parts = []
        if applied_date:
            notes_parts.append(f"Applied: {applied_date}")
        if resume_version:
            notes_parts.append(f"Resume: {resume_version}")
        if cover_letter_notes:
            notes_parts.append(f"Cover letter: {cover_letter_notes}")
        record = brain.update_application_status(
            application_id=application_id, status=status,
            notes="; ".join(notes_parts) if notes_parts else "",
        )
        return (
            f"Application updated!\n"
            f"ID:     {application_id}\n"
            f"Status: {status}"
        )
    except Exception as exc:
        logger.error("submit_application failed: %s", exc)
        return f"Error updating application: {exc}"


@mcp.tool()
async def schedule_interview(
    application_id: str,
    interview_type: str = "phone",
    scheduled_at: str = "",
    duration_minutes: int = 60,
    interviewer_name: str = "",
    notes: str = "",
) -> str:
    """Schedule an interview for a job application.

    Args:
        application_id: UUID of the application.
        interview_type: Type (phone, video, onsite, panel, technical, final).
        scheduled_at: Date and time (YYYY-MM-DD HH:MM or ISO format).
        duration_minutes: Expected duration in minutes.
        interviewer_name: Name of the interviewer.
        notes: Preparation notes.
    """
    logger.info("schedule_interview called: app=%s", application_id)
    try:
        record = brain.schedule_interview(
            application_id=application_id, interview_type=interview_type,
            scheduled_at=scheduled_at, duration_minutes=duration_minutes,
            interviewer_name=interviewer_name, notes=notes,
        )
        return (
            f"Interview scheduled!\n"
            f"ID:          {record.get('id', 'n/a')}\n"
            f"Application: {application_id}\n"
            f"Type:        {interview_type}\n"
            f"When:        {scheduled_at or 'TBD'}\n"
            f"Duration:    {duration_minutes} min\n"
            f"Interviewer: {interviewer_name or 'TBD'}"
        )
    except Exception as exc:
        logger.error("schedule_interview failed: %s", exc)
        return f"Error scheduling interview: {exc}"


@mcp.tool()
async def log_interview_notes(
    interview_id: str,
    feedback: str = "",
    rating: Optional[int] = None,
) -> str:
    """Log feedback and rating for a completed interview.

    Args:
        interview_id: UUID of the interview.
        feedback: Detailed feedback and impressions.
        rating: Overall rating (1-5).
    """
    logger.info("log_interview_notes called: %s", interview_id)
    try:
        record = brain.log_interview_notes(
            interview_id=interview_id, feedback=feedback, rating=rating,
        )
        return (
            f"Interview notes logged!\n"
            f"ID:     {interview_id}\n"
            f"Rating: {rating or 'not rated'}/5\n"
            f"Status: completed"
        )
    except Exception as exc:
        logger.error("log_interview_notes failed: %s", exc)
        return f"Error logging interview notes: {exc}"


@mcp.tool()
async def get_pipeline_overview(days_ahead: int = 7) -> str:
    """Get an overview of the job hunt pipeline.

    Args:
        days_ahead: Days ahead to check for upcoming interviews. Default 7.
    """
    logger.info("get_pipeline_overview called")
    try:
        overview = brain.get_pipeline_overview(days_ahead=days_ahead)
        lines = [
            "Job Hunt Pipeline Overview",
            "=" * 30,
            f"Total applications: {overview['total_applications']}",
            f"Active (in progress): {overview['active_count']}",
            "\nBy status:",
        ]
        for status, info in overview.get('by_status', {}).items():
            lines.append(f"  {status}: {info['count']}")
            for role in info.get('roles', [])[:3]:
                lines.append(f"    \u2022 {role}")

        interviews = overview.get('upcoming_interviews', [])
        if interviews:
            lines.append(f"\nUpcoming interviews ({len(interviews)}):\n")
            for iv in interviews:
                app = iv.get('jh_applications', {})
                lines.append(
                    f"  \u2022 {iv.get('scheduled_at', 'TBD')} — "
                    f"{app.get('company', 'n/a')}: {app.get('job_title', 'n/a')} "
                    f"({iv.get('interview_type', 'n/a')})"
                )
        else:
            lines.append(f"\nNo interviews scheduled in the next {days_ahead} days.")

        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_pipeline_overview failed: %s", exc)
        return f"Error getting pipeline overview: {exc}"


@mcp.tool()
async def get_upcoming_interviews(days_ahead: int = 14) -> str:
    """Get upcoming interviews.

    Args:
        days_ahead: Number of days to look ahead. Default 14.
    """
    logger.info("get_upcoming_interviews called")
    try:
        interviews = brain.get_upcoming_interviews(days_ahead=days_ahead)
        if not interviews:
            return f"No interviews scheduled in the next {days_ahead} days."
        lines = [f"Upcoming interviews ({len(interviews)}):\n"]
        for i, iv in enumerate(interviews, 1):
            app = iv.get('jh_applications', {})
            lines.append(
                f"{i}. [{iv.get('scheduled_at', 'TBD')}] "
                f"{app.get('company', 'n/a')}: {app.get('job_title', 'n/a')}\n"
                f"   Type: {iv.get('interview_type', 'n/a')} | "
                f"Duration: {iv.get('duration_minutes', 60)} min\n"
                f"   Interviewer: {iv.get('interviewer_name', 'TBD')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        logger.error("get_upcoming_interviews failed: %s", exc)
        return f"Error getting upcoming interviews: {exc}"


@mcp.tool()
async def link_contact_to_professional_crm(jh_contact_id: str) -> str:
    """Link a job hunt contact to the professional contacts CRM for long-term networking.

    Args:
        jh_contact_id: UUID of the job hunt contact to link.
    """
    logger.info("link_contact_to_professional_crm called: %s", jh_contact_id)
    try:
        result = brain.link_contact_to_professional_crm(jh_contact_id=jh_contact_id)
        if result.get('error'):
            return f"Error: {result['error']}"
        if result.get('message') == 'Already linked':
            return f"Contact already linked to professional CRM (ID: {result.get('professional_contact_id', 'n/a')})."
        return (
            f"Contact linked to professional CRM!\n"
            f"Professional ID: {result.get('id', 'n/a')}\n"
            f"Name:           {result.get('name', 'n/a')}\n"
            f"Company:        {result.get('company', 'n/a')}"
        )
    except Exception as exc:
        logger.error("link_contact_to_professional_crm failed: %s", exc)
        return f"Error linking contact: {exc}"


# ===================================================================
# HELPERS
# ===================================================================

def _format_results(results: list[dict[str, Any]], *, include_similarity: bool) -> str:
    """Format a list of memory records into a human-readable string."""
    parts: list[str] = []
    for i, row in enumerate(results, start=1):
        meta = row.get("metadata") or {}
        lines = [f"--- Memory {i} ---"]
        lines.append(f"ID:       {row.get('id', 'n/a')}")
        lines.append(f"Created:  {row.get('created_at', 'n/a')}")
        if include_similarity and "similarity" in row:
            lines.append(f"Score:    {row['similarity']:.4f}")
        if meta.get("category"):
            lines.append(f"Category: {meta['category']}")
        if meta.get("tags"):
            lines.append(f"Tags:     {', '.join(meta['tags'])}")
        if meta.get("people"):
            lines.append(f"People:   {', '.join(meta['people'])}")
        if meta.get("source_user"):
            lines.append(f"Captured by: {meta['source_user']}")
        lines.append(f"\n{row.get('content', '')}")
        parts.append("\n".join(lines))
    return f"Found {len(results)} memories.\n\n" + "\n\n".join(parts)


# ===================================================================
# BUILD ASGI APP WITH OAUTH ROUTES
# ===================================================================

def _build_app(transport: str) -> Starlette:
    """Build the ASGI app for HTTP or SSE transport.

    The key insight: the MCP app returned by streamable_http_app() has a
    lifespan handler that initialises the session manager's task group.
    We MUST preserve this lifespan.  We achieve this by:

    1. Getting the MCP Starlette app (which owns the lifespan).
    2. Injecting our OAuth routes into the MCP app's route list (prepended
       so they take priority over the catch-all /mcp route).
    3. Wrapping the whole thing in CombinedAuthMiddleware.

    This way the MCP app's lifespan is preserved and the session manager
    initialises correctly.
    """
    # Get the MCP Starlette app (with lifespan)
    if transport == "http":
        mcp_app = mcp.streamable_http_app()
    else:
        mcp_app = mcp.sse_app()

    # Define OAuth route handlers
    async def _protected_resource(request: Request):
        return await oauth_module.protected_resource_metadata(request, settings)

    async def _auth_server_meta(request: Request):
        return await oauth_module.authorization_server_metadata(request, settings)

    async def _authorize(request: Request):
        if request.method == "GET":
            return await oauth_module.authorize_get(request, settings)
        else:
            return await oauth_module.authorize_post(request, settings)

    async def _token(request: Request):
        return await oauth_module.token_endpoint(request, settings)

    async def _health(request: Request):
        return JSONResponse({"status": "healthy", "service": "family-brain-mcp"})

    # Build OAuth routes
    oauth_routes = [
        Route("/.well-known/oauth-protected-resource", _protected_resource, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", _auth_server_meta, methods=["GET"]),
        Route("/authorize", _authorize, methods=["GET", "POST"]),
        Route("/token", _token, methods=["POST"]),
        Route("/health", _health, methods=["GET"]),
    ]

    # Prepend OAuth routes to the MCP app's route list so they take priority
    for route in reversed(oauth_routes):
        mcp_app.router.routes.insert(0, route)

    # Wrap with auth middleware
    static_token = settings.mcp_auth_token
    oauth_enabled = bool(settings.oauth_user_password)

    wrapped_app = CombinedAuthMiddleware(
        mcp_app,
        static_token=static_token,
        oauth_enabled=oauth_enabled,
    )

    return wrapped_app


# ===================================================================
# ENTRY POINT
# ===================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Family Brain MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http", "sse"],
        default=None,
        help="Transport mode (overrides MCP_TRANSPORT env var)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Host to bind (overrides MCP_HOST env var)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to listen on (overrides MCP_PORT env var)",
    )
    args = parser.parse_args()

    transport = args.transport or settings.mcp_transport
    host = args.host or settings.mcp_host
    port = args.port or settings.mcp_port

    if transport == "stdio":
        logger.info("Starting Family Brain MCP server (transport=stdio)...")
        mcp.run(transport="stdio")

    elif transport in ("http", "sse"):
        auth_token = settings.mcp_auth_token
        oauth_enabled = bool(settings.oauth_user_password)
        logger.info(
            "Starting Family Brain MCP server (transport=%s, host=%s, port=%d, "
            "static_auth=%s, oauth=%s, tools=11)...",
            transport, host, port,
            "enabled" if auth_token else "disabled",
            "enabled" if oauth_enabled else "disabled",
        )
        app = _build_app(transport)
        uvicorn.run(app, host=host, port=port)

    else:
        logger.error("Unknown transport: %s", transport)
        sys.exit(1)


if __name__ == "__main__":
    main()

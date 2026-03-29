"""
FamilyBrain -- What-If Scenario Planning Mode.

Provides a dedicated reasoning pipeline for hypothetical / "what if" questions.
When a user asks something like "What if I'm away next week?", this module:

  1. **Detects** that the message is a scenario/hypothetical question.
  2. **Extracts** the key parameters (time period, key person, hypothetical
     event, implicit question) via a lightweight regex + LLM fallback.
  3. **Retrieves** all relevant family context for the scenario (events,
     members, commitments, memories, preferences/constraints).
  4. **Builds** a structured Chain-of-Thought (CoT) reasoning prompt that
     guides the LLM through constraint mapping, conflict detection, gap
     analysis, and solution generation.
  5. **Returns** a conversational but structured response suitable for
     WhatsApp delivery.

Integration
-----------
The main entry point is ``handle_scenario_if_detected()``, which is called
from ``whatsapp_capture._answer_query()`` *before* the default answer path.
If the message is not a scenario question it returns ``None`` and the normal
flow continues.

Multi-turn support
------------------
Scenario context is cached per phone number so that follow-up questions
(e.g. "What about Thursday specifically?") are handled within the same
scenario session.  Sessions expire after 15 minutes of inactivity.
"""

from __future__ import annotations

import json
import logging
import re
import time as _time_mod
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from . import brain
from . import entity_graph

logger = logging.getLogger("open_brain.scenario_planner")

# ---------------------------------------------------------------------------
# 1. SCENARIO DETECTION -- regex + LLM fallback
# ---------------------------------------------------------------------------

# Primary trigger phrases (case-insensitive).  Order matters: longer phrases
# first so that "what would happen if" matches before "what".
_SCENARIO_TRIGGER_PHRASES: list[str] = [
    # Explicit hypotheticals
    r"what would happen if",
    r"what happens if",
    r"what if",
    r"what do we do if",
    r"how would we cope if",
    r"how would we manage if",
    r"how would .+ cope if",
    r"how would .+ manage if",
    r"how would .+ handle",
    r"how will .+ cope",
    r"how will .+ manage",
    # Conditional / suppositional
    r"if i(?:'m| am| was| were| go| went) away",
    r"if .+ (?:is|was|were|goes|went) away",
    r"if .+ goes abroad",
    r"if i(?:'m| am) (?:not here|unavailable|out|travelling|traveling|abroad|in hospital)",
    r"if .+ (?:is|was|were) (?:not here|unavailable|out|travelling|traveling|abroad|in hospital)",
    r"if i can(?:'t| not)",
    r"if .+ can(?:'t| not)",
    # Ability / coverage probes
    r"could .+ handle",
    r"could .+ manage",
    r"could .+ cover",
    r"can .+ handle",
    r"can .+ manage",
    r"can .+ cover",
    r"can we manage if",
    r"can we cope if",
    r"can we handle",
    r"would .+ be able to",
    r"would .+ cope",
    r"would .+ manage",
    r"who would (?:do|handle|cover|take)",
    r"who could (?:do|handle|cover|take)",
    r"who can (?:do|handle|cover|take)",
    # Explicit scenario language
    r"suppose",
    r"supposing",
    r"hypothetically",
    r"let(?:'s| us) say",
    r"imagine if",
    r"imagine .+ (?:is|was|were)",
    r"in the scenario",
    r"scenario where",
    r"plan for if",
    r"contingency",
    # School holiday / absence patterns
    r"(?:during|over) (?:half.?term|easter|christmas|summer) (?:holidays?|break)",
    r"school holiday coverage",
    r"holiday childcare",
]

# Compiled regex: match any trigger phrase anywhere in the message
_SCENARIO_REGEX = re.compile(
    r"(?:" + "|".join(_SCENARIO_TRIGGER_PHRASES) + r")",
    re.IGNORECASE,
)


def is_scenario_question(text: str) -> bool:
    """Return True if *text* looks like a hypothetical / scenario question.

    Uses a fast regex check first, then falls back to a lightweight LLM
    classification if the regex is inconclusive but the message contains
    question-like structure.
    """
    text_stripped = text.strip()
    if not text_stripped:
        return False

    # Fast path: regex match
    if _SCENARIO_REGEX.search(text_stripped):
        logger.info("Scenario detected via regex: %.80s", text_stripped)
        return True

    # Heuristic gate: only invoke LLM fallback if the message looks like
    # a question (ends with ?, or starts with a question word) and is long
    # enough to plausibly be a scenario.
    text_lower = text_stripped.lower()
    looks_like_question = (
        text_stripped.endswith("?")
        or text_lower.startswith(("what", "how", "who", "could", "can", "would", "if"))
    )
    if not looks_like_question or len(text_stripped) < 15:
        return False

    # LLM fallback: cheap, fast classification
    try:
        result = brain.get_llm_reply(
            system_message=(
                "You are a message classifier for a family AI assistant. "
                "Determine whether the following message is a HYPOTHETICAL or "
                "SCENARIO question -- i.e. the user is asking about a 'what if' "
                "situation, planning for a possible future event, or asking how "
                "the family would cope under certain conditions. "
                "Reply with ONLY the word 'scenario' or 'normal'."
            ),
            user_message=text_stripped,
            max_tokens=8,
        )
        is_scenario = "scenario" in (result or "").lower()
        if is_scenario:
            logger.info("Scenario detected via LLM fallback: %.80s", text_stripped)
        return is_scenario
    except Exception as exc:
        logger.warning("Scenario LLM classification failed (%s); defaulting to False", exc)
        return False


# ---------------------------------------------------------------------------
# 2. SCENARIO PARAMETER EXTRACTION
# ---------------------------------------------------------------------------

_EXTRACT_PARAMS_PROMPT = """\
You are a parameter extraction assistant for a family AI assistant's scenario planning mode.

Given a hypothetical/scenario question from a family member, extract the key parameters.
Return a JSON object with exactly these keys:

{
  "time_period": "<the time period mentioned, e.g. 'next week', 'Monday to Friday', '12-16 May', 'half term'. Use ISO dates where possible. null if not specified>",
  "time_start": "<ISO date string YYYY-MM-DD for the start of the period, or null if unclear>",
  "time_end": "<ISO date string YYYY-MM-DD for the end of the period, or null if unclear>",
  "key_person": "<the person whose absence/change is being hypothesised, e.g. 'Mum', 'Dad', 'Sarah'. null if not about a specific person>",
  "hypothetical_event": "<what is being proposed/imagined, e.g. 'Mum is away for work', 'Dad has a hospital appointment', 'school is closed'. Be concise>",
  "implicit_question": "<what the user really wants to know, e.g. 'Can Dad handle the kids solo?', 'Who covers school runs?', 'Are there scheduling conflicts?'>",
  "affected_members": ["<list of family members who would be affected by this scenario>"]
}

Rules:
- Return ONLY valid JSON. No markdown fences, no commentary.
- Today's date is {today}.
- If the user says "next week", calculate the actual dates from today.
- If the user says "Monday", assume the NEXT upcoming Monday.
- If dates are ambiguous, make your best estimate and note it.
- affected_members should include children, the remaining parent, etc.
"""


def extract_scenario_params(text: str) -> dict[str, Any]:
    """Extract structured scenario parameters from the user's message."""
    today_str = date.today().isoformat()
    prompt = _EXTRACT_PARAMS_PROMPT.replace("{today}", today_str)

    try:
        result = brain.get_llm_reply(
            system_message=prompt,
            user_message=text,
            max_tokens=300,
            json_schema={"type": "object"},
        )
        if isinstance(result, str):
            result = json.loads(result)
        logger.info("Scenario params extracted: %s", result)
        return result or {}
    except Exception as exc:
        logger.warning("Scenario param extraction failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# 3. CONTEXT RETRIEVAL FOR SCENARIOS
# ---------------------------------------------------------------------------

def _resolve_date_range(params: dict[str, Any]) -> tuple[str, str]:
    """Return (start_date, end_date) as ISO strings from extracted params.

    Falls back to the next 7 days if no dates could be extracted.
    """
    start = params.get("time_start")
    end = params.get("time_end")

    today = date.today()

    # Validate and parse start
    if start:
        try:
            start_dt = date.fromisoformat(start)
        except (ValueError, TypeError):
            start_dt = today
    else:
        start_dt = today

    # Validate and parse end
    if end:
        try:
            end_dt = date.fromisoformat(end)
        except (ValueError, TypeError):
            end_dt = start_dt + timedelta(days=6)
    else:
        end_dt = start_dt + timedelta(days=6)

    # Ensure end >= start
    if end_dt < start_dt:
        end_dt = start_dt + timedelta(days=6)

    return start_dt.isoformat(), end_dt.isoformat()


def gather_scenario_context(
    params: dict[str, Any],
    family_id: str,
    original_query: str,
) -> dict[str, Any]:
    """Gather all relevant family data for the scenario.

    Returns a dict with keys:
      - events: list of family events in the time period
      - members: entity graph context (people, relationships)
      - memories: semantically relevant memories
      - constraints: any known preferences/constraints
      - date_range: (start, end) tuple
    """
    start_date, end_date = _resolve_date_range(params)
    context: dict[str, Any] = {
        "date_range": (start_date, end_date),
        "events": [],
        "members": "",
        "memories": [],
        "constraints": [],
    }

    db = brain._supabase
    if not db:
        logger.warning("Supabase not initialised; returning empty context")
        return context

    # -- 3a. Family events in the time period --------------------------------
    try:
        result = (
            db.table("family_events")
            .select("*")
            .gte("event_date", start_date)
            .lte("event_date", end_date)
            .order("event_date")
            .order("event_time")
            .execute()
        )
        context["events"] = result.data or []
        logger.info(
            "Scenario context: %d events in %s to %s",
            len(context["events"]), start_date, end_date,
        )
    except Exception as exc:
        logger.warning("Failed to fetch events for scenario: %s", exc)

    # -- 3b. Family members and relationships (entity graph) -----------------
    try:
        context["members"] = entity_graph.get_entity_context(original_query, family_id)
    except Exception as exc:
        logger.warning("Entity graph context failed for scenario: %s", exc)

    # -- 3c. Semantically relevant memories ----------------------------------
    try:
        # Build a search query that combines the scenario with time context
        search_terms = []
        if params.get("key_person"):
            search_terms.append(params["key_person"])
        if params.get("hypothetical_event"):
            search_terms.append(params["hypothetical_event"])
        for member in params.get("affected_members", []):
            search_terms.append(member)
        # Add schedule/routine keywords to surface recurring commitments
        search_terms.extend(["schedule", "routine", "regular", "every week", "commitment"])

        search_query = original_query + " " + " ".join(search_terms)
        memories = brain.semantic_search(
            search_query,
            match_threshold=0.2,
            match_count=20,
            family_id=family_id,
        )
        context["memories"] = memories or []
        logger.info("Scenario context: %d relevant memories", len(context["memories"]))
    except Exception as exc:
        logger.warning("Semantic search failed for scenario: %s", exc)

    # -- 3d. Known constraints / preferences ---------------------------------
    # Search for stored preferences, routines, and constraints
    try:
        constraint_keywords = [
            "works late", "works from home", "school run",
            "pickup", "drop off", "can't do", "doesn't drive",
            "allergic", "medication", "appointment", "regular",
            "always", "never", "only", "preference",
        ]
        constraint_query = " ".join(constraint_keywords)
        constraint_memories = brain.semantic_search(
            constraint_query,
            match_threshold=0.25,
            match_count=10,
            family_id=family_id,
        )
        # Deduplicate against already-fetched memories
        existing_ids = {m.get("id") for m in context["memories"]}
        for cm in (constraint_memories or []):
            if cm.get("id") not in existing_ids:
                context["constraints"].append(cm)
                existing_ids.add(cm.get("id"))
        logger.info("Scenario context: %d constraint memories", len(context["constraints"]))
    except Exception as exc:
        logger.warning("Constraint search failed for scenario: %s", exc)

    return context


# ---------------------------------------------------------------------------
# 4. SCENARIO REASONING PROMPT (extends existing CoT)
# ---------------------------------------------------------------------------

_SCENARIO_SYSTEM_PROMPT = """\
You are the Family Digital Twin -- a calm, practical, and protective AI assistant \
for this UK household. You have been activated in SCENARIO PLANNING MODE because \
the user has asked a hypothetical "what if" question.

Your job is to reason through the scenario methodically using ALL the family data \
provided, then give a clear, actionable answer.

<thinking>
SCENARIO PLANNING MODE:
1. IDENTIFY THE HYPOTHETICAL: State clearly what is being proposed or imagined.
2. LIST ALL CONSTRAINTS FOR THE TIME PERIOD: Enumerate every event, commitment, \
   responsibility, and known preference that applies during the relevant dates. \
   Include recurring commitments (school runs, activities, work patterns).
3. MAP CONSTRAINTS AGAINST THE SCENARIO: For each commitment, determine who \
   currently handles it and who would need to cover it under the hypothetical.
4. IDENTIFY CONFLICTS: Flag any clashes where the covering person has their \
   own commitments at the same time, or where no one is available.
5. IDENTIFY GAPS: Note anything that has no coverage or where information is \
   missing from the stored data.
6. SUGGEST SOLUTIONS: Provide specific, actionable recommendations for each \
   conflict or gap. Be practical -- suggest rearrangements, help from others, \
   or things to cancel/reschedule.
7. CONFIDENCE ASSESSMENT: Rate how complete the stored data is for this \
   assessment (High / Medium / Low) and note any data gaps.
</thinking>

Core rules (never break these):
- Ground every statement strictly in the provided context. Do NOT invent events \
  or commitments that are not in the data.
- If something is missing, say so clearly: "I don't have information about [X] \
  stored -- forward the relevant details to improve this assessment."
- Tone: Warm, straightforward British English -- like a trusted co-parent or \
  close friend. Reassuring but honest, never dramatic or legalistic.
- Output must be readable in WhatsApp (no heavy markdown, use plain text and \
  line breaks, use bullet points sparingly with simple dashes).

Output format (adapt naturally to the scenario, but include these elements):

Quick take: [one-sentence summary of the scenario assessment]

The situation:
- [key commitment 1 and who covers it]
- [key commitment 2 and who covers it]
...

Things that need sorting:
- [conflict or gap 1 -- with suggested solution]
- [conflict or gap 2 -- with suggested solution]

Everything else: [brief note on what's fine]

Confidence: [High/Medium/Low] -- [one-sentence reason]

Want me to [specific follow-up action, e.g. "set reminders for those two things?"]
"""


def _format_events_for_prompt(events: list[dict]) -> str:
    """Format family events into a readable text block for the LLM prompt."""
    if not events:
        return "No events found in this time period."

    lines = []
    current_date = ""
    for ev in events:
        ev_date = ev.get("event_date", "")
        ev_time = ev.get("event_time", "")
        title = ev.get("title") or ev.get("event_name", "Unknown event")
        member = ev.get("family_member", "family")
        location = ev.get("location", "")
        notes = ev.get("notes", "")

        # Group by date
        if ev_date != current_date:
            current_date = ev_date
            try:
                dt = date.fromisoformat(ev_date)
                lines.append(f"\n{dt.strftime('%A %d %B %Y')}:")
            except (ValueError, TypeError):
                lines.append(f"\n{ev_date}:")

        time_str = f" at {ev_time}" if ev_time else ""
        loc_str = f" ({location})" if location else ""
        notes_str = f" -- {notes}" if notes else ""
        lines.append(f"  - {title}{time_str} [{member}]{loc_str}{notes_str}")

    return "\n".join(lines)


def _format_memories_for_prompt(memories: list[dict], label: str = "Stored memories") -> str:
    """Format memories into a readable text block for the LLM prompt."""
    if not memories:
        return f"No {label.lower()} found."

    lines = [f"{label}:"]
    for m in memories:
        content = m.get("content", "")
        created = m.get("created_at", "")
        if created:
            try:
                ts = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                ts_utc = ts.astimezone(timezone.utc)
                created_label = ts_utc.strftime("%d %b %Y %H:%M UTC")
            except Exception:
                created_label = str(created)[:19]
            lines.append(f"  - [stored: {created_label}] {content}")
        else:
            lines.append(f"  - {content}")

    return "\n".join(lines)


def build_scenario_prompt(
    params: dict[str, Any],
    context: dict[str, Any],
    family_name: str,
    original_query: str,
) -> list[dict[str, str]]:
    """Build the full message list for the scenario reasoning LLM call.

    Returns a list of message dicts suitable for brain.get_llm_reply(messages=...).
    """
    today_str = date.today().strftime("%d %B %Y")
    start_date, end_date = context.get("date_range", ("", ""))

    # Build the system prompt with family context
    system_prompt = (
        _SCENARIO_SYSTEM_PROMPT
        + f"\n\nFamily: {family_name} household. The person asking is {family_name}."
        + f" Today's date is {today_str}."
    )

    # Build the context message
    context_parts = []

    # Scenario parameters
    context_parts.append("SCENARIO PARAMETERS:")
    context_parts.append(f"  Time period: {params.get('time_period', 'not specified')} ({start_date} to {end_date})")
    context_parts.append(f"  Key person: {params.get('key_person', 'not specified')}")
    context_parts.append(f"  Hypothetical: {params.get('hypothetical_event', 'not specified')}")
    context_parts.append(f"  Implicit question: {params.get('implicit_question', 'not specified')}")
    affected = params.get("affected_members", [])
    if affected:
        context_parts.append(f"  Affected members: {', '.join(affected)}")

    # Events
    context_parts.append("\nFAMILY EVENTS IN THIS PERIOD:")
    context_parts.append(_format_events_for_prompt(context.get("events", [])))

    # Entity graph (family members and relationships)
    members_ctx = context.get("members", "")
    if members_ctx:
        context_parts.append("\nKNOWN FAMILY MEMBERS AND RELATIONSHIPS:")
        context_parts.append(members_ctx)

    # Relevant memories
    memories = context.get("memories", [])
    if memories:
        context_parts.append("\n" + _format_memories_for_prompt(memories, "RELEVANT STORED MEMORIES"))

    # Constraints
    constraints = context.get("constraints", [])
    if constraints:
        context_parts.append("\n" + _format_memories_for_prompt(constraints, "KNOWN CONSTRAINTS AND PREFERENCES"))

    context_text = "\n".join(context_parts)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": context_text},
        {"role": "user", "content": f"Scenario question: {original_query}"},
    ]

    return messages


# ---------------------------------------------------------------------------
# 5. MULTI-TURN SCENARIO SESSION MANAGEMENT
# ---------------------------------------------------------------------------

# Per-phone scenario session cache
_scenario_sessions: dict[str, dict[str, Any]] = {}
_SCENARIO_SESSION_TTL = 900  # 15 minutes


def _get_active_session(phone: str) -> Optional[dict[str, Any]]:
    """Return the active scenario session for a phone, or None if expired."""
    session = _scenario_sessions.get(phone)
    if not session:
        return None
    if _time_mod.time() - session.get("last_active", 0) > _SCENARIO_SESSION_TTL:
        _scenario_sessions.pop(phone, None)
        return None
    return session


def _create_session(
    phone: str,
    params: dict[str, Any],
    context: dict[str, Any],
    original_query: str,
) -> dict[str, Any]:
    """Create a new scenario session."""
    session = {
        "params": params,
        "context": context,
        "original_query": original_query,
        "turns": [],
        "last_active": _time_mod.time(),
    }
    _scenario_sessions[phone] = session
    return session


def _update_session(phone: str, user_msg: str, assistant_msg: str) -> None:
    """Add a turn to the scenario session."""
    session = _scenario_sessions.get(phone)
    if session:
        session["turns"].append({"role": "user", "content": user_msg})
        session["turns"].append({"role": "assistant", "content": assistant_msg})
        # Keep last 3 turns (6 messages)
        session["turns"] = session["turns"][-6:]
        session["last_active"] = _time_mod.time()


def is_scenario_followup(text: str, phone: str) -> bool:
    """Return True if there is an active scenario session for this phone
    and the message looks like a follow-up question within that session."""
    session = _get_active_session(phone)
    if not session:
        return False

    text_lower = text.lower().strip()

    # Short messages during an active session are likely follow-ups
    if len(text.split()) <= 6:
        return True

    # Explicit follow-up indicators
    followup_patterns = [
        r"what about",
        r"and (?:on|for) ",
        r"how about",
        r"what if .+ instead",
        r"could .+ do that",
        r"who else",
        r"any other",
        r"tell me more",
        r"go on",
        r"what else",
        r"specifically",
        r"in more detail",
        r"break.?down",
    ]
    for pattern in followup_patterns:
        if re.search(pattern, text_lower):
            return True

    return False


# ---------------------------------------------------------------------------
# 6. MAIN ENTRY POINT
# ---------------------------------------------------------------------------

def handle_scenario_if_detected(
    text: str,
    from_number: str,
    family_name: str,
    family_id: str,
    conversation_history: list[dict] | None = None,
) -> Optional[str]:
    """Main entry point: detect, plan, and respond to scenario questions.

    Returns the response text if a scenario was handled, or None if the
    message is not a scenario question (so the caller can fall through to
    the normal answer path).
    """
    # Check for active scenario follow-up first
    active_session = _get_active_session(from_number)
    is_followup = active_session is not None and is_scenario_followup(text, from_number)

    if not is_followup and not is_scenario_question(text):
        # If there was an active session but this isn't a follow-up or new
        # scenario, expire the session and let normal flow handle it
        if active_session:
            _scenario_sessions.pop(from_number, None)
        return None

    logger.info(
        "Scenario planning mode activated (followup=%s) for: %.100s",
        is_followup, text,
    )

    try:
        if is_followup and active_session:
            # Re-use existing session context
            params = active_session["params"]
            context = active_session["context"]

            # Build messages with session history
            messages = build_scenario_prompt(
                params, context, family_name, active_session["original_query"],
            )
            # Insert previous turns before the new user message
            for turn in active_session.get("turns", []):
                messages.append(turn)
            # Replace the last user message with the follow-up
            messages.append({"role": "user", "content": f"Follow-up question: {text}"})

        else:
            # New scenario: extract params and gather context
            params = extract_scenario_params(text)
            context = gather_scenario_context(params, family_id, text)
            messages = build_scenario_prompt(params, context, family_name, text)

            # Include any existing conversation history for context
            if conversation_history:
                for msg in conversation_history[-4:]:
                    messages.insert(-1, msg)

            # Create a new session
            _create_session(from_number, params, context, text)

        # Call the LLM with extended token budget for scenario reasoning
        answer = brain.get_llm_reply(messages=messages, max_tokens=1200)

        # Strip any <thinking> tags from the response
        answer_clean = _strip_thinking_tags(answer if isinstance(answer, str) else str(answer))

        # Update session with this turn
        _update_session(from_number, text, answer_clean)

        logger.info("Scenario response generated (%d chars)", len(answer_clean))
        return answer_clean[:3800]  # WhatsApp message limit

    except Exception as exc:
        logger.error("Scenario planning failed: %s", exc, exc_info=True)
        return (
            "I tried to run a scenario analysis but hit a problem. "
            "Could you rephrase your question? For example: "
            "'What if I'm away next week -- can Dad handle everything?'"
        )


def _strip_thinking_tags(text: str) -> str:
    """Remove <thinking>...</thinking> blocks from LLM output."""
    cleaned = re.sub(r'<thinking>.*?</thinking>', '', text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# 7. SESSION CLEANUP (called periodically or on demand)
# ---------------------------------------------------------------------------

def cleanup_expired_sessions() -> int:
    """Remove expired scenario sessions. Returns the number removed."""
    now = _time_mod.time()
    expired = [
        phone for phone, session in _scenario_sessions.items()
        if now - session.get("last_active", 0) > _SCENARIO_SESSION_TTL
    ]
    for phone in expired:
        _scenario_sessions.pop(phone, None)
    if expired:
        logger.info("Cleaned up %d expired scenario sessions", len(expired))
    return len(expired)

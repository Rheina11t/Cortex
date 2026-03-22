#!/usr/bin/env python3
"""
Standalone test for the recurring-event early-interception logic.

Tests two things:
1. The keyword-based guard (`is_recurring_message`) correctly identifies
   recurring-event phrases and would NOT route them to the query handler.
2. The LLM event-detection prompt correctly extracts is_recurring=True,
   recurrence_rule, and recurrence_day from representative messages.

Run from the repo root:
    python test_recurring_detection.py
"""

import re
import sys

# ---------------------------------------------------------------------------
# Part 1: keyword guard (pure Python, no imports needed)
# ---------------------------------------------------------------------------

RECURRING_KEYWORDS = ["every", "weekly", "monthly", "fortnightly", "weekdays", "weekends"]

def is_recurring_message(text: str) -> bool:
    return any(kw in text.lower() for kw in RECURRING_KEYWORDS)

def is_query_heuristic(text: str) -> bool:
    """Simplified version of _is_query (heuristics only, no LLM call)."""
    QUESTION_WORDS = (
        "when", "where", "what", "who", "which", "how", "why",
        "did", "do", "does", "is", "are", "was", "were",
        "have", "has", "had", "can", "could", "would", "should",
        "tell", "show", "find", "search", "look", "remind", "recall", "remember",
    )
    QUERY_PHRASES = (
        "when did", "where did", "what did", "who did",
        "have i", "do i", "did i", "do we", "did we", "have we",
        "is my", "are my", "was my", "were my",
    )
    t = text.lower()
    if t.endswith("?"):
        return True
    if t.startswith(QUESTION_WORDS):
        return True
    if any(p in t for p in QUERY_PHRASES):
        return True
    return False

def would_reach_capture(text: str) -> bool:
    """
    Simulate the fixed routing logic:
      - If recurring keyword found → skip query check → goes to capture ✓
      - Else if heuristic says query → goes to query handler ✗ (for recurring msgs)
      - Else → goes to capture ✓
    Returns True if the message would reach the capture/event-detection path.
    """
    if is_recurring_message(text):
        return True          # early interception: bypass _is_query entirely
    if is_query_heuristic(text):
        return False         # routed to query handler
    return True              # default: capture

# Test cases: (message, expected_reaches_capture, description)
ROUTING_TESTS = [
    # --- Should reach capture (recurring) ---
    ("Izzy has ballet every Saturday at 10:15-11:15", True,
     "every Saturday — canonical recurring event"),
    ("Jack has football every Tuesday at 4pm", True,
     "every Tuesday"),
    ("Mia has piano every week on Thursday", True,
     "every week"),
    ("School run every weekday at 8:30am", True,
     "every weekday"),
    ("Gym class fortnightly on Mondays", True,
     "fortnightly"),
    ("Family dinner every Sunday", True,
     "every Sunday"),
    ("Yoga class weekly on Wednesday at 7pm", True,
     "weekly keyword"),
    ("Swimming every weekend at 9am", True,
     "every weekend"),
    ("Monthly dentist check-up", True,
     "monthly keyword"),

    # --- Should reach query handler (genuine questions) ---
    ("When does Izzy have ballet?", False,
     "question mark → query"),
    ("What time is Jack's football?", False,
     "what time → query"),
    ("Is Mia free on Saturday?", False,
     "is ... free → query"),

    # --- Should reach capture (one-off events, no recurring keyword) ---
    ("Izzy has a dentist appointment on Friday at 3pm", True,
     "one-off event, no recurring keyword"),
    ("School play next Tuesday at 6pm", True,
     "one-off event, relative date"),
]

print("=" * 60)
print("PART 1: Routing guard tests")
print("=" * 60)
passed = 0
failed = 0
for text, expected, desc in ROUTING_TESTS:
    result = would_reach_capture(text)
    status = "PASS" if result == expected else "FAIL"
    if status == "PASS":
        passed += 1
    else:
        failed += 1
    route = "capture" if result else "query"
    expected_route = "capture" if expected else "query"
    print(f"[{status}] {desc}")
    if status == "FAIL":
        print(f"       Message : {text!r}")
        print(f"       Expected: {expected_route}  Got: {route}")

print()
print(f"Routing guard: {passed}/{passed+failed} passed")

# ---------------------------------------------------------------------------
# Part 2: LLM event detection (requires OPENAI_API_KEY in environment)
# ---------------------------------------------------------------------------

print()
print("=" * 60)
print("PART 2: LLM event detection tests")
print("=" * 60)

try:
    import os, json
    from datetime import datetime
    from openai import OpenAI

    api_key = os.environ.get("OPENAI_API_KEY", "")
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    if not api_key:
        print("SKIP: OPENAI_API_KEY not set — skipping LLM tests")
        llm_skipped = True
    else:
        llm_skipped = False

    if not llm_skipped:
        client = OpenAI(api_key=api_key, base_url=base_url)

        EVENT_PROMPT = (
            "You are an event detection assistant for a Family Brain system.\n"
            "Given a message, determine if it contains a schedulable event.\n"
            "Return a JSON object:\n"
            "{\n"
            '  "is_event": true/false,\n'
            '  "event_name": "<name>",\n'
            '  "event_date": "<YYYY-MM-DD or null>",\n'
            '  "event_time": "<HH:MM or null>",\n'
            '  "end_time": "<HH:MM or null>",\n'
            '  "family_member": "<who>",\n'
            '  "is_recurring": true/false,\n'
            '  "recurrence_rule": "<WEEKLY|BIWEEKLY|MONTHLY|WEEKDAYS|WEEKENDS|null>",\n'
            '  "recurrence_day": "<day of week or null>"\n'
            "}\n"
            f"Today's date is {datetime.now().strftime('%Y-%m-%d')}.\n"
            "Rules:\n"
            "- Return ONLY valid JSON.\n"
            "- 'every Saturday', 'every week on Monday' -> WEEKLY + day\n"
            "- 'every other week', 'fortnightly' -> BIWEEKLY + day\n"
            "- 'every month', 'monthly' -> MONTHLY\n"
            "- 'every weekday' -> WEEKDAYS\n"
            "- 'every weekend' -> WEEKENDS\n"
        )

        LLM_TESTS = [
            {
                "msg": "Izzy has ballet every Saturday at 10:15-11:15",
                "expect_recurring": True,
                "expect_rule": "WEEKLY",
                "expect_day": "SATURDAY",
                "expect_time": "10:15",
                "expect_end_time": "11:15",
            },
            {
                "msg": "Jack has football every Tuesday at 4pm",
                "expect_recurring": True,
                "expect_rule": "WEEKLY",
                "expect_day": "TUESDAY",
            },
            {
                "msg": "Gym class fortnightly on Mondays at 7am",
                "expect_recurring": True,
                "expect_rule": "BIWEEKLY",
                "expect_day": "MONDAY",
            },
            {
                "msg": "School run every weekday at 8:30am",
                "expect_recurring": True,
                "expect_rule": "WEEKDAYS",
            },
            {
                "msg": "Izzy has a dentist appointment on Friday at 3pm",
                "expect_recurring": False,
            },
        ]

        llm_passed = 0
        llm_failed = 0
        for t in LLM_TESTS:
            try:
                resp = client.chat.completions.create(
                    model="gpt-4.1-mini",
                    temperature=0.0,
                    messages=[
                        {"role": "system", "content": EVENT_PROMPT},
                        {"role": "user", "content": f"Sender: Dan\n\nMessage: {t['msg']}"},
                    ],
                    response_format={"type": "json_object"},
                )
                data = json.loads(resp.choices[0].message.content or "{}")
                ok = True
                notes = []

                if data.get("is_recurring") != t["expect_recurring"]:
                    ok = False
                    notes.append(f"is_recurring={data.get('is_recurring')} expected={t['expect_recurring']}")

                if t.get("expect_rule") and data.get("recurrence_rule") != t["expect_rule"]:
                    ok = False
                    notes.append(f"rule={data.get('recurrence_rule')!r} expected={t['expect_rule']!r}")

                if t.get("expect_day"):
                    got_day = (data.get("recurrence_day") or "").upper()
                    if got_day != t["expect_day"]:
                        ok = False
                        notes.append(f"day={got_day!r} expected={t['expect_day']!r}")

                if t.get("expect_time") and data.get("event_time") != t["expect_time"]:
                    ok = False
                    notes.append(f"event_time={data.get('event_time')!r} expected={t['expect_time']!r}")

                if t.get("expect_end_time") and data.get("end_time") != t["expect_end_time"]:
                    ok = False
                    notes.append(f"end_time={data.get('end_time')!r} expected={t['expect_end_time']!r}")

                status = "PASS" if ok else "FAIL"
                if ok:
                    llm_passed += 1
                else:
                    llm_failed += 1
                print(f"[{status}] {t['msg']!r}")
                if notes:
                    for n in notes:
                        print(f"       {n}")
                if ok:
                    rule = data.get("recurrence_rule")
                    day = data.get("recurrence_day")
                    t_start = data.get("event_time")
                    t_end = data.get("end_time")
                    print(f"       -> rule={rule}, day={day}, time={t_start}-{t_end}")

            except Exception as e:
                llm_failed += 1
                print(f"[ERROR] {t['msg']!r}: {e}")

        print()
        print(f"LLM detection: {llm_passed}/{llm_passed+llm_failed} passed")

except ImportError as e:
    print(f"SKIP: openai package not available ({e})")

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
print("=" * 60)
total_fail = failed + (llm_failed if 'llm_failed' in dir() else 0)
if total_fail == 0:
    print("ALL TESTS PASSED")
    sys.exit(0)
else:
    print(f"FAILURES: {total_fail}")
    sys.exit(1)

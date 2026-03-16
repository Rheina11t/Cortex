"""
Cortex Capture - Structured Summary Ingestion for Open Brain

This script captures a summary of a Cortex work session, processes it with an
LLM to extract structured metadata, and stores it in the Open Brain Supabase DB.

Usage:
  - Capture a new session summary:
    - Pass text directly: python -m src.cortex_capture --summary "Worked on pricing models."
    - Pipe from stdin:    echo "Worked on pricing models." | python -m src.cortex_capture

  - List the last 10 Cortex sessions:
    python -m src.cortex_capture --list
"""

import argparse
import json
import logging
import os
import sys
from typing import Any

# Ensure the package root is in the path for relative imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src import brain
from src.config import Settings, get_settings

logger = logging.getLogger("open_brain.cortex_capture")

# ---------------------------------------------------------------------------
# LLM Metadata Extraction Prompt for Cortex Sessions
# ---------------------------------------------------------------------------

_CORTEX_EXTRACTION_SYSTEM_PROMPT = (
    "You are a metadata extraction assistant for a personal knowledge base called Open Brain.\n"
    "You will be given a raw summary from a work session in an application called Cortex.\n"
    "\n"
    "Your task is to process the summary and return a JSON object with exactly these keys:\n"
    "\n"
    "{\n"
    '  "summary": "<a clean 2-3 sentence summary of what was worked on>",\n'
    '  "decisions": ["<list of key decisions made, if any>"],\n'
    '  "action_items": ["<list of action items, if any>"],\n'
    '  "tags": ["<list of relevant topics/tags, e.g., product-strategy, pricing>"],\n'
    '  "people": ["<list of names of people mentioned, if any>"],\n'
    '  "sentiment": "<one of: positive, neutral, negative>"\n'
    "}\n"
    "\n"
    "Rules:\n"
    "- Return ONLY valid JSON. No markdown fences, no commentary.\n"
    "- If a field has no value (e.g., no decisions were made), use an empty list [].\n"
    "- The summary field should be a concise, well-written summary, not just a copy of the input.\n"
    "- Tags should be lowercase and hyphenated.\n"
)


def extract_cortex_metadata(raw_text: str, llm_client: Any, llm_model: str) -> dict[str, Any]:
    """Call the OpenAI API to extract structured metadata from a Cortex session summary."""
    try:
        response = llm_client.chat.completions.create(
            model=llm_model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": _CORTEX_EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": raw_text},
            ],
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or "{}"
        parsed: dict[str, Any] = json.loads(content)
        logger.info(
            "Cortex metadata extracted via OpenAI – tags=%s, decisions=%d, action_items=%d",
            parsed.get("tags"),
            len(parsed.get("decisions", [])),
            len(parsed.get("action_items", [])),
        )
        return parsed

    except Exception as exc:
        logger.error("OpenAI Cortex metadata extraction failed: %s", exc)
        raise


# ---------------------------------------------------------------------------
# Database and Application Logic
# ---------------------------------------------------------------------------

def capture_session(summary_text: str, settings: Settings) -> None:
    """Process and store a Cortex session summary."""
    logger.info("Starting Cortex session capture for %d-char summary.", len(summary_text))

    # 1. Extract structured metadata using the dedicated Cortex prompt
    if brain._llm_client is None:
        raise RuntimeError("Brain not initialized. Cannot access LLM client.")

    extracted_data = extract_cortex_metadata(
        summary_text, brain._llm_client, settings.llm_model
    )

    # 2. Prepare the final content and metadata for storage
    cleaned_content = extracted_data.get("summary", summary_text)

    # Determine thought_type: "decision" if decisions were made, else "observation"
    thought_type = "decision" if extracted_data.get("decisions") else "observation"

    # Ensure "cortex" tag is always present
    tags: list[str] = extracted_data.get("tags", [])
    if "cortex" not in tags:
        tags.append("cortex")

    metadata: dict[str, Any] = {
        "source": "cortex-session",
        "category": "meeting-notes",
        "thought_type": thought_type,
        "sentiment": extracted_data.get("sentiment", "neutral"),
        "tags": tags,
        "people": extracted_data.get("people", []),
        "action_items": extracted_data.get("action_items", []),
        "decisions": extracted_data.get("decisions", []),
    }

    # 3. Generate embedding for the cleaned content
    logger.info("Generating embedding for cleaned content...")
    embedding = brain.generate_embedding(cleaned_content)

    # 4. Store the memory in Supabase
    logger.info("Storing memory in Supabase...")
    record = brain.store_memory(cleaned_content, embedding, metadata)

    # 5. Print confirmation
    print("=" * 60)
    print("Cortex Session Captured Successfully!")
    print("=" * 60)
    print(f"ID:         {record.get('id')}")
    print(f"Created At: {record.get('created_at')}")
    print(f"\nSummary:\n  {cleaned_content}\n")
    print("Metadata:")
    print(f"  source:       {metadata['source']}")
    print(f"  category:     {metadata['category']}")
    print(f"  thought_type: {metadata['thought_type']}")
    print(f"  sentiment:    {metadata['sentiment']}")
    print(f"  tags:         {', '.join(metadata['tags'])}")
    if metadata["people"]:
        print(f"  people:       {', '.join(metadata['people'])}")
    if metadata["decisions"]:
        print("\nDecisions:")
        for d in metadata["decisions"]:
            print(f"  - {d}")
    if metadata["action_items"]:
        print("\nAction Items:")
        for a in metadata["action_items"]:
            print(f"  - {a}")
    print("=" * 60)


def list_cortex_sessions(limit: int = 10) -> None:
    """List the most recent memories from source='cortex-session'."""
    logger.info("Fetching last %d Cortex session memories...", limit)
    db, _ = brain._require_init()

    result = (
        db.table("memories")
        .select("id, content, metadata, created_at")
        .eq("metadata->>source", "cortex-session")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )

    if not result.data:
        print("No Cortex session memories found.")
        return

    print("=" * 60)
    print(f"Last {len(result.data)} Cortex Session Memories")
    print("=" * 60)

    for i, record in enumerate(result.data):
        meta = record.get("metadata") or {}
        print(f"\n[{i + 1}] ID: {record.get('id')}")
        print(f"    Created At:   {record.get('created_at')}")
        print(f"    Summary:      {record.get('content')}")
        print(f"    Category:     {meta.get('category', 'N/A')}")
        print(f"    Thought Type: {meta.get('thought_type', 'N/A')}")
        print(f"    Sentiment:    {meta.get('sentiment', 'N/A')}")
        print(f"    Tags:         {', '.join(meta.get('tags', []))}")
        if meta.get("people"):
            print(f"    People:       {', '.join(meta['people'])}")
        if meta.get("decisions"):
            print("    Decisions:")
            for d in meta["decisions"]:
                print(f"      - {d}")
        if meta.get("action_items"):
            print("    Action Items:")
            for a in meta["action_items"]:
                print(f"      - {a}")
        print("-" * 60)


# ---------------------------------------------------------------------------
# Main execution block
# ---------------------------------------------------------------------------

def main() -> None:
    """Main script entry point."""
    parser = argparse.ArgumentParser(
        description="Capture a Cortex work session summary into Open Brain.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument(
        "--summary",
        type=str,
        help="The session summary text. If not provided, reads from stdin.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the last 10 Cortex session memories instead of capturing.",
    )
    args = parser.parse_args()

    try:
        # Load settings from .env and initialize brain module
        settings = get_settings()
        brain.init(settings)

        if args.list:
            list_cortex_sessions()
        else:
            summary_text = ""
            if args.summary:
                summary_text = args.summary
            elif not sys.stdin.isatty():
                summary_text = sys.stdin.read().strip()

            if not summary_text:
                print(
                    "Error: No summary text provided. Use --summary or pipe from stdin.",
                    file=sys.stderr,
                )
                parser.print_help()
                sys.exit(1)

            capture_session(summary_text, settings)

    except (RuntimeError, EnvironmentError) as exc:
        logger.critical("Fatal error: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.exception("An unexpected error occurred: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

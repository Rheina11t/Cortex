#!/usr/bin/env python3
"""
Test both LLM backends (OpenAI and Anthropic) for metadata extraction.
Also verifies that embeddings work correctly regardless of LLM backend.
"""

import os
import sys
import json
import dataclasses

# Ensure we load the .env file with override=True before importing anything
from dotenv import load_dotenv
load_dotenv(override=True)

sys.path.insert(0, os.path.dirname(__file__))

from src.config import Settings, get_settings
from src import brain

TEST_TEXT = (
    "Had a great call with Sarah and Tom today about the Q3 product roadmap. "
    "We decided to prioritise the mobile app launch over the API redesign. "
    "Action items: Tom to draft the mobile spec by Friday, Sarah to update the "
    "stakeholder deck. Budget approved at $50k."
)

PASS = "✅ PASS"
FAIL = "❌ FAIL"


def run_test(label: str, llm_backend: str, llm_model: str) -> bool:
    print(f"\n{'='*60}")
    print(f"Testing: {label}")
    print(f"  LLM backend: {llm_backend}")
    print(f"  LLM model:   {llm_model}")
    print(f"{'='*60}")

    # Build a settings object with the desired backend
    base = get_settings()
    settings = dataclasses.replace(
        base,
        llm_backend=llm_backend,
        llm_model=llm_model,
    )

    try:
        brain.init(settings)
    except Exception as e:
        print(f"{FAIL} brain.init() raised: {e}")
        return False

    # ── Test 1: metadata extraction ─────────────────────────────────────────
    print("\n[1] Metadata extraction…")
    try:
        metadata = brain.extract_metadata(TEST_TEXT)
        print(f"    cleaned_content: {metadata.get('cleaned_content', '')[:80]}…")
        print(f"    category:        {metadata.get('category')}")
        print(f"    tags:            {metadata.get('tags')}")
        print(f"    people:          {metadata.get('people')}")
        print(f"    action_items:    {metadata.get('action_items')}")

        assert isinstance(metadata, dict), "Result must be a dict"
        assert "cleaned_content" in metadata, "Missing cleaned_content"
        assert "category" in metadata, "Missing category"
        assert isinstance(metadata.get("tags"), list), "tags must be a list"
        assert isinstance(metadata.get("people"), list), "people must be a list"
        print(f"    {PASS}")
    except Exception as e:
        print(f"    {FAIL}: {e}")
        return False

    # ── Test 2: embedding generation ────────────────────────────────────────
    print("\n[2] Embedding generation (OpenAI, always)…")
    try:
        embedding = brain.generate_embedding(TEST_TEXT)
        assert len(embedding) == 1536, f"Expected 1536 dims, got {len(embedding)}"
        print(f"    Dimensions: {len(embedding)}")
        print(f"    First 3 values: {embedding[:3]}")
        print(f"    {PASS}")
    except Exception as e:
        print(f"    {FAIL}: {e}")
        return False

    return True


def main():
    print("Open Brain – LLM Backend Test Suite")
    print("=====================================")

    results = {}

    # Test 1: OpenAI backend
    results["OpenAI (gpt-4.1-mini)"] = run_test(
        label="OpenAI GPT-4.1-mini",
        llm_backend="openai",
        llm_model="gpt-4.1-mini",
    )

    # Test 2: Anthropic backend
    results["Anthropic (claude-3-5-haiku-20241022)"] = run_test(
        label="Anthropic Claude 3.5 Haiku",
        llm_backend="anthropic",
        llm_model="claude-3-5-haiku-20241022",
    )

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    all_passed = True
    for name, passed in results.items():
        status = PASS if passed else FAIL
        print(f"  {status}  {name}")
        if not passed:
            all_passed = False

    print()
    if all_passed:
        print("All tests passed! Both LLM backends are working correctly.")
        sys.exit(0)
    else:
        print("Some tests failed. Check the output above for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()

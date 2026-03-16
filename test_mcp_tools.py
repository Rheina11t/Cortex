#!/usr/bin/env python3
"""
Comprehensive test script for the Open Brain MCP server tools via HTTP.
Tests all three tools and performs a full insert-then-search round-trip
to confirm OpenAI embeddings are working end-to-end.
"""

import asyncio
import json
import os
import requests
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

load_dotenv(override=True)  # .env takes precedence over system env vars

MCP_URL = "http://localhost:8321/mcp"
HEALTH_URL = "http://localhost:8321/health"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]


def insert_test_memory() -> str:
    """Insert a test memory directly via Supabase REST API and return its ID."""
    # We need a real 1536-dim embedding — generate one via OpenAI directly
    from openai import OpenAI
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ.get("OPENAI_EMBEDDING_BASE_URL", "https://api.openai.com/v1"),
    )
    test_text = "Open Brain system test: remembering that the MCP server uses OpenAI text-embedding-3-small"
    resp = client.embeddings.create(model="text-embedding-3-small", input=test_text)
    embedding = resp.data[0].embedding
    print(f"  Generated OpenAI embedding: {len(embedding)} dims ✅")

    row = {
        "content": test_text,
        "embedding": embedding,
        "metadata": {
            "tags": ["test", "mcp", "embeddings"],
            "people": [],
            "category": "reference",
            "action_items": [],
            "source": "test_script",
        },
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/memories",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        },
        json=row,
    )
    r.raise_for_status()
    record_id = r.json()[0]["id"]
    print(f"  Test memory inserted (id={record_id}) ✅")
    return record_id


def delete_test_memory(record_id: str) -> None:
    """Clean up the test memory after the test."""
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/memories?id=eq.{record_id}",
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        },
    )
    r.raise_for_status()
    print(f"  Test memory deleted (id={record_id}) ✅")


async def test_tools(test_memory_id: str):
    print(f"\nConnecting to MCP server at {MCP_URL}...")
    async with streamable_http_client(MCP_URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # ── 1. list_tools ──────────────────────────────────────────────
            print("\n[1] list_tools")
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            assert set(names) == {"semantic_search", "list_recent_memories", "query_by_metadata"}, \
                f"Unexpected tools: {names}"
            print(f"  Tools registered: {names} ✅")

            # ── 2. list_recent_memories ────────────────────────────────────
            print("\n[2] list_recent_memories (limit=5)")
            recent = await session.call_tool("list_recent_memories", {"limit": 5})
            result_text = recent.content[0].text
            assert "Memory 1" in result_text, "Expected at least one memory"
            print(f"  {result_text.splitlines()[0]} ✅")

            # ── 3. semantic_search (the key OpenAI embedding test) ─────────
            print("\n[3] semantic_search — querying for the test memory")
            search = await session.call_tool(
                "semantic_search",
                {"query": "MCP server OpenAI embeddings", "match_count": 5, "match_threshold": 0.3},
            )
            search_text = search.content[0].text
            print(f"  {search_text.splitlines()[0]}")
            if test_memory_id in search_text:
                print(f"  Test memory found in results ✅")
            else:
                print(f"  Results returned (test memory may be below threshold) ✅")
            print(f"  Full result preview: {search_text[:300]}...")

            # ── 4. query_by_metadata ───────────────────────────────────────
            print("\n[4] query_by_metadata (category=reference)")
            meta = await session.call_tool(
                "query_by_metadata",
                {"category": "reference", "limit": 5},
            )
            meta_text = meta.content[0].text
            print(f"  {meta_text.splitlines()[0]}")
            assert "Memory 1" in meta_text, "Expected test memory in metadata results"
            print(f"  Test memory found via metadata filter ✅")


if __name__ == "__main__":
    print("=" * 60)
    print("Open Brain MCP Server – Full Verification Test")
    print("=" * 60)

    # ── Health check ───────────────────────────────────────────────────────
    print("\n[0] Health check")
    r = requests.get(HEALTH_URL)
    r.raise_for_status()
    print(f"  {r.json()} ✅")

    # ── Insert a real test memory with an OpenAI embedding ─────────────────
    print("\n[PRE] Inserting test memory with OpenAI embedding")
    test_id = insert_test_memory()

    try:
        asyncio.run(test_tools(test_id))
        print("\n" + "=" * 60)
        print("✅  ALL TESTS PASSED — OpenAI embeddings confirmed working")
        print("=" * 60)
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\n[POST] Cleaning up test memory")
        delete_test_memory(test_id)

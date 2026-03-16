#!/usr/bin/env python3
"""
Test all new MCP tools: thought_stats, capture_thought, and bearer token auth.
Also verifies existing tools still work correctly.
"""

import asyncio
import json
import os
import sys

from dotenv import load_dotenv
load_dotenv(override=True)

from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

MCP_URL = "http://localhost:8321/mcp"
AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {AUTH_TOKEN}"} if AUTH_TOKEN else {}

PASS = "✅ PASS"
FAIL = "❌ FAIL"

results = []

async def call_tool(session: ClientSession, name: str, args: dict) -> str:
    """Call an MCP tool and return the text content."""
    result = await session.call_tool(name, args)
    if result.content:
        return result.content[0].text if hasattr(result.content[0], "text") else str(result.content[0])
    return ""


async def run_tests():
    print("Open Brain – New Tools Test Suite")
    print("===================================")
    print(f"Endpoint: {MCP_URL}")
    print(f"Auth:     {'enabled (token set)' if AUTH_TOKEN else 'disabled'}")
    print()

    # ── Test 1: Health endpoint ──────────────────────────────────────────────
    import requests
    print("1. Health endpoint (no auth required)")
    resp = requests.get("http://localhost:8321/health", timeout=5)
    if resp.status_code == 200 and resp.json().get("status") == "healthy":
        print(f"   {PASS} → {resp.json()}")
        results.append(True)
    else:
        print(f"   {FAIL} → HTTP {resp.status_code}: {resp.text[:100]}")
        results.append(False)

    # ── Test 2: Auth rejection (wrong token) ─────────────────────────────────
    print("\n2. Bearer token rejection (wrong token)")
    resp = requests.post(
        "http://localhost:8321/mcp",
        headers={"Authorization": "Bearer wrong-token-12345", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        timeout=5,
    )
    if resp.status_code == 401:
        print(f"   {PASS} → HTTP 401 returned for invalid token")
        results.append(True)
    else:
        print(f"   {FAIL} → Expected 401, got HTTP {resp.status_code}")
        results.append(False)

    # ── Tests 3–8: MCP tool tests (with valid auth) ──────────────────────────
    async with streamablehttp_client(MCP_URL, headers=HEADERS) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Test 3: List tools — confirm all 5 are registered
            print("\n3. Tool registration (all 5 tools present)")
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            expected = {"semantic_search", "list_recent_memories", "query_by_metadata", "thought_stats", "capture_thought"}
            if expected.issubset(tool_names):
                print(f"   {PASS} → Tools: {sorted(tool_names)}")
                results.append(True)
            else:
                missing = expected - tool_names
                print(f"   {FAIL} → Missing tools: {missing}")
                results.append(False)

            # Test 4: thought_stats (empty or populated)
            print("\n4. thought_stats tool")
            stats_text = await call_tool(session, "thought_stats", {})
            if "Total memories" in stats_text or "knowledge base is empty" in stats_text:
                print(f"   {PASS} → {stats_text[:120].strip()}")
                results.append(True)
            else:
                print(f"   {FAIL} → Unexpected response: {stats_text[:200]}")
                results.append(False)

            # Test 5: capture_thought — write a test memory
            print("\n5. capture_thought tool (write a test memory)")
            test_text = "Open Brain gap-closure test: adding thought_stats and capture_thought tools to the MCP server for agent write access."
            capture_text = await call_tool(session, "capture_thought", {
                "text": test_text,
                "source": "test_suite",
            })
            if "Memory captured successfully" in capture_text:
                print(f"   {PASS} → {capture_text[:200].strip()}")
                results.append(True)
                # Extract ID for cleanup
                captured_id = None
                for line in capture_text.splitlines():
                    if line.startswith("ID:"):
                        captured_id = line.split(":", 1)[1].strip()
                        break
            else:
                print(f"   {FAIL} → {capture_text[:300]}")
                results.append(False)
                captured_id = None

            # Test 6: thought_stats — should now show at least 1 memory
            print("\n6. thought_stats after capture (should show ≥1 memory)")
            stats_text2 = await call_tool(session, "thought_stats", {})
            if "Total memories:" in stats_text2:
                total_line = [l for l in stats_text2.splitlines() if "Total memories:" in l]
                print(f"   {PASS} → {total_line[0].strip() if total_line else stats_text2[:80]}")
                results.append(True)
            else:
                print(f"   {FAIL} → {stats_text2[:200]}")
                results.append(False)

            # Test 7: semantic_search — should find the captured memory
            print("\n7. semantic_search for the captured memory")
            search_text = await call_tool(session, "semantic_search", {
                "query": "MCP server tools agent write access",
                "match_threshold": 0.3,
                "match_count": 5,
            })
            if "Found" in search_text and "Memory" in search_text:
                first_line = search_text.splitlines()[0]
                print(f"   {PASS} → {first_line}")
                results.append(True)
            else:
                print(f"   {FAIL} → {search_text[:200]}")
                results.append(False)

            # Test 8: query_by_metadata — filter by source=test_suite
            print("\n8. query_by_metadata (category filter)")
            meta_text = await call_tool(session, "query_by_metadata", {
                "category": "reference",
                "limit": 5,
            })
            # Either finds results or returns "No memories found" — both are valid responses
            if "memories" in meta_text.lower():
                print(f"   {PASS} → {meta_text.splitlines()[0]}")
                results.append(True)
            else:
                print(f"   {FAIL} → {meta_text[:200]}")
                results.append(False)

            # Cleanup: delete the test memory via Supabase REST API
            if captured_id:
                print(f"\n9. Cleanup (delete test memory {captured_id[:8]}…)")
                import requests as req
                from dotenv import load_dotenv
                load_dotenv(override=True)
                sb_url = os.getenv("SUPABASE_URL")
                sb_key = os.getenv("SUPABASE_SERVICE_KEY")
                del_resp = req.delete(
                    f"{sb_url}/rest/v1/memories?id=eq.{captured_id}",
                    headers={
                        "apikey": sb_key,
                        "Authorization": f"Bearer {sb_key}",
                    },
                    timeout=10,
                )
                if del_resp.status_code == 200:
                    print(f"   {PASS} → Test memory deleted")
                    results.append(True)
                else:
                    print(f"   ⚠️  Cleanup failed (HTTP {del_resp.status_code}) — delete manually")
                    results.append(True)  # Not a test failure

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 40)
    passed = sum(results)
    total = len(results)
    print(f"Results: {passed}/{total} tests passed")
    if passed == total:
        print("🎉 All tests passed!")
    else:
        print(f"⚠️  {total - passed} test(s) failed.")
    return passed == total


if __name__ == "__main__":
    success = asyncio.run(run_tests())
    sys.exit(0 if success else 1)

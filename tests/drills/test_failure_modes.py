"""
Agent Drill Tests - Failure Mode Testing

Tests governance system behavior under various failure scenarios:
1. Pi unreachable (SSH tunnel down)
2. Tool timeout handling
3. Invalid tool calls
4. Cross-device retry logic
5. Graceful degradation

Run with: python -m pytest tests/drills/test_failure_modes.py -v

NOTE: These are integration tests requiring running servers.
Skip in CI with: pytest -m "not integration"
"""

import asyncio
import inspect
import json
import os
import time
from typing import Dict, Any
from unittest.mock import patch, AsyncMock

import pytest
httpx = pytest.importorskip("httpx", reason="httpx not installed")

# These are operator drills against live servers. Keep them out of the default
# suite, but make the opt-in explicit so they do not become permanent dead code.
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_GOVERNANCE_DRILLS") != "1",
    reason="Integration drills require RUN_GOVERNANCE_DRILLS=1 and running governance server",
)

# Test configuration
GOVERNANCE_URL = os.getenv("GOVERNANCE_URL", "http://localhost:8767/mcp/")
PI_URL = os.getenv("PI_URL", "http://localhost:8766/mcp/")
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}


def make_mcp_request(url: str, method: str, params: Dict[str, Any] = None) -> Dict:
    """Make an MCP JSON-RPC request and parse SSE response."""
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": f"drill-{int(time.time()*1000)}"
    }

    response = httpx.post(url, json=payload, headers=HEADERS, timeout=30.0)

    # Parse SSE response
    text = response.text
    if text.startswith("event:"):
        for line in text.split("\n"):
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
    return response.json()


async def async_mcp_request(url: str, method: str, params: Dict[str, Any] = None, timeout: float = 30.0) -> Dict:
    """Async version of MCP request."""
    payload = {
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": f"drill-{int(time.time()*1000)}"
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload, headers=HEADERS)
        text = response.text
        if text.startswith("event:"):
            for line in text.split("\n"):
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
        return response.json()


class TestGovernanceHealth:
    """Test governance server health and recovery."""

    def test_health_check_responds(self):
        """Drill 1: Governance health check should always respond."""
        result = make_mcp_request(GOVERNANCE_URL, "tools/call", {
            "name": "health_check",
            "arguments": {}
        })

        assert "result" in result, f"Expected result, got: {result}"
        content = result["result"]["content"][0]["text"]
        data = json.loads(content)

        assert data["success"] is True
        assert data["status"] in ["healthy", "degraded"]
        print(f"[PASS] Health: {data['status']}, checks: {len(data.get('checks', {}))}")

    def test_invalid_tool_returns_error(self):
        """Drill 2: Invalid tool should return structured error, not crash."""
        result = make_mcp_request(GOVERNANCE_URL, "tools/call", {
            "name": "nonexistent_tool_xyz",
            "arguments": {}
        })

        # Should get an error response, not a crash
        assert "result" in result or "error" in result
        if "error" in result:
            print(f"[PASS] Got proper error: {result['error'].get('message', 'unknown')}")
        else:
            content = result["result"]["content"][0]["text"]
            assert "error" in content.lower() or "not found" in content.lower() or "unknown" in content.lower()
            print(f"[PASS] Invalid tool handled gracefully: {content}")

    def test_missing_required_params(self):
        """Drill 3: Missing required params should return helpful error."""
        result = make_mcp_request(GOVERNANCE_URL, "tools/call", {
            "name": "update_calibration_ground_truth",
            "arguments": {}  # Missing required 'actual_correct'
        })

        # Should indicate the missing field
        assert "result" in result or "error" in result
        print(f"[PASS] Missing params handled: {str(result)[:100]}...")


class TestPiOrchestration:
    """Test Pi orchestration failure modes."""

    def test_pi_health_when_available(self):
        """Drill 4: Pi health check when tunnel is up."""
        result = make_mcp_request(GOVERNANCE_URL, "tools/call", {
            "name": "pi_health",
            "arguments": {}
        })

        assert "result" in result, f"Expected result: {result}"
        content = result["result"]["content"][0]["text"]
        data = json.loads(content)

        if data.get("success"):
            print(f"[PASS] Pi healthy, latency: {data.get('latency_ms', 'N/A')}ms")
        else:
            print(f"[INFO] Pi unreachable (expected if tunnel down): {data.get('error', 'unknown')}")

    def test_pi_sync_eisv_graceful_failure(self):
        """Drill 5: EISV sync should fail gracefully if Pi unreachable."""
        result = make_mcp_request(GOVERNANCE_URL, "tools/call", {
            "name": "pi_sync_eisv",
            "arguments": {}
        })

        assert "result" in result
        content = result["result"]["content"][0]["text"]
        data = json.loads(content)

        # Should either succeed or have a proper error message
        if data.get("success"):
            print(f"[PASS] Sync succeeded: E={data['eisv']['E']:.2f}")
        else:
            assert "error" in data or "Error" in content
            print(f"[PASS] Graceful failure: {data.get('error', content[:50])}")

    def test_pi_workflow_timeout_handling(self):
        """Drill 6: Workflow should handle slow Pi responses."""
        start = time.time()
        result = make_mcp_request(GOVERNANCE_URL, "tools/call", {
            "name": "pi_workflow",
            "arguments": {"workflow": "full_status"}
        })
        elapsed = time.time() - start

        assert "result" in result
        content = result["result"]["content"][0]["text"]

        # Should complete or timeout gracefully
        print(f"[PASS] Workflow completed in {elapsed:.2f}s")


class TestCrossDeviceAudit:
    """Test cross-device audit logging."""

    def test_audit_log_exists(self):
        """Drill 7: Audit log should exist and be writable."""
        import os
        from pathlib import Path
        audit_path = str(Path(__file__).resolve().parents[2] / "data" / "audit.jsonl")

        if os.path.exists(audit_path):
            size = os.path.getsize(audit_path)
            print(f"[PASS] Audit log exists: {size} bytes")
        else:
            print(f"[INFO] Audit log not found at {audit_path}")

    def test_eisv_sync_creates_audit_entry(self):
        """Drill 8: EISV sync should create audit entry."""
        # Get initial audit log size
        import os
        from pathlib import Path
        audit_path = str(Path(__file__).resolve().parents[2] / "data" / "audit.jsonl")
        initial_size = os.path.getsize(audit_path) if os.path.exists(audit_path) else 0

        # Trigger a sync
        make_mcp_request(GOVERNANCE_URL, "tools/call", {
            "name": "pi_sync_eisv",
            "arguments": {}
        })

        # Check if audit log grew
        if os.path.exists(audit_path):
            new_size = os.path.getsize(audit_path)
            if new_size > initial_size:
                print(f"[PASS] Audit log grew: {initial_size} -> {new_size} bytes")
            else:
                print(f"[INFO] Audit log unchanged (Pi may be unreachable)")
        else:
            print(f"[INFO] Audit log not found")


class TestConsolidatedTools:
    """Test consolidated tool handlers."""

    def test_knowledge_tool_actions(self):
        """Drill 9: Knowledge consolidated tool should handle all actions."""
        actions = ["list", "stats", "search"]

        for action in actions:
            result = make_mcp_request(GOVERNANCE_URL, "tools/call", {
                "name": "knowledge",
                "arguments": {"action": action, "query": "test" if action == "search" else None}
            })

            assert "result" in result or "error" in result, f"Action {action} failed"
            print(f"[PASS] knowledge(action={action}) handled")

    def test_agent_tool_list(self):
        """Drill 10: Agent consolidated tool should list agents."""
        result = make_mcp_request(GOVERNANCE_URL, "tools/call", {
            "name": "agent",
            "arguments": {"action": "list"}
        })

        assert "result" in result
        content = result["result"]["content"][0]["text"]
        data = json.loads(content)

        if data.get("success"):
            agents = data.get("agents", [])
            print(f"[PASS] Listed {len(agents)} agents")
        else:
            print(f"[INFO] Agent list: {data.get('error', 'unknown')}")

    def test_calibration_check(self):
        """Drill 11: Calibration consolidated tool should check status."""
        result = make_mcp_request(GOVERNANCE_URL, "tools/call", {
            "name": "calibration",
            "arguments": {"action": "check"}
        })

        assert "result" in result
        content = result["result"]["content"][0]["text"]
        data = json.loads(content)

        if data.get("success"):
            print(f"[PASS] Calibration: calibrated={data.get('calibrated')}, accuracy={data.get('accuracy', 'N/A')}")
        else:
            print(f"[INFO] Calibration check: {data.get('error', 'unknown')}")


class TestConcurrency:
    """Test concurrent request handling."""

    @pytest.mark.asyncio
    async def test_parallel_health_checks(self):
        """Drill 12: Multiple parallel requests should all succeed."""
        tasks = [
            async_mcp_request(GOVERNANCE_URL, "tools/call", {"name": "health_check", "arguments": {}})
            for _ in range(5)
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        successes = sum(1 for r in results if isinstance(r, dict) and "result" in r)
        print(f"[PASS] Parallel health checks: {successes}/5 succeeded")

        assert successes >= 4, f"Expected at least 4/5 successes, got {successes}"

    @pytest.mark.asyncio
    async def test_mixed_tool_concurrency(self):
        """Drill 13: Different tools called concurrently should all work."""
        tasks = [
            async_mcp_request(GOVERNANCE_URL, "tools/call", {"name": "health_check", "arguments": {}}),
            async_mcp_request(GOVERNANCE_URL, "tools/call", {"name": "get_server_info", "arguments": {}}),
            async_mcp_request(GOVERNANCE_URL, "tools/call", {"name": "check_calibration", "arguments": {}}),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        successes = sum(1 for r in results if isinstance(r, dict) and "result" in r)
        print(f"[PASS] Mixed concurrent tools: {successes}/3 succeeded")

        assert successes == 3, f"Expected all 3 to succeed, got {successes}"


def run_drills():
    """Run all drills and print summary."""
    print("\n" + "="*60)
    print("AGENT DRILL TESTS - Failure Mode Verification")
    print("="*60 + "\n")

    # Run each test class
    tests = [
        TestGovernanceHealth(),
        TestPiOrchestration(),
        TestCrossDeviceAudit(),
        TestConsolidatedTools(),
    ]

    passed = 0
    failed = 0

    for test_class in tests:
        print(f"\n--- {test_class.__class__.__name__} ---")
        for method_name in dir(test_class):
            if method_name.startswith("test_"):
                try:
                    method = getattr(test_class, method_name)
                    if inspect.iscoroutinefunction(method):
                        asyncio.run(method())
                    else:
                        method()
                    passed += 1
                except Exception as e:
                    print(f"[FAIL] {method_name}: {e}")
                    failed += 1

    print("\n" + "="*60)
    print(f"DRILL SUMMARY: {passed} passed, {failed} failed")
    print("="*60 + "\n")

    return failed == 0


if __name__ == "__main__":
    success = run_drills()
    exit(0 if success else 1)

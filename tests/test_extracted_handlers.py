import pytest
"""
Test extracted handlers individually.

Tests the more complex handlers to ensure they work correctly.

NOTE: With identity_v2, missing agent_id triggers auto-binding (session-based identity).
Tests updated to reflect this behavior.
"""

import sys
import asyncio
from pathlib import Path
import json

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


@pytest.mark.asyncio
async def test_get_governance_metrics():
    """Test get_governance_metrics handler

    With identity_v2, missing agent_id triggers auto-binding.
    The handler should succeed and return metrics for the auto-bound agent.

    Note: Once a session is bound, you CANNOT switch to a different agent_id.
    This is by design - each session is bound to one agent identity.
    """
    print("\nTesting get_governance_metrics...")

    from src.mcp_handlers import dispatch_tool

    # Test with missing agent_id - should auto-bind (identity_v2 behavior)
    result = await dispatch_tool("get_governance_metrics", {})
    assert result is not None, "Should return response"

    response_data = json.loads(result[0].text)
    # With identity_v2, auto-binding creates a new agent, so success=True
    assert response_data.get("success") == True, "Should succeed with auto-binding"
    assert "agent_signature" in response_data, \
        "Should have identity info from auto-binding"
    print("✅ Auto-binds agent when agent_id missing (identity_v2)")

    # Subsequent calls without agent_id should also succeed (uses bound identity)
    result = await dispatch_tool("get_governance_metrics", {})
    assert result is not None, "Should return response"
    response_data = json.loads(result[0].text)
    assert response_data.get("success") == True, "Should succeed on subsequent calls"
    print("✅ Works on subsequent calls (uses bound identity)")

    print("✅ get_governance_metrics handler tests passed")


@pytest.mark.legacy_identity_defaults
@pytest.mark.asyncio
async def test_process_agent_update():
    """Test process_agent_update handler

    With identity_v2, auto-binding allows updates without prior registration.
    """
    print("\nTesting process_agent_update...")

    from src.mcp_handlers import dispatch_tool

    # Test with parameters (uses auto-bound identity)
    result = await dispatch_tool("process_agent_update", {
        "complexity": 0.3,
        "confidence": 0.9,
        "response_text": "Test update from extracted handlers test"
    })

    assert result is not None, "Should return result"
    response_data = json.loads(result[0].text)
    assert response_data.get("success") == True, "Should succeed"
    # Action location depends on response mode: minimal → top-level, compact → under decision, mirror → verdict
    has_action = (
        "action" in response_data
        or "action" in response_data.get("decision", {})
        or "verdict" in response_data
    )
    assert has_action, "Should have governance action (top-level or in decision or verdict)"
    print("✅ process_agent_update with parameters works")

    print("✅ process_agent_update handler tests passed")


@pytest.mark.asyncio
async def test_config():
    """Test config handler (consolidated from get_thresholds/set_thresholds)"""
    print("\nTesting config...")

    from src.mcp_handlers import dispatch_tool

    # Verify config works (read-only, no auth needed)
    result = await dispatch_tool("config", {})
    response_data = json.loads(result[0].text)
    assert response_data.get("success") == True, "Should succeed for read-only"
    print("✅ config works (read-only)")

    print("✅ config handler tests passed")


@pytest.mark.asyncio
async def test_error_handling():
    """Test error handling in handlers"""
    print("\nTesting error handling...")

    from src.mcp_handlers import dispatch_tool

    # Test with unknown tool
    result = await dispatch_tool("nonexistent_tool", {})

    assert result is not None, "Should return result"
    response_data = json.loads(result[0].text)
    assert response_data.get("success") == False, "Should fail for unknown tool"
    print("✅ Handles unknown tool gracefully")

    print("✅ Error handling tests passed")


async def main():
    """Run all handler tests"""
    try:
        await test_get_governance_metrics()
        await test_process_agent_update()
        await test_config()
        await test_error_handling()
        print("\n🎉 All handler tests passed!")
        return 0
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

"""
Tests for self_recovery_review tool per SELF_RECOVERY_SPEC.md
"""

import asyncio
import json
import pytest
from typing import Dict, Any
from unittest.mock import AsyncMock, patch
from mcp.types import TextContent

from src.mcp_handlers.lifecycle.handlers import handle_self_recovery_review


MOCK_AGENT_ID = "test_agent_recovery"

# Shared patch targets
_PATCH_AUTH = patch(
    "src.mcp_handlers.lifecycle.handlers.require_registered_agent",
    return_value=(MOCK_AGENT_ID, None),
)
_PATCH_OWNERSHIP = patch(
    "src.mcp_handlers.utils.verify_agent_ownership",
    return_value=True,
)


@pytest.fixture
def test_agent_setup():
    """Setup test agent metadata."""
    from src.mcp_handlers.shared import get_mcp_server
    mcp_server = get_mcp_server()

    # Create test agent - need to register it properly
    agent_uuid = MOCK_AGENT_ID
    agent_id = MOCK_AGENT_ID

    # Create metadata
    meta = mcp_server.get_or_create_metadata(agent_uuid)
    meta.status = "paused"
    meta.agent_uuid = agent_uuid
    meta.label = agent_id  # Set label so agent_id lookup works

    # Register agent in metadata dict
    mcp_server.agent_metadata[agent_uuid] = meta
    if agent_id:
        # Also register by agent_id for lookup
        mcp_server.agent_metadata[agent_id] = meta

    # Create monitor with safe metrics
    monitor = mcp_server.get_or_create_monitor(agent_uuid)
    # Update state via unitaires_state
    from governance_core import State
    monitor.state.unitaires_state = State(E=0.7, I=0.8, S=0.2, V=0.0)
    monitor.state.coherence = 0.5
    monitor.state.void_active = False

    # Set safe metrics
    metrics = monitor.get_metrics()
    metrics["mean_risk"] = 0.4

    return {
        "agent_id": agent_id,
        "_agent_uuid": agent_uuid,
        "client_session_id": "test_session",
    }


@pytest.mark.asyncio
async def test_basic_recovery(test_agent_setup):
    """Test 1: Basic recovery with valid reflection."""
    arguments = {
        **test_agent_setup,
        "reflection": "I got stuck optimizing the same function. Should have tried different approach.",
        "proposed_conditions": ["Try alternative approaches before deep optimization"],
    }

    with _PATCH_AUTH, _PATCH_OWNERSHIP:
        result = await handle_self_recovery_review(arguments)

    # Should succeed
    assert len(result) > 0
    result_text = result[0].text if hasattr(result[0], 'text') else str(result[0])
    assert "success" in result_text.lower() or "resumed" in result_text.lower()


@pytest.mark.asyncio
async def test_reflection_too_short(test_agent_setup):
    """Test 2: Reflection too short should be rejected."""
    arguments = {
        **test_agent_setup,
        "reflection": "stuck",
    }

    with _PATCH_AUTH, _PATCH_OWNERSHIP:
        result = await handle_self_recovery_review(arguments)

    # Should fail with REFLECTION_REQUIRED
    assert len(result) > 0
    result_text = result[0].text if hasattr(result[0], 'text') else str(result[0])
    assert "REFLECTION_REQUIRED" in result_text or "reflection required" in result_text.lower()


@pytest.mark.asyncio
async def test_dangerous_conditions_rejected(test_agent_setup):
    """Test 3: Dangerous conditions should be rejected."""
    arguments = {
        **test_agent_setup,
        "reflection": "I want to recover by disabling safety checks",
        "proposed_conditions": ["bypass governance"],
    }

    with _PATCH_AUTH, _PATCH_OWNERSHIP:
        result = await handle_self_recovery_review(arguments)

    # Should fail with UNSAFE_CONDITIONS
    assert len(result) > 0
    result_text = result[0].text if hasattr(result[0], 'text') else str(result[0])
    assert "UNSAFE_CONDITIONS" in result_text or "dangerous" in result_text.lower()


@pytest.mark.asyncio
async def test_low_legacy_coherence_does_not_block_resume(test_agent_setup):
    """Test 4: Directional C(V) remains diagnostic during recovery."""
    from src.mcp_handlers.shared import get_mcp_server
    mcp_server = get_mcp_server()

    # Set low coherence
    monitor = mcp_server.get_or_create_monitor(test_agent_setup["_agent_uuid"])
    from governance_core import State
    monitor.state.unitaires_state = State(E=0.7, I=0.8, S=0.2, V=0.0)
    monitor.state.coherence = 0.2
    monitor.state.void_active = False

    arguments = {
        **test_agent_setup,
        "reflection": "I got stuck and need to recover. I will try a different approach.",
    }

    with _PATCH_AUTH, _PATCH_OWNERSHIP:
        result = await handle_self_recovery_review(arguments)

    assert len(result) > 0
    payload = json.loads(result[0].text)
    data = payload.get("data", payload)
    assert data["success"] is True
    coherence_policy = data["recovery_policy"]["diagnostic_inputs"]["coherence"]
    assert coherence_policy["authoritative"] is False
    assert coherence_policy["health_evidence"] is False


@pytest.mark.asyncio
async def test_high_risk_not_resumed(test_agent_setup):
    """Test 5: High risk should prevent resume."""
    from src.mcp_handlers.shared import get_mcp_server
    mcp_server = get_mcp_server()

    # Set high risk via risk_history (get_metrics() computes mean_risk from this)
    monitor = mcp_server.get_or_create_monitor(test_agent_setup["_agent_uuid"])
    monitor.state.risk_history = [0.8] * 10  # Above threshold of 0.65
    monitor.state.update_count = 10

    arguments = {
        **test_agent_setup,
        "reflection": "I got stuck and need to recover. I will try a different approach.",
    }

    with _PATCH_AUTH, _PATCH_OWNERSHIP, patch(
        "src.mcp_handlers.lifecycle.operations.agent_storage.get_latest_agent_state",
        new_callable=AsyncMock,
        return_value=None,
    ):
        result = await handle_self_recovery_review(arguments)

    # Should not resume
    assert len(result) > 0
    result_text = result[0].text if hasattr(result[0], 'text') else str(result[0])
    assert "not_resumed" in result_text.lower() or "not yet safe" in result_text.lower()


@pytest.mark.asyncio
async def test_void_active_not_resumed(test_agent_setup):
    """Test 6: Void active should prevent resume."""
    from src.mcp_handlers.shared import get_mcp_server
    mcp_server = get_mcp_server()

    # Set void active
    monitor = mcp_server.get_or_create_monitor(test_agent_setup["_agent_uuid"])
    from governance_core import State
    monitor.state.unitaires_state = State(E=0.7, I=0.8, S=0.2, V=0.5)
    monitor.state.coherence = 0.5
    monitor.state.void_active = True

    arguments = {
        **test_agent_setup,
        "reflection": "I got stuck and need to recover. I will try a different approach.",
    }

    with _PATCH_AUTH, _PATCH_OWNERSHIP:
        result = await handle_self_recovery_review(arguments)

    # Should not resume
    assert len(result) > 0
    result_text = result[0].text if hasattr(result[0], 'text') else str(result[0])
    assert "not_resumed" in result_text.lower() or "not yet safe" in result_text.lower()


@pytest.mark.asyncio
async def test_reflection_logged_even_if_not_resumed(test_agent_setup):
    """Test 7: Reflection should be logged even if not resumed."""
    from src.mcp_handlers.shared import get_mcp_server
    mcp_server = get_mcp_server()

    # Set unsafe state
    monitor = mcp_server.get_or_create_monitor(test_agent_setup["_agent_uuid"])
    from governance_core import State
    monitor.state.unitaires_state = State(E=0.7, I=0.8, S=0.2, V=0.0)
    monitor.state.coherence = 0.2
    monitor.state.void_active = False

    arguments = {
        **test_agent_setup,
        "reflection": "I got stuck and need to recover. I will try a different approach.",
        "root_cause": "Complexity was too high",
    }

    with _PATCH_AUTH, _PATCH_OWNERSHIP:
        result = await handle_self_recovery_review(arguments)

    # Should indicate reflection was logged
    assert len(result) > 0
    result_text = result[0].text if hasattr(result[0], 'text') else str(result[0])
    assert "reflection_logged" in result_text.lower() or "logged" in result_text.lower()

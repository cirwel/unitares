"""Round-trip: a real sync_state experience envelope through the SDK parser.

#2366: ``sync_state`` is an experience alias, so what the SDK receives is
``build_experience_envelope(...)`` over the canonical payload. In the SDK's
default ``response_mode="compact"`` that envelope omits ``raw_governance`` and
carries the verdict at ``state_summary.action`` — while both SDK clients read
top-level ``decision.action`` / ``metrics``, keys the envelope never has, so
every verdict parsed as ``"proceed"`` and coherence/risk as ``None``.

``agents/sdk/tests/test_client.py`` and ``agents/sdk/tests/test_sync_client.py`` feed canonical
fixtures straight to the parser and ``test_agent_experience_envelope.py``
never runs the SDK over its output, so the two shapes drifted with green
tests on both sides. This module is the missing seam: it builds the envelope
with the server's own builder and asserts the SDK reads the canonical values
back out of it.
"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from src.mcp_handlers.middleware.envelope_step import build_experience_envelope
from src.mcp_handlers.response_formatter import format_response
from unitares_sdk.client import GovernanceClient
from unitares_sdk.sync_client import SyncGovernanceClient

# One canonical process_agent_update payload per decision shape the server
# emits. ``decision.action`` is binary (``proceed`` / ``pause``, see
# ``src/monitor_decision.py``); a guide is ``proceed`` with
# ``sub_action="guide"``, never an action of its own. The metric values are
# distinct per shape so a parser that reads the wrong branch cannot pass by
# coincidence.
_CANONICAL = {
    "approve": {
        "decision": {"action": "proceed", "sub_action": "approve", "reason": "Low risk (0.12)"},
        "metrics": {"coherence": 0.82, "risk_score": 0.12, "verdict": "safe"},
    },
    "guide": {
        "decision": {
            "action": "proceed",
            "sub_action": "guide",
            "reason": "Operating near basin boundary (0.45)",
            "guidance": "Narrow the change and check in again.",
        },
        "metrics": {"coherence": 0.61, "risk_score": 0.45, "verdict": "caution"},
    },
    "pause": {
        "decision": {"action": "pause", "sub_action": "risk_pause", "reason": "High risk (0.91)"},
        "metrics": {"coherence": 0.41, "risk_score": 0.91, "verdict": "unsafe"},
    },
}


def _compact_envelope(shape: str) -> dict:
    """What a compact sync_state caller actually receives over the wire."""
    source = {
        "success": True,
        "status": "healthy",
        "health_status": "healthy",
        **deepcopy(_CANONICAL[shape]),
    }
    formatted = format_response(
        source, {"response_mode": "compact"}, task_type="feature"
    )
    env = build_experience_envelope(
        "sync_state",
        "process_agent_update",
        formatted,
        {"response_mode": "compact"},
    )
    # Guard the premise: this is the enveloped shape, not the canonical one.
    assert formatted["_mode"] == "compact"
    assert "raw_governance" not in env
    assert "decision" not in env and "metrics" not in env
    assert env["state_summary"]["action"] == _CANONICAL[shape]["decision"]["action"]
    return env


def _assert_canonical(result, shape: str) -> None:
    canonical = _CANONICAL[shape]
    action = canonical["decision"]["action"]
    assert result.verdict == action, (
        f"SDK parsed verdict {result.verdict!r} off the compact envelope; "
        f"canonical decision.action was {action!r}"
    )
    assert result.coherence == canonical["metrics"]["coherence"]
    assert result.risk == canonical["metrics"]["risk_score"]


@pytest.mark.parametrize("shape", sorted(_CANONICAL))
@pytest.mark.asyncio
async def test_async_client_reads_verdict_off_compact_envelope(shape: str):
    client = GovernanceClient(timeout=5.0, retry_delay=0.01)
    client.call_tool = AsyncMock(return_value=_compact_envelope(shape))

    result = await client.checkin("test work")

    _assert_canonical(result, shape)


@pytest.mark.parametrize("shape", sorted(_CANONICAL))
def test_sync_client_reads_verdict_off_compact_envelope(shape: str):
    client = SyncGovernanceClient(transport="rest")
    client.call_tool = lambda tool_name, arguments, **kwargs: _compact_envelope(shape)

    result = client.checkin("test work")

    _assert_canonical(result, shape)


def test_compact_envelope_guidance_falls_back_to_next_action():
    """The compact envelope lifts no ``guidance``; on a pause ``next_action``
    is the concrete instruction it carries instead (it names self_recovery)."""
    client = SyncGovernanceClient(transport="rest")
    client.call_tool = lambda tool_name, arguments, **kwargs: _compact_envelope("pause")

    result = client.checkin("test work")

    assert result.verdict == "pause"
    assert result.guidance and "self_recovery" in result.guidance


def test_compact_guide_carries_the_servers_reason_not_the_continue_prompt():
    """A guide arrives as proceed + sub_action="guide". Its guidance must be
    the server's own reason (action_summary.reason), not the generic "keep
    working" next_action every non-pause envelope carries."""
    env = _compact_envelope("guide")
    assert env["action_summary"]["sub_action"] == "guide"
    assert "Keep working" in env["next_action"]

    client = SyncGovernanceClient(transport="rest")
    client.call_tool = lambda tool_name, arguments, **kwargs: env

    result = client.checkin("test work")

    assert result.verdict == "proceed"
    assert result.guidance == env["action_summary"]["reason"]
    assert "basin boundary" in result.guidance
    assert "Keep working" not in result.guidance


def test_compact_plain_proceed_has_no_guidance():
    """An approve carries no guidance; the continue prompt is not guidance."""
    env = _compact_envelope("approve")
    assert env.get("next_action")

    client = SyncGovernanceClient(transport="rest")
    client.call_tool = lambda tool_name, arguments, **kwargs: env

    result = client.checkin("test work")

    assert result.verdict == "proceed"
    assert result.guidance is None


def test_full_envelope_prefers_raw_governance_over_state_summary():
    """response_mode='full' keeps the canonical payload under raw_governance;
    the SDK reads it from there before falling back to state_summary. The
    two copies are made to disagree, so reversing the precedence fails."""
    source = {"success": True, **deepcopy(_CANONICAL["pause"])}
    env = build_experience_envelope(
        "sync_state", "process_agent_update", source, {"response_mode": "full"}
    )
    assert env["raw_governance"]["decision"]["action"] == "pause"
    env["state_summary"]["action"] = "proceed"
    env["state_summary"]["coherence"] = 0.99
    env["state_summary"]["risk_score"] = 0.01

    client = SyncGovernanceClient(transport="rest")
    client.call_tool = lambda tool_name, arguments, **kwargs: env

    result = client.checkin("test work")

    _assert_canonical(result, "pause")


def test_legacy_badged_coherence_dict_resolves_to_its_value():
    """envelope_step badges a legacy tanh coherence as a dict
    (``{"value": ..., "status": ...}``); the SDK must read ``value``."""
    source = {
        "success": True,
        "_mode": "compact",
        "decision": {"action": "proceed"},
        "metrics": {
            "coherence": 0.49,
            "coherence_source": "legacy_tanh_v",
            "coherence_role": "ode_control_feedback",
            "risk_score": 0.05,
        },
    }
    env = build_experience_envelope("sync_state", "process_agent_update", source)
    assert isinstance(env["state_summary"]["coherence"], dict)

    client = SyncGovernanceClient(transport="rest")
    client.call_tool = lambda tool_name, arguments, **kwargs: env

    result = client.checkin("test work")

    assert result.verdict == "proceed"
    assert result.coherence == 0.49
    assert result.risk == 0.05

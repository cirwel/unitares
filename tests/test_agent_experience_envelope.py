"""Agent-experience response envelope (middleware/envelope_step.py).

Alias-gated: only calls invoked via an experience alias (start_session,
sync_state, check_working_state, search_shared_memory, record_result,
request_review) are reshaped. The two contract guarantees pinned here:

1. Canonical names stay byte-identical - the envelope NEVER touches a
   response unless the invoked name is an experience alias.
2. The envelope never breaks a response - malformed payloads, error
   payloads, and builder failures all fall back to the raw result.
"""

from __future__ import annotations

import json

import pytest
from mcp.types import TextContent

from src.mcp_handlers.middleware import DispatchContext
from src.mcp_handlers.middleware.envelope_step import (
    apply_experience_envelope,
    build_experience_envelope,
)
from src.mcp_handlers.tool_stability import is_experience_alias


def _result(payload) -> list:
    return [TextContent(type="text", text=json.dumps(payload))]


def _ctx(original_name: str) -> DispatchContext:
    return DispatchContext(original_name=original_name)


def _parse(result) -> dict:
    return json.loads(result[0].text)


# ---------------------------------------------------------------------------
# Registry flag
# ---------------------------------------------------------------------------


def test_experience_flag_inventory():
    """Exactly the six workflow aliases are experience-enveloped - a
    seventh (or a canonical name) sneaking in changes response shapes
    and must be a deliberate edit here."""
    expected = {
        "start_session", "sync_state", "check_working_state",
        "search_shared_memory", "record_result", "request_review",
    }
    from src.mcp_handlers.tool_stability import _TOOL_ALIASES

    flagged = {n for n, a in _TOOL_ALIASES.items() if a.experience}
    assert flagged == expected


def test_canonical_names_are_not_experience_aliases():
    for name in ("onboard", "process_agent_update", "get_governance_metrics",
                 "knowledge", "outcome_event", "dialectic", "status", "checkin"):
        assert not is_experience_alias(name), name


# ---------------------------------------------------------------------------
# Step gating: who gets reshaped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canonical_invocation_passes_through_byte_identical():
    raw = _result({"success": True, "agent_uuid": "u-1"})
    out = await apply_experience_envelope(
        "onboard", {}, _ctx("onboard"), raw
    )
    assert out is raw  # same object, not just equal


@pytest.mark.asyncio
async def test_legacy_alias_passes_through():
    """Pre-existing intuitive aliases (status, checkin) keep their raw
    shape - only experience aliases opt in."""
    raw = _result({"success": True})
    out = await apply_experience_envelope(
        "get_governance_metrics", {}, _ctx("status"), raw
    )
    assert out is raw


@pytest.mark.asyncio
async def test_experience_alias_gets_envelope():
    raw = _result({"success": True, "agent_uuid": "u-1", "client_session_id": "s-1"})
    out = await apply_experience_envelope(
        "onboard", {}, _ctx("start_session"), raw
    )
    data = _parse(out)
    assert data["tool"] == "start_session"
    assert data["agent_uuid"] == "u-1"
    assert data["client_session_id"] == "s-1"
    assert data["raw_governance"]["agent_uuid"] == "u-1"
    assert "next_action" in data


@pytest.mark.asyncio
async def test_error_payload_passes_through():
    """Error responses keep the raw contract (typed refusals, recovery
    blocks) - the envelope only reshapes successes."""
    for payload in ({"success": False, "error": "nope"},
                    {"error": "boom"},
                    {"success": False, "status": "identity_required"}):
        raw = _result(payload)
        out = await apply_experience_envelope(
            "outcome_event", {}, _ctx("record_result"), raw
        )
        assert out is raw, payload


@pytest.mark.asyncio
async def test_malformed_result_passes_through():
    for raw in ([TextContent(type="text", text="not json")],
                [TextContent(type="text", text="[1, 2]")],
                [], None):
        out = await apply_experience_envelope(
            "onboard", {}, _ctx("start_session"), raw
        )
        assert out is raw


@pytest.mark.asyncio
async def test_builder_failure_returns_raw(monkeypatch):
    """A bug in the builder must degrade to the raw response."""
    import src.mcp_handlers.middleware.envelope_step as es

    def _boom(*a, **k):
        raise RuntimeError("builder bug")

    monkeypatch.setattr(es, "build_experience_envelope", _boom)
    raw = _result({"success": True})
    out = await apply_experience_envelope(
        "onboard", {}, _ctx("start_session"), raw
    )
    assert out is raw


# ---------------------------------------------------------------------------
# Builder: per-tool harvesting (pure)
# ---------------------------------------------------------------------------


def test_onboard_envelope_surfaces_predecessor():
    payload = {
        "success": True,
        "agent_uuid": "u-new",
        "lineage_state": "no_lineage_declared",
        "thread_context": {"predecessor": {"uuid": "u-prior"}},
    }
    env = build_experience_envelope("start_session", "onboard", payload)
    assert env["state_summary"]["predecessor_uuid"] == "u-prior"
    assert "parent_agent_id" in env["next_action"]
    assert env["raw_governance"] is payload


def test_sync_state_envelope_summarizes_decision_and_risk():
    payload = {
        "success": True,
        "decision": {"action": "continue", "margin": 0.31, "nearest_edge": "S_min"},
        "metrics": {"coherence": 0.82, "risk_score": 0.21},
        "health_status": "healthy",
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    summary = env["state_summary"]
    assert summary["action"] == "continue"
    assert summary["coherence"] == 0.82
    assert env["risk_summary"].startswith("risk low")
    assert "recovery_hint" not in env  # healthy state stays quiet


def test_sync_state_envelope_emits_recovery_hint_when_degraded():
    payload = {
        "success": True,
        "metrics": {"coherence": 0.45, "risk_score": 0.75},
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert "self_recovery_review" in env["recovery_hint"]
    assert env["risk_summary"].startswith("risk high")


def test_sync_state_envelope_surfaces_discoveries():
    payload = {
        "success": True,
        "relevant_discoveries": [
            {"discovery_id": f"d{i}", "summary": f"finding {i}"} for i in range(5)
        ],
    }
    env = build_experience_envelope("sync_state", "process_agent_update", payload)
    assert len(env["memory_suggestions"]) == 3  # truncated
    assert env["memory_suggestions"][0]["discovery_id"] == "d0"


def test_metrics_envelope_maps_existing_friendly_fields():
    payload = {
        "success": True,
        "verdict": {"verdict": "proceed", "explanation": "stable"},
        "guidance": "keep going",
        "next_action": {"tool": "process_agent_update", "example": "..."},
    }
    env = build_experience_envelope("check_working_state", "get_governance_metrics", payload)
    assert env["next_action"] == payload["next_action"]
    assert env["state_summary"] == payload["verdict"]


def test_search_envelope_counts_and_suggests():
    payload = {
        "success": True,
        "results": [{"id": "d1", "summary": "prior art"}],
        "total_count": 1,
    }
    env = build_experience_envelope("search_shared_memory", "knowledge", payload)
    assert "1 prior discoveries matched" in env["next_action"]
    assert env["memory_suggestions"][0]["summary"] == "prior art"


def test_record_result_envelope_lifts_outcome():
    payload = {
        "success": True,
        "outcome_id": "o-1",
        "eisv_snapshot": {"E": 0.7, "I": 0.6, "S": 0.8, "V": 0.1},
    }
    env = build_experience_envelope("record_result", "outcome_event", payload)
    assert env["state_summary"]["outcome_id"] == "o-1"
    assert env["state_summary"]["eisv_snapshot"]["E"] == 0.7


def test_request_review_envelope_threads_session_id():
    payload = {"success": True, "session_id": "sess-42", "phase": "thesis"}
    env = build_experience_envelope("request_review", "dialectic", payload)
    assert "sess-42" in env["next_action"]
    assert env["state_summary"]["phase"] == "thesis"


# ---------------------------------------------------------------------------
# Pipeline integration: the seam itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_runs_post_execution_steps(monkeypatch):
    """run_tool_dispatch_pipeline applies post_execution_steps to the
    handler result and survives a raising step."""
    from src.services.tool_dispatch_service import run_tool_dispatch_pipeline
    import src.mcp_handlers as mh

    async def fake_handler(arguments):
        return _result({"success": True, "marker": "raw"})

    monkeypatch.setitem(mh.TOOL_HANDLERS, "tmp_envelope_tool", fake_handler)

    async def reshape(name, arguments, ctx, result):
        data = _parse(result)
        data["reshaped"] = True
        return [TextContent(type="text", text=json.dumps(data))]

    async def explode(name, arguments, ctx, result):
        raise RuntimeError("step bug")

    out = await run_tool_dispatch_pipeline(
        name="tmp_envelope_tool",
        arguments={},
        pre_steps=[],
        post_steps=[],
        post_execution_steps=[explode, reshape],
    )
    data = _parse(out)
    assert data["marker"] == "raw"
    assert data["reshaped"] is True  # raising step skipped, next still ran

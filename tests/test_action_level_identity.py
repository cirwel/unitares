"""#425 action-level identity classification.

Mixed read-write tools (knowledge/dialectic/agent/calibration/config/
observe) cannot be honestly classified at tool granularity: tool-level
``pre_onboard`` opens their writes, tool-level ``required`` refuses
their browsable reads (the dashboard's whole sweep). Both #425 gates
now resolve at CALL granularity via
``decorators.get_call_identity_requirement`` — alias-aware, action-
aware, default_action-aware.

Inert by default: the strict flag is off everywhere; flag-off the only
behavior change is that pre_onboard-resolved calls skip the dispatch
auto-mint and run honestly unbound (shrinking the in-memory ghost
population for reads).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

# Settle handler imports so consolidated tools register (same import-
# order anchor as test_zero_observation_honesty.py).
import src.mcp_handlers.core  # noqa: F401
import src.mcp_handlers.consolidated  # noqa: F401

from src.mcp_handlers.decorators import (
    action_router,
    get_call_identity_requirement,
    get_tool_definition,
    mcp_tool,
)


# ---------------------------------------------------------------------------
# Resolver semantics
# ---------------------------------------------------------------------------


def test_tool_level_pre_onboard_wins_outright():
    assert get_call_identity_requirement("get_governance_metrics", {}) == "pre_onboard"
    assert get_call_identity_requirement("onboard", {"force_new": True}) == "pre_onboard"


def test_unknown_tool_fails_closed():
    assert get_call_identity_requirement("no_such_tool_xyz", {}) == "required"


def test_read_action_resolves_pre_onboard():
    assert get_call_identity_requirement("knowledge", {"action": "search"}) == "pre_onboard"
    assert get_call_identity_requirement("knowledge", {"action": "stats"}) == "pre_onboard"
    assert get_call_identity_requirement("agent", {"action": "list"}) == "pre_onboard"
    assert get_call_identity_requirement("observe", {"action": "anomalies"}) == "pre_onboard"


def test_write_action_stays_required():
    assert get_call_identity_requirement("knowledge", {"action": "store"}) == "required"
    assert get_call_identity_requirement("knowledge", {"action": "note"}) == "required"
    assert get_call_identity_requirement("agent", {"action": "archive"}) == "required"
    assert get_call_identity_requirement("agent", {"action": "delete"}) == "required"
    assert get_call_identity_requirement("config", {"action": "set"}) == "required"
    assert get_call_identity_requirement("dialectic", {"action": "synthesis"}) == "required"
    assert get_call_identity_requirement("calibration", {"action": "rebuild"}) == "required"


def test_default_action_mirrors_router_semantics():
    """An action-less call must be judged as the action the router will
    actually run: calibration() defaults to 'check' (read),
    dialectic() to 'list' (read), config() to 'get' (read)."""
    assert get_call_identity_requirement("calibration", {}) == "pre_onboard"
    assert get_call_identity_requirement("dialectic", {}) == "pre_onboard"
    assert get_call_identity_requirement("config", {}) == "pre_onboard"
    # A tool with no default and no action: nothing to exempt — the
    # router will error on the missing action anyway, so fail closed.
    assert get_call_identity_requirement("knowledge", {}) == "required"


def test_op_alias_for_action_param():
    assert get_call_identity_requirement("knowledge", {"op": "search"}) == "pre_onboard"
    assert get_call_identity_requirement("knowledge", {"op": "store"}) == "required"


def test_action_case_insensitive():
    assert get_call_identity_requirement("knowledge", {"action": "SEARCH"}) == "pre_onboard"


def test_legacy_alias_canonicalizes_with_injected_action():
    """The dashboard's legacy names must resolve like the canonical
    calls they dispatch to: detect_anomalies → observe(anomalies),
    check_calibration → calibration(check), list_agents → agent(list).
    Without alias-awareness these fail closed and refuse under strict
    even though their canonical forms pass."""
    assert get_call_identity_requirement("detect_anomalies", {}) == "pre_onboard"
    assert get_call_identity_requirement("check_calibration", {}) == "pre_onboard"
    assert get_call_identity_requirement("list_agents", {}) == "pre_onboard"


def test_workflow_alias_identity_classification_matches_canonical_calls():
    assert get_call_identity_requirement("start_session", {"force_new": True}) == "pre_onboard"
    assert get_call_identity_requirement("check_working_state", {}) == "pre_onboard"
    assert get_call_identity_requirement("search_shared_memory", {}) == "pre_onboard"

    assert get_call_identity_requirement("sync_state", {}) == "required"
    assert get_call_identity_requirement("record_result", {}) == "required"
    assert get_call_identity_requirement("request_review", {}) == "required"


def test_legacy_write_alias_stays_required():
    """A write-implying alias (request_dialectic_review →
    dialectic(request)) must NOT inherit read treatment."""
    assert get_call_identity_requirement("request_dialectic_review", {}) == "required"


def test_standalone_read_tools_classified():
    assert get_call_identity_requirement("detect_stuck_agents", {}) == "pre_onboard"
    assert get_call_identity_requirement("search_knowledge_graph", {"query": "x"}) == "pre_onboard"


# ---------------------------------------------------------------------------
# Classification pins (drift guards)
# ---------------------------------------------------------------------------


def test_write_actions_never_in_exemption_sets():
    """THE drift guard: no mutating action may ever appear in a
    pre_onboard_actions set. A future edit that adds one fails here
    before it opens an unbound write path.

    KNOWN LIMIT (council, PR #611): this guard reasons over action
    NAMES — an argument-gated write inside a read-named action is
    invisible to it. The one known instance, dialectic(get,
    check_timeout=true) (reviewer reassignment + transcript appends +
    phase flip), is closed by suppressing check_timeout for unbound
    callers in the handler itself — pinned by
    test_unbound_dialectic_get_ignores_check_timeout below. Any new
    pre_onboard action that reads a mutation-triggering argument needs
    the same handler-level suppression + pin."""
    writes = {
        "knowledge": {"store", "update", "note", "cleanup", "synthesize", "supersede", "audit"},
        "agent": {"update", "archive", "resume", "delete"},
        "calibration": {"update", "backfill", "rebuild"},
        "config": {"set"},
        "dialectic": {"quick", "request", "thesis", "antithesis", "synthesis", "reassign"},
        "observe": {"telemetry", "audit_events"},  # operator surfaces, kept gated
    }
    for tool, write_set in writes.items():
        td = get_tool_definition(tool)
        assert td is not None, tool
        exempted = td.pre_onboard_actions or frozenset()
        leaked = exempted & write_set
        assert not leaked, f"{tool}: write/operator actions in exemption set: {sorted(leaked)}"


def test_exemption_sets_match_declared_inventory():
    expected = {
        "knowledge": {"search", "get", "list", "details", "stats"},
        "agent": {"list", "get"},
        "calibration": {"check"},
        "config": {"get"},
        "observe": {"agent", "compare", "similar", "anomalies", "aggregate"},
        "dialectic": {"get", "list"},
    }
    for tool, actions in expected.items():
        td = get_tool_definition(tool)
        assert td.pre_onboard_actions == frozenset(actions), tool


@pytest.mark.asyncio
async def test_unbound_dialectic_get_ignores_check_timeout():
    """The argument-gated write inside the pre_onboard read: an unbound
    caller passing check_timeout=true must get the plain read (fast
    path), never the janitorial mutation sweep."""
    from unittest.mock import AsyncMock
    from src.mcp_handlers.dialectic import handlers as dh

    fast = AsyncMock(return_value={"session_id": "s1", "phase": "thesis"})
    stuck = AsyncMock()
    with patch(
        "src.mcp_handlers.context.get_context_agent_id", return_value=None
    ), patch.object(dh, "load_session_as_dict", fast), patch.object(
        dh, "check_reviewer_stuck", stuck
    ):
        result = await dh.handle_get_dialectic_session(
            {"session_id": "s1", "check_timeout": True}
        )

    fast.assert_awaited_once()       # served as the read-only fast path
    stuck.assert_not_awaited()       # the mutation sweep never ran
    assert result


@pytest.mark.asyncio
async def test_bound_dialectic_get_keeps_check_timeout():
    """Bound callers (and the background reaper) keep the janitorial
    sweep — the suppression is unbound-only."""
    from unittest.mock import AsyncMock, MagicMock
    from src.mcp_handlers.dialectic import handlers as dh

    fast = AsyncMock()
    stuck = AsyncMock(return_value=False)
    # MagicMock, not AsyncMock: the handler calls session methods
    # synchronously (check_timeout(), transcript.append) — an AsyncMock
    # session leaks unawaited coroutines, which the repo's leak detector
    # rightly fails the run on.
    session = MagicMock()
    session.check_timeout = MagicMock(return_value=None)
    with patch(
        "src.mcp_handlers.context.get_context_agent_id",
        return_value="bound-agent",
    ), patch.object(dh, "load_session_as_dict", fast), patch.object(
        dh, "check_reviewer_stuck", stuck
    ), patch.object(dh, "load_session", AsyncMock(return_value=session)):
        await dh.handle_get_dialectic_session(
            {"session_id": "s1", "check_timeout": True}
        )

    fast.assert_not_awaited()        # slow path taken
    stuck.assert_awaited()           # the sweep ran for the bound caller


# ---------------------------------------------------------------------------
# Declaration validation
# ---------------------------------------------------------------------------


def test_action_router_rejects_unregistered_exemption():
    async def _h(arguments):
        return []

    with pytest.raises(ValueError, match="unregistered actions"):
        action_router(
            "tmp_router_bad_exemption",
            actions={"read": _h},
            pre_onboard_actions={"read", "typo_action"},
        )


def test_mcp_tool_rejects_exemptions_on_pre_onboard_tool():
    with pytest.raises(ValueError, match="pre_onboard_actions only applies"):
        @mcp_tool(
            "tmp_tool_bad_combo",
            register=False,
            requires_identity="pre_onboard",
            pre_onboard_actions={"x"},
        )
        async def _h(arguments):
            return []


# ---------------------------------------------------------------------------
# Gate integration (REST gate, strict on)
# ---------------------------------------------------------------------------


@pytest.fixture
def strict_on(monkeypatch):
    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")


@pytest.fixture
def unbound_context():
    with patch(
        "src.mcp_handlers.context.get_context_agent_id", return_value=None
    ):
        yield


def test_rest_gate_passes_unbound_read_actions(strict_on, unbound_context):
    from src.services.http_tool_service import _strict_identity_refusal_or_none

    assert _strict_identity_refusal_or_none(
        "knowledge", {"action": "search", "query": "x"}
    ) is None
    assert _strict_identity_refusal_or_none("detect_anomalies", {}) is None
    assert _strict_identity_refusal_or_none("dialectic", {}) is None  # default list
    assert _strict_identity_refusal_or_none("check_calibration", {}) is None


def test_rest_gate_refuses_unbound_write_actions(strict_on, unbound_context):
    from src.services.http_tool_service import _strict_identity_refusal_or_none

    for tool, args in (
        ("knowledge", {"action": "store", "summary": "x"}),
        ("agent", {"action": "archive", "agent_id_target": "x"}),
        ("config", {"action": "set", "thresholds": {}}),
        ("dialectic", {"action": "synthesis", "session_id": "s"}),
    ):
        refusal = _strict_identity_refusal_or_none(tool, args)
        assert refusal is not None, (tool, args)
        assert refusal["status"] == "identity_required"

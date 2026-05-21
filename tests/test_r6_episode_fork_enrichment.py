"""R6 process_agent_update fork-kind enrichment tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.mcp_handlers.updates.context import UpdateContext
from src.mcp_handlers.updates.enrichments import _classify_fork, enrich_thread_identity


@pytest.mark.parametrize(
    (
        "position",
        "agent_uuid",
        "parent_uuid",
        "spawn_reason",
        "expected_kind",
        "expected_lineage",
    ),
    [
        (1, "agent-1", None, None, "none", False),
        (1, "agent-1", None, "new_session", "none", False),
        (2, "agent-1", None, None, "sibling_locus", False),
        (2, "agent-1", None, "new_session", "sibling_locus", False),
        (1, "child", "parent", "new_session", "identity_lineage", True),
        (1, "child", "parent", "subagent", "identity_lineage", True),
        (1, "child", "parent", "compaction", "identity_lineage", True),
        (1, "child", "parent", "resident_observer", "identity_lineage", True),
        (1, "agent-1", None, "dispatch_auto_mint", "none", False),
        (2, "lumen", "lumen", "explicit", "sibling_locus", False),
    ],
)
def test_classify_fork_r6_v2_cases(
    position,
    agent_uuid,
    parent_uuid,
    spawn_reason,
    expected_kind,
    expected_lineage,
):
    assert _classify_fork(position, agent_uuid, parent_uuid, spawn_reason) == (
        expected_kind,
        expected_lineage,
    )


def test_classify_fork_sync_race_fallback_warns(caplog):
    kind, lineage = _classify_fork(
        position=1,
        agent_uuid="child",
        parent_uuid=None,
        spawn_reason="subagent",
    )

    assert (kind, lineage) == ("identity_lineage", True)
    assert "[R6_SYNC_RACE]" in caplog.text


def _ctx_for_thread(
    *,
    position,
    agent_uuid="agent-1",
    parent_uuid=None,
    spawn_reason=None,
):
    return UpdateContext(
        agent_uuid=agent_uuid,
        meta=SimpleNamespace(
            agent_id=agent_uuid,
            agent_uuid=agent_uuid,
            thread_id="thread-r6",
            node_index=position,
            parent_agent_id=parent_uuid,
            spawn_reason=spawn_reason,
        ),
    )


def test_enrich_thread_identity_adds_r6_thin_shape_for_sibling_locus():
    ctx = _ctx_for_thread(position=2)

    enrich_thread_identity(ctx)

    thread_context = ctx.response_data["thread_context"]
    assert thread_context["thread_id"] == "thread-r6"
    assert thread_context["position"] == 2
    assert thread_context["is_fork"] is True
    assert thread_context["episode_fork_kind"] == "sibling_locus"
    assert thread_context["identity_lineage_fork"] is False
    assert thread_context["is_fork"] == (thread_context["episode_fork_kind"] != "none")
    assert "registry UUID" in thread_context["honest_message"]
    assert "whether you have integrated it is yours to demonstrate" in thread_context["honest_message"]


def test_enrich_thread_identity_adds_r6_thin_shape_for_identity_lineage():
    ctx = _ctx_for_thread(
        position=1,
        agent_uuid="child",
        parent_uuid="parent",
        spawn_reason="new_session",
    )

    enrich_thread_identity(ctx)

    thread_context = ctx.response_data["thread_context"]
    assert thread_context["is_fork"] is False
    assert thread_context["episode_fork_kind"] == "identity_lineage"
    assert thread_context["identity_lineage_fork"] is True
    # Legacy is_fork remains position-based for backward compatibility; identity
    # lineage at root position is now disambiguated by identity_lineage_fork.
    assert thread_context["is_fork"] is False
    assert "Lineage was declared" in thread_context["honest_message"]
    assert "R2's protocol" in thread_context["honest_message"]


def test_enrich_thread_identity_adds_r6_thin_shape_for_root():
    ctx = _ctx_for_thread(position=1)

    enrich_thread_identity(ctx)

    thread_context = ctx.response_data["thread_context"]
    assert thread_context["is_fork"] is False
    assert thread_context["episode_fork_kind"] == "none"
    assert thread_context["identity_lineage_fork"] is False
    assert thread_context["is_fork"] == (thread_context["episode_fork_kind"] != "none")
    assert thread_context["honest_message"] == "You are the first observation under this thread. No fork."


# Spec test plan items 10 + 11 from ``
# §"Test plan" — every R6 v2 case must (10) populate the thin thread_context
# shape (`thread_id`, `position`, `is_fork`, `episode_fork_kind`,
# `identity_lineage_fork`, `honest_message`) and (11) preserve the legacy
# `is_fork == (position > 1)` invariant. Spec §"Compatibility boundary" calls
# out that `is_fork` is purely position-based and is NOT equivalent to
# `episode_fork_kind != "none"` — the identity-lineage-at-root case is the
# canonical counterexample (case 3 below).
@pytest.mark.parametrize(
    (
        "spec_case",
        "position",
        "agent_uuid",
        "parent_uuid",
        "spawn_reason",
        "expected_kind",
        "expected_lineage",
    ),
    [
        ("1 root node", 1, "agent-1", None, None, "none", False),
        ("1b root new_session no parent", 1, "agent-1", None, "new_session", "none", False),
        ("2 sibling-locus (April 30 case)", 2, "agent-1", None, None, "sibling_locus", False),
        ("2b sibling-locus new_session no parent", 2, "agent-1", None, "new_session", "sibling_locus", False),
        ("3 identity-lineage via has_child_uuid", 1, "child", "parent", "new_session", "identity_lineage", True),
        ("4 subagent fork", 1, "child", "parent", "subagent", "identity_lineage", True),
        ("5 compaction fork", 1, "child", "parent", "compaction", "identity_lineage", True),
        ("7 dispatch_auto_mint no parent", 1, "agent-1", None, "dispatch_auto_mint", "none", False),
        ("8 resident_observer with has_child_uuid", 1, "child", "parent", "resident_observer", "identity_lineage", True),
        ("9 substrate-earned restart (Lumen)", 2, "lumen", "lumen", "explicit", "sibling_locus", False),
    ],
)
def test_enrich_thread_identity_thin_shape_and_is_fork_invariant(
    spec_case,
    position,
    agent_uuid,
    parent_uuid,
    spawn_reason,
    expected_kind,
    expected_lineage,
):
    """Spec test plan items 10 + 11. Case 6 (sync-race fallback) is covered
    separately by `test_classify_fork_sync_race_fallback_warns` because it
    asserts a side-effect (warning log) the parametrize harness can't see.
    """
    ctx = _ctx_for_thread(
        position=position,
        agent_uuid=agent_uuid,
        parent_uuid=parent_uuid,
        spawn_reason=spawn_reason,
    )

    enrich_thread_identity(ctx)

    tc = ctx.response_data["thread_context"]

    # Spec test 10 — thin shape contains all 6 keys.
    assert set(tc.keys()) == {
        "thread_id",
        "position",
        "is_fork",
        "episode_fork_kind",
        "identity_lineage_fork",
        "honest_message",
    }, f"unexpected thread_context keys for case {spec_case!r}: {set(tc.keys())}"

    # Preserved fields.
    assert tc["thread_id"] == "thread-r6"
    assert tc["position"] == position

    # R6 v2 fork classification.
    assert tc["episode_fork_kind"] == expected_kind, f"case {spec_case!r}"
    assert tc["identity_lineage_fork"] is expected_lineage, f"case {spec_case!r}"

    # Spec test 11 — `is_fork` remains purely position-based.
    assert tc["is_fork"] is (position > 1), (
        f"case {spec_case!r}: is_fork ({tc['is_fork']}) must equal position > 1 "
        f"({position > 1}); see spec §'Compatibility boundary'"
    )

    # Honest message is non-empty for every case (specific phrase asserted by
    # the per-kind tests above; here we only require content).
    assert isinstance(tc["honest_message"], str) and tc["honest_message"], (
        f"case {spec_case!r}: honest_message must be non-empty"
    )


# ─── S22 durable provenance persistence ─────────────────────────────
# R6 v2/v2.1 promoted episode_fork_kind + identity_lineage_fork to thread_context
# response shape, but the durable S22 envelope written to
# core.agent_state.state_json.provenance_context had 0/7 coverage on these
# fields (2026-05-08 audit on R6 H1/H3 + S22 H5 keys). These tests pin the
# server-side classification flowing into build_s22_write_context at
# prepare_unlocked_inputs so future regressions surface as test failures
# rather than silent dropouts.


def _make_phases_ctx(*, agent_uuid, meta, arguments):
    """Build an UpdateContext shaped like prepare_unlocked_inputs expects."""
    mcp_server = MagicMock()
    mcp_server.agent_metadata = {agent_uuid: meta}
    mcp_server.monitors = {}
    return UpdateContext(
        agent_id=agent_uuid,
        agent_uuid=agent_uuid,
        arguments=arguments,
        response_text=arguments.get("response_text", "test"),
        complexity=float(arguments.get("complexity", 0.0)),
        ethical_drift=[0.0, 0.0, 0.0],
        is_new_agent=False,
        meta=meta,
        loop=AsyncMock(),
        mcp_server=mcp_server,
    )


@pytest.mark.asyncio
async def test_prepare_unlocked_inputs_persists_identity_lineage_fork():
    """Server-classified identity_lineage at root must land in the durable
    provenance_context so KG and agent_state queries can answer fork questions
    later. Plan-row R6/S22 follow-up after the 2026-05-08 envelope audit."""
    from src.mcp_handlers.updates.phases import prepare_unlocked_inputs

    meta = SimpleNamespace(
        agent_id="child-uuid",
        agent_uuid="child-uuid",
        thread_id="thread-1",
        node_index=1,
        parent_agent_id="parent-uuid",
        spawn_reason="new_session",
    )
    ctx = _make_phases_ctx(
        agent_uuid="child-uuid",
        meta=meta,
        arguments={
            "harness_type": "hermes",
            "comparison_key": "r6-h3-test",
            "task_label": "fork persistence test",
        },
    )

    await prepare_unlocked_inputs(ctx)

    pc = ctx.agent_state.get("provenance_context")
    assert pc is not None, "provenance_context should be populated"
    assert pc["episode_fork_kind"] == "identity_lineage"
    assert pc["identity_lineage_fork"] is True


@pytest.mark.asyncio
async def test_prepare_unlocked_inputs_persists_sibling_locus_fork():
    """Sibling-locus restart (Lumen pattern) must also persist its fork kind."""
    from src.mcp_handlers.updates.phases import prepare_unlocked_inputs

    meta = SimpleNamespace(
        agent_id="lumen",
        agent_uuid="lumen",
        thread_id="lumen-thread",
        node_index=2,
        parent_agent_id="lumen",
        spawn_reason="explicit",
    )
    ctx = _make_phases_ctx(
        agent_uuid="lumen",
        meta=meta,
        arguments={"harness_type": "anima"},
    )

    await prepare_unlocked_inputs(ctx)

    pc = ctx.agent_state.get("provenance_context")
    assert pc is not None
    assert pc["episode_fork_kind"] == "sibling_locus"
    assert pc["identity_lineage_fork"] is False


def test_restamp_fork_after_thread_identity_update_uses_post_mutation_node_index():
    """Regression for the stale-node_index trap.

    ``prepare_unlocked_inputs`` classifies fork using whatever ``node_index`` is
    on ``ctx.meta`` at that moment, but ``execute_locked_update`` increments
    ``node_index`` on session-key transitions before the persist write. Without
    the re-stamp, the durable provenance would persist ``"none"`` while
    ``enrich_thread_identity`` (order=230, runs against the post-mutation value)
    would return ``"sibling_locus"`` in the response. This test pins that
    ``_restamp_fork_after_thread_identity_update`` reclassifies after the
    mutation so the persisted envelope agrees with the response.
    """
    from src.mcp_handlers.updates.phases import (
        _restamp_fork_after_thread_identity_update,
    )

    meta = SimpleNamespace(
        agent_id="agent-X",
        agent_uuid="agent-X",
        thread_id="thread-X",
        node_index=1,
        parent_agent_id=None,
        spawn_reason=None,
    )
    ctx = _make_phases_ctx(
        agent_uuid="agent-X",
        meta=meta,
        arguments={"harness_type": "hermes"},
    )
    # Simulate the early-stamp result from prepare_unlocked_inputs at node_index=1.
    ctx.agent_state = {
        "provenance_context": {
            "schema": "s22.write_context.v1",
            "context_source": "process_agent_update",
            "harness_type": "hermes",
            "episode_fork_kind": "none",
            "identity_lineage_fork": False,
        }
    }
    # Now simulate the execute_locked_update node_index mutation (line 857):
    # second-session-start increments node_index to 2.
    meta.node_index = 2

    _restamp_fork_after_thread_identity_update(ctx)

    pc = ctx.agent_state["provenance_context"]
    assert pc["episode_fork_kind"] == "sibling_locus", (
        "post-mutation node_index=2 must reclassify as sibling_locus; "
        "if this fails the durable envelope diverges from enrich_thread_identity"
    )
    assert pc["identity_lineage_fork"] is False


def test_restamp_fork_is_no_op_when_no_provenance_context():
    """If no S22 envelope was built (no signals, e.g. unbound transport), the
    re-stamp must not synthesize one — that would create noisy empty rows."""
    from src.mcp_handlers.updates.phases import (
        _restamp_fork_after_thread_identity_update,
    )

    meta = SimpleNamespace(
        agent_id="agent-Y",
        agent_uuid="agent-Y",
        thread_id="thread-Y",
        node_index=2,
        parent_agent_id=None,
        spawn_reason=None,
    )
    ctx = _make_phases_ctx(
        agent_uuid="agent-Y",
        meta=meta,
        arguments={},
    )
    ctx.agent_state = {}

    _restamp_fork_after_thread_identity_update(ctx)

    assert "provenance_context" not in ctx.agent_state


@pytest.mark.asyncio
async def test_prepare_unlocked_inputs_server_overrides_client_fork_claim():
    """A client-supplied identity_lineage_fork must not survive when server
    classification disagrees — fork-kind is server-authoritative provenance,
    not a client claim."""
    from src.mcp_handlers.updates.phases import prepare_unlocked_inputs

    meta = SimpleNamespace(
        agent_id="agent-1",
        agent_uuid="agent-1",
        thread_id="thread-2",
        node_index=1,
        parent_agent_id=None,
        spawn_reason=None,
    )
    ctx = _make_phases_ctx(
        agent_uuid="agent-1",
        meta=meta,
        arguments={
            "harness_type": "hermes",
            "identity_lineage_fork": "true",  # client claim — should be overridden
        },
    )

    await prepare_unlocked_inputs(ctx)

    pc = ctx.agent_state.get("provenance_context")
    assert pc is not None
    assert pc["episode_fork_kind"] == "none"
    assert pc["identity_lineage_fork"] is False


def test_enrich_thread_identity_identity_lineage_at_root_disambiguates_is_fork():
    """Spec §'Compatibility boundary' canonical counterexample: a fresh child
    UUID at position 1 is an identity-lineage fork by R6 ontology, but legacy
    `is_fork` stays False because position == 1. The new
    `identity_lineage_fork` scalar is what disambiguates."""
    ctx = _ctx_for_thread(
        position=1,
        agent_uuid="child",
        parent_uuid="parent",
        spawn_reason="new_session",
    )

    enrich_thread_identity(ctx)

    tc = ctx.response_data["thread_context"]
    assert tc["is_fork"] is False, "legacy is_fork is purely position-based"
    assert tc["identity_lineage_fork"] is True, (
        "identity_lineage_fork must surface the lineage event that legacy "
        "is_fork would miss"
    )
    assert tc["episode_fork_kind"] == "identity_lineage"

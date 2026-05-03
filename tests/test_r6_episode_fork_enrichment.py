"""R6 process_agent_update fork-kind enrichment tests."""

from types import SimpleNamespace

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
        (2, "agent-1", None, None, "sibling_locus", False),
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


# Spec test plan items 10 + 11 from `docs/ontology/r6-episode-fork-response-shape.md`
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
        ("2 sibling-locus (April 30 case)", 2, "agent-1", None, None, "sibling_locus", False),
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

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

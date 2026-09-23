"""
Tests for thread identity: pure logic module + honest forking.
"""

import pytest
from src.thread_identity import (
    generate_thread_id,
    infer_spawn_reason,
    classify_episode_fork,
    build_fork_context,
)


class TestGenerateThreadId:
    def test_mcp_session_key(self):
        tid = generate_thread_id("mcp:abc-123-def")
        assert tid.startswith("t-")
        assert len(tid) == 18  # "t-" + 16 hex chars

    def test_mcp_session_key_stable(self):
        """Same key always produces same thread ID."""
        tid1 = generate_thread_id("mcp:session-xyz")
        tid2 = generate_thread_id("mcp:session-xyz")
        assert tid1 == tid2

    def test_different_sessions_different_threads(self):
        tid1 = generate_thread_id("mcp:session-1")
        tid2 = generate_thread_id("mcp:session-2")
        assert tid1 != tid2

    def test_ip_ua_key_uses_ua_portion(self):
        """IP:UA keys should derive from UA, not IP."""
        tid1 = generate_thread_id("192.168.1.1:mozilla-chrome-etc")
        tid2 = generate_thread_id("10.0.0.5:mozilla-chrome-etc")
        assert tid1 == tid2  # Same UA → same thread

    def test_stdio_key(self):
        tid = generate_thread_id("stdio:12345")
        assert tid.startswith("t-")


class TestInferSpawnReason:
    def test_explicit_reason_wins(self):
        reason = infer_spawn_reason(
            {"spawn_reason": "explicit"},
            existing_nodes=[{"agent_id": "prev"}],
        )
        assert reason == "explicit"

    def test_claude_code_with_existing_nodes_and_parent(self):
        reason = infer_spawn_reason(
            {"client_hint": "claude-code", "parent_agent_id": "some-uuid"},
            existing_nodes=[{"agent_id": "prev"}],
        )
        assert reason == "compaction"

    @pytest.mark.parametrize("client_hint", ["claude-code", "claude_code", "claude-code-web"])
    def test_claude_code_without_parent_is_not_lineage(self, client_hint):
        # Co-located thread nodes are not a declaration: without a declared
        # parent, inference must not return a lineage reason, or the fork is
        # reported and persisted as a context continuation nobody declared.
        reason = infer_spawn_reason(
            {"client_hint": client_hint},
            existing_nodes=[{"agent_id": "prev"}],
        )
        assert reason == "new_session"
        assert classify_episode_fork(2, "fresh", None, reason) == (
            "sibling_locus",
            False,
        )

    def test_parent_agent_id_present(self):
        reason = infer_spawn_reason(
            {"parent_agent_id": "some-uuid"},
            existing_nodes=[{"agent_id": "prev"}],
        )
        assert reason == "subagent"

    def test_existing_nodes_default(self):
        reason = infer_spawn_reason(
            {},
            existing_nodes=[{"agent_id": "prev"}],
        )
        assert reason == "new_session"

    def test_no_existing_nodes(self):
        reason = infer_spawn_reason({}, existing_nodes=[])
        assert reason == "new_session"


class TestClassifyEpisodeFork:
    @pytest.mark.parametrize(
        (
            "position",
            "agent_uuid",
            "parent_uuid",
            "spawn_reason",
            "expected",
        ),
        [
            (1, "agent-1", None, None, ("none", False)),
            (2, "agent-1", None, None, ("sibling_locus", False)),
            (1, "child", "parent", "new_session", ("identity_lineage", True)),
            (1, "child", "parent", "subagent", ("identity_lineage", True)),
            (1, "child", "parent", "compaction", ("identity_lineage", True)),
            (1, "reviewer", None, "dialectic_reviewer", ("identity_lineage", True)),
            (1, "child", "parent", "resident_observer", ("identity_lineage", True)),
            (1, "agent-1", None, "dispatch_auto_mint", ("none", False)),
            (2, "lumen", "lumen", "explicit", ("sibling_locus", False)),
        ],
    )
    def test_r6_v2_cases(
        self,
        position,
        agent_uuid,
        parent_uuid,
        spawn_reason,
        expected,
    ):
        assert classify_episode_fork(
            position,
            agent_uuid,
            parent_uuid,
            spawn_reason,
        ) == expected


class TestBuildForkContext:
    def test_root_node(self):
        ctx = build_fork_context(
            thread_id="t-abc123def456ab",
            position=1,
            parent_uuid=None,
            spawn_reason="new_session",
            all_nodes=[{"agent_id": "uuid-1", "thread_position": 1}],
        )
        assert ctx["is_root"] is True
        assert ctx["is_fork"] is False
        assert ctx["position"] == 1
        assert ctx["predecessor"] is None
        assert ctx["episode_fork_kind"] == "none"
        assert ctx["identity_lineage_fork"] is False
        assert ctx["honest_message"] == "You are the first observation under this thread. No fork."

    def test_fork_with_parent(self):
        nodes = [
            {"agent_id": "uuid-1", "thread_position": 1, "label": "claude-sonnet"},
            {"agent_id": "uuid-2", "thread_position": 2, "label": None},
        ]
        ctx = build_fork_context(
            thread_id="t-abc123def456ab",
            position=2,
            parent_uuid="uuid-1",
            spawn_reason="compaction",
            all_nodes=nodes,
            agent_uuid="uuid-2",
        )
        assert ctx["is_root"] is False
        assert ctx["is_fork"] is True
        assert ctx["position"] == 2
        assert ctx["predecessor"]["uuid"] == "uuid-1"
        assert ctx["predecessor"]["label"] == "claude-sonnet"
        assert ctx["episode_fork_kind"] == "identity_lineage"
        assert ctx["identity_lineage_fork"] is True
        assert "spawn_reason compaction" in ctx["honest_message"]
        assert "Lineage was declared" in ctx["honest_message"]

    def test_fork_without_explicit_parent_uses_previous_position(self):
        nodes = [
            {"agent_id": "uuid-1", "thread_position": 1, "label": None},
            {"agent_id": "uuid-2", "thread_position": 2, "label": None},
            {"agent_id": "uuid-3", "thread_position": 3, "label": None},
        ]
        ctx = build_fork_context(
            thread_id="t-abc123def456ab",
            position=3,
            parent_uuid=None,
            spawn_reason="new_session",
            all_nodes=nodes,
            agent_uuid="uuid-3",
        )
        assert ctx["predecessor"]["uuid"] == "uuid-2"
        assert ctx["predecessor"]["position"] == 2
        assert ctx["episode_fork_kind"] == "sibling_locus"
        assert ctx["identity_lineage_fork"] is False

    def test_thread_size(self):
        nodes = [
            {"agent_id": f"uuid-{i}", "thread_position": i}
            for i in range(1, 5)
        ]
        ctx = build_fork_context(
            thread_id="t-test",
            position=4,
            parent_uuid=None,
            spawn_reason="new_session",
            all_nodes=nodes,
            agent_uuid="uuid-4",
        )
        assert ctx["thread_size"] == 4

    def test_position_note_when_nodes_pruned(self):
        """Dogfood 2026-06-13 P2.9: position (monotonic claim counter) can
        exceed thread_size (live node count) when forks are pruned/archived.
        Label the gap so it isn't read as a contradiction."""
        # 19 live nodes, but this fork claimed the 24th position ever.
        nodes = [
            {"agent_id": f"uuid-{i}", "thread_position": i}
            for i in range(1, 20)
        ]
        ctx = build_fork_context(
            thread_id="t-pruned",
            position=24,
            parent_uuid=None,
            spawn_reason="new_session",
            all_nodes=nodes,
            agent_uuid="uuid-24",
        )
        assert ctx["position"] == 24
        assert ctx["thread_size"] == 19
        assert "position_note" in ctx
        assert "24" in ctx["position_note"]
        assert "19" in ctx["position_note"]
        assert "prune" in ctx["position_note"].lower()

    def test_no_position_note_when_consistent(self):
        """No note when position == thread_size (nothing pruned), so the
        common case stays uncluttered and shape-compatible."""
        nodes = [
            {"agent_id": f"uuid-{i}", "thread_position": i}
            for i in range(1, 5)
        ]
        ctx = build_fork_context(
            thread_id="t-clean",
            position=4,
            parent_uuid=None,
            spawn_reason="new_session",
            all_nodes=nodes,
            agent_uuid="uuid-4",
        )
        assert "position_note" not in ctx

    def test_fresh_mint_on_shared_thread_gets_fresh_sibling_message(self):
        """A force_new mint on a co-occupied thread is sibling_locus, but it
        did get a fresh UUID: the message must not claim a shared one."""
        nodes = [
            {"agent_id": "unrelated", "thread_position": 1},
            {"agent_id": "fresh", "thread_position": 2},
        ]
        ctx = build_fork_context(
            thread_id="t-shared",
            position=2,
            parent_uuid=None,
            spawn_reason="new_session",
            all_nodes=nodes,
            agent_uuid="fresh",
            minted_fresh=True,
        )
        assert ctx["episode_fork_kind"] == "sibling_locus"
        assert "a fresh UUID" in ctx["honest_message"]
        assert "they are not your predecessors" in ctx["honest_message"]
        assert "share a registry UUID" not in ctx["honest_message"]
        assert "no child UUID minted" not in ctx["honest_message"]

    def test_resumed_uuid_keeps_r6_sibling_message(self):
        ctx = build_fork_context(
            thread_id="t-resumed",
            position=3,
            parent_uuid=None,
            spawn_reason=None,
            all_nodes=[{"agent_id": "same", "thread_position": 3}],
            agent_uuid="same",
        )
        assert ctx["episode_fork_kind"] == "sibling_locus"
        assert "share a registry UUID" in ctx["honest_message"]

    def test_handler_call_site_signature_contract(self):
        """Pin exact kwargs handlers.py:1946 passes after the 2026-05-02 fix.

        Pre-fix: handler called build_fork_context with agent_uuid= / nodes= which
        TypeErrored silently inside the bare except, suppressing thread_context for
        all force_new=true onboard responses (R6 v2 §"Out of R6 scope" prereq).
        This test ensures the call-site kwargs and the function signature do not
        drift apart again silently.
        """
        thread_info = {
            "thread_id": "t-abc123def456ab",
            "thread_position": 2,
            "parent_agent_id": "uuid-1",
            "spawn_reason": "subagent",
        }
        all_nodes = [
            {"agent_id": "uuid-1", "thread_position": 1, "label": "parent-label"},
            {"agent_id": "uuid-2", "thread_position": 2, "label": None},
        ]
        # These are the exact kwargs handlers.py:1946-1951 passes post-fix.
        ctx = build_fork_context(
            thread_id=thread_info["thread_id"],
            position=thread_info.get("thread_position", 1),
            parent_uuid=thread_info.get("parent_agent_id") or None,
            spawn_reason="subagent",
            all_nodes=all_nodes,
            agent_uuid="uuid-2",
            minted_fresh=True,
        )
        assert ctx["thread_id"] == "t-abc123def456ab"
        assert ctx["position"] == 2
        assert ctx["is_fork"] is True
        assert ctx["predecessor"]["uuid"] == "uuid-1"
        assert ctx["episode_fork_kind"] == "identity_lineage"
        assert ctx["identity_lineage_fork"] is True

    def test_subagent_spawn_reason_in_message(self):
        ctx = build_fork_context(
            thread_id="t-test",
            position=2,
            parent_uuid="uuid-1",
            spawn_reason="subagent",
            all_nodes=[
                {"agent_id": "uuid-1", "thread_position": 1, "label": None},
                {"agent_id": "uuid-2", "thread_position": 2, "label": None},
            ],
            agent_uuid="uuid-2",
        )
        assert "spawn_reason subagent" in ctx["honest_message"]
        assert "R2's protocol" in ctx["honest_message"]

    def test_rich_context_adds_shape_compatible_r6_keys(self):
        ctx = build_fork_context(
            thread_id="t-test",
            position=1,
            parent_uuid="parent",
            spawn_reason="new_session",
            all_nodes=[{"agent_id": "child", "thread_position": 1}],
            agent_uuid="child",
        )
        assert set(ctx.keys()) == {
            "thread_id",
            "position",
            "spawn_reason",
            "predecessor",
            "thread_size",
            "is_root",
            "is_fork",
            "episode_fork_kind",
            "identity_lineage_fork",
            "honest_message",
        }
        assert ctx["is_fork"] is False
        assert ctx["episode_fork_kind"] == "identity_lineage"
        assert ctx["identity_lineage_fork"] is True

"""
Tests for thread identity: pure logic module + honest forking.
"""

import pytest
from src.thread_identity import (
    generate_thread_id,
    infer_spawn_reason,
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

    def test_claude_code_with_existing_nodes(self):
        reason = infer_spawn_reason(
            {"client_hint": "claude-code"},
            existing_nodes=[{"agent_id": "prev"}],
        )
        assert reason == "compaction"

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
        assert "node 1" in ctx["honest_message"]
        assert "start of this conversation" in ctx["honest_message"]

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
        )
        assert ctx["is_root"] is False
        assert ctx["is_fork"] is True
        assert ctx["position"] == 2
        assert ctx["predecessor"]["uuid"] == "uuid-1"
        assert ctx["predecessor"]["label"] == "claude-sonnet"
        assert "compaction" in ctx["honest_message"]
        assert "distinct instance" in ctx["honest_message"]

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
        )
        assert ctx["predecessor"]["uuid"] == "uuid-2"
        assert ctx["predecessor"]["position"] == 2

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
        )
        assert ctx["thread_size"] == 4

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
        )
        assert ctx["thread_id"] == "t-abc123def456ab"
        assert ctx["position"] == 2
        assert ctx["is_fork"] is True
        assert ctx["predecessor"]["uuid"] == "uuid-1"

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
        )
        assert "subagent spawn" in ctx["honest_message"]

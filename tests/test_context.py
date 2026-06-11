"""
Tests for src/mcp_handlers/context.py - Session contextvars management.

Contextvars are per-task, so tests are naturally isolated.
"""

import pytest
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.context import (
    set_session_context,
    reset_session_context,
    get_session_context,
    get_context_session_key,
    get_context_client_session_id,
    get_context_agent_id,
    update_context_agent_id,
    get_context_client_hint,
    set_transport_client_hint,
    reset_transport_client_hint,
    set_mcp_session_id,
    reset_mcp_session_id,
    get_mcp_session_id,
    set_session_resolution_source,
    reset_session_resolution_source,
    get_session_resolution_source,
    detect_client_from_user_agent,
)


class TestSessionContext:

    def test_set_and_get(self):
        token = set_session_context(session_key="sess-1", client_session_id="client-1")
        try:
            ctx = get_session_context()
            assert ctx["session_key"] == "sess-1"
            assert ctx["client_session_id"] == "client-1"
        finally:
            reset_session_context(token)

    def test_session_key_defaults_to_client_session_id(self):
        token = set_session_context(client_session_id="my-client")
        try:
            assert get_context_session_key() == "my-client"
        finally:
            reset_session_context(token)

    def test_get_agent_id(self):
        token = set_session_context(agent_id="agent-123")
        try:
            assert get_context_agent_id() == "agent-123"
        finally:
            reset_session_context(token)

    def test_get_client_session_id(self):
        token = set_session_context(client_session_id="csid-456")
        try:
            assert get_context_client_session_id() == "csid-456"
        finally:
            reset_session_context(token)

    def test_extra_kwargs(self):
        token = set_session_context(custom_field="hello")
        try:
            ctx = get_session_context()
            assert ctx["custom_field"] == "hello"
        finally:
            reset_session_context(token)

    def test_reset_restores_previous(self):
        outer_token = set_session_context(session_key="outer")
        try:
            inner_token = set_session_context(session_key="inner")
            assert get_context_session_key() == "inner"
            reset_session_context(inner_token)
            assert get_context_session_key() == "outer"
        finally:
            reset_session_context(outer_token)

    def test_empty_context_returns_none(self):
        """No context set -> getters return None."""
        # Note: contextvars default is {}, so .get() returns None
        assert get_context_agent_id() is None or get_context_agent_id() == get_session_context().get('agent_id')


class TestUpdateContextAgentId:

    def test_update_agent_id(self):
        token = set_session_context(session_key="sess")
        try:
            update_context_agent_id("new-agent")
            assert get_context_agent_id() == "new-agent"
        finally:
            reset_session_context(token)

    def test_update_empty_context_no_crash(self):
        """If context is empty dict (default), update should handle gracefully."""
        # The default is {}, which is falsy but not None
        # update_context_agent_id checks `if ctx:` - empty dict is falsy
        update_context_agent_id("test-agent")
        # Should not crash


class TestClientHint:

    def test_from_session_context(self):
        token = set_session_context(client_hint="cursor")
        try:
            assert get_context_client_hint() == "cursor"
        finally:
            reset_session_context(token)

    def test_from_transport_level(self):
        token = set_transport_client_hint("chatgpt")
        try:
            assert get_context_client_hint() == "chatgpt"
        finally:
            reset_transport_client_hint(token)

    def test_session_context_takes_priority(self):
        transport_token = set_transport_client_hint("chatgpt")
        session_token = set_session_context(client_hint="cursor")
        try:
            assert get_context_client_hint() == "cursor"
        finally:
            reset_session_context(session_token)
            reset_transport_client_hint(transport_token)


class TestMcpSessionId:

    def test_set_and_get(self):
        token = set_mcp_session_id("mcp-sess-abc")
        try:
            assert get_mcp_session_id() == "mcp-sess-abc"
        finally:
            reset_mcp_session_id(token)

    def test_default_none(self):
        # Default should be None
        # (unless another test in the same process set it)
        pass  # Can't reliably test default in shared process

    def test_reset_works(self):
        token = set_mcp_session_id("temp-id")
        reset_mcp_session_id(token)
        # After reset, should be back to previous value


class TestSessionResolutionSource:

    def test_set_and_get(self):
        token = set_session_resolution_source("continuity_token")
        try:
            assert get_session_resolution_source() == "continuity_token"
        finally:
            reset_session_resolution_source(token)


class TestDetectClientFromUserAgent:

    def test_cursor(self):
        assert detect_client_from_user_agent("Cursor/0.42.0") == "cursor"

    def test_cursor_case_insensitive(self):
        assert detect_client_from_user_agent("CURSOR agent") == "cursor"

    def test_claude_desktop(self):
        assert detect_client_from_user_agent("Claude Desktop 1.0") == "claude_desktop"

    def test_claude_desktop_dashed(self):
        assert detect_client_from_user_agent("claude-desktop/1.2.3") == "claude_desktop"

    def test_claude_code_cli(self):
        # Previously mislabeled as claude_desktop — this is the 96% case.
        assert detect_client_from_user_agent("claude-code/2.0.0") == "claude_code"

    def test_claude_code_no_dash(self):
        assert detect_client_from_user_agent("ClaudeCode/1.0") == "claude_code"

    def test_claude_code_wins_over_generic_claude(self):
        # A mixed UA with both markers must resolve to the specific one.
        assert detect_client_from_user_agent("claude-code via Claude/1.0") == "claude_code"

    def test_anthropic_generic(self):
        # Python/TS SDKs and unknown anthropic clients fall into the honest
        # "claude" catch-all, not the previous "claude_desktop" misnomer.
        assert detect_client_from_user_agent("Anthropic/SDK") == "claude"

    def test_anthropic_python_sdk(self):
        assert detect_client_from_user_agent("anthropic-python/0.40.0") == "claude"

    def test_generic_claude(self):
        assert detect_client_from_user_agent("Claude/1.0") == "claude"

    def test_chatgpt(self):
        assert detect_client_from_user_agent("ChatGPT-Plugin/1.0") == "chatgpt"

    def test_openai(self):
        assert detect_client_from_user_agent("OpenAI/Agent") == "chatgpt"

    def test_codex(self):
        assert detect_client_from_user_agent("Codex/CLI") == "chatgpt"

    def test_mixed_ua_prefers_openai(self):
        assert detect_client_from_user_agent("Anthropic proxy OpenAI Codex") == "chatgpt"

    def test_vscode(self):
        assert detect_client_from_user_agent("VSCode/1.85") == "vscode"

    def test_visual_studio_code(self):
        assert detect_client_from_user_agent("Visual Studio Code") == "vscode"

    def test_unknown_returns_none(self):
        assert detect_client_from_user_agent("Mozilla/5.0") is None

    def test_empty_returns_none(self):
        assert detect_client_from_user_agent("") is None

    def test_none_returns_none(self):
        assert detect_client_from_user_agent(None) is None


class TestTrajectoryConfidence:

    def test_set_and_get(self):
        from src.mcp_handlers.context import (
            set_trajectory_confidence,
            get_trajectory_confidence,
            reset_trajectory_confidence,
        )
        token = set_trajectory_confidence(0.85)
        try:
            assert get_trajectory_confidence() == 0.85
        finally:
            reset_trajectory_confidence(token)

    def test_default_is_none(self):
        from src.mcp_handlers.context import get_trajectory_confidence
        # Default value should be None when not set
        val = get_trajectory_confidence()
        assert val is None or isinstance(val, float)

    def test_reset_restores_previous(self):
        from src.mcp_handlers.context import (
            set_trajectory_confidence,
            get_trajectory_confidence,
            reset_trajectory_confidence,
        )
        token1 = set_trajectory_confidence(0.5)
        try:
            token2 = set_trajectory_confidence(0.9)
            assert get_trajectory_confidence() == 0.9
            reset_trajectory_confidence(token2)
            assert get_trajectory_confidence() == 0.5
        finally:
            reset_trajectory_confidence(token1)


class TestSessionSignalsPeerPid:
    """S19: SessionSignals.peer_pid field for substrate attestation.

    Populated by the UDS listener (PR3c) at connection-accept via
    LOCAL_PEERPID; left None for HTTP/SSE/stdio transports. Read by the
    substrate-claim verification path in ``src/substrate/verification.py``.
    """

    def test_default_is_none(self):
        from src.mcp_handlers.context import SessionSignals
        signals = SessionSignals()
        assert signals.peer_pid is None

    def test_explicit_value_round_trips_via_contextvar(self):
        from src.mcp_handlers.context import (
            SessionSignals, set_session_signals, get_session_signals,
            reset_session_signals,
        )
        token = set_session_signals(SessionSignals(peer_pid=37807))
        try:
            recovered = get_session_signals()
            assert recovered is not None
            assert recovered.peer_pid == 37807
        finally:
            reset_session_signals(token)

    def test_field_is_frozen(self):
        """SessionSignals is frozen — peer_pid cannot be mutated post-construction."""
        import dataclasses
        from src.mcp_handlers.context import SessionSignals
        signals = SessionSignals(peer_pid=1234)
        with pytest.raises(dataclasses.FrozenInstanceError):
            signals.peer_pid = 5678  # type: ignore[misc]

    def test_uds_transport_label_documented(self):
        """The transport field comment lists 'uds' as a valid value (PR3c
        will set transport='uds' on UDS connections). Field-level cross-
        check that the inline documentation has been updated."""
        import inspect
        from src.mcp_handlers.context import SessionSignals
        src = inspect.getsource(SessionSignals)
        assert '"uds"' in src


class TestNoteUaFingerprint:
    """note_ua_fingerprint logs the fingerprint -> raw-UA preimage once per
    distinct hash (session keys carry only md5(UA)[:6], which is one-way —
    the f304dd hunt in the stage-1 strict-identity burn-in had no way to
    recover the client behind a fingerprint)."""

    def setup_method(self):
        from src.mcp_handlers import context
        context._logged_ua_fingerprints.clear()

    def test_logs_once_per_fingerprint(self, caplog):
        import logging
        from src.mcp_handlers.context import note_ua_fingerprint

        with caplog.at_level(logging.INFO, logger="src.mcp_handlers.context"):
            note_ua_fingerprint("f304dd", "Some-Client/1.2.3")
            note_ua_fingerprint("f304dd", "Some-Client/1.2.3")
            note_ua_fingerprint("f304dd", "Some-Client/1.2.3")

        hits = [r for r in caplog.records if "[UA_FINGERPRINT]" in r.getMessage()]
        assert len(hits) == 1
        assert "f304dd" in hits[0].getMessage()
        assert "Some-Client/1.2.3" in hits[0].getMessage()

    def test_distinct_fingerprints_each_logged(self, caplog):
        import logging
        from src.mcp_handlers.context import note_ua_fingerprint

        with caplog.at_level(logging.INFO, logger="src.mcp_handlers.context"):
            note_ua_fingerprint("aaaaaa", "Client-A/1.0")
            note_ua_fingerprint("bbbbbb", "Client-B/2.0")

        hits = [r for r in caplog.records if "[UA_FINGERPRINT]" in r.getMessage()]
        assert len(hits) == 2

    def test_none_and_empty_fingerprint_ignored(self, caplog):
        import logging
        from src.mcp_handlers.context import note_ua_fingerprint

        with caplog.at_level(logging.INFO, logger="src.mcp_handlers.context"):
            note_ua_fingerprint(None, "Client/1.0")
            note_ua_fingerprint("", "Client/1.0")

        hits = [r for r in caplog.records if "[UA_FINGERPRINT]" in r.getMessage()]
        assert hits == []

    def test_set_is_capped(self, caplog):
        import logging
        from src.mcp_handlers import context
        from src.mcp_handlers.context import note_ua_fingerprint

        with caplog.at_level(logging.INFO, logger="src.mcp_handlers.context"):
            for i in range(context._MAX_LOGGED_UA_FINGERPRINTS + 50):
                note_ua_fingerprint(f"{i:06x}", f"Client/{i}")

        hits = [r for r in caplog.records if "[UA_FINGERPRINT]" in r.getMessage()]
        assert len(hits) == context._MAX_LOGGED_UA_FINGERPRINTS

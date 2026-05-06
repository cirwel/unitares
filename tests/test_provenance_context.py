from types import SimpleNamespace

from src.mcp_handlers.context import (
    SessionSignals,
    reset_session_signals,
    set_session_signals,
)
from src.provenance_context import build_s22_write_context


def test_build_s22_write_context_prefers_explicit_values():
    context = build_s22_write_context(
        {
            "harness": "codex-cli",
            "harness_id": "codex-local-1",
            "transport": "mcp-stdio",
            "model_provider": "openai",
            "model": "gpt-5.5",
            "tool_surface": "terminal, mcp:unitares, terminal",
            "memory_context": "repo+kg",
            "locus": {"workspace": "/repo"},
            "affordance_state": {"shell": True},
            "identity_lineage_fork": "true",
            "comparison_key": "h5-bounded-task",
            "task_label": "H5 bounded task",
            "task_outcome": "passed",
            "episode_id": "episode-1",
            "invocation_id": "run-1",
            "process_instance_id": "opaque-process",
        },
        meta=SimpleNamespace(
            parent_agent_id="parent-uuid",
            spawn_reason="new_session",
            thread_id="thread-uuid",
        ),
        context_source="knowledge.store",
        default_governance_mode="explicit",
    )

    assert context["schema"] == "s22.write_context.v1"
    assert context["context_source"] == "knowledge.store"
    assert context["harness_type"] == "codex-cli"
    assert context["harness_id"] == "codex-local-1"
    assert context["transport"] == "mcp-stdio"
    assert context["model_provider"] == "openai"
    assert context["model"] == "gpt-5.5"
    assert context["tool_surface"] == ["terminal", "mcp:unitares"]
    assert context["memory_context"] == "repo+kg"
    assert context["parent_agent_id"] == "parent-uuid"
    assert context["spawn_reason"] == "new_session"
    assert context["thread_id"] == "thread-uuid"
    assert context["identity_lineage_fork"] is True
    assert context["comparison_key"] == "h5-bounded-task"
    assert context["task_label"] == "H5 bounded task"
    assert context["task_outcome"] == "passed"
    assert context["governance_mode"] == "explicit"


def test_build_s22_write_context_uses_transport_context_defaults():
    token = set_session_signals(
        SessionSignals(transport="mcp", client_hint="claude_code")
    )
    try:
        context = build_s22_write_context(
            {},
            context_source="knowledge.store",
        )
    finally:
        reset_session_signals(token)

    assert context["transport"] == "mcp"
    assert context["harness_type"] == "claude_code"


def test_build_s22_write_context_empty_without_signals_or_explicit_fields():
    context = build_s22_write_context(
        {},
        context_source="knowledge.store",
        default_governance_mode="explicit",
    )

    assert context == {}

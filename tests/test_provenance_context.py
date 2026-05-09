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


def test_build_s22_write_context_persists_server_classified_fork_fields():
    """R6 v2 fork discriminators must land in the durable provenance_context.

    Plan-row R6/S22 follow-up: response-shape promotion shipped 2026-05-02/05
    via thread_context, but the persisted S22 envelope had 0/7 coverage on
    these fields per the 2026-05-08 envelope audit. Server-side classification
    is authoritative; explicit kwargs override any client-supplied value.
    """
    context = build_s22_write_context(
        {
            "harness_type": "hermes",
            "identity_lineage_fork": "false",  # client claim — server must override
        },
        context_source="process_agent_update",
        episode_fork_kind="identity_lineage",
        identity_lineage_fork=True,
    )

    assert context["episode_fork_kind"] == "identity_lineage"
    assert context["identity_lineage_fork"] is True


def test_build_s22_write_context_kwargs_optional_when_no_fork_classified():
    """When the call site has no meta (e.g. early process_agent_update path with
    no agent_metadata entry yet), classification is skipped and the kwargs are
    omitted — the helper must not stamp ``None`` values into the envelope."""
    context = build_s22_write_context(
        {"harness_type": "hermes"},
        context_source="process_agent_update",
    )

    assert "episode_fork_kind" not in context
    assert "identity_lineage_fork" not in context

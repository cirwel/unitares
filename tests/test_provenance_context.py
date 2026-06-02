from types import SimpleNamespace

from src.mcp_handlers.context import (
    SessionSignals,
    reset_session_signals,
    set_session_signals,
)
from src.provenance_context import (
    build_s22_write_context,
    recover_mangled_s22_provenance,
)


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


def test_build_s22_write_context_reads_public_provenance_context_object():
    """LLM-facing callers need one visible slot for S22 situating metadata."""
    context = build_s22_write_context(
        {
            "comparison_key": "top-level-wins",
            "provenance_context": {
                "harness_type": "hermes",
                "model_provider": "openai-codex",
                "model": "gpt-5.5",
                "transport": "hermes-one-shot",
                "tool_surface": ["mcp:unitares", "terminal", "mcp:unitares"],
                "comparison_key": "nested-loses",
                "locus": {"profile": "default"},
            },
        },
        context_source="process_agent_update",
        default_governance_mode="explicit",
    )

    assert context["harness_type"] == "hermes"
    assert context["model_provider"] == "openai-codex"
    assert context["model"] == "gpt-5.5"
    assert context["transport"] == "hermes-one-shot"
    assert context["tool_surface"] == ["mcp:unitares", "terminal"]
    assert context["comparison_key"] == "top-level-wins"
    assert context["locus"] == {"profile": "default"}


def test_build_s22_write_context_top_level_alias_family_beats_public_context():
    context = build_s22_write_context(
        {
            "harness": "top-harness",
            "model_type": "top-model",
            "task": "top-task",
            "outcome": "top-outcome",
            "tool_surface": "top-tool",
            "provenance_context": {
                "harness_type": "nested-harness",
                "model": "nested-model",
                "task_label": "nested-task",
                "task_outcome": "nested-outcome",
                "tool_surface": ["nested-tool"],
            },
        },
        context_source="process_agent_update",
    )

    assert context["harness_type"] == "top-harness"
    assert context["model"] == "top-model"
    assert context["task_label"] == "top-task"
    assert context["task_outcome"] == "top-outcome"
    assert context["tool_surface"] == ["top-tool"]


def test_recover_mangled_s22_provenance_lifts_recent_tool_result_metadata():
    arguments = {
        "response_text": "recording H7/H8 evidence",
        "recent_tool_results": [
            {
                "harness_type": "hermes",
                "model_provider": "openai-codex",
                "model": "gpt-5.5",
                "transport": "hermes-one-shot",
                "tool_surface": ["mcp:unitares", "terminal"],
                "comparison_key": "r6-h7-2026-06-01",
                "task_label": "H7 tool-surface perturbation",
                "task_outcome": "tool-surface-contrast-entry",
            },
            {
                "tool": "terminal",
                "summary": "diagnostic command passed",
                "kind": "command",
                "transport": "should-not-stay-on-evidence",
            },
        ],
    }

    warnings = recover_mangled_s22_provenance(arguments)
    context = build_s22_write_context(
        arguments,
        context_source="process_agent_update",
    )

    assert warnings == [
        "recovered_mangled_provenance: lifted S22 provenance fields out of recent_tool_results"
    ]
    assert context["harness_type"] == "hermes"
    assert context["model_provider"] == "openai-codex"
    assert context["model"] == "gpt-5.5"
    assert context["transport"] == "hermes-one-shot"
    assert context["tool_surface"] == ["mcp:unitares", "terminal"]
    assert context["comparison_key"] == "r6-h7-2026-06-01"
    assert context["task_label"] == "H7 tool-surface perturbation"
    assert context["task_outcome"] == "tool-surface-contrast-entry"
    assert arguments["_recovered_s22_context"]["harness_type"] == "hermes"
    assert "harness_type" not in arguments
    assert arguments["recent_tool_results"] == [
        {"tool": "terminal", "summary": "diagnostic command passed", "kind": "command"}
    ]


def test_recover_mangled_s22_provenance_preserves_explicit_public_context():
    arguments = {
        "provenance_context": {
            "transport": "explicit-rest",
            "harness": "explicit-hermes",
        },
        "recent_tool_results": [
            {
                "transport": "mangled-one-shot",
                "harness_type": "mangled-hermes",
                "comparison_key": "r6-h8-2026-06-01",
            }
        ],
    }

    recover_mangled_s22_provenance(arguments)
    context = build_s22_write_context(
        arguments,
        context_source="process_agent_update",
    )

    assert context["transport"] == "explicit-rest"
    assert context["harness_type"] == "explicit-hermes"
    assert context["comparison_key"] == "r6-h8-2026-06-01"


def test_recover_mangled_s22_provenance_preserves_top_level_alias_family():
    arguments = {
        "harness": "explicit-harness",
        "model_type": "explicit-model",
        "task": "explicit-task",
        "outcome": "explicit-outcome",
        "recent_tool_results": [
            {
                "harness_type": "mangled-harness",
                "model": "mangled-model",
                "task_label": "mangled-task",
                "task_outcome": "mangled-outcome",
                "comparison_key": "r6-h8-2026-06-01",
            }
        ],
    }

    recover_mangled_s22_provenance(arguments)
    context = build_s22_write_context(
        arguments,
        context_source="process_agent_update",
    )

    assert context["harness_type"] == "explicit-harness"
    assert context["model"] == "explicit-model"
    assert context["task_label"] == "explicit-task"
    assert context["task_outcome"] == "explicit-outcome"
    assert context["comparison_key"] == "r6-h8-2026-06-01"


def test_recover_mangled_s22_provenance_does_not_promote_operational_fields():
    arguments = {
        "recent_tool_results": [
            {
                "provenance_context": {
                    "harness_type": "hermes",
                    "comparison_key": "r6-h8-2026-06-01",
                    "confidence": 1.0,
                    "agent_id": "evil-label",
                    "require_strong_identity": False,
                }
            }
        ],
    }

    recover_mangled_s22_provenance(arguments)
    context = build_s22_write_context(
        arguments,
        context_source="process_agent_update",
    )

    assert context["harness_type"] == "hermes"
    assert context["comparison_key"] == "r6-h8-2026-06-01"
    assert "confidence" not in arguments
    assert "agent_id" not in arguments
    assert "require_strong_identity" not in arguments
    assert "confidence" not in arguments["_recovered_s22_context"]
    assert "agent_id" not in arguments["_recovered_s22_context"]
    assert "require_strong_identity" not in arguments["_recovered_s22_context"]


def test_recover_mangled_s22_provenance_does_not_override_server_meta():
    from types import SimpleNamespace

    arguments = {
        "recent_tool_results": [
            {
                "provenance_context": {
                    "parent_agent_id": "mangled-parent",
                    "spawn_reason": "mangled-spawn",
                    "thread_id": "mangled-thread",
                    "comparison_key": "r6-h8-2026-06-01",
                }
            }
        ],
    }
    meta = SimpleNamespace(
        parent_agent_id="server-parent",
        spawn_reason="server-spawn",
        thread_id="server-thread",
    )

    recover_mangled_s22_provenance(arguments)
    context = build_s22_write_context(
        arguments,
        meta=meta,
        context_source="process_agent_update",
    )

    assert context["parent_agent_id"] == "server-parent"
    assert context["spawn_reason"] == "server-spawn"
    assert context["thread_id"] == "server-thread"
    assert context["comparison_key"] == "r6-h8-2026-06-01"


def test_process_agent_update_schema_exposes_public_provenance_context_slot():
    from src.mcp_handlers.schemas.core import ProcessAgentUpdateParams

    schema = ProcessAgentUpdateParams.model_json_schema()

    assert "provenance_context" in schema["properties"]
    assert schema["properties"]["provenance_context"]["type"] == "object"


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

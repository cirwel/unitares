from __future__ import annotations

from datetime import date, datetime, timezone

from src.identity.r6_dogfood import (
    assess_r6_dogfood_entries,
    build_r6_dogfood_payloads,
    default_r6_comparison_key,
)


def _row(
    *,
    agent_id: str,
    model: str = "gpt-5.5",
    comparison_key: str = "r6-h1-2026-05-06",
    memory_context: str = "same-hermes-memory",
    transport: str = "hermes-cli",
    tool_surface: list[str] | None = None,
):
    tool_surface = tool_surface or ["mcp:unitares", "hermes"]
    return {
        "entry_id": f"{agent_id}-{model}-{transport}",
        "source": "agent_state",
        "agent_id": agent_id,
        "recorded_at": datetime(2026, 5, 6, tzinfo=timezone.utc),
        "s22_context": {
            "schema": "s22.write_context.v1",
            "context_source": "process_agent_update",
            "harness_type": "hermes",
            "transport": transport,
            "model_provider": "openai",
            "model": model,
            "memory_context": memory_context,
            "comparison_key": comparison_key,
            "task_label": "R6 dogfood",
            "tool_surface": tool_surface,
        },
    }


def test_default_r6_comparison_key_uses_experiment_and_date():
    assert (
        default_r6_comparison_key("h1", today=date(2026, 5, 6))
        == "r6-h1-2026-05-06"
    )
    assert (
        default_r6_comparison_key("h8", today=date(2026, 5, 23))
        == "r6-h8-2026-05-23"
    )


def test_build_h1_payloads_include_two_model_entries_and_note():
    payloads = build_r6_dogfood_payloads(
        "h1",
        comparison_key="r6-h1-2026-05-06",
        model="gpt-5.5",
        variant_model="gpt-5.4",
    )

    assert [payload["name"] for payload in payloads] == [
        "process_agent_update",
        "process_agent_update",
        "store_knowledge_graph",
    ]
    models = [
        payload["arguments"]["model"]
        for payload in payloads
        if payload["name"] == "process_agent_update"
    ]
    assert models == ["gpt-5.5", "gpt-5.4"]
    for payload in payloads:
        args = payload["arguments"]
        assert args["harness_type"] == "hermes"
        assert args["comparison_key"] == "r6-h1-2026-05-06"
        assert args["memory_context"] == "same-hermes-memory"


def test_build_h3_payloads_include_baseline_then_force_new_onboard():
    payloads = build_r6_dogfood_payloads(
        "h3",
        comparison_key="r6-h3-2026-05-06",
        parent_agent_id="parent-uuid",
    )

    assert payloads[0]["name"] == "process_agent_update"
    assert payloads[0]["arguments"]["task_outcome"] == "pre-fresh-uuid-entry"
    assert payloads[1] == {
        "name": "onboard",
        "arguments": {
            "force_new": True,
            "resume": False,
            "client_hint": "hermes",
            "model_type": "gpt-5.5",
            "spawn_reason": "new_session",
            "parent_agent_id": "parent-uuid",
        },
    }
    assert payloads[2]["name"] == "process_agent_update"
    assert payloads[2]["arguments"]["comparison_key"] == "r6-h3-2026-05-06"
    assert payloads[3]["name"] == "store_knowledge_graph"


def test_build_h7_payloads_include_distinct_tool_surfaces_and_note():
    payloads = build_r6_dogfood_payloads(
        "h7",
        comparison_key="r6-h7-2026-05-23",
        baseline_tool_surface="mcp:unitares,hermes",
        variant_tool_surface="mcp:unitares,hermes,mcp:knowledge-graph",
    )

    assert [payload["name"] for payload in payloads] == [
        "process_agent_update",
        "process_agent_update",
        "store_knowledge_graph",
    ]
    baseline_args = payloads[0]["arguments"]
    variant_args = payloads[1]["arguments"]
    note_args = payloads[2]["arguments"]
    assert baseline_args["task_outcome"] == "baseline-tool-surface-entry"
    assert baseline_args["tool_surface"] == ["mcp:unitares", "hermes"]
    assert variant_args["task_outcome"] == "tool-surface-contrast-entry"
    assert variant_args["tool_surface"] == [
        "mcp:unitares",
        "hermes",
        "mcp:knowledge-graph",
    ]
    assert note_args["tool_surface"] == variant_args["tool_surface"]


def test_build_h8_payloads_include_interactive_and_noninteractive_transports():
    payloads = build_r6_dogfood_payloads(
        "h8",
        comparison_key="r6-h8-2026-05-23",
        variant_transport="hermes-cron",
    )

    assert [payload["name"] for payload in payloads] == [
        "process_agent_update",
        "process_agent_update",
        "store_knowledge_graph",
    ]
    assert payloads[0]["arguments"]["task_outcome"] == "interactive-transport-entry"
    assert payloads[0]["arguments"]["transport"] == "hermes-cli"
    assert payloads[1]["arguments"]["task_outcome"] == "transport-contrast-entry"
    assert payloads[1]["arguments"]["transport"] == "hermes-cron"
    assert payloads[2]["arguments"]["transport"] == "hermes-cron"


def test_assess_h1_complete_for_same_identity_distinct_models():
    assessment = assess_r6_dogfood_entries(
        [
            _row(agent_id="same-agent", model="gpt-5.5"),
            _row(agent_id="same-agent", model="gpt-5.4"),
        ],
        experiment_id="h1",
        comparison_key="r6-h1-2026-05-06",
    )

    assert assessment["decision"] == "complete"
    assert assessment["reason"] == "same_identity_distinct_models_observed"
    assert assessment["distinct_agent_ids"] == ["same-agent"]
    assert assessment["distinct_models"] == ["gpt-5.4", "gpt-5.5"]


def test_assess_h1_rejects_multiple_identities():
    assessment = assess_r6_dogfood_entries(
        [
            _row(agent_id="agent-a", model="gpt-5.5"),
            _row(agent_id="agent-b", model="gpt-5.4"),
        ],
        experiment_id="h1",
        comparison_key="r6-h1-2026-05-06",
    )

    assert assessment["decision"] == "incomplete"
    assert assessment["reason"] == "not_same_identity"


def test_assess_h3_complete_for_fresh_identity_same_memory():
    assessment = assess_r6_dogfood_entries(
        [
            _row(
                agent_id="agent-a",
                comparison_key="r6-h3-2026-05-06",
                memory_context="profile-alpha",
            ),
            _row(
                agent_id="agent-b",
                comparison_key="r6-h3-2026-05-06",
                memory_context="profile-alpha",
            ),
        ],
        experiment_id="h3",
        comparison_key="r6-h3-2026-05-06",
    )

    assert assessment["decision"] == "complete"
    assert assessment["reason"] == "fresh_identity_shared_memory_context_observed"
    assert assessment["distinct_agent_ids"] == ["agent-a", "agent-b"]
    assert assessment["distinct_memory_contexts"] == ["profile-alpha"]


def test_assess_h3_requires_shared_memory_context():
    assessment = assess_r6_dogfood_entries(
        [
            _row(
                agent_id="agent-a",
                comparison_key="r6-h3-2026-05-06",
                memory_context="profile-alpha",
            ),
            _row(
                agent_id="agent-b",
                comparison_key="r6-h3-2026-05-06",
                memory_context="profile-beta",
            ),
        ],
        experiment_id="h3",
        comparison_key="r6-h3-2026-05-06",
    )

    assert assessment["decision"] == "incomplete"
    assert assessment["reason"] == "memory_context_not_shared"


def test_assess_h7_complete_for_same_identity_distinct_tool_surfaces():
    assessment = assess_r6_dogfood_entries(
        [
            _row(
                agent_id="same-agent",
                comparison_key="r6-h7-2026-05-23",
                tool_surface=["mcp:unitares", "hermes"],
            ),
            _row(
                agent_id="same-agent",
                comparison_key="r6-h7-2026-05-23",
                tool_surface=["mcp:unitares", "hermes", "mcp:knowledge-graph"],
            ),
        ],
        experiment_id="h7",
        comparison_key="r6-h7-2026-05-23",
    )

    assert assessment["decision"] == "complete"
    assert assessment["reason"] == "same_identity_distinct_tool_surfaces_observed"
    assert assessment["distinct_agent_ids"] == ["same-agent"]
    assert assessment["distinct_tool_surfaces"] == [
        "mcp:unitares,hermes",
        "mcp:unitares,hermes,mcp:knowledge-graph",
    ]


def test_assess_h7_requires_distinct_tool_surfaces():
    assessment = assess_r6_dogfood_entries(
        [
            _row(agent_id="same-agent", comparison_key="r6-h7-2026-05-23"),
            _row(agent_id="same-agent", comparison_key="r6-h7-2026-05-23"),
        ],
        experiment_id="h7",
        comparison_key="r6-h7-2026-05-23",
    )

    assert assessment["decision"] == "incomplete"
    assert assessment["reason"] == "missing_distinct_tool_surface_entries"


def test_assess_h8_complete_for_same_identity_distinct_transports():
    assessment = assess_r6_dogfood_entries(
        [
            _row(
                agent_id="same-agent",
                comparison_key="r6-h8-2026-05-23",
                transport="hermes-cli",
            ),
            _row(
                agent_id="same-agent",
                comparison_key="r6-h8-2026-05-23",
                transport="hermes-gateway",
            ),
        ],
        experiment_id="h8",
        comparison_key="r6-h8-2026-05-23",
    )

    assert assessment["decision"] == "complete"
    assert (
        assessment["reason"]
        == "same_identity_interactive_noninteractive_transport_observed"
    )
    assert assessment["distinct_agent_ids"] == ["same-agent"]
    assert assessment["distinct_transports"] == ["hermes-cli", "hermes-gateway"]


def test_assess_h8_requires_noninteractive_transport():
    assessment = assess_r6_dogfood_entries(
        [
            _row(
                agent_id="same-agent",
                comparison_key="r6-h8-2026-05-23",
                transport="hermes-cli",
            ),
            _row(
                agent_id="same-agent",
                comparison_key="r6-h8-2026-05-23",
                transport="interactive-terminal",
            ),
        ],
        experiment_id="h8",
        comparison_key="r6-h8-2026-05-23",
    )

    assert assessment["decision"] == "incomplete"
    assert assessment["reason"] == "missing_noninteractive_transport_entry"

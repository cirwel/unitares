"""Focused tests for the bounded KG agent-adoption pilot protocol."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.eval.kg_agent_adoption import (
    RESULT_SCHEMA,
    ProtocolError,
    analyze_results,
    build_counterbalanced_schedule,
    compute_net_utility,
    experiment_cells,
    lexical_search,
    normalize_prior_results,
    render_step_prompt,
    score_step,
    validate_task_chains,
)


REPO_ROOT = Path(__file__).parents[1]
TASKS_PATH = REPO_ROOT / "tests/kg_agent_adoption/task-chains-v0.json"


def _tasks():
    return json.loads(TASKS_PATH.read_text(encoding="utf-8"))


def _costs(**overrides):
    costs = {
        "latency_ms": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_failures": 0,
        "invalid_citations": 0,
        "regret": 0,
        "operator_interventions": 0,
    }
    costs.update(overrides)
    return costs


def _result_row(
    instance_id,
    *,
    cell_id,
    arm,
    backend,
    step_index,
    net_utility,
    tool_successes=0,
):
    surfaced = arm == "surfaced_then_withdrawn" and step_index == 0
    withdrawn = arm == "surfaced_then_withdrawn" and step_index > 0
    catalog = arm != "unavailable"
    return {
        "chain_instance_id": instance_id,
        "chain_id": "measurement-funnel",
        "family": "measurement-authority",
        "repetition": 1,
        "cell_id": cell_id,
        "arm": arm,
        "backend": backend,
        "step_index": step_index,
        "eligible": True,
        "catalog_exposed": catalog,
        "contextual_surface": surfaced,
        "reminder_withdrawn": withdrawn,
        "result_injected": False,
        "reachable": catalog,
        "recording_verified": catalog,
        "tool_invocations": tool_successes,
        "tool_successes": tool_successes,
        "material_use": bool(tool_successes),
        "quality": 1.0,
        "net_utility": net_utility,
        "costs_complete": True,
    }


def test_frozen_task_chains_validate_and_reject_duplicate_sources():
    tasks = _tasks()
    assert len(validate_task_chains(tasks)["chains"]) == 2

    duplicate = deepcopy(tasks)
    duplicate["substitute_corpus"][1]["source_id"] = duplicate["substitute_corpus"][0][
        "source_id"
    ]
    with pytest.raises(ProtocolError, match="duplicate substitute source_id"):
        validate_task_chains(duplicate)


def test_schedule_is_deterministic_counterbalanced_and_whole_chain():
    tasks = _tasks()
    first = build_counterbalanced_schedule(tasks, assignment_seed=19, repetitions=2)
    second = build_counterbalanced_schedule(tasks, assignment_seed=19, repetitions=2)

    assert first == second
    assert len(first) == 2 * 2 * 7
    for block_id in {row["block_id"] for row in first}:
        block = [row for row in first if row["block_id"] == block_id]
        assert len({row["cell_id"] for row in block}) == 7
        assert len({row["sample_seed"] for row in block}) == 1
        assert all(len(row["step_ids"]) == 3 for row in block)
        assert all(row["fresh_model_context_required"] for row in block)
        assert all(row["fresh_agent_identity_required"] for row in block)

    assert len(experiment_cells()) == 7


def test_surface_arms_keep_tool_contract_constant_and_withdraw_only_reminder():
    chain = _tasks()["chains"][0]
    unavailable = render_step_prompt(
        chain, 0, arm="unavailable", backend=None
    )
    passive = render_step_prompt(
        chain, 0, arm="passive", backend="unitares_kg"
    )
    substitute = render_step_prompt(
        chain, 0, arm="passive", backend="lexical_substitute"
    )
    surfaced = render_step_prompt(
        chain, 0, arm="surfaced_then_withdrawn", backend="unitares_kg"
    )
    withdrawn = render_step_prompt(
        chain, 1, arm="surfaced_then_withdrawn", backend="unitares_kg"
    )

    assert unavailable["tools"] == []
    assert "Relevant prior work may exist" not in passive["prompt"]
    assert passive["tools"] == substitute["tools"] == surfaced["tools"]
    assert "Relevant prior work may exist" in surfaced["prompt"]
    assert surfaced["exposure"]["contextual_surface"] is True
    assert "Relevant prior work may exist" not in withdrawn["prompt"]
    assert withdrawn["tools"] == surfaced["tools"]
    assert withdrawn["exposure"]["reminder_withdrawn"] is True


def test_injected_arm_requires_explicit_results_and_other_arms_reject_them():
    chain = _tasks()["chains"][0]
    result = {
        "source_id": "measurement-authority-contract",
        "title": "Measurement authority contract",
        "excerpt": "Separate surfaced, reachable, and recorded.",
        "score": 1.0,
    }

    injected = render_step_prompt(
        chain,
        0,
        arm="injected",
        backend="lexical_substitute",
        injected_results=[result],
    )
    assert injected["exposure"]["result_injected"] is True
    assert "measurement-authority-contract" in injected["prompt"]

    with pytest.raises(ProtocolError, match="requires explicit"):
        render_step_prompt(
            chain, 0, arm="injected", backend="lexical_substitute"
        )
    with pytest.raises(ProtocolError, match="must not receive"):
        render_step_prompt(
            chain,
            0,
            arm="passive",
            backend="lexical_substitute",
            injected_results=[result],
        )


def test_lexical_substitute_is_deterministic_and_normalizer_accepts_kg_envelope():
    tasks = _tasks()
    query = "zero usage surfaced reachable recorded capability retirement"
    first = lexical_search(tasks["substitute_corpus"], query)
    second = lexical_search(tasks["substitute_corpus"], query)

    assert first == second
    assert first[0]["source_id"] == "measurement-authority-contract"

    normalized = normalize_prior_results(
        {
            "raw_governance": {
                "discoveries": [
                    {
                        "discovery_id": "kg-123",
                        "summary": "A KG result",
                        "details": "Relevant details",
                        "relevance": "0.75",
                    }
                ]
            }
        }
    )
    assert normalized == [
        {
            "source_id": "kg-123",
            "title": "A KG result",
            "excerpt": "Relevant details",
            "score": 0.75,
        }
    ]


def test_step_score_requires_delivered_material_source_and_counts_invalid_citation():
    step = _tasks()["chains"][0]["steps"][0]
    response = {
        "answer": "VERIFY_FUNNEL",
        "source_ids": ["measurement-authority-contract", "not-delivered"],
        "reason": "Recorder state must be separated from usage.",
    }
    score = score_step(
        step,
        response,
        delivered_source_ids=["measurement-authority-contract"],
    )

    assert score["quality"] == 1.0
    assert score["material_use"] is True
    assert score["material_source_ids"] == ["measurement-authority-contract"]
    assert score["invalid_source_ids"] == ["not-delivered"]


def test_net_utility_subtracts_frozen_costs_and_never_imputes_missing_costs():
    weights = _costs(latency_ms=0.001, tool_failures=0.2)
    scored = compute_net_utility(
        1.0,
        _costs(latency_ms=100, tool_failures=1),
        weights,
    )
    assert scored["cost_penalty"] == 0.3
    assert scored["net_utility"] == 0.7

    incomplete_costs = _costs()
    incomplete_costs.pop("latency_ms")
    incomplete = compute_net_utility(1.0, incomplete_costs, weights)
    assert incomplete["costs_complete"] is False
    assert incomplete["net_utility"] is None

    with pytest.raises(ProtocolError, match="non-negative"):
        compute_net_utility(1.0, _costs(latency_ms=-1), weights)


def test_summary_uses_paired_chain_utility_and_labels_continuation_secondary():
    rows = []
    cells = (
        (
            "kg",
            "surfaced_then_withdrawn__unitares_kg",
            "surfaced_then_withdrawn",
            "unitares_kg",
            0.9,
        ),
        (
            "substitute",
            "surfaced_then_withdrawn__lexical_substitute",
            "surfaced_then_withdrawn",
            "lexical_substitute",
            0.6,
        ),
        ("unavailable", "unavailable", "unavailable", None, 0.4),
    )
    for instance_id, cid, arm, backend, utility in cells:
        for step_index in range(3):
            successes = 1 if instance_id == "kg" and step_index in {0, 2} else 0
            rows.append(
                _result_row(
                    instance_id,
                    cell_id=cid,
                    arm=arm,
                    backend=backend,
                    step_index=step_index,
                    net_utility=utility,
                    tool_successes=successes,
                )
            )

    summary = analyze_results(
        {
            "schema": RESULT_SCHEMA,
            "experiment_id": "pilot",
            "rows": rows,
        }
    )
    assert summary["primary_contrasts"][0]["mean_delta"] == 0.3
    assert summary["primary_contrasts"][1]["mean_delta"] == 0.5
    assert summary["post_withdrawal_retrieval"] == {
        "eligible_chain_instances": 1,
        "later_successful_retrieval": 1,
        "rate": 1.0,
        "interpretation": "secondary continuation telemetry; not causal by itself",
    }
    assert summary["claim_status"] == "pilot_descriptive_only"


def test_summary_rejects_task_chain_that_crosses_experiment_cells():
    rows = [
        _result_row(
            "crossed",
            cell_id="unavailable",
            arm="unavailable",
            backend=None,
            step_index=0,
            net_utility=0.5,
        ),
        _result_row(
            "crossed",
            cell_id="passive__unitares_kg",
            arm="passive",
            backend="unitares_kg",
            step_index=1,
            net_utility=0.5,
        ),
    ]
    with pytest.raises(ProtocolError, match="crossed experiment cells"):
        analyze_results({"schema": RESULT_SCHEMA, "rows": rows})

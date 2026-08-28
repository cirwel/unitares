"""Fail-closed runner tests for the bounded KG agent-adoption pilot."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.eval.kg_agent_adoption import ProtocolError
from scripts.eval.run_kg_agent_adoption import (
    PilotBlockedError,
    build_offline_fixture_receipt,
    build_schedule_report,
    main,
    refuse_scored_execution,
    sha256_file,
    validate_enrollment,
    validate_result_binding,
)


REPO_ROOT = Path(__file__).parents[1]
TASKS_PATH = REPO_ROOT / "tests/kg_agent_adoption/task-chains-v0.json"
SCHEMA_PATH = (
    REPO_ROOT / "docs/evaluations/kg-agent-adoption/enrollment-v0.schema.json"
)
ENROLLMENT_PATH = (
    REPO_ROOT / "docs/evaluations/kg-agent-adoption/enrollment-v0.example.json"
)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _complete_result():
    enrollment = _load(ENROLLMENT_PATH)
    tasks = _load(TASKS_PATH)
    schedule = build_schedule_report(enrollment, tasks)["schedule"]
    rows = []
    for scheduled in schedule:
        arm = scheduled["arm"]
        backend = scheduled["backend"]
        for step_index, _step_id in enumerate(scheduled["step_ids"]):
            available = arm != "unavailable"
            injected = arm == "injected"
            surfaced = arm == "surfaced_then_withdrawn" and step_index == 0
            withdrawn = arm == "surfaced_then_withdrawn" and step_index > 0
            tool_successes = 1 if available and not injected else 0
            rows.append(
                {
                    "chain_instance_id": (
                        f"{scheduled['block_id']}--{scheduled['cell_id']}"
                    ),
                    "chain_id": scheduled["chain_id"],
                    "family": scheduled["family"],
                    "repetition": scheduled["repetition"],
                    "cell_id": scheduled["cell_id"],
                    "arm": arm,
                    "backend": backend,
                    "step_index": step_index,
                    "eligible": True,
                    "catalog_exposed": available,
                    "contextual_surface": surfaced,
                    "reminder_withdrawn": withdrawn,
                    "result_injected": injected,
                    "reachable": available,
                    "recording_verified": True,
                    "tool_invocations": tool_successes,
                    "tool_successes": tool_successes,
                    "material_use": available,
                    "quality": 1.0,
                    "net_utility": 1.0,
                    "costs_complete": True,
                }
            )
    return {
        "schema": "unitares.kg-agent-adoption.result.v0",
        "experiment_id": enrollment["experiment_id"],
        "enrollment_sha256": sha256_file(ENROLLMENT_PATH),
        "task_manifest_sha256": sha256_file(TASKS_PATH),
        "schedule_sha256": enrollment["assignment"]["schedule_sha256"],
        "rows": rows,
    }


def test_enrollment_matches_schema_and_validates_against_frozen_fixture():
    jsonschema = pytest.importorskip("jsonschema")
    enrollment = _load(ENROLLMENT_PATH)
    tasks = _load(TASKS_PATH)

    jsonschema.Draft202012Validator(_load(SCHEMA_PATH)).validate(enrollment)
    validation = validate_enrollment(
        enrollment,
        tasks_path=TASKS_PATH,
        task_document=tasks,
    )

    assert validation["review_status"] == "unreviewed"
    assert validation["execution_authorized"] is False
    assert validation["chain_count"] == 2
    assert validation["schedule_rows"] == 14
    assert build_schedule_report(enrollment, tasks)["execution_authorized"] is False


def test_enrollment_rejects_fixture_drift_and_any_live_or_write_authority():
    enrollment = _load(ENROLLMENT_PATH)
    tasks = _load(TASKS_PATH)

    digest_drift = deepcopy(enrollment)
    digest_drift["task_manifest"]["sha256"] = "0" * 64
    with pytest.raises(ProtocolError, match="digest mismatch"):
        validate_enrollment(
            digest_drift,
            tasks_path=TASKS_PATH,
            task_document=tasks,
        )

    live = deepcopy(enrollment)
    live["model"]["live_calls_authorized"] = True
    with pytest.raises(ProtocolError, match="must be false"):
        validate_enrollment(live, tasks_path=TASKS_PATH, task_document=tasks)

    writable = deepcopy(enrollment)
    writable["backends"]["unitares_kg"]["writes_authorized"] = True
    with pytest.raises(ProtocolError, match="must remain unauthorized"):
        validate_enrollment(writable, tasks_path=TASKS_PATH, task_document=tasks)


def test_scored_execution_refuses_even_if_review_flags_are_locally_changed():
    enrollment = _load(ENROLLMENT_PATH)
    with pytest.raises(PilotBlockedError, match="not implemented or authorized"):
        refuse_scored_execution(enrollment)

    enrollment["review"] = {
        "status": "offline_fixture_reviewed",
        "scope": "offline_fixture_validation",
        "governed_review_session": "review-session",
        "approved_by": "operator",
    }
    with pytest.raises(PilotBlockedError, match="not implemented or authorized"):
        refuse_scored_execution(enrollment)


def test_cli_validates_and_schedules_without_authorizing_execution(capsys):
    assert main(["validate"]) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["valid"] is True
    assert validation["writes_performed"] is False
    assert validation["execution_authorized"] is False

    assert main(["schedule"]) == 0
    schedule = json.loads(capsys.readouterr().out)
    assert schedule["schedule_rows"] == 14
    assert schedule["execution_authorized"] is False


def test_cli_summarizes_existing_receipts_without_mutating_them(tmp_path, capsys):
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(_complete_result()), encoding="utf-8")

    assert main(["summarize", "--result", str(result_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["funnel"]["step_receipts"] == 42
    assert summary["claim_status"] == "pilot_descriptive_only"


def test_result_binding_rejects_partial_or_wrong_schedule_receipts():
    enrollment = _load(ENROLLMENT_PATH)
    tasks = _load(TASKS_PATH)
    partial = _complete_result()
    partial["rows"] = partial["rows"][:1]
    with pytest.raises(ProtocolError, match="complete task chain|incomplete"):
        validate_result_binding(
            partial,
            enrollment=enrollment,
            enrollment_path=ENROLLMENT_PATH,
            tasks=tasks,
            tasks_path=TASKS_PATH,
        )

    wrong_digest = _complete_result()
    wrong_digest["schedule_sha256"] = "0" * 64
    with pytest.raises(ProtocolError, match="schedule_sha256"):
        validate_result_binding(
            wrong_digest,
            enrollment=enrollment,
            enrollment_path=ENROLLMENT_PATH,
            tasks=tasks,
            tasks_path=TASKS_PATH,
        )


def test_offline_fixture_canary_is_deterministic_and_denies_live_claims():
    enrollment = _load(ENROLLMENT_PATH)
    tasks = _load(TASKS_PATH)
    kwargs = {
        "enrollment_path": ENROLLMENT_PATH,
        "tasks": tasks,
        "tasks_path": TASKS_PATH,
        "review_session": "review-hold",
    }

    first = build_offline_fixture_receipt(enrollment, **kwargs)
    second = build_offline_fixture_receipt(enrollment, **kwargs)

    assert first == second
    assert first["receipt_content_sha256"]
    content = first["content"]
    assert content["scope"] == "offline_fixture_validation"
    assert content["status"] == "passed"
    assert content["review_basis"]["verdict_at_execution"] == "hold"
    assert set(content["attempted_operations"].values()) == {0}
    assert content["claims"] == {
        "offline_fixture_passed": True,
        "behavioral_evidence": False,
        "live_kg_parity_proven": False,
        "production_plumbing_proven": False,
        "scored_execution_authorized": False,
        "live_execution_authorized": False,
    }


def test_cli_canary_atomically_writes_private_content_addressed_receipt(
    tmp_path, capsys
):
    receipt_path = tmp_path / "receipt.json"
    args = [
        "canary",
        "--review-session",
        "review-hold",
        "--receipt",
        str(receipt_path),
    ]
    assert main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == output
    assert (receipt_path.stat().st_mode & 0o777) == 0o600
    assert main(args) == 0
    capsys.readouterr()


def test_cli_run_command_fails_closed(capsys):
    assert main(["run"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not implemented or authorized" in captured.err


def test_runner_source_has_no_live_execution_or_production_write_primitive():
    source = (
        REPO_ROOT / "scripts/eval/run_kg_agent_adoption.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "append_audit_event",
        "audit.outcome_events",
        "requests.",
        "import psycopg",
        "from psycopg",
        "import asyncpg",
        "from asyncpg",
    ):
        assert forbidden not in source
    assert "def _deny_external_operations" in source
    assert 'patch.object(socket, "socket"' in source
    assert 'patch.object(subprocess, "Popen"' in source

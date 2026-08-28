"""Fail-closed runner tests for the bounded KG agent-adoption pilot."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.eval.kg_agent_adoption import ProtocolError
from scripts.eval.run_kg_agent_adoption import (
    PilotBlockedError,
    build_schedule_report,
    main,
    refuse_scored_execution,
    validate_enrollment,
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
        "status": "reviewed",
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
    result_path.write_text(
        json.dumps(
            {
                "schema": "unitares.kg-agent-adoption.result.v0",
                "experiment_id": "kg-agent-adoption-pilot-v0-example",
                "rows": [
                    {
                        "chain_instance_id": "receipt-only",
                        "chain_id": "measurement-funnel",
                        "family": "measurement-authority",
                        "repetition": 1,
                        "cell_id": "unavailable",
                        "arm": "unavailable",
                        "backend": None,
                        "step_index": 0,
                        "eligible": True,
                        "catalog_exposed": False,
                        "contextual_surface": False,
                        "reminder_withdrawn": False,
                        "result_injected": False,
                        "reachable": False,
                        "recording_verified": True,
                        "tool_invocations": 0,
                        "tool_successes": 0,
                        "material_use": False,
                        "quality": 1.0,
                        "net_utility": 1.0,
                        "costs_complete": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["summarize", "--result", str(result_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["funnel"]["step_receipts"] == 1
    assert summary["claim_status"] == "pilot_descriptive_only"


def test_cli_run_command_fails_closed(capsys):
    assert main(["run"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not implemented or authorized" in captured.err


def test_runner_source_has_no_live_execution_or_write_primitive():
    source = (
        REPO_ROOT / "scripts/eval/run_kg_agent_adoption.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "append_audit_event",
        "audit.outcome_events",
        "requests.",
        "urlopen(",
        "subprocess.",
        "write_text(",
        "write_bytes(",
    ):
        assert forbidden not in source

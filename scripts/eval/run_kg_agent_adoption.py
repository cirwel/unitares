#!/usr/bin/env python3
"""Validate, schedule, or summarize the bounded KG adoption pilot.

This runner intentionally has no scored execution path.  It cannot call a
model, invoke a retrieval backend, write an audit event, create an enrollment,
or mutate the repository.  The ``run`` command exists only as an executable
fail-closed gate: a future reviewed enrollment still requires a separately
reviewed implementation change before any scored live-model work is possible.

Examples::

    python scripts/eval/run_kg_agent_adoption.py validate
    python scripts/eval/run_kg_agent_adoption.py schedule
    python scripts/eval/run_kg_agent_adoption.py summarize --result /path/result.json
    python scripts/eval/run_kg_agent_adoption.py run  # always refuses in v0
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval.kg_agent_adoption import (  # noqa: E402
    ENROLLMENT_SCHEMA,
    PROTOCOL_SCHEMA,
    ProtocolError,
    analyze_results,
    build_counterbalanced_schedule,
    schedule_digest,
    validate_task_chains,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS = REPO_ROOT / "tests/kg_agent_adoption/task-chains-v0.json"
DEFAULT_ENROLLMENT = (
    REPO_ROOT
    / "docs/evaluations/kg-agent-adoption/enrollment-v0.example.json"
)
_UTILITY_COST_FIELDS = {
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "tool_failures",
    "invalid_citations",
    "regret",
    "operator_interventions",
}


class PilotBlockedError(RuntimeError):
    """Raised when a caller attempts an unauthorized scored/live operation."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProtocolError(f"file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError(f"JSON root must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_fields(value: Mapping[str, Any], fields: set[str], *, where: str) -> None:
    missing = fields - set(value)
    if missing:
        raise ProtocolError(f"{where} missing fields: {sorted(missing)}")


def _nonnegative_number(value: Any, *, where: str) -> float:
    if isinstance(value, bool):
        raise ProtocolError(f"{where} must be a non-negative number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"{where} must be a non-negative number") from exc
    if number < 0:
        raise ProtocolError(f"{where} must be non-negative")
    return number


def validate_enrollment(
    document: Mapping[str, Any],
    *,
    tasks_path: Path,
    task_document: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the unreviewed, non-executable v0 enrollment contract."""
    if not isinstance(document, Mapping):
        raise ProtocolError("enrollment must be an object")
    _require_fields(
        document,
        {
            "schema",
            "experiment_id",
            "status",
            "protocol",
            "task_manifest",
            "assignment",
            "utility",
            "model",
            "backends",
            "review",
            "execution",
        },
        where="enrollment",
    )
    if document.get("schema") != ENROLLMENT_SCHEMA:
        raise ProtocolError(f"enrollment schema must be {ENROLLMENT_SCHEMA}")
    if document.get("status") != "pilot_provisional":
        raise ProtocolError("v0 enrollment status must remain pilot_provisional")
    experiment_id = document.get("experiment_id")
    if not isinstance(experiment_id, str) or not experiment_id.strip():
        raise ProtocolError("enrollment experiment_id must be a non-empty string")

    protocol = document.get("protocol")
    if not isinstance(protocol, Mapping) or protocol.get("schema") != PROTOCOL_SCHEMA:
        raise ProtocolError(f"protocol.schema must be {PROTOCOL_SCHEMA}")
    if protocol.get("kg_decision_id") != "2026-08-27T23:46:30.942448+00:00":
        raise ProtocolError("protocol must bind the resolved KG adoption decision")

    validated_tasks = validate_task_chains(task_document)
    manifest = document.get("task_manifest")
    if not isinstance(manifest, Mapping):
        raise ProtocolError("task_manifest must be an object")
    _require_fields(manifest, {"path", "sha256", "schema"}, where="task_manifest")
    if manifest.get("schema") != validated_tasks["schema"]:
        raise ProtocolError("task_manifest schema does not match the loaded fixture")
    recorded_path = Path(str(manifest.get("path")))
    try:
        actual_relative = tasks_path.resolve().relative_to(REPO_ROOT)
    except ValueError as exc:
        raise ProtocolError("task fixture must live inside this repository") from exc
    if recorded_path != actual_relative:
        raise ProtocolError(
            f"task_manifest.path mismatch: recorded={recorded_path} actual={actual_relative}"
        )
    actual_digest = sha256_file(tasks_path)
    if manifest.get("sha256") != actual_digest:
        raise ProtocolError(
            f"task fixture digest mismatch: recorded={manifest.get('sha256')} actual={actual_digest}"
        )

    assignment = document.get("assignment")
    if not isinstance(assignment, Mapping):
        raise ProtocolError("assignment must be an object")
    _require_fields(
        assignment,
        {"seed", "repetitions", "unit", "counterbalance"},
        where="assignment",
    )
    seed = assignment.get("seed")
    repetitions = assignment.get("repetitions")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ProtocolError("assignment.seed must be an integer")
    if not isinstance(repetitions, int) or isinstance(repetitions, bool) or repetitions <= 0:
        raise ProtocolError("assignment.repetitions must be a positive integer")
    if assignment.get("unit") != "complete_task_chain":
        raise ProtocolError("assignment.unit must remain complete_task_chain")
    if assignment.get("counterbalance") != "digest_rotated_reversed_seven_cell_blocks":
        raise ProtocolError("assignment.counterbalance does not match the v0 scheduler")

    utility = document.get("utility")
    if not isinstance(utility, Mapping):
        raise ProtocolError("utility must be an object")
    _require_fields(
        utility,
        {"quality_range", "cost_weights", "effect_floor", "claim_status"},
        where="utility",
    )
    if utility.get("quality_range") != [0.0, 1.0]:
        raise ProtocolError("utility.quality_range must be [0.0, 1.0]")
    weights = utility.get("cost_weights")
    if not isinstance(weights, Mapping) or set(weights) != _UTILITY_COST_FIELDS:
        raise ProtocolError(
            f"utility.cost_weights must contain exactly {sorted(_UTILITY_COST_FIELDS)}"
        )
    for key, value in weights.items():
        _nonnegative_number(value, where=f"utility.cost_weights.{key}")
    if utility.get("effect_floor") is not None:
        raise ProtocolError("pilot effect_floor must remain null before a power read")
    if utility.get("claim_status") != "pilot_descriptive_only":
        raise ProtocolError("utility.claim_status must remain pilot_descriptive_only")

    model = document.get("model")
    if not isinstance(model, Mapping):
        raise ProtocolError("model must be an object")
    _require_fields(
        model,
        {"provider", "model_id", "model_digest", "live_calls_authorized"},
        where="model",
    )
    if model.get("live_calls_authorized") is not False:
        raise ProtocolError("v0 model.live_calls_authorized must be false")

    backends = document.get("backends")
    if not isinstance(backends, Mapping) or set(backends) != {
        "unitares_kg",
        "lexical_substitute",
    }:
        raise ProtocolError("backends must define unitares_kg and lexical_substitute")
    kg_backend = backends["unitares_kg"]
    lexical_backend = backends["lexical_substitute"]
    if not isinstance(kg_backend, Mapping) or kg_backend.get("mode") != "read_only":
        raise ProtocolError("unitares_kg backend must be read_only")
    if kg_backend.get("allowed_actions") != ["search", "details"]:
        raise ProtocolError("unitares_kg allowed_actions must be search/details only")
    if kg_backend.get("writes_authorized") is not False:
        raise ProtocolError("unitares_kg writes must remain unauthorized")
    if not isinstance(lexical_backend, Mapping):
        raise ProtocolError("lexical_substitute must be an object")
    if lexical_backend.get("algorithm") != "bm25_v1":
        raise ProtocolError("lexical_substitute algorithm must be bm25_v1")
    if lexical_backend.get("corpus") != "task_manifest.substitute_corpus":
        raise ProtocolError("lexical_substitute must use the frozen task corpus")

    review = document.get("review")
    if not isinstance(review, Mapping):
        raise ProtocolError("review must be an object")
    _require_fields(
        review,
        {"status", "governed_review_session", "approved_by"},
        where="review",
    )
    if review.get("status") not in {"unreviewed", "reviewed"}:
        raise ProtocolError("review.status must be unreviewed or reviewed")
    if review.get("status") == "reviewed" and not (
        review.get("governed_review_session") and review.get("approved_by")
    ):
        raise ProtocolError("reviewed enrollment requires session and approver")

    execution = document.get("execution")
    if not isinstance(execution, Mapping):
        raise ProtocolError("execution must be an object")
    _require_fields(
        execution,
        {"scored_live_model_runs_authorized", "writes_authorized", "runner_mode"},
        where="execution",
    )
    if execution.get("scored_live_model_runs_authorized") is not False:
        raise ProtocolError("scored live-model runs must remain unauthorized in v0")
    if execution.get("writes_authorized") is not False:
        raise ProtocolError("writes must remain unauthorized in v0")
    if execution.get("runner_mode") != "validate_schedule_summarize_only":
        raise ProtocolError("v0 runner_mode must remain validation-only")

    schedule = build_counterbalanced_schedule(
        validated_tasks,
        assignment_seed=seed,
        repetitions=repetitions,
    )
    return {
        "experiment_id": experiment_id,
        "review_status": review["status"],
        "execution_authorized": False,
        "tasks_sha256": actual_digest,
        "chain_count": len(validated_tasks["chains"]),
        "schedule_rows": len(schedule),
        "schedule_digest": schedule_digest(schedule),
        "assignment_seed": seed,
        "repetitions": repetitions,
    }


def build_schedule_report(
    enrollment: Mapping[str, Any], tasks: Mapping[str, Any]
) -> dict[str, Any]:
    assignment = enrollment["assignment"]
    schedule = build_counterbalanced_schedule(
        tasks,
        assignment_seed=int(assignment["seed"]),
        repetitions=int(assignment["repetitions"]),
    )
    return {
        "schema": "unitares.kg-agent-adoption.schedule.v0",
        "experiment_id": enrollment["experiment_id"],
        "schedule_digest": schedule_digest(schedule),
        "schedule_rows": len(schedule),
        "execution_authorized": False,
        "schedule": schedule,
    }


def refuse_scored_execution(enrollment: Mapping[str, Any]) -> None:
    """Fail closed even if a caller locally edits the review flag."""
    review = enrollment.get("review") if isinstance(enrollment, Mapping) else None
    status = review.get("status") if isinstance(review, Mapping) else "unknown"
    raise PilotBlockedError(
        "Scored live-model execution is not implemented or authorized in this v0 "
        f"runner (enrollment review status: {status}). Land a separately reviewed "
        "enrollment and execution implementation before any live calls or writes."
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    parser.add_argument("--enrollment", type=Path, default=DEFAULT_ENROLLMENT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("schedule")
    summarize = commands.add_parser("summarize")
    summarize.add_argument("--result", type=Path, required=True)
    commands.add_parser("run")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        tasks_path = args.tasks.resolve()
        enrollment_path = args.enrollment.resolve()
        tasks = _load_json(tasks_path)
        enrollment = _load_json(enrollment_path)
        validation = validate_enrollment(
            enrollment,
            tasks_path=tasks_path,
            task_document=tasks,
        )
        if args.command == "validate":
            print(
                json.dumps(
                    {
                        "valid": True,
                        "writes_performed": False,
                        **validation,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "schedule":
            print(json.dumps(build_schedule_report(enrollment, tasks), indent=2, sort_keys=True))
            return 0
        if args.command == "summarize":
            result = _load_json(args.result.resolve())
            if result.get("experiment_id") != enrollment.get("experiment_id"):
                raise ProtocolError("result experiment_id does not match enrollment")
            print(json.dumps(analyze_results(result), indent=2, sort_keys=True))
            return 0
        if args.command == "run":
            refuse_scored_execution(enrollment)
        raise AssertionError(f"unhandled command: {args.command}")
    except (ProtocolError, PilotBlockedError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

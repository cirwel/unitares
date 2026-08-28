#!/usr/bin/env python3
"""Validate, canary, schedule, or summarize the bounded KG adoption pilot.

This runner intentionally has no scored execution path.  It cannot call a
model, invoke a live retrieval backend, write an audit event, or create an
enrollment.  Its ``canary`` command validates only frozen offline fixtures and
may atomically create one explicitly requested content-addressed receipt.  The
``run`` command exists only as an executable fail-closed gate: a future reviewed
enrollment still requires a separately reviewed implementation change before
any scored live-model work is possible.

Examples::

    python scripts/eval/run_kg_agent_adoption.py validate
    python scripts/eval/run_kg_agent_adoption.py schedule
    python scripts/eval/run_kg_agent_adoption.py canary --review-session SESSION
    python scripts/eval/run_kg_agent_adoption.py summarize --result /path/result.json
    python scripts/eval/run_kg_agent_adoption.py run  # always refuses in v0
"""

from __future__ import annotations

import argparse
import ast
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
import platform
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from typing import Any, Mapping, Sequence
from unittest.mock import patch
import urllib.request

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.eval.kg_agent_adoption import (  # noqa: E402
    ENROLLMENT_SCHEMA,
    PROTOCOL_SCHEMA,
    ProtocolError,
    analyze_results,
    build_counterbalanced_schedule,
    canonical_json,
    compute_net_utility,
    experiment_cells,
    lexical_search,
    prior_work_tool_schema,
    render_step_prompt,
    schedule_digest,
    sha256_json,
    validate_result_rows,
    validate_task_chains,
)
from scripts.dev.portable_knowledge_bundle import (  # noqa: E402
    BundleValidationError,
    create_bundle,
    restore_sqlite,
    search_sqlite,
    validate_bundle,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_TASKS = REPO_ROOT / "tests/kg_agent_adoption/task-chains-v0.json"
DEFAULT_ENROLLMENT = (
    REPO_ROOT
    / "docs/evaluations/kg-agent-adoption/enrollment-v0.example.json"
)
DEFAULT_ENROLLMENT_SCHEMA = (
    REPO_ROOT
    / "docs/evaluations/kg-agent-adoption/enrollment-v0.schema.json"
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
OFFLINE_CANARY_SCHEMA = "unitares.kg-agent-adoption.offline-fixture-receipt.v0"
_FORBIDDEN_CANARY_IMPORTS = {
    "anthropic",
    "asyncpg",
    "httpx",
    "mcp",
    "openai",
    "psycopg",
    "psycopg2",
    "requests",
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
    extra = set(value) - fields
    if missing:
        raise ProtocolError(f"{where} missing fields: {sorted(missing)}")
    if extra:
        raise ProtocolError(f"{where} has unregistered fields: {sorted(extra)}")


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
        {
            "seed",
            "analysis_seed",
            "repetitions",
            "unit",
            "counterbalance",
            "schedule_sha256",
        },
        where="assignment",
    )
    seed = assignment.get("seed")
    analysis_seed = assignment.get("analysis_seed")
    repetitions = assignment.get("repetitions")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ProtocolError("assignment.seed must be an integer")
    if not isinstance(analysis_seed, int) or isinstance(analysis_seed, bool):
        raise ProtocolError("assignment.analysis_seed must be an integer")
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
        {"status", "scope", "governed_review_session", "approved_by"},
        where="review",
    )
    if review.get("status") not in {"unreviewed", "offline_fixture_reviewed"}:
        raise ProtocolError(
            "review.status must be unreviewed or offline_fixture_reviewed"
        )
    if review.get("status") == "offline_fixture_reviewed":
        if review.get("scope") != "offline_fixture_validation":
            raise ProtocolError("offline fixture review requires its exact scope")
        if not (review.get("governed_review_session") and review.get("approved_by")):
            raise ProtocolError(
                "offline fixture review requires governed session and approver"
            )
    elif any(
        review.get(field) is not None
        for field in ("scope", "governed_review_session", "approved_by")
    ):
        raise ProtocolError("unreviewed enrollment must not claim review metadata")

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
    if execution.get("runner_mode") != "validate_schedule_canary_summarize_only":
        raise ProtocolError("v0 runner_mode must remain offline-only")

    schedule = build_counterbalanced_schedule(
        validated_tasks,
        assignment_seed=seed,
        repetitions=repetitions,
    )
    actual_schedule_digest = schedule_digest(schedule)
    if assignment.get("schedule_sha256") != actual_schedule_digest:
        raise ProtocolError(
            "assignment.schedule_sha256 does not match the frozen schedule"
        )
    return {
        "experiment_id": experiment_id,
        "review_status": review["status"],
        "execution_authorized": False,
        "tasks_sha256": actual_digest,
        "chain_count": len(validated_tasks["chains"]),
        "schedule_rows": len(schedule),
        "schedule_digest": actual_schedule_digest,
        "assignment_seed": seed,
        "analysis_seed": analysis_seed,
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


def validate_result_binding(
    document: Mapping[str, Any],
    *,
    enrollment: Mapping[str, Any],
    enrollment_path: Path,
    tasks: Mapping[str, Any],
    tasks_path: Path,
) -> list[dict[str, Any]]:
    """Bind a complete result document to the exact enrollment and schedule."""
    _require_fields(
        document,
        {
            "schema",
            "experiment_id",
            "enrollment_sha256",
            "task_manifest_sha256",
            "schedule_sha256",
            "rows",
        },
        where="result",
    )
    if document.get("experiment_id") != enrollment.get("experiment_id"):
        raise ProtocolError("result experiment_id does not match enrollment")
    expected_digests = {
        "enrollment_sha256": sha256_file(enrollment_path),
        "task_manifest_sha256": sha256_file(tasks_path),
        "schedule_sha256": str(enrollment["assignment"]["schedule_sha256"]),
    }
    for field, expected in expected_digests.items():
        if document.get(field) != expected:
            raise ProtocolError(f"result {field} does not match the frozen enrollment")

    rows = validate_result_rows(document)
    schedule = build_counterbalanced_schedule(
        tasks,
        assignment_seed=int(enrollment["assignment"]["seed"]),
        repetitions=int(enrollment["assignment"]["repetitions"]),
    )
    expected = {
        (row["chain_id"], int(row["repetition"]), row["cell_id"]): row
        for row in schedule
    }
    chains = {chain["chain_id"]: chain for chain in validate_task_chains(tasks)["chains"]}
    by_instance: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_instance[str(row["chain_instance_id"])].append(row)

    observed: set[tuple[str, int, str]] = set()
    for instance_id, instance_rows in by_instance.items():
        first = instance_rows[0]
        binding = (
            str(first["chain_id"]),
            int(first["repetition"]),
            str(first["cell_id"]),
        )
        if binding not in expected:
            raise ProtocolError(f"result chain instance {instance_id} is not scheduled")
        if binding in observed:
            raise ProtocolError(f"duplicate scheduled chain binding: {binding}")
        observed.add(binding)
        scheduled = expected[binding]
        chain = chains[binding[0]]
        if first["family"] != scheduled["family"]:
            raise ProtocolError(f"result chain instance {instance_id} family mismatch")
        expected_indexes = list(range(len(scheduled["step_ids"])))
        actual_indexes = sorted(int(row["step_index"]) for row in instance_rows)
        if actual_indexes != expected_indexes:
            raise ProtocolError(
                f"result chain instance {instance_id} does not contain the complete task chain"
            )
        for row in instance_rows:
            step = chain["steps"][int(row["step_index"])]
            if row["eligible"] is not bool(step["eligible_for_prior_work"]):
                raise ProtocolError(
                    f"result chain instance {instance_id} eligibility mismatch"
                )

    missing = set(expected) - observed
    if missing:
        raise ProtocolError(
            f"result is incomplete; missing {len(missing)} scheduled chain instance(s)"
        )
    return rows


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ProtocolError(message)


@contextmanager
def _deny_external_operations():
    """Deny process/network escape while the offline fixture code executes."""
    attempts = {"network_calls": 0, "subprocess_calls": 0}

    def block(kind: str):
        def denied(*_args, **_kwargs):
            attempts[kind] += 1
            raise ProtocolError(f"offline fixture blocked attempted {kind}")

        return denied

    with (
        patch.object(socket, "socket", block("network_calls")),
        patch.object(socket, "create_connection", block("network_calls")),
        patch.object(urllib.request, "urlopen", block("network_calls")),
        patch.object(subprocess, "Popen", block("subprocess_calls")),
    ):
        yield attempts


def _probe_deny_all_guard() -> dict[str, bool]:
    """Prove the runtime guard itself blocks representative escape attempts."""
    with _deny_external_operations() as attempts:
        for operation in (
            lambda: socket.socket(),
            lambda: subprocess.Popen(["offline-canary-must-not-run"]),
            lambda: urllib.request.urlopen("https://invalid.example"),
        ):
            try:
                operation()
            except ProtocolError:
                pass
            else:  # pragma: no cover - defensive fail-closed branch
                raise ProtocolError("offline deny-all guard allowed a probe")
    _require(attempts == {"network_calls": 2, "subprocess_calls": 1}, "guard probe drift")
    return {"network_blocked": True, "subprocess_blocked": True}


def _assert_same_tool_contract(left: Any, right: Any) -> None:
    if canonical_json(left) != canonical_json(right):
        raise ProtocolError("backend tool contracts are asymmetric")


def _scan_offline_import_boundary(paths: Sequence[Path]) -> dict[str, Any]:
    imported_roots: set[str] = set()
    file_digests: dict[str, str] = {}
    for path in paths:
        source = path.read_text(encoding="utf-8")
        file_digests[str(path.relative_to(REPO_ROOT))] = sha256_file(path)
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_roots.add(node.module.split(".", 1)[0])
    prohibited = sorted(imported_roots & _FORBIDDEN_CANARY_IMPORTS)
    if prohibited:
        raise ProtocolError(f"offline canary imports prohibited adapters: {prohibited}")
    return {
        "checked_files": file_digests,
        "prohibited_imports_found": prohibited,
    }


def _validate_schedule_invariants(schedule: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    expected_cells = {row["cell_id"] for row in experiment_cells()}
    row_ids = [f"{row['block_id']}::{row['cell_id']}" for row in schedule]
    _require(len(row_ids) == len(set(row_ids)), "schedule row identifiers are not unique")
    _require(
        [int(row["call_order"]) for row in schedule]
        == list(range(1, len(schedule) + 1)),
        "schedule call_order is not complete and contiguous",
    )
    blocks: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in schedule:
        blocks[str(row["block_id"])].append(row)
    for block_id, rows in blocks.items():
        _require(
            {str(row["cell_id"]) for row in rows} == expected_cells,
            f"schedule block {block_id} does not contain every cell exactly once",
        )
        _require(
            len({int(row["sample_seed"]) for row in rows}) == 1,
            f"schedule block {block_id} does not share one sample seed",
        )
        _require(
            sorted(int(row["within_block_order"]) for row in rows)
            == list(range(1, len(expected_cells) + 1)),
            f"schedule block {block_id} ordering is incomplete",
        )
        _require(
            all(
                row["fresh_model_context_required"]
                and row["fresh_agent_identity_required"]
                for row in rows
            ),
            f"schedule block {block_id} weakened identity/context isolation",
        )
    return {
        "rows": len(schedule),
        "blocks": len(blocks),
        "cells_per_block": len(expected_cells),
        "unique_row_ids": True,
        "fixed_ordering": True,
        "adaptive_substitution": False,
    }


def _fixture_records(tasks: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": source["source_id"],
            "agent_id": "offline-fixture",
            "type": "note",
            "summary": source["title"],
            "details": source["text"],
            "tags": list(source.get("tags", [])),
            "status": "fixture",
            "provenance": {"scope": "offline_fixture_validation"},
        }
        for source in tasks["substitute_corpus"]
    ]


def _source_receipts(
    source_ids: Sequence[str], corpus: Sequence[Mapping[str, Any]]
) -> list[dict[str, str]]:
    by_id = {str(row["source_id"]): row for row in corpus}
    return [
        {
            "source_id": source_id,
            "source_sha256": sha256_json(by_id[source_id]),
        }
        for source_id in source_ids
    ]


def _run_exit_fixture_drill(tasks: Mapping[str, Any]) -> dict[str, Any]:
    workspace_path: Path | None = None
    with TemporaryDirectory(prefix="unitares-kg-adoption-offline-") as workspace:
        workspace_path = Path(workspace)
        os.chmod(workspace_path, 0o700)
        full_bundle = workspace_path / "full-bundle"
        small_bundle = workspace_path / "small-bundle"
        corrupt_bundle = workspace_path / "corrupt-bundle"
        database = workspace_path / "substitute.sqlite3"
        records = _fixture_records(tasks)
        manifest = create_bundle(
            records,
            full_bundle,
            created_at="1970-01-01T00:00:00+00:00",
            source="kg-agent-adoption-offline-fixture-v0",
        )
        _, validated = validate_bundle(full_bundle)
        _require(validated == sorted(records, key=lambda row: row["id"]), "bundle changed records")
        first = restore_sqlite(full_bundle, database)
        second = restore_sqlite(full_bundle, database)
        _require(first == second, "full bundle restore is not idempotent")

        create_bundle(
            records[:1],
            small_bundle,
            created_at="1970-01-01T00:00:00+00:00",
            source="kg-agent-adoption-offline-fixture-v0",
        )
        smaller = restore_sqlite(small_bundle, database)
        _require(smaller["store_records"] == 1, "smaller restore retained stale rows")

        create_bundle(
            records,
            corrupt_bundle,
            created_at="1970-01-01T00:00:00+00:00",
            source="kg-agent-adoption-offline-fixture-v0",
        )
        with (corrupt_bundle / "discoveries.jsonl").open("ab") as handle:
            handle.write(b"{}\n")
        try:
            validate_bundle(corrupt_bundle)
        except BundleValidationError:
            corruption_rejected = True
        else:  # pragma: no cover - defensive fail-closed branch
            raise ProtocolError("corrupted portability bundle was accepted")

        final = restore_sqlite(full_bundle, database)
        _require(final == first, "full restore after replacement changed record counts")
        _require((database.stat().st_mode & 0o777) == 0o600, "SQLite store is not private")

        retrievals: list[dict[str, Any]] = []
        for chain in tasks["chains"]:
            for step in chain["steps"]:
                delivered = [
                    row["id"]
                    for row in search_sqlite(
                        database,
                        step["injection_query"],
                        limit=len(records),
                    )
                ]
                required = set(step["answer_key"]["material_source_ids"])
                _require(
                    required.issubset(delivered),
                    f"SQLite exit retrieval missed registered sources for {step['step_id']}",
                )
                retrievals.append(
                    {
                        "step_id": step["step_id"],
                        "ordered_sources": _source_receipts(
                            delivered, tasks["substitute_corpus"]
                        ),
                    }
                )
        residual = sorted(
            path.name
            for path in workspace_path.iterdir()
            if path.name.endswith(("-journal", "-wal", "-shm"))
        )
        _require(not residual, f"SQLite left residual state: {residual}")
        receipt = {
            "bundle_manifest_sha256": sha256_json(manifest),
            "records_sha256": sha256_json(validated),
            "record_count": len(validated),
            "restore_idempotent": True,
            "replacement_exact": True,
            "corruption_rejected": corruption_rejected,
            "sqlite_mode": "0600",
            "retrievals": retrievals,
        }
    _require(workspace_path is not None and not workspace_path.exists(), "temporary state survived")
    return dict(receipt, residual_state=False)


def _expect_protocol_error(name: str, operation) -> str:
    try:
        operation()
    except ProtocolError:
        return name
    raise ProtocolError(f"negative canary did not fail closed: {name}")


def _run_atomic_receipt_drill() -> dict[str, bool]:
    workspace_path: Path | None = None
    with TemporaryDirectory(prefix="unitares-kg-adoption-receipt-") as workspace:
        workspace_path = Path(workspace)
        receipt_path = workspace_path / "receipt.json"
        receipt = {"schema": "receipt-probe", "content": {"value": 1}}
        with patch.object(os, "link", side_effect=OSError("simulated interruption")):
            try:
                write_content_addressed_receipt(receipt_path, receipt)
            except OSError:
                pass
            else:  # pragma: no cover - defensive fail-closed branch
                raise ProtocolError("simulated receipt interruption was ignored")
        _require(not receipt_path.exists(), "interrupted receipt became visible")
        _require(
            not list(workspace_path.glob(".*.tmp")),
            "interrupted receipt left a partial temporary file",
        )

        write_content_addressed_receipt(receipt_path, receipt)
        original = receipt_path.read_bytes()
        _expect_protocol_error(
            "receipt_overwrite",
            lambda: write_content_addressed_receipt(
                receipt_path,
                {"schema": "receipt-probe", "content": {"value": 2}},
            ),
        )
        _require(receipt_path.read_bytes() == original, "receipt overwrite changed bytes")
        _require((receipt_path.stat().st_mode & 0o777) == 0o600, "receipt is not private")
    _require(workspace_path is not None and not workspace_path.exists(), "receipt drill leaked state")
    return {
        "interrupted_publish_hidden": True,
        "partial_temporary_removed": True,
        "different_overwrite_rejected": True,
        "private_mode": True,
        "residual_state": False,
    }


def _build_offline_fixture_receipt(
    enrollment: Mapping[str, Any],
    *,
    enrollment_path: Path,
    tasks: Mapping[str, Any],
    tasks_path: Path,
    review_session: str,
) -> dict[str, Any]:
    """Execute deterministic offline-fixture checks and return a hashed receipt."""
    _require(bool(review_session.strip()), "canary review_session must be non-empty")
    validation = validate_enrollment(
        enrollment,
        tasks_path=tasks_path,
        task_document=tasks,
    )
    schedule_a = build_counterbalanced_schedule(
        tasks,
        assignment_seed=int(enrollment["assignment"]["seed"]),
        repetitions=int(enrollment["assignment"]["repetitions"]),
    )
    schedule_b = build_counterbalanced_schedule(
        deepcopy(tasks),
        assignment_seed=int(enrollment["assignment"]["seed"]),
        repetitions=int(enrollment["assignment"]["repetitions"]),
    )
    _require(
        canonical_json(schedule_a) == canonical_json(schedule_b),
        "schedule rerun is not byte-identical",
    )
    schedule_checks = _validate_schedule_invariants(schedule_a)

    tool_contract = prior_work_tool_schema()
    _assert_same_tool_contract(tool_contract, deepcopy(tool_contract))
    retrievals: list[dict[str, Any]] = []
    prompt_pairs = 0
    leak_markers = {
        str(enrollment["experiment_id"]),
        review_session,
        "offline_fixture_reviewed",
    }
    for chain in tasks["chains"]:
        for step_index, step in enumerate(chain["steps"]):
            results = lexical_search(
                tasks["substitute_corpus"],
                step["injection_query"],
                limit=len(tasks["substitute_corpus"]),
            )
            delivered = [row["source_id"] for row in results]
            missing = sorted(set(step["answer_key"]["material_source_ids"]) - set(delivered))
            _require(not missing, f"lexical preflight missed {missing} for {step['step_id']}")
            retrievals.append(
                {
                    "step_id": step["step_id"],
                    "ordered_sources": _source_receipts(
                        delivered, tasks["substitute_corpus"]
                    ),
                }
            )
            for arm, injected in (
                ("passive", None),
                ("surfaced_then_withdrawn", None),
                ("injected", results),
            ):
                left = render_step_prompt(
                    chain,
                    step_index,
                    arm=arm,
                    backend="unitares_kg",
                    injected_results=injected,
                )
                right = render_step_prompt(
                    chain,
                    step_index,
                    arm=arm,
                    backend="lexical_substitute",
                    injected_results=injected,
                )
                _assert_same_tool_contract(left["tools"], right["tools"])
                _require(left["prompt"] == right["prompt"], "backend label leaked into prompt")
                _require(
                    not any(marker in left["prompt"] for marker in leak_markers),
                    "condition, experiment, or review metadata leaked into prompt",
                )
                prompt_pairs += 1

    bad_digest = deepcopy(enrollment)
    bad_digest["task_manifest"]["sha256"] = "0" * 64
    missing_source = deepcopy(tasks)
    missing_source["substitute_corpus"] = missing_source["substitute_corpus"][1:]
    impossible_row = {
        "schema": "unitares.kg-agent-adoption.result.v0",
        "rows": [
            {
                "chain_instance_id": "impossible",
                "chain_id": "measurement-funnel",
                "family": "measurement-authority",
                "repetition": 1,
                "cell_id": "unavailable",
                "arm": "unavailable",
                "backend": None,
                "step_index": 0,
                "eligible": True,
                "catalog_exposed": True,
                "contextual_surface": False,
                "reminder_withdrawn": False,
                "result_injected": False,
                "reachable": True,
                "recording_verified": True,
                "tool_invocations": -1,
                "tool_successes": 0,
                "material_use": False,
                "quality": 99,
                "net_utility": 99,
                "costs_complete": True,
            }
        ],
    }
    asymmetric = deepcopy(tool_contract)
    asymmetric["function"]["description"] += " backend-specific"
    missing_score = deepcopy(impossible_row)
    missing_score["rows"][0].update(
        {
            "catalog_exposed": False,
            "reachable": False,
            "tool_invocations": 0,
            "quality": None,
            "net_utility": None,
        }
    )
    negative_cases = [
        _expect_protocol_error(
            "task_digest_mismatch",
            lambda: validate_enrollment(
                bad_digest, tasks_path=tasks_path, task_document=tasks
            ),
        ),
        _expect_protocol_error(
            "missing_registered_source", lambda: validate_task_chains(missing_source)
        ),
        _expect_protocol_error(
            "asymmetric_tool_contract",
            lambda: _assert_same_tool_contract(tool_contract, asymmetric),
        ),
        _expect_protocol_error(
            "impossible_result_row", lambda: validate_result_rows(impossible_row)
        ),
        _expect_protocol_error(
            "out_of_range_quality",
            lambda: compute_net_utility(99, {}, enrollment["utility"]["cost_weights"]),
        ),
        _expect_protocol_error(
            "missing_score", lambda: validate_result_rows(missing_score)
        ),
        _expect_protocol_error(
            "duplicate_schedule_row",
            lambda: _validate_schedule_invariants(
                [*schedule_a, deepcopy(schedule_a[0])]
            ),
        ),
        _expect_protocol_error(
            "missing_schedule_row",
            lambda: _validate_schedule_invariants(schedule_a[:-1]),
        ),
    ]
    incomplete_costs = compute_net_utility(
        1.0, {}, enrollment["utility"]["cost_weights"]
    )
    _require(
        incomplete_costs["net_utility"] is None
        and not incomplete_costs["costs_complete"],
        "missing costs were imputed",
    )

    boundary = _scan_offline_import_boundary(
        [
            Path(__file__).resolve(),
            REPO_ROOT / "scripts/eval/kg_agent_adoption.py",
            REPO_ROOT / "scripts/dev/portable_knowledge_bundle.py",
        ]
    )
    exit_drill = _run_exit_fixture_drill(tasks)
    atomic_receipt_drill = _run_atomic_receipt_drill()
    content = {
        "schema": OFFLINE_CANARY_SCHEMA,
        "scope": "offline_fixture_validation",
        "status": "passed",
        "experiment_id": enrollment["experiment_id"],
        "review_basis": {
            "session_id": review_session,
            "verdict_at_execution": "hold",
            "enrollment_review_status": enrollment["review"]["status"],
        },
        "digests": {
            "enrollment_sha256": sha256_file(enrollment_path),
            "task_manifest_sha256": sha256_file(tasks_path),
            "enrollment_schema_sha256": sha256_file(DEFAULT_ENROLLMENT_SCHEMA),
            "schedule_sha256": schedule_digest(schedule_a),
            "tool_contract_sha256": sha256_json(tool_contract),
            "substitute_corpus_sha256": sha256_json(tasks["substitute_corpus"]),
            "analysis_seed": validation["analysis_seed"],
        },
        "dependency_versions": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "receipt_canonicalization": "json-sort-keys-compact-utf8-v1",
        },
        "checks": {
            "schedule": schedule_checks,
            "byte_identical_schedule_rerun": True,
            "retrieval_preflight": retrievals,
            "backend_neutral_prompt_pairs": prompt_pairs,
            "negative_cases": negative_cases,
            "missing_costs_fail_closed": True,
            "offline_import_boundary": boundary,
            "exit_fixture_drill": exit_drill,
            "atomic_receipt_drill": atomic_receipt_drill,
        },
        "attempted_operations": {
            "live_model_calls": 0,
            "network_calls": 0,
            "unitares_kg_calls": 0,
            "audit_writes": 0,
            "production_database_calls": 0,
        },
        "claims": {
            "offline_fixture_passed": True,
            "behavioral_evidence": False,
            "live_kg_parity_proven": False,
            "production_plumbing_proven": False,
            "scored_execution_authorized": False,
            "live_execution_authorized": False,
        },
        "unproven_live_gates": [
            "independent task and enrollment content",
            "configured provider, model, and model digest",
            "objective effect floor and power rationale",
            "frozen-to-live UNITARES KG corpus and source-ID mapping",
            "awaited audit.events write/read canary",
            "fresh identity and model-context enforcement",
            "private raw prompt/response storage",
            "complete end-to-end event receipts",
            "explicit operator authorization",
        ],
    }
    return {
        "schema": "unitares.content-addressed-receipt.v0",
        "receipt_content_sha256": sha256_json(content),
        "content": content,
    }


def build_offline_fixture_receipt(
    enrollment: Mapping[str, Any],
    *,
    enrollment_path: Path,
    tasks: Mapping[str, Any],
    tasks_path: Path,
    review_session: str,
) -> dict[str, Any]:
    """Run the offline fixture under runtime network/process denial."""
    guard_probe = _probe_deny_all_guard()
    with _deny_external_operations() as attempts:
        receipt = _build_offline_fixture_receipt(
            enrollment,
            enrollment_path=enrollment_path,
            tasks=tasks,
            tasks_path=tasks_path,
            review_session=review_session,
        )
    _require(
        attempts == {"network_calls": 0, "subprocess_calls": 0},
        f"offline fixture attempted an external operation: {attempts}",
    )
    content = deepcopy(receipt["content"])
    content["checks"]["runtime_deny_all_guard"] = guard_probe
    content["attempted_operations"].update(attempts)
    return {
        "schema": receipt["schema"],
        "receipt_content_sha256": sha256_json(content),
        "content": content,
    }


def write_content_addressed_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    """Atomically create or verify one canonical content-addressed receipt."""
    data = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != data:
            raise ProtocolError(f"refusing to overwrite different receipt: {path}")
        os.chmod(path, 0o600)
        return
    parent_existed = path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ProtocolError(f"receipt appeared with different content: {path}")
    finally:
        temporary.unlink(missing_ok=True)
    os.chmod(path, 0o600)


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
    canary = commands.add_parser("canary")
    canary.add_argument("--review-session", required=True)
    canary.add_argument("--receipt", type=Path)
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
        if args.command == "canary":
            receipt = build_offline_fixture_receipt(
                enrollment,
                enrollment_path=enrollment_path,
                tasks=tasks,
                tasks_path=tasks_path,
                review_session=args.review_session,
            )
            if args.receipt is not None:
                write_content_addressed_receipt(args.receipt.resolve(), receipt)
            print(json.dumps(receipt, indent=2, sort_keys=True))
            return 0
        if args.command == "summarize":
            result = _load_json(args.result.resolve())
            validate_result_binding(
                result,
                enrollment=enrollment,
                enrollment_path=enrollment_path,
                tasks=tasks,
                tasks_path=tasks_path,
            )
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

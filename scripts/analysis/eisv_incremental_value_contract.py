#!/usr/bin/env python3
"""Validate the prospective EISV incremental-value episode contract.

This module is deliberately database-free. It supplies the fail-closed seam
between episode collection and later discrimination analysis:

* JSON-Schema plus cross-record semantic validation;
* substrate/producer independence-registry checks;
* rolling-origin temporal-split validation; and
* immutable analysis-access receipts for the dedicated study namespace.

It does not score EISV, query outcomes, or authorize a policy action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from jsonschema import Draft202012Validator, FormatChecker
except ImportError:  # pragma: no cover - exercised by an explicit unit mutation
    Draft202012Validator = None  # type: ignore[assignment,misc]
    FormatChecker = None  # type: ignore[assignment,misc]


REPO_ROOT = Path(__file__).resolve().parents[2]
EVALUATION_DIR = REPO_ROOT / "docs" / "evaluations" / "eisv-incremental-value"
DEFAULT_SCHEMA_PATH = EVALUATION_DIR / "eisv-ablation-episode-v2.schema.json"
DEFAULT_EXAMPLE_PATH = EVALUATION_DIR / "example-episode-v2.json"

STUDY_ID = "eisv-incremental-value-v1"
DATASET_NAMESPACE = STUDY_ID
ACCESS_POLICY_VERSION = "eisv-incremental-access.v1"
MAX_HORIZON_SECONDS = 86_400
MAX_REQUEST_CLOCK_SKEW_SECONDS = 300
READ_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,127}")
SHA256_PATTERN = re.compile(r"[a-f0-9]{64}")

EXPECTED_ARM_IDS = frozenset(
    {
        "a0_base_rate",
        "a0_persistence",
        "a1_direct_evidence",
        "a2_behavioral_no_eisv",
        "a3_behavioral_plus_eisv",
        "a4_eisv_only",
        "a5_minus_e",
        "a5_minus_i",
        "a5_minus_s",
        "a5_minus_v",
        "a6_legacy_only",
        "a7_production",
    }
)
PRIMARY_PRODUCER_CLASSES = frozenset({"external_system", "human", "server_primitive"})
ACCESS_CLASSES = frozenset({"structural", "predictions", "outcomes"})


class ContractViolation(ValueError):
    """The episode or analysis declaration violates the frozen contract."""


class AccessDenied(ContractViolation):
    """A requested read is not authorized by the study access policy."""


@dataclass(frozen=True)
class ReadRequest:
    """One declared access request, evaluated before any data is returned."""

    read_id: str
    namespace: str
    phase: str
    purpose: str
    read_protocol: str
    access_classes: frozenset[str]
    requested_at: datetime
    as_of: datetime | None = None
    not_before: datetime | None = None
    config_sha256: str | None = None
    preregistration_sha256: str | None = None
    contamination_acknowledged: bool = False


def _parse_datetime(value: str | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractViolation(f"{field} is not an ISO-8601 datetime") from exc
    if parsed.tzinfo is None:
        raise ContractViolation(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _normalize_datetime(value: datetime, *, field: str) -> datetime:
    """Require an aware datetime supplied by an analysis caller."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ContractViolation(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _format_path(path: Iterable[object]) -> str:
    rendered = "/".join(str(part) for part in path)
    return rendered or "$"


def load_json(path: Path) -> dict[str, Any]:
    """Load one JSON object with a path-qualified error."""

    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractViolation(f"cannot load JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractViolation(f"{path} must contain a JSON object")
    return value


def schema_errors(
    episode: Mapping[str, Any],
    *,
    schema: Mapping[str, Any],
) -> list[str]:
    """Return Draft-2020-12 shape errors without hiding missing tooling."""

    if Draft202012Validator is None or FormatChecker is None:
        return ["jsonschema is unavailable; schema validation is UNASSESSED"]
    try:
        Draft202012Validator.check_schema(dict(schema))
    except Exception as exc:  # jsonschema exception types vary across releases
        return [f"schema definition is invalid: {exc}"]
    validator = Draft202012Validator(
        dict(schema),
        format_checker=FormatChecker(),
    )
    errors = sorted(
        validator.iter_errors(dict(episode)),
        key=lambda error: list(error.absolute_path),
    )
    return [f"{_format_path(error.absolute_path)}: {error.message}" for error in errors]


def semantic_errors(episode: Mapping[str, Any]) -> list[str]:
    """Check relationships JSON Schema cannot express cleanly."""

    errors: list[str] = []
    config = episode.get("study_config", {})
    identity = episode.get("identity", {})
    context = episode.get("observation_context", {})
    validation = episode.get("instrument_validation", {})
    assignment = episode.get("evaluation_assignment", {})
    eligibility = episode.get("eligibility", {})
    prediction = episode.get("prediction", {})
    snapshot = episode.get("feature_snapshot", {})
    outcome = episode.get("outcome", {})
    policy = episode.get("production_policy", {})
    arms = episode.get("arms", [])

    if config.get("dataset_namespace") != DATASET_NAMESPACE:
        errors.append("study_config.dataset_namespace is not the dedicated namespace")
    if config.get("access_policy_version") != ACCESS_POLICY_VERSION:
        errors.append("study_config.access_policy_version is not registered")

    arm_ids = [arm.get("arm_id") for arm in arms if isinstance(arm, Mapping)]
    if len(arm_ids) != len(set(arm_ids)):
        errors.append("arms contain duplicate arm_id values")
    if set(arm_ids) != EXPECTED_ARM_IDS:
        missing = sorted(EXPECTED_ARM_IDS - set(arm_ids))
        extra = sorted(set(arm_ids) - EXPECTED_ARM_IDS)
        errors.append(f"arms differ from registry: missing={missing}, extra={extra}")

    snapshot_hash = snapshot.get("snapshot_sha256")
    for index, arm in enumerate(arms):
        if not isinstance(arm, Mapping):
            continue
        if arm.get("input_snapshot_sha256") != snapshot_hash:
            errors.append(f"arms/{index}: input snapshot hash differs from episode")

    try:
        captured_at = _parse_datetime(snapshot.get("captured_at"), field="captured_at")
        cutoff_at = _parse_datetime(prediction.get("cutoff_at"), field="cutoff_at")
        generated_at = _parse_datetime(
            prediction.get("generated_at"),
            field="generated_at",
        )
    except ContractViolation as exc:
        errors.append(str(exc))
        captured_at = cutoff_at = generated_at = None
    if captured_at and cutoff_at and generated_at:
        if not captured_at <= cutoff_at <= generated_at:
            errors.append(
                "timestamp order must be captured_at <= cutoff_at <= generated_at"
            )

    previous_raw = context.get("previous_authored_observation_at")
    elapsed = context.get("seconds_since_previous_authored_observation")
    if previous_raw is not None and elapsed is not None and cutoff_at is not None:
        try:
            previous_at = _parse_datetime(previous_raw, field="previous_authored")
        except ContractViolation as exc:
            errors.append(str(exc))
        else:
            observed_elapsed = (cutoff_at - previous_at).total_seconds()
            if abs(observed_elapsed - float(elapsed)) > 1.0:
                errors.append(
                    "seconds_since_previous_authored_observation does not match cutoff"
                )

    if eligibility.get("eligible") and validation.get("status") != "pass":
        errors.append("eligible episode has non-passing instrument validation")
    if eligibility.get("eligible") and eligibility.get(
        "analysis_population"
    ) != episode.get("phase"):
        errors.append("eligible episode analysis population differs from phase")
    if episode.get("phase") == "pilot" and assignment.get("assignment") != "pilot_only":
        errors.append("pilot episode is not assigned pilot_only")

    event_ids: list[object] = []
    primary_events: list[Mapping[str, Any]] = []
    window_closed_at: datetime | None = None
    window_closed_raw = outcome.get("window_closed_at")
    if window_closed_raw is not None:
        try:
            window_closed_at = _parse_datetime(
                window_closed_raw,
                field="outcome.window_closed_at",
            )
        except ContractViolation as exc:
            errors.append(str(exc))
        else:
            if cutoff_at is not None:
                if window_closed_at <= cutoff_at:
                    errors.append("outcome window does not begin after the cutoff")
                if window_closed_at > cutoff_at + timedelta(
                    seconds=MAX_HORIZON_SECONDS
                ):
                    errors.append("outcome window exceeds the 24-hour horizon")
    for index, event in enumerate(outcome.get("events", [])):
        if not isinstance(event, Mapping):
            continue
        event_ids.append(event.get("event_id"))
        occurred_raw = event.get("occurred_at")
        if occurred_raw is not None and cutoff_at is not None:
            try:
                occurred_at = _parse_datetime(
                    occurred_raw,
                    field=f"outcome.events/{index}/occurred_at",
                )
            except ContractViolation as exc:
                errors.append(str(exc))
            else:
                if occurred_at <= cutoff_at:
                    errors.append(f"outcome.events/{index}: event is not post-cutoff")
                if window_closed_at is not None and occurred_at > window_closed_at:
                    errors.append(
                        f"outcome.events/{index}: event is after window close"
                    )
        if event.get("counts_toward_primary"):
            primary_events.append(event)
            provenance = event.get("label_provenance", {})
            if provenance.get("producer_class") not in PRIMARY_PRODUCER_CLASSES:
                errors.append(
                    f"outcome.events/{index}: primary producer is not allowed"
                )
            if provenance.get("independence_class") != "independent":
                errors.append(
                    f"outcome.events/{index}: primary label is not independent"
                )
            if provenance.get("governance_input_overlap") is not False:
                errors.append(
                    f"outcome.events/{index}: primary label overlaps governance inputs"
                )
    if len(event_ids) != len(set(event_ids)):
        errors.append("outcome events contain duplicate event_id values")
    primary_value = outcome.get("primary_adverse_outcome")
    if primary_value is not None and bool(primary_events) is not primary_value:
        errors.append("primary_adverse_outcome disagrees with qualifying events")

    if policy.get("enforcement_applied"):
        if not policy.get("enforcement_requested"):
            errors.append("applied enforcement was not requested")
        if not policy.get("actuation_id") or not policy.get("applied_at"):
            errors.append("applied enforcement lacks actuation provenance")
    if policy.get("agent_delivery_status") == "suppressed":
        if policy.get("delivered_action") is not None:
            errors.append("suppressed policy unexpectedly has a delivered action")
        if not policy.get("suppression_reason"):
            errors.append("suppressed policy lacks a suppression reason")

    required_group_keys = ["independence_unit_id", "task_id"]
    if assignment.get("group_keys") != required_group_keys:
        errors.append("evaluation assignment does not lock independence unit and task")
    if assignment.get("purge_seconds", 0) < MAX_HORIZON_SECONDS:
        errors.append("evaluation purge is shorter than the prediction horizon")
    if not identity.get("producer_group_id"):
        errors.append("identity lacks producer_group_id")
    return errors


def validate_episode(
    episode: Mapping[str, Any],
    *,
    schema: Mapping[str, Any] | None = None,
) -> None:
    """Raise one aggregated failure for invalid or semantically inconsistent data."""

    active_schema = schema if schema is not None else load_json(DEFAULT_SCHEMA_PATH)
    shape_errors = schema_errors(episode, schema=active_schema)
    if shape_errors:
        raise ContractViolation("; ".join(shape_errors))
    relationship_errors = semantic_errors(episode)
    if relationship_errors:
        raise ContractViolation("; ".join(relationship_errors))


def independence_registry_errors(episodes: Sequence[Mapping[str, Any]]) -> list[str]:
    """Reject one substrate or producer group assigned to multiple units."""

    errors: list[str] = []
    seen: dict[tuple[str, str], str] = {}
    for episode in episodes:
        identity = episode.get("identity", {})
        unit = identity.get("independence_unit_id")
        episode_id = episode.get("episode_id", "unknown")
        for key_name in ("substrate_id_hash", "producer_group_id"):
            value = identity.get(key_name)
            if not isinstance(value, str) or not value or not isinstance(unit, str):
                errors.append(f"{episode_id}: incomplete independence key {key_name}")
                continue
            key = (key_name, value)
            existing = seen.setdefault(key, unit)
            if existing != unit:
                errors.append(
                    f"{episode_id}: {key_name} maps to both {existing!r} and {unit!r}"
                )
    return errors


def split_assignment_errors(episodes: Sequence[Mapping[str, Any]]) -> list[str]:
    """Reject cross-record pilot reuse and group leakage between split sides."""

    errors: list[str] = []
    episode_phases: dict[str, str] = {}
    group_assignments: dict[tuple[str, str, str], str] = {}
    for episode in episodes:
        episode_id = episode.get("episode_id", "unknown")
        phase = episode.get("phase")
        assignment = episode.get("evaluation_assignment", {})
        side = assignment.get("assignment")
        fold_id = assignment.get("fold_id")
        identity = episode.get("identity", {})

        if isinstance(episode_id, str) and isinstance(phase, str):
            existing_phase = episode_phases.setdefault(episode_id, phase)
            if existing_phase != phase:
                errors.append(
                    f"{episode_id}: episode appears in both {existing_phase} and {phase}"
                )
        if phase == "pilot" and side != "pilot_only":
            errors.append(f"{episode_id}: pilot episode is not pilot_only")
        if phase == "confirmatory" and side == "pilot_only":
            errors.append(f"{episode_id}: confirmatory episode is pilot_only")

        if side not in {"train", "test"}:
            continue
        if not isinstance(fold_id, str) or not fold_id:
            errors.append(f"{episode_id}: train/test assignment lacks fold_id")
            continue
        for key_name in ("independence_unit_id", "task_id"):
            value = identity.get(key_name)
            if not isinstance(value, str) or not value:
                errors.append(f"{episode_id}: assignment lacks {key_name}")
                continue
            key = (fold_id, key_name, value)
            existing_side = group_assignments.setdefault(key, side)
            if existing_side != side:
                errors.append(
                    f"{episode_id}: {key_name} {value!r} leaks across "
                    f"{existing_side} and {side} in {fold_id}"
                )
    return errors


def temporal_split_errors(policy: Mapping[str, Any]) -> list[str]:
    """Validate the frozen rolling-origin fold geometry."""

    errors: list[str] = []
    if policy.get("method") != "rolling-origin-purged.v1":
        errors.append("temporal split method must be rolling-origin-purged.v1")
    if policy.get("group_keys") != ["independence_unit_id", "task_id"]:
        errors.append("temporal split must group by independence unit and task")
    purge_seconds = policy.get("purge_seconds")
    if not isinstance(purge_seconds, int) or purge_seconds < MAX_HORIZON_SECONDS:
        errors.append("temporal split purge must cover the 24-hour horizon")
        purge_seconds = MAX_HORIZON_SECONDS
    folds = policy.get("folds")
    if not isinstance(folds, list) or not folds:
        return errors + ["temporal split has no folds"]
    previous_test_end: datetime | None = None
    for index, fold in enumerate(folds):
        if not isinstance(fold, Mapping):
            errors.append(f"folds/{index} is not an object")
            continue
        try:
            train_end = _parse_datetime(fold.get("train_end"), field="train_end")
            test_start = _parse_datetime(fold.get("test_start"), field="test_start")
            test_end = _parse_datetime(fold.get("test_end"), field="test_end")
        except ContractViolation as exc:
            errors.append(f"folds/{index}: {exc}")
            continue
        if train_end is None or test_start is None or test_end is None:
            errors.append(f"folds/{index}: split timestamps are required")
            continue
        if train_end + timedelta(seconds=purge_seconds) > test_start:
            errors.append(f"folds/{index}: purge gap is too short")
        if test_start >= test_end:
            errors.append(f"folds/{index}: test window is empty or reversed")
        if previous_test_end is not None and test_start < previous_test_end:
            errors.append(f"folds/{index}: test windows overlap")
        previous_test_end = test_end
    return errors


def _authorize_read_at(request: ReadRequest, *, observed_at: datetime) -> None:
    """Evaluate a read against a trusted wall-clock observation."""

    errors: list[str] = []
    try:
        requested_at = _normalize_datetime(request.requested_at, field="requested_at")
    except ContractViolation as exc:
        errors.append(str(exc))
        requested_at = None
    now = _normalize_datetime(observed_at, field="observed_at")
    if requested_at is not None and abs((requested_at - now).total_seconds()) > (
        MAX_REQUEST_CLOCK_SKEW_SECONDS
    ):
        errors.append(
            "requested_at differs from the trusted clock by more than 5 minutes"
        )
    normalized_not_before: datetime | None = None
    normalized_as_of: datetime | None = None
    for field_name, value in (
        ("not_before", request.not_before),
        ("as_of", request.as_of),
    ):
        if value is None:
            continue
        try:
            normalized = _normalize_datetime(value, field=field_name)
        except ContractViolation as exc:
            errors.append(str(exc))
        else:
            if field_name == "not_before":
                normalized_not_before = normalized
            else:
                normalized_as_of = normalized
    if request.namespace != DATASET_NAMESPACE:
        errors.append("unknown or cross-study dataset namespace")
    if READ_ID_PATTERN.fullmatch(request.read_id) is None:
        errors.append("read_id does not satisfy the immutable receipt pattern")
    if not request.access_classes or not request.access_classes <= ACCESS_CLASSES:
        errors.append("access_classes are empty or unknown")

    if request.purpose == "pilot_instrumentation":
        if request.phase != "pilot" or request.read_protocol != "pilot_instrumentation":
            errors.append("pilot instrumentation has the wrong phase or protocol")
        if {"predictions", "outcomes"} <= request.access_classes:
            errors.append("pilot instrumentation cannot pair predictions with outcomes")
    elif request.purpose == "registered_analysis":
        if request.phase != "confirmatory" or request.read_protocol != "registered":
            errors.append("confirmatory analysis requires the registered protocol")
        if normalized_not_before is None or now is None or now < normalized_not_before:
            errors.append("registered analysis is early or lacks not_before")
        if normalized_as_of is None or now is None or normalized_as_of > now:
            errors.append("registered analysis lacks a valid frozen as_of")
        for field_name, value in (
            ("config_sha256", request.config_sha256),
            ("preregistration_sha256", request.preregistration_sha256),
        ):
            if value is None or SHA256_PATTERN.fullmatch(value) is None:
                errors.append(f"registered analysis lacks a valid {field_name}")
    elif request.purpose == "reproduction":
        if request.read_protocol != "reproduction":
            errors.append("reproduction purpose requires reproduction protocol")
        if not request.contamination_acknowledged:
            errors.append("reproduction requires contamination acknowledgement")
    else:
        errors.append("unknown analysis purpose")

    if errors:
        raise AccessDenied("; ".join(errors))


def authorize_read(request: ReadRequest) -> None:
    """Fail closed before an analysis reader can access the study namespace."""

    _authorize_read_at(request, observed_at=datetime.now(timezone.utc))


def record_access_receipt(request: ReadRequest, *, ledger_dir: Path) -> Path:
    """Authorize then atomically persist an immutable analysis-access receipt."""

    authorized_at = datetime.now(timezone.utc)
    _authorize_read_at(request, observed_at=authorized_at)
    ledger_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256(request.read_id.encode("utf-8")).hexdigest()
    receipt_path = ledger_dir / f"{digest}.json"
    receipt = {
        "schema": "unitares.eisv_incremental_access_receipt.v1",
        "study_id": STUDY_ID,
        "access_policy_version": ACCESS_POLICY_VERSION,
        "read_id": request.read_id,
        "namespace": request.namespace,
        "phase": request.phase,
        "purpose": request.purpose,
        "read_protocol": request.read_protocol,
        "access_classes": sorted(request.access_classes),
        "requested_at": request.requested_at.astimezone(timezone.utc).isoformat(),
        "authorized_at": authorized_at.isoformat(),
        "as_of": request.as_of.astimezone(timezone.utc).isoformat()
        if request.as_of
        else None,
        "not_before": request.not_before.astimezone(timezone.utc).isoformat()
        if request.not_before
        else None,
        "config_sha256": request.config_sha256,
        "preregistration_sha256": request.preregistration_sha256,
        "contamination_acknowledged": request.contamination_acknowledged,
    }
    payload = (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode()
    try:
        fd = os.open(
            os.fspath(receipt_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise AccessDenied(
            f"read id {request.read_id!r} already has a receipt"
        ) from exc
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise AccessDenied(f"short receipt write: {receipt_path}")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    return receipt_path


def _status_for_episode(episode: Mapping[str, Any]) -> str:
    validation = episode.get("instrument_validation", {})
    status = validation.get("status")
    return status.upper() if status in {"pass", "fail", "unassessed"} else "FAIL"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", nargs="?", default=str(DEFAULT_EXAMPLE_PATH))
    parser.add_argument("--schema", default=str(DEFAULT_SCHEMA_PATH))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        schema = load_json(Path(args.schema))
        episode = load_json(Path(args.episode))
        validate_episode(episode, schema=schema)
    except ContractViolation as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    status = _status_for_episode(episode)
    print(f"{status}: {args.episode}")
    return 0 if status == "PASS" else 2 if status == "UNASSESSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())

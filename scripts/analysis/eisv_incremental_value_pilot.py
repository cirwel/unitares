#!/usr/bin/env python3
"""Create and operate the isolated EISV instrumentation-pilot store.

The store is deliberately narrow:

* initialization installs one create-only, pilot-provisional manifest;
* emission accepts only fully validated ``pilot``/``pilot_only`` episodes;
* each episode is committed as one immutable, private directory; and
* inventory reads only score-free structural sidecars after recording an
  access receipt for structural/outcome aggregates.

There is no command to enable collection, enumerate raw episodes, pair arm
scores with outcomes, enroll a cohort, or freeze confirmatory configuration.
An enabled manifest must be separately reviewed and supplied with an explicit
authorization identifier and a not-before boundary.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.analysis import eisv_incremental_value_contract as contract


REPO_ROOT = Path(__file__).resolve().parents[2]
EVALUATION_DIR = REPO_ROOT / "docs" / "evaluations" / "eisv-incremental-value"
DEFAULT_MANIFEST_SCHEMA_PATH = EVALUATION_DIR / "pilot-manifest-v1.schema.json"
DEFAULT_MANIFEST_PATH = EVALUATION_DIR / "pilot-manifest-v1.example.json"
PROTOCOL_PATH = REPO_ROOT / "docs" / "proposals" / (
    "eisv-incremental-value-ablation-v1.md"
)
STORE_MANIFEST_NAME = "pilot-manifest.json"
EPISODES_DIR_NAME = "episodes"
STRUCTURAL_SCHEMA = "eisv-ablation-pilot-structural.v1"
INVENTORY_SCHEMA = "eisv-ablation-pilot-inventory.v1"


class PilotStoreViolation(contract.ContractViolation):
    """The pilot manifest or immutable store violates its contract."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(value), indent=2, sort_keys=True) + "\n").encode()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise PilotStoreViolation(f"cannot hash artifact {path}: {exc}") from exc
    return digest.hexdigest()


def _parse_aware(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise PilotStoreViolation(f"{field} is not an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PilotStoreViolation(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    schema: Mapping[str, Any] | None = None,
) -> None:
    """Validate a pilot manifest and bind it to the checked-in artifacts."""

    active_schema = schema or contract.load_json(DEFAULT_MANIFEST_SCHEMA_PATH)
    errors = contract.schema_errors(manifest, schema=active_schema)
    if errors:
        raise PilotStoreViolation("; ".join(errors))

    artifacts = manifest["artifacts"]
    expected_hashes = {
        "protocol_sha256": _sha256_file(PROTOCOL_PATH),
        "episode_schema_sha256": _sha256_file(contract.DEFAULT_SCHEMA_PATH),
    }
    mismatches = [
        f"{field} does not match the checked-in artifact"
        for field, expected in expected_hashes.items()
        if artifacts[field] != expected
    ]
    if mismatches:
        raise PilotStoreViolation("; ".join(mismatches))


def _study_dir(root: Path) -> Path:
    return root / contract.DATASET_NAMESPACE


def _manifest_path(root: Path) -> Path:
    return _study_dir(root) / STORE_MANIFEST_NAME


def _episodes_dir(root: Path) -> Path:
    return _study_dir(root) / EPISODES_DIR_NAME


def _write_create_only(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    try:
        fd = os.open(os.fspath(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError as exc:
        raise PilotStoreViolation(f"immutable path already exists: {path}") from exc
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise PilotStoreViolation(f"short write: {path}")
            remaining = remaining[written:]
        os.fsync(fd)
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(fd)


def initialize_store(root: Path, manifest_path: Path = DEFAULT_MANIFEST_PATH) -> Path:
    """Install one validated manifest and private episode directory.

    Initialization is idempotent only when the installed manifest has exactly
    the same canonical representation. A different manifest requires a new
    explicit store root; an existing pilot cannot be reconfigured in place.
    """

    manifest = contract.load_json(manifest_path)
    validate_manifest(manifest)
    payload = _canonical_json(manifest)
    study_dir = _study_dir(root)
    episodes_dir = _episodes_dir(root)
    try:
        study_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        episodes_dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(study_dir, 0o700)
        os.chmod(episodes_dir, 0o700)
    except OSError as exc:
        raise PilotStoreViolation(f"cannot initialize private pilot directories: {exc}")
    installed = _manifest_path(root)
    if installed.exists():
        try:
            current = installed.read_bytes()
        except OSError as exc:
            raise PilotStoreViolation(f"cannot read installed manifest: {exc}") from exc
        if current != payload:
            raise PilotStoreViolation("installed pilot manifest is immutable and differs")
        return installed
    _write_create_only(installed, payload)
    _fsync_directory(study_dir)
    return installed


def _load_installed_manifest(root: Path) -> dict[str, Any]:
    manifest = contract.load_json(_manifest_path(root))
    validate_manifest(manifest)
    return manifest


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY)
    except OSError as exc:
        raise PilotStoreViolation(f"cannot open directory for fsync {path}: {exc}") from exc
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def _ingest_lock(root: Path) -> Iterator[None]:
    lock_path = _study_dir(root) / ".ingest.lock"
    try:
        fd = os.open(os.fspath(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        os.chmod(lock_path, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as exc:
        raise PilotStoreViolation(f"cannot acquire pilot ingest lock: {exc}") from exc
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _validate_collection_authorization(
    manifest: Mapping[str, Any], *, observed_at: datetime
) -> None:
    if not manifest["collection_enabled"]:
        raise PilotStoreViolation("pilot collection is disabled by the manifest")
    authorization = manifest.get("pilot_authorization")
    if not isinstance(authorization, Mapping):
        raise PilotStoreViolation("enabled pilot lacks explicit authorization")
    start = _parse_aware(
        authorization["start_not_before"], field="pilot_authorization.start_not_before"
    )
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise PilotStoreViolation("trusted observed_at must be timezone-aware")
    if observed_at.astimezone(timezone.utc) < start:
        raise PilotStoreViolation("pilot collection is earlier than start_not_before")


def _validate_episode_versions(
    episode: Mapping[str, Any], manifest: Mapping[str, Any]
) -> None:
    if episode.get("phase") != "pilot":
        raise PilotStoreViolation("pilot store accepts only pilot episodes")
    assignment = episode.get("evaluation_assignment", {})
    if assignment.get("assignment") != "pilot_only":
        raise PilotStoreViolation("pilot store accepts only pilot_only assignment")
    expected = manifest["versions"]
    actual = episode.get("study_config", {})
    mappings = {
        "protocol_version": "protocol_version",
        "access_policy_version": "access_policy_version",
        "independence_resolver_version": "independence_resolver_version",
        "config_version": "config_version",
        "feature_registry_version": "feature_registry_version",
        "scorer_bundle_version": "scorer_bundle_version",
    }
    mismatches = [
        field
        for field, manifest_field in mappings.items()
        if actual.get(field) != expected[manifest_field]
    ]
    validation = episode.get("instrument_validation", {})
    if validation.get("bundle_version") != expected[
        "instrument_validation_bundle_version"
    ]:
        mismatches.append("instrument_validation.bundle_version")
    arm_ids = {arm.get("arm_id") for arm in episode.get("arms", [])}
    registered = set(manifest["arm_registry"]["shadow_arm_ids"])
    registered.add(manifest["arm_registry"]["production_arm_id"])
    if arm_ids != registered:
        mismatches.append("arm_registry")
    if mismatches:
        raise PilotStoreViolation(
            "episode differs from pilot-provisional manifest: " + ", ".join(mismatches)
        )


def _structural_record(episode: Mapping[str, Any]) -> dict[str, Any]:
    identity = episode["identity"]
    outcome = episode["outcome"]
    return {
        "schema_version": STRUCTURAL_SCHEMA,
        "study_id": episode["study_id"],
        "episode_id": episode["episode_id"],
        "prediction_id": episode["prediction"]["prediction_id"],
        "event_ids": [event["event_id"] for event in outcome["events"]],
        "independence_unit_id": identity["independence_unit_id"],
        "substrate_id_hash": identity["substrate_id_hash"],
        "producer_group_id": identity["producer_group_id"],
        "task_id": identity["task_id"],
        "schedule_class": episode["observation_context"]["schedule_class"],
        "maturity_stage": identity["maturity_stage"],
        "prediction_cutoff_at": episode["prediction"]["cutoff_at"],
        "eligible": episode["eligibility"]["eligible"],
        "instrument_validation_status": episode["instrument_validation"]["status"],
        "record_complete": episode["quality"]["record_complete"],
        "outcome_status": outcome["status"],
        "outcome_window_closed_at": outcome["window_closed_at"],
        "primary_adverse_outcome": outcome["primary_adverse_outcome"],
        "config_version": episode["study_config"]["config_version"],
        "feature_registry_version": episode["study_config"][
            "feature_registry_version"
        ],
        "scorer_bundle_version": episode["study_config"]["scorer_bundle_version"],
    }


def _load_structural_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        directories = sorted(_episodes_dir(root).iterdir())
    except OSError as exc:
        raise PilotStoreViolation(f"cannot inspect pilot episode directory: {exc}") from exc
    for directory in directories:
        if directory.name.startswith(".tmp-") or not directory.is_dir():
            continue
        record = contract.load_json(directory / "structural.json")
        if record.get("schema_version") != STRUCTURAL_SCHEMA:
            raise PilotStoreViolation(f"unknown structural sidecar: {directory}")
        records.append(record)
    return records


def _cross_record_errors(
    records: Sequence[Mapping[str, Any]], candidate: Mapping[str, Any]
) -> list[str]:
    errors: list[str] = []
    candidate_episode = candidate["episode_id"]
    if any(row["episode_id"] == candidate_episode for row in records):
        errors.append(f"duplicate episode_id {candidate_episode}")
    if any(row["prediction_id"] == candidate["prediction_id"] for row in records):
        errors.append(f"duplicate prediction_id {candidate['prediction_id']}")
    existing_events = {
        event_id for row in records for event_id in row.get("event_ids", [])
    }
    duplicates = sorted(existing_events.intersection(candidate.get("event_ids", [])))
    if duplicates:
        errors.append(f"duplicate event_id values {duplicates}")
    for key in ("substrate_id_hash", "producer_group_id"):
        for row in records:
            if (
                row[key] == candidate[key]
                and row["independence_unit_id"] != candidate["independence_unit_id"]
            ):
                errors.append(
                    f"{key} maps to both {row['independence_unit_id']!r} and "
                    f"{candidate['independence_unit_id']!r}"
                )
                break
    return errors


def emit_episode(
    root: Path,
    episode: Mapping[str, Any],
    *,
    observed_at: datetime | None = None,
) -> Path:
    """Validate and atomically commit one immutable pilot episode bundle."""

    manifest = _load_installed_manifest(root)
    now = observed_at or datetime.now(timezone.utc)
    _validate_collection_authorization(manifest, observed_at=now)
    contract.validate_episode(episode)
    _validate_episode_versions(episode, manifest)
    try:
        episode_id = str(uuid.UUID(str(episode["episode_id"])))
    except (KeyError, ValueError) as exc:
        raise PilotStoreViolation("episode_id must be a canonical UUID") from exc
    if episode_id != episode["episode_id"]:
        raise PilotStoreViolation("episode_id must use canonical lowercase UUID form")
    structural = _structural_record(episode)
    episodes_dir = _episodes_dir(root)

    with _ingest_lock(root):
        records = _load_structural_records(root)
        errors = _cross_record_errors(records, structural)
        if errors:
            raise PilotStoreViolation("; ".join(errors))
        target = episodes_dir / episode_id
        if target.exists():
            raise PilotStoreViolation(f"immutable episode already exists: {episode_id}")
        temp_dir = Path(tempfile.mkdtemp(prefix=".tmp-", dir=episodes_dir))
        try:
            os.chmod(temp_dir, 0o700)
            _write_create_only(temp_dir / "episode.json", _canonical_json(episode))
            _write_create_only(temp_dir / "structural.json", _canonical_json(structural))
            _fsync_directory(temp_dir)
            os.rename(temp_dir, target)
            _fsync_directory(episodes_dir)
        except Exception:
            if temp_dir.exists():
                shutil.rmtree(temp_dir)
            raise
    return target


def _cluster_sizes(records: Sequence[Mapping[str, Any]], key: str) -> list[int]:
    return sorted(Counter(str(row[key]) for row in records).values())


def read_federation_material(
    root: Path,
    *,
    read_id: str,
    ledger_dir: Path,
    requested_at: datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], Path]:
    """Read score-free pilot material for a local federated export.

    The detailed structural rows never leave this process.  The federation
    exporter converts their identifiers to keyed linkage tokens before writing
    a site package.  Keeping this read here ensures it uses the same access
    receipt and installed-manifest checks as the ordinary inventory command.
    """

    manifest = _load_installed_manifest(root)
    request = contract.ReadRequest(
        read_id=read_id,
        namespace=contract.DATASET_NAMESPACE,
        phase="pilot",
        purpose="pilot_instrumentation",
        read_protocol="pilot_instrumentation",
        access_classes=frozenset({"structural", "outcomes"}),
        requested_at=requested_at or datetime.now(timezone.utc),
    )
    receipt = contract.record_access_receipt(request, ledger_dir=ledger_dir)
    return manifest, _load_structural_records(root), receipt


def inventory_from_records(
    records: Sequence[Mapping[str, Any]], *, access_receipt: str
) -> dict[str, Any]:
    """Build the score-free pilot inventory from already authorized rows."""

    records = list(records)
    censored = [row for row in records if row["outcome_status"] == "censored"]
    pending = [row for row in records if row["outcome_status"] == "pending"]
    observed = [row for row in records if row["outcome_status"] == "observed"]
    unscorable = [
        row
        for row in observed
        if not row["eligible"]
        or row["instrument_validation_status"] != "pass"
        or not row["record_complete"]
    ]
    usable = [row for row in observed if row not in unscorable]
    primary = sum(bool(row["primary_adverse_outcome"]) for row in usable)
    return {
        "schema_version": INVENTORY_SCHEMA,
        "status": "PILOT_AGGREGATE_ONLY",
        "study_id": contract.STUDY_ID,
        "access_receipt": access_receipt,
        "episode_denominator": len(records),
        "pending_episodes": len(pending),
        "censored_episodes": len(censored),
        "unscorable_observed_episodes": len(unscorable),
        "usable_episodes": len(usable),
        "usable_primary_adverse_outcomes": primary,
        "usable_primary_adverse_rate": primary / len(usable) if usable else None,
        "independence_unit_cluster_sizes": _cluster_sizes(
            records, "independence_unit_id"
        ),
        "task_cluster_sizes": _cluster_sizes(records, "task_id"),
        "schedule_class_counts": dict(
            sorted(Counter(row["schedule_class"] for row in records).items())
        ),
        "maturity_stage_counts": dict(
            sorted(Counter(row["maturity_stage"] for row in records).items())
        ),
        "paired_loss_difference_sd": None,
        "paired_score_outcome_access": "NOT_AUTHORIZED_BY_PILOT_INSTRUMENTATION",
    }


def inventory(
    root: Path,
    *,
    read_id: str,
    ledger_dir: Path,
    requested_at: datetime | None = None,
) -> dict[str, Any]:
    """Return score-free aggregate pilot geometry after an access receipt."""

    _, records, receipt = read_federation_material(
        root,
        read_id=read_id,
        ledger_dir=ledger_dir,
        requested_at=requested_at,
    )
    return inventory_from_records(records, access_receipt=receipt.name)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="install a create-only pilot manifest")
    init.add_argument("--root", type=Path, required=True)
    init.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)

    emit = commands.add_parser("emit", help="atomically append one pilot episode")
    emit.add_argument("--root", type=Path, required=True)
    emit.add_argument("--episode", type=Path, required=True)

    inspect = commands.add_parser(
        "inventory", help="write a receipt and return score-free pilot aggregates"
    )
    inspect.add_argument("--root", type=Path, required=True)
    inspect.add_argument("--read-id", required=True)
    inspect.add_argument("--ledger-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "init":
            path = initialize_store(args.root, args.manifest)
            print(f"PASS: installed immutable pilot manifest at {path}")
        elif args.command == "emit":
            episode = contract.load_json(args.episode)
            path = emit_episode(args.root, episode)
            print(f"PASS: committed immutable pilot episode at {path}")
        else:
            report = inventory(
                args.root,
                read_id=args.read_id,
                ledger_dir=args.ledger_dir,
            )
            print(json.dumps(report, indent=2, sort_keys=True))
    except contract.ContractViolation as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

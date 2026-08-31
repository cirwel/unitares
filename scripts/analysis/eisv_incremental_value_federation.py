#!/usr/bin/env python3
"""Exchange and combine privacy-preserving EISV pilot inventories.

Each site reads only the pilot instrumentation surface, replaces local cluster
identifiers with federation-scoped HMAC linkage tokens, and signs the resulting
package with an Ed25519 site key.  The coordinator verifies packages against an
explicit registry and combines cluster geometry without receiving raw episodes,
arm scores, agent identifiers, or local cluster identifiers.

This is a pilot geometry executor.  It has no confirmatory-analysis or policy
authority and cannot pair predictions with outcomes.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import math
import os
import secrets
import stat
import sys
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.analysis import eisv_incremental_value_contract as contract
from scripts.analysis import eisv_incremental_value_pilot as pilot


EVALUATION_DIR = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "evaluations"
    / "eisv-incremental-value"
)
PACKAGE_SCHEMA_PATH = EVALUATION_DIR / "federation-site-package-v1.schema.json"
REGISTRY_SCHEMA_PATH = EVALUATION_DIR / "federation-registry-v1.schema.json"
REPORT_SCHEMA_PATH = EVALUATION_DIR / "federation-pilot-report-v1.schema.json"
PACKAGE_SCHEMA = "eisv-federation-site-package.v1"
REGISTRY_SCHEMA = "eisv-federation-registry.v1"
REPORT_SCHEMA = "eisv-federation-pilot-report.v1"
RECEIPT_SCHEMA = "eisv-federation-combine-receipt.v1"
STATUS = "PILOT_AGGREGATE_ONLY"
TOKEN_ALGORITHM = "hmac-sha256-v1"
SIGNATURE_ALGORITHM = "ed25519-v1"
LINKAGE_KEY_BYTES = 32


class FederationViolation(contract.ContractViolation):
    """A federation package, registry, or combine request is invalid."""


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _pretty_json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(value), indent=2, sort_keys=True) + "\n").encode()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise FederationViolation(f"cannot hash {path}: {exc}") from exc
    return digest.hexdigest()


def _create_only(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(os.fspath(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError as exc:
        raise FederationViolation(
            f"refusing to replace immutable artifact: {path}"
        ) from exc
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise FederationViolation(f"short write: {path}")
            remaining = remaining[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _validate_schema(value: Mapping[str, Any], schema_path: Path) -> None:
    schema = contract.load_json(schema_path)
    errors = contract.schema_errors(value, schema=schema)
    if errors:
        raise FederationViolation("; ".join(errors))


def _load_linkage_key(path: Path) -> bytes:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        payload = path.read_bytes()
    except OSError as exc:
        raise FederationViolation(f"cannot read linkage key {path}: {exc}") from exc
    if mode & 0o077:
        raise FederationViolation("linkage key must not be group/world accessible")
    if len(payload) != LINKAGE_KEY_BYTES:
        raise FederationViolation(f"linkage key must contain {LINKAGE_KEY_BYTES} bytes")
    return payload


def _linkage_token(
    key: bytes, federation_id: str, kind: str, namespace: str, value: str
) -> str:
    message = "\0".join((federation_id, kind, namespace, value)).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def contract_fingerprint(manifest: Mapping[str, Any]) -> dict[str, str]:
    artifacts = manifest["artifacts"]
    versions = manifest["versions"]
    return {
        "protocol_sha256": str(artifacts["protocol_sha256"]),
        "episode_schema_sha256": str(artifacts["episode_schema_sha256"]),
        "protocol_version": str(versions["protocol_version"]),
        "access_policy_version": str(versions["access_policy_version"]),
        "independence_resolver_version": str(versions["independence_resolver_version"]),
        "config_version": str(versions["config_version"]),
        "feature_registry_version": str(versions["feature_registry_version"]),
        "scorer_bundle_version": str(versions["scorer_bundle_version"]),
        "instrument_validation_bundle_version": str(
            versions["instrument_validation_bundle_version"]
        ),
    }


def generate_signing_key(
    private_key_path: Path, public_record_path: Path, *, key_id: str
) -> None:
    """Create one private site key and its registry-ready public record."""

    if contract.READ_ID_PATTERN.fullmatch(key_id) is None:
        raise FederationViolation("key_id does not satisfy the identifier pattern")
    key = Ed25519PrivateKey.generate()
    private_bytes = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    _create_only(private_key_path, private_bytes, mode=0o600)
    _create_only(
        public_record_path,
        _pretty_json(
            {
                "key_id": key_id,
                "algorithm": SIGNATURE_ALGORITHM,
                "public_key_base64": base64.b64encode(public_bytes).decode("ascii"),
            }
        ),
        mode=0o644,
    )


def generate_linkage_key(path: Path) -> None:
    """Create a federation linkage key for distribution to participating sites."""

    _create_only(path, secrets.token_bytes(LINKAGE_KEY_BYTES), mode=0o600)


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        value = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise FederationViolation(f"cannot load signing key {path}: {exc}") from exc
    if mode & 0o077:
        raise FederationViolation("signing key must not be group/world accessible")
    if not isinstance(value, Ed25519PrivateKey):
        raise FederationViolation("signing key is not Ed25519")
    return value


def _cluster_material(
    records: Sequence[Mapping[str, Any]],
    *,
    federation_id: str,
    site_id: str,
    identity_namespace: str,
    task_namespace: str,
    linkage_key: bytes,
    minimum_cell_count: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    independence: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    tasks: Counter[str] = Counter()
    substrate_counts: Counter[str] = Counter()
    producer_counts: Counter[str] = Counter()
    for row in records:
        independence[str(row["independence_unit_id"])].append(row)
        task_token = _linkage_token(
            linkage_key,
            federation_id,
            "task",
            task_namespace,
            str(row["task_id"]),
        )
        tasks[task_token] += 1
        substrate_counts[str(row["substrate_id_hash"])] += 1
        producer_counts[str(row["producer_group_id"])] += 1

    clusters: list[dict[str, Any]] = []
    suppressed_independence = 0
    for local_unit, rows in independence.items():
        if len(rows) < minimum_cell_count:
            suppressed_independence += len(rows)
            continue
        clusters.append(
            {
                "cluster_token": _linkage_token(
                    linkage_key,
                    federation_id,
                    "local_independence_unit",
                    site_id,
                    local_unit,
                ),
                "episode_count": len(rows),
                "substrate_tokens": sorted(
                    {
                        _linkage_token(
                            linkage_key,
                            federation_id,
                            "substrate",
                            identity_namespace,
                            str(row["substrate_id_hash"]),
                        )
                        for row in rows
                        if substrate_counts[str(row["substrate_id_hash"])]
                        >= minimum_cell_count
                    }
                ),
                "producer_tokens": sorted(
                    {
                        _linkage_token(
                            linkage_key,
                            federation_id,
                            "producer",
                            identity_namespace,
                            str(row["producer_group_id"]),
                        )
                        for row in rows
                        if producer_counts[str(row["producer_group_id"])]
                        >= minimum_cell_count
                    }
                ),
            }
        )
    task_clusters = [
        {"task_token": token, "episode_count": count}
        for token, count in sorted(tasks.items())
        if count >= minimum_cell_count
    ]
    suppressed_tasks = sum(
        count for count in tasks.values() if count < minimum_cell_count
    )
    return (
        sorted(clusters, key=lambda row: row["cluster_token"]),
        task_clusters,
        suppressed_independence,
        suppressed_tasks,
    )


def _parse_utc(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise FederationViolation(f"{field} is not an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FederationViolation(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _reporting_window(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    predictions = [
        _parse_utc(str(row["prediction_cutoff_at"]), field="prediction_cutoff_at")
        for row in records
    ]
    closes = [
        _parse_utc(
            str(row["outcome_window_closed_at"]), field="outcome_window_closed_at"
        )
        for row in records
        if row.get("outcome_window_closed_at") is not None
    ]
    return {
        "first_prediction_at": min(predictions).isoformat() if predictions else None,
        "last_prediction_at": max(predictions).isoformat() if predictions else None,
        "latest_outcome_close_at": max(closes).isoformat() if closes else None,
    }


def _public_key_base64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def export_site_package(
    root: Path,
    *,
    registry: Mapping[str, Any],
    site_id: str,
    export_sequence: int,
    read_id: str,
    ledger_dir: Path,
    signing_key_path: Path,
    linkage_key_path: Path,
    output_path: Path,
    requested_at: datetime | None = None,
) -> dict[str, Any]:
    """Create one immutable, signed, score-free site package."""

    validate_registry(registry)
    if isinstance(export_sequence, bool) or export_sequence < 1:
        raise FederationViolation("export_sequence must be a positive integer")
    site = _registry_sites(registry).get(site_id)
    if site is None:
        raise FederationViolation(f"unregistered site_id {site_id!r}")
    now = (requested_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if site["key_status"] != "active":
        raise FederationViolation(f"site {site_id!r} signing key is not active")
    valid_from = _parse_utc(str(site["key_valid_from"]), field="key_valid_from")
    valid_until = _parse_utc(str(site["key_valid_until"]), field="key_valid_until")
    if not valid_from <= now <= valid_until:
        raise FederationViolation(f"site {site_id!r} signing key is outside validity")
    signing_key = _load_private_key(signing_key_path)
    if _public_key_base64(signing_key) != site["public_key_base64"]:
        raise FederationViolation(f"site {site_id!r} signing key is not registry-bound")

    manifest, records, receipt = pilot.read_federation_material(
        root,
        read_id=read_id,
        ledger_dir=ledger_dir,
        requested_at=now,
    )
    fingerprint = contract_fingerprint(manifest)
    if fingerprint != registry["expected_contract"]:
        raise FederationViolation(f"site {site_id!r} contract mismatch")
    inventory = pilot.inventory_from_records(records, access_receipt=receipt.name)
    del inventory["paired_loss_difference_sd"]
    linkage_key = _load_linkage_key(linkage_key_path)
    minimum_cell_count = int(registry["privacy_policy"]["minimum_cell_count"])
    (
        independence_clusters,
        task_clusters,
        suppressed_independence,
        suppressed_tasks,
    ) = _cluster_material(
        records,
        federation_id=str(registry["federation_id"]),
        site_id=site_id,
        identity_namespace=str(site["identity_namespace"]),
        task_namespace=str(site["task_namespace"]),
        linkage_key=linkage_key,
        minimum_cell_count=minimum_cell_count,
    )
    unsigned_without_digest: dict[str, Any] = {
        "schema_version": PACKAGE_SCHEMA,
        "status": STATUS,
        "decision_authority": "NONE",
        "study_id": contract.STUDY_ID,
        "dataset_namespace": contract.DATASET_NAMESPACE,
        "phase": "pilot",
        "federation_id": registry["federation_id"],
        "pilot_run_id": registry["pilot_run_id"],
        "registry_snapshot_sha256": _sha256_bytes(_canonical_json(registry)),
        "linkage_key_id": registry["linkage_key_id"],
        "site_id": site_id,
        "identity_namespace": site["identity_namespace"],
        "task_namespace": site["task_namespace"],
        "export_sequence": export_sequence,
        "package_nonce": secrets.token_hex(16),
        "generated_at": now.isoformat(),
        "reporting_window": _reporting_window(records),
        "local_read": {
            "read_id": read_id,
            "receipt_name": receipt.name,
            "receipt_sha256": _sha256_file(receipt),
        },
        "contract": fingerprint,
        "inventory": inventory,
        "linkage": {
            "algorithm": TOKEN_ALGORITHM,
            "minimum_cell_count": minimum_cell_count,
            "independence_clusters": independence_clusters,
            "suppressed_independence_episode_count": suppressed_independence,
            "task_clusters": task_clusters,
            "suppressed_task_episode_count": suppressed_tasks,
        },
        "privacy": {
            "raw_episodes_exported": False,
            "arm_scores_exported": False,
            "raw_identifiers_exported": False,
            "linkage_tokens_are_federation_scoped": True,
            "small_cells_suppressed": True,
        },
    }
    payload_sha256 = _sha256_bytes(_canonical_json(unsigned_without_digest))
    unsigned = {**unsigned_without_digest, "payload_sha256": payload_sha256}
    signature = signing_key.sign(_canonical_json(unsigned))
    package = {
        **unsigned,
        "attestation": {
            "algorithm": SIGNATURE_ALGORITHM,
            "key_id": site["key_id"],
            "signature_base64": base64.b64encode(signature).decode("ascii"),
        },
    }
    _validate_schema(package, PACKAGE_SCHEMA_PATH)
    semantic_errors = site_package_semantic_errors(package)
    if semantic_errors:
        raise FederationViolation("; ".join(semantic_errors))
    _create_only(output_path, _pretty_json(package), mode=0o600)
    return package


def _registry_sites(registry: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    sites = registry.get("sites", [])
    return {str(site["site_id"]): site for site in sites}


def validate_registry(registry: Mapping[str, Any]) -> None:
    _validate_schema(registry, REGISTRY_SCHEMA_PATH)
    site_ids = [str(site["site_id"]) for site in registry["sites"]]
    key_ids = [str(site["key_id"]) for site in registry["sites"]]
    public_keys = [str(site["public_key_base64"]) for site in registry["sites"]]
    if len(site_ids) != len(set(site_ids)):
        raise FederationViolation("registry site_id values must be unique")
    if len(key_ids) != len(set(key_ids)):
        raise FederationViolation("registry key_id values must be unique")
    if len(public_keys) != len(set(public_keys)):
        raise FederationViolation("registry public keys must be unique per site")
    for site in registry["sites"]:
        valid_from = _parse_utc(str(site["key_valid_from"]), field="key_valid_from")
        valid_until = _parse_utc(str(site["key_valid_until"]), field="key_valid_until")
        if valid_from >= valid_until:
            raise FederationViolation(
                f"site {site['site_id']!r} key validity interval is empty"
            )
        _decode_public_key(str(site["public_key_base64"]))


def site_package_semantic_errors(package: Mapping[str, Any]) -> list[str]:
    """Check aggregate relationships that JSON Schema cannot express."""

    errors: list[str] = []
    inventory = package["inventory"]
    linkage = package["linkage"]
    denominator = int(inventory["episode_denominator"])
    partition_total = sum(
        int(inventory[field])
        for field in (
            "pending_episodes",
            "censored_episodes",
            "unscorable_observed_episodes",
            "usable_episodes",
        )
    )
    if partition_total != denominator:
        errors.append("inventory status partitions do not equal episode_denominator")
    primary = int(inventory["usable_primary_adverse_outcomes"])
    usable = int(inventory["usable_episodes"])
    if primary > usable:
        errors.append("usable_primary_adverse_outcomes exceeds usable_episodes")
    expected_rate = primary / usable if usable else None
    actual_rate = inventory["usable_primary_adverse_rate"]
    if actual_rate is None and expected_rate is not None:
        errors.append("usable_primary_adverse_rate is missing")
    elif actual_rate is not None and expected_rate is None:
        errors.append("usable_primary_adverse_rate exists without usable episodes")
    elif actual_rate is not None and not math.isclose(
        float(actual_rate), float(expected_rate), rel_tol=1e-12, abs_tol=1e-12
    ):
        errors.append("usable_primary_adverse_rate does not match counts")
    for field in ("schedule_class_counts", "maturity_stage_counts"):
        if sum(int(value) for value in inventory[field].values()) != denominator:
            errors.append(f"{field} does not equal episode_denominator")
    for field, rows, suppressed_field in (
        (
            "independence_clusters",
            linkage["independence_clusters"],
            "suppressed_independence_episode_count",
        ),
        ("task_clusters", linkage["task_clusters"], "suppressed_task_episode_count"),
    ):
        visible = sum(int(row["episode_count"]) for row in rows)
        if visible + int(linkage[suppressed_field]) != denominator:
            errors.append(f"{field} does not equal episode_denominator")
        if any(
            int(row["episode_count"]) < int(linkage["minimum_cell_count"])
            for row in rows
        ):
            errors.append(f"{field} contains a cell below minimum_cell_count")
    cluster_tokens = [
        str(row["cluster_token"]) for row in linkage["independence_clusters"]
    ]
    task_tokens = [str(row["task_token"]) for row in linkage["task_clusters"]]
    if len(cluster_tokens) != len(set(cluster_tokens)):
        errors.append("independence cluster tokens are not unique")
    if len(task_tokens) != len(set(task_tokens)):
        errors.append("task cluster tokens are not unique")
    for kind in ("substrate_tokens", "producer_tokens"):
        owners: set[str] = set()
        for row in linkage["independence_clusters"]:
            tokens = set(str(value) for value in row[kind])
            overlap = owners.intersection(tokens)
            if overlap:
                errors.append(f"{kind} map to multiple local independence clusters")
                break
            owners.update(tokens)
    if inventory["access_receipt"] != package["local_read"]["receipt_name"]:
        errors.append("inventory and local_read refer to different receipts")
    window = package["reporting_window"]
    first = window["first_prediction_at"]
    last = window["last_prediction_at"]
    if denominator == 0 and any(value is not None for value in window.values()):
        errors.append("empty package has a non-empty reporting window")
    if denominator > 0 and (first is None or last is None):
        errors.append("non-empty package lacks a prediction reporting window")
    if first is not None and last is not None:
        if _parse_utc(str(first), field="first_prediction_at") > _parse_utc(
            str(last), field="last_prediction_at"
        ):
            errors.append("reporting window predictions are reversed")
    unsigned = dict(package)
    unsigned.pop("attestation", None)
    claimed_payload = unsigned.pop("payload_sha256", None)
    if claimed_payload != _sha256_bytes(_canonical_json(unsigned)):
        errors.append("payload_sha256 does not match the signed payload")
    return errors


def _decode_public_key(value: str) -> Ed25519PublicKey:
    try:
        raw = base64.b64decode(value, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise FederationViolation(
            "registry contains an invalid Ed25519 public key"
        ) from exc


def verify_site_package(
    package: Mapping[str, Any], registry: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Verify one package's schema, registry binding, contract, and signature."""

    _validate_schema(package, PACKAGE_SCHEMA_PATH)
    semantic_errors = site_package_semantic_errors(package)
    if semantic_errors:
        raise FederationViolation("; ".join(semantic_errors))
    validate_registry(registry)
    if package["federation_id"] != registry["federation_id"]:
        raise FederationViolation("package belongs to a different federation")
    if package["pilot_run_id"] != registry["pilot_run_id"]:
        raise FederationViolation("package belongs to a different pilot run")
    if package["linkage_key_id"] != registry["linkage_key_id"]:
        raise FederationViolation("package used a different linkage-key epoch")
    registry_hash = _sha256_bytes(_canonical_json(registry))
    if package["registry_snapshot_sha256"] != registry_hash:
        raise FederationViolation("package is not bound to this registry snapshot")
    site = _registry_sites(registry).get(str(package["site_id"]))
    if site is None:
        raise FederationViolation(f"unregistered site_id {package['site_id']!r}")
    for field in ("identity_namespace", "task_namespace"):
        if package[field] != site[field]:
            raise FederationViolation(f"site {package['site_id']!r} changed {field}")
    if package["attestation"]["key_id"] != site["key_id"]:
        raise FederationViolation(
            f"site {package['site_id']!r} used an unregistered key"
        )
    if site["key_status"] != "active":
        raise FederationViolation(f"site {package['site_id']!r} signing key is revoked")
    generated_at = _parse_utc(str(package["generated_at"]), field="generated_at")
    if generated_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise FederationViolation("package generated_at is in the future")
    if not (
        _parse_utc(str(site["key_valid_from"]), field="key_valid_from")
        <= generated_at
        <= _parse_utc(str(site["key_valid_until"]), field="key_valid_until")
    ):
        raise FederationViolation(
            f"site {package['site_id']!r} signed outside its key validity interval"
        )
    if (
        package["linkage"]["minimum_cell_count"]
        != registry["privacy_policy"]["minimum_cell_count"]
    ):
        raise FederationViolation("package changed the registry privacy floor")
    if package["contract"] != registry["expected_contract"]:
        raise FederationViolation(f"site {package['site_id']!r} contract mismatch")
    signature_text = package["attestation"]["signature_base64"]
    try:
        signature = base64.b64decode(signature_text, validate=True)
    except (ValueError, TypeError) as exc:
        raise FederationViolation("package signature is not valid base64") from exc
    unsigned = dict(package)
    del unsigned["attestation"]
    try:
        _decode_public_key(str(site["public_key_base64"])).verify(
            signature, _canonical_json(unsigned)
        )
    except InvalidSignature as exc:
        raise FederationViolation(
            f"site {package['site_id']!r} signature is invalid"
        ) from exc
    return site


class _UnionFind:
    def __init__(self, values: Iterable[str]) -> None:
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            parent = self.parent[value]
            self.parent[value] = root
            value = parent
        return root

    def union(self, left: str, right: str) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _merge_cluster_sizes(
    packages: Sequence[Mapping[str, Any]],
    sites: Mapping[str, Mapping[str, Any]],
) -> tuple[list[int], int, int]:
    rows: list[tuple[str, Mapping[str, Any], str, str]] = []
    for package in packages:
        site_id = str(package["site_id"])
        for cluster in package["linkage"]["independence_clusters"]:
            node = f"{site_id}:{cluster['cluster_token']}"
            rows.append(
                (
                    node,
                    cluster,
                    str(sites[site_id]["shared_state_domain"]),
                    site_id,
                )
            )
        suppressed = int(package["linkage"]["suppressed_independence_episode_count"])
        if suppressed:
            rows.append(
                (
                    f"{site_id}:suppressed:{package['payload_sha256']}",
                    {
                        "episode_count": suppressed,
                        "substrate_tokens": [],
                        "producer_tokens": [],
                    },
                    str(sites[site_id]["shared_state_domain"]),
                    site_id,
                )
            )
    union = _UnionFind(node for node, _, _, _ in rows)
    token_owner: dict[tuple[str, str], str] = {}
    domain_owner: dict[str, str] = {}
    domain_sites: dict[str, set[str]] = defaultdict(set)
    for _, _, domain, site_id in rows:
        domain_sites[domain].add(site_id)
    token_links = 0
    domain_links = 0
    for node, cluster, domain, _ in rows:
        for kind, values in (
            ("substrate", cluster["substrate_tokens"]),
            ("producer", cluster["producer_tokens"]),
        ):
            for token in values:
                prior = token_owner.setdefault((kind, str(token)), node)
                if union.find(node) != union.find(prior):
                    union.union(node, prior)
                    token_links += 1
        if len(domain_sites[domain]) > 1:
            prior_domain = domain_owner.setdefault(domain, node)
            if union.find(node) != union.find(prior_domain):
                union.union(node, prior_domain)
                domain_links += 1
    counts: Counter[str] = Counter()
    for node, cluster, _, _ in rows:
        counts[union.find(node)] += int(cluster["episode_count"])
    return sorted(counts.values()), token_links, domain_links


def _merge_task_sizes(packages: Sequence[Mapping[str, Any]]) -> tuple[list[int], int]:
    counts: Counter[str] = Counter()
    appearances: Counter[str] = Counter()
    suppressed_by_namespace: Counter[str] = Counter()
    for package in packages:
        for row in package["linkage"]["task_clusters"]:
            token = str(row["task_token"])
            counts[token] += int(row["episode_count"])
            appearances[token] += 1
        suppressed_by_namespace[str(package["task_namespace"])] += int(
            package["linkage"]["suppressed_task_episode_count"]
        )
    for namespace, count in suppressed_by_namespace.items():
        if count:
            counts[f"suppressed:{namespace}"] += count
    return sorted(counts.values()), sum(value - 1 for value in appearances.values())


def _sum_count_maps(
    packages: Sequence[Mapping[str, Any]], field: str
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for package in packages:
        counts.update(package["inventory"][field])
    return dict(sorted(counts.items()))


def _maximum_share(values: Iterable[int]) -> float | None:
    values = list(values)
    total = sum(values)
    return max(values) / total if total else None


@contextmanager
def _combine_ledger_lock(ledger_dir: Path) -> Iterable[None]:
    ledger_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = ledger_dir / ".combine.lock"
    fd = os.open(os.fspath(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_combine_receipts(ledger_dir: Path) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for path in sorted(ledger_dir.glob("*.json")):
        value = contract.load_json(path)
        if value.get("schema_version") != RECEIPT_SCHEMA:
            raise FederationViolation(f"unknown artifact in combine ledger: {path}")
        receipts.append(value)
    return receipts


def _package_acceptance_records(
    packages: Sequence[Mapping[str, Any]], package_hashes: Mapping[str, str]
) -> list[dict[str, Any]]:
    return [
        {
            "site_id": str(package["site_id"]),
            "package_sha256": package_hashes[str(package["site_id"])],
            "payload_sha256": str(package["payload_sha256"]),
            "package_nonce": str(package["package_nonce"]),
            "export_sequence": int(package["export_sequence"]),
            "generated_at": str(package["generated_at"]),
        }
        for package in packages
    ]


def _replay_errors(
    registry: Mapping[str, Any],
    packages: Sequence[Mapping[str, Any]],
    package_records: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
) -> list[str]:
    errors: list[str] = []
    federation_id = str(registry["federation_id"])
    pilot_run_id = str(registry["pilot_run_id"])
    registry_hash = _sha256_bytes(_canonical_json(registry))
    prior_packages: list[Mapping[str, Any]] = []
    for receipt in receipts:
        if (
            receipt.get("federation_id") == federation_id
            and receipt.get("pilot_run_id") == pilot_run_id
            and receipt.get("registry_sha256") != registry_hash
        ):
            errors.append("registry snapshot changed within one pilot run")
        prior_packages.extend(receipt.get("packages", []))
    seen_hashes = {str(row["package_sha256"]) for row in prior_packages}
    seen_nonces = {str(row["package_nonce"]) for row in prior_packages}
    minimum_interval = int(
        registry["privacy_policy"]["minimum_export_interval_seconds"]
    )
    for package, current in zip(packages, package_records, strict=True):
        site_id = str(current["site_id"])
        if current["package_sha256"] in seen_hashes:
            errors.append(f"site {site_id!r} package is an exact replay")
        if current["package_nonce"] in seen_nonces:
            errors.append(f"site {site_id!r} package nonce was already accepted")
        prior_site = [row for row in prior_packages if row.get("site_id") == site_id]
        if not prior_site:
            continue
        max_sequence = max(int(row["export_sequence"]) for row in prior_site)
        if int(current["export_sequence"]) <= max_sequence:
            errors.append(f"site {site_id!r} export_sequence is stale or conflicting")
        latest_generated = max(
            _parse_utc(str(row["generated_at"]), field="generated_at")
            for row in prior_site
        )
        generated = _parse_utc(str(package["generated_at"]), field="generated_at")
        if (generated - latest_generated).total_seconds() < minimum_interval:
            errors.append(f"site {site_id!r} violates the minimum export interval")
    return errors


def _record_combine_receipt(
    *,
    read_id: str,
    ledger_dir: Path,
    federation_id: str,
    pilot_run_id: str,
    registry_sha256: str,
    packages: Sequence[Mapping[str, Any]],
    report_sha256: str,
    requested_at: datetime,
) -> Path:
    if contract.READ_ID_PATTERN.fullmatch(read_id) is None:
        raise FederationViolation(
            "read_id does not satisfy the immutable receipt pattern"
        )
    digest = hashlib.sha256(read_id.encode()).hexdigest()
    receipt_path = ledger_dir / f"{digest}.json"
    body = {
        "schema_version": RECEIPT_SCHEMA,
        "study_id": contract.STUDY_ID,
        "read_id": read_id,
        "requested_at": requested_at.astimezone(timezone.utc).isoformat(),
        "federation_id": federation_id,
        "pilot_run_id": pilot_run_id,
        "registry_sha256": registry_sha256,
        "report_sha256": report_sha256,
        "packages": sorted(packages, key=lambda row: str(row["site_id"])),
    }
    _create_only(receipt_path, _pretty_json(body), mode=0o600)
    return receipt_path


def combine_site_packages(
    registry: Mapping[str, Any],
    packages: Sequence[Mapping[str, Any]],
    *,
    read_id: str,
    ledger_dir: Path,
    output_path: Path,
    requested_at: datetime | None = None,
) -> dict[str, Any]:
    """Verify and combine site packages without expanding their privacy surface."""

    validate_registry(registry)
    if not packages:
        raise FederationViolation("at least one site package is required")
    sites = _registry_sites(registry)
    verified: dict[str, Mapping[str, Any]] = {}
    package_hashes: dict[str, str] = {}
    for package in packages:
        site = verify_site_package(package, registry)
        site_id = str(site["site_id"])
        if site_id in verified:
            raise FederationViolation(f"duplicate package for site {site_id!r}")
        verified[site_id] = package
        package_hashes[site_id] = _sha256_bytes(_canonical_json(package))
    missing = sorted(
        site_id
        for site_id, site in sites.items()
        if site["required"] and site_id not in verified
    )
    if missing:
        raise FederationViolation(f"required sites are missing: {missing}")

    ordered = [verified[site_id] for site_id in sorted(verified)]
    independence_sizes, token_links, domain_links = _merge_cluster_sizes(ordered, sites)
    task_sizes, task_links = _merge_task_sizes(ordered)
    total_fields = (
        "episode_denominator",
        "pending_episodes",
        "censored_episodes",
        "unscorable_observed_episodes",
        "usable_episodes",
        "usable_primary_adverse_outcomes",
    )
    totals = {
        field: sum(int(package["inventory"][field]) for package in ordered)
        for field in total_fields
    }
    site_counts = {
        str(package["site_id"]): int(package["inventory"]["episode_denominator"])
        for package in ordered
    }
    federation_unit_counts: Counter[str] = Counter()
    for site_id, count in site_counts.items():
        federation_unit_counts[str(sites[site_id]["federation_unit_id"])] += count
    now = requested_at or datetime.now(timezone.utc)
    registry_hash = _sha256_bytes(_canonical_json(registry))
    receipt_name = f"{hashlib.sha256(read_id.encode()).hexdigest()}.json"
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "status": STATUS,
        "decision_authority": "NONE",
        "study_id": contract.STUDY_ID,
        "dataset_namespace": contract.DATASET_NAMESPACE,
        "federation_id": registry["federation_id"],
        "pilot_run_id": registry["pilot_run_id"],
        "registry_version": registry["registry_version"],
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "combine_receipt": receipt_name,
        "registry_sha256": registry_hash,
        "site_package_sha256": dict(sorted(package_hashes.items())),
        "site_count": len(ordered),
        "federation_unit_count": len(federation_unit_counts),
        "totals": totals,
        "usable_primary_adverse_rate": (
            totals["usable_primary_adverse_outcomes"] / totals["usable_episodes"]
            if totals["usable_episodes"]
            else None
        ),
        "independence_unit_cluster_sizes": independence_sizes,
        "task_cluster_sizes": task_sizes,
        "schedule_class_counts": _sum_count_maps(ordered, "schedule_class_counts"),
        "maturity_stage_counts": _sum_count_maps(ordered, "maturity_stage_counts"),
        "concentration": {
            "maximum_site_episode_share": _maximum_share(site_counts.values()),
            "maximum_federation_unit_episode_share": _maximum_share(
                federation_unit_counts.values()
            ),
            "maximum_independence_unit_episode_share": _maximum_share(
                independence_sizes
            ),
            "maximum_task_episode_share": _maximum_share(task_sizes),
        },
        "cross_site_linkage": {
            "independence_token_unions": token_links,
            "shared_state_domain_unions": domain_links,
            "task_token_unions": task_links,
        },
        "privacy": {
            "raw_episodes_received": False,
            "arm_scores_received": False,
            "raw_identifiers_received": False,
            "only_signed_site_aggregates_accepted": True,
        },
        "paired_score_outcome_access": "NOT_AUTHORIZED_BY_PILOT_INSTRUMENTATION",
        "required_next_step": "SEPARATELY_REVIEWED_REGISTERED_ANALYSIS",
    }
    _validate_schema(report, REPORT_SCHEMA_PATH)
    report_payload = _pretty_json(report)
    package_records = _package_acceptance_records(ordered, package_hashes)
    with _combine_ledger_lock(ledger_dir):
        if output_path.exists():
            raise FederationViolation(
                f"refusing to replace immutable artifact: {output_path}"
            )
        replay_errors = _replay_errors(
            registry,
            ordered,
            package_records,
            _load_combine_receipts(ledger_dir),
        )
        if replay_errors:
            raise FederationViolation("; ".join(replay_errors))
        _record_combine_receipt(
            read_id=read_id,
            ledger_dir=ledger_dir,
            federation_id=str(registry["federation_id"]),
            pilot_run_id=str(registry["pilot_run_id"]),
            registry_sha256=registry_hash,
            packages=package_records,
            report_sha256=_sha256_bytes(report_payload),
            requested_at=now,
        )
        _create_only(output_path, report_payload, mode=0o600)
    return report


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    keygen = commands.add_parser("keygen", help="create an Ed25519 site keypair")
    keygen.add_argument("--private-key", type=Path, required=True)
    keygen.add_argument("--public-record", type=Path, required=True)
    keygen.add_argument("--key-id", required=True)

    linkage = commands.add_parser(
        "linkage-keygen", help="create a shared federation linkage key"
    )
    linkage.add_argument("--output", type=Path, required=True)

    export = commands.add_parser("export", help="write a signed site pilot package")
    export.add_argument("--root", type=Path, required=True)
    export.add_argument("--registry", type=Path, required=True)
    export.add_argument("--read-id", required=True)
    export.add_argument("--ledger-dir", type=Path, required=True)
    export.add_argument("--site-id", required=True)
    export.add_argument("--export-sequence", type=int, required=True)
    export.add_argument("--signing-key", type=Path, required=True)
    export.add_argument("--linkage-key", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)

    combine = commands.add_parser(
        "combine", help="verify and combine signed site pilot packages"
    )
    combine.add_argument("--registry", type=Path, required=True)
    combine.add_argument("--package", type=Path, action="append", required=True)
    combine.add_argument("--read-id", required=True)
    combine.add_argument("--ledger-dir", type=Path, required=True)
    combine.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "keygen":
            generate_signing_key(
                args.private_key, args.public_record, key_id=args.key_id
            )
            print(
                f"PASS: wrote private key {args.private_key} and {args.public_record}"
            )
        elif args.command == "linkage-keygen":
            generate_linkage_key(args.output)
            print(f"PASS: wrote private linkage key {args.output}")
        elif args.command == "export":
            package = export_site_package(
                args.root,
                registry=contract.load_json(args.registry),
                read_id=args.read_id,
                ledger_dir=args.ledger_dir,
                site_id=args.site_id,
                export_sequence=args.export_sequence,
                signing_key_path=args.signing_key,
                linkage_key_path=args.linkage_key,
                output_path=args.output,
            )
            print(
                f"PASS: wrote signed package for {package['site_id']} to {args.output}"
            )
        else:
            report = combine_site_packages(
                contract.load_json(args.registry),
                [contract.load_json(path) for path in args.package],
                read_id=args.read_id,
                ledger_dir=args.ledger_dir,
                output_path=args.output,
            )
            print(json.dumps(report, indent=2, sort_keys=True))
    except contract.ContractViolation as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

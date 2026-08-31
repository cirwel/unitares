"""Tests for the signed, privacy-preserving EISV pilot federation seam."""

from __future__ import annotations

import copy
import hashlib
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, strategies as st

from scripts.analysis import eisv_incremental_value_contract as contract
from scripts.analysis import eisv_incremental_value_federation as federation
from scripts.analysis import eisv_incremental_value_pilot as pilot


OBSERVED_AT = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)
EPISODE_IDS = (
    "ee96f5fc-3127-4baa-9c12-9af493d352a7",
    "ee96f5fc-3127-4baa-9c12-9af493d352a8",
    "ee96f5fc-3127-4baa-9c12-9af493d352a9",
    "ee96f5fc-3127-4baa-9c12-9af493d352aa",
)
PREDICTION_IDS = (
    "eb659c1f-a64e-4b5d-a2fb-90cd0205bd7a",
    "eb659c1f-a64e-4b5d-a2fb-90cd0205bd7b",
    "eb659c1f-a64e-4b5d-a2fb-90cd0205bd7c",
    "eb659c1f-a64e-4b5d-a2fb-90cd0205bd7d",
)


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return path


def _enabled_manifest(tmp_path: Path, suffix: str) -> tuple[Path, dict[str, Any]]:
    manifest = contract.load_json(pilot.DEFAULT_MANIFEST_PATH)
    manifest["collection_enabled"] = True
    manifest["pilot_authorization"] = {
        "authorization_id": f"federation-test-{suffix}",
        "start_not_before": "2026-08-27T11:00:00Z",
    }
    return _write_json(tmp_path / f"manifest-{suffix}.json", manifest), manifest


def _episode(
    index: int,
    *,
    independence_unit: str,
    substrate_hash: str,
    producer_group: str,
    task_id: str,
) -> dict[str, Any]:
    episode = copy.deepcopy(contract.load_json(contract.DEFAULT_EXAMPLE_PATH))
    episode["episode_id"] = EPISODE_IDS[index]
    episode["prediction"]["prediction_id"] = PREDICTION_IDS[index]
    identity = episode["identity"]
    identity["independence_unit_id"] = independence_unit
    identity["substrate_id_hash"] = substrate_hash
    identity["producer_group_id"] = producer_group
    identity["task_id"] = task_id
    identity["deployment_id"] = f"deployment-{index}"
    for event_index, event in enumerate(episode["outcome"]["events"]):
        event["event_id"] = f"event-{index}-{event_index}"
        event["label_provenance"]["source_record_id"] = f"source-{index}-{event_index}"
    return episode


def _prepare_store(
    tmp_path: Path, site_id: str, episodes: list[dict[str, Any]]
) -> tuple[Path, dict[str, Any]]:
    manifest_path, manifest = _enabled_manifest(tmp_path, site_id)
    root = tmp_path / f"store-{site_id}"
    pilot.initialize_store(root, manifest_path)
    for episode in episodes:
        pilot.emit_episode(root, episode, observed_at=OBSERVED_AT)
    return root, manifest


def _site_keys(tmp_path: Path, site_id: str) -> tuple[Path, dict[str, Any]]:
    private = tmp_path / f"{site_id}.pem"
    public = tmp_path / f"{site_id}-public.json"
    federation.generate_signing_key(private, public, key_id=f"key-{site_id}")
    return private, contract.load_json(public)


def _site_entry(
    site_id: str,
    public: dict[str, Any],
    *,
    required: bool = True,
    shared_state_domain: str | None = None,
) -> dict[str, Any]:
    return {
        "site_id": site_id,
        "key_id": public["key_id"],
        "public_key_base64": public["public_key_base64"],
        "key_status": "active",
        "key_valid_from": "2026-01-01T00:00:00Z",
        "key_valid_until": "2027-01-01T00:00:00Z",
        "required": required,
        "identity_namespace": "identities-shared",
        "task_namespace": "tasks-shared",
        "shared_state_domain": shared_state_domain or f"state-{site_id}",
        "federation_unit_id": f"unit-{site_id}",
    }


def _registry(manifest: dict[str, Any], sites: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": federation.REGISTRY_SCHEMA,
        "study_id": contract.STUDY_ID,
        "dataset_namespace": contract.DATASET_NAMESPACE,
        "phase": "pilot",
        "federation_id": "federation-test",
        "pilot_run_id": "pilot-run-test",
        "registry_version": "registry-test-v1",
        "created_at": "2026-08-27T10:00:00Z",
        "linkage_key_id": "linkage-test-v1",
        "coordinator_ledger_id": "coordinator-ledger-test-v1",
        "privacy_policy": {
            "minimum_cell_count": 2,
            "minimum_export_interval_seconds": 3600,
            "maximum_exports_per_site": 1,
        },
        "expected_contract": federation.contract_fingerprint(manifest),
        "sites": sites,
    }


def _export(
    tmp_path: Path,
    *,
    site_id: str,
    episodes: list[dict[str, Any]],
    linkage_key: Path,
    sequence: int = 1,
    requested_at: datetime | None = None,
    registry: dict[str, Any] | None = None,
    private: Path | None = None,
    public: dict[str, Any] | None = None,
    suffix: str = "",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    root, manifest = _prepare_store(tmp_path, f"{site_id}{suffix}", episodes)
    if private is None or public is None:
        private, public = _site_keys(tmp_path, f"{site_id}{suffix}")
    if registry is None:
        registry = _registry(manifest, [_site_entry(site_id, public)])
    package = federation.export_site_package(
        root,
        registry=registry,
        site_id=site_id,
        export_sequence=sequence,
        read_id=f"export-{site_id}{suffix}-{sequence}",
        ledger_dir=tmp_path / f"ledger-{site_id}{suffix}",
        signing_key_path=private,
        linkage_key_path=linkage_key,
        output_path=tmp_path / f"package-{site_id}{suffix}-{sequence}.json",
        requested_at=requested_at,
    )
    return package, public, manifest


def test_generated_secrets_are_private_and_create_only(tmp_path: Path) -> None:
    private = tmp_path / "site.pem"
    public = tmp_path / "site-public.json"
    linkage = tmp_path / "linkage.key"
    federation.generate_signing_key(private, public, key_id="key-site-one")
    federation.generate_linkage_key(linkage)
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(linkage.stat().st_mode) == 0o600
    assert stat.S_IMODE(public.stat().st_mode) == 0o644
    with pytest.raises(federation.FederationViolation, match="refusing to replace"):
        federation.generate_linkage_key(linkage)


def test_singleton_site_release_is_rejected(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    episode = _episode(
        0,
        independence_unit="local-cluster-secret",
        substrate_hash="a" * 64,
        producer_group="producer-secret",
        task_id="task-secret",
    )
    with pytest.raises(federation.FederationViolation, match="denominator is below"):
        _export(tmp_path, site_id="site-one", episodes=[episode], linkage_key=linkage)
    assert not (tmp_path / "package-site-one-1.json").exists()


def test_valid_release_and_receipts_contain_no_raw_values(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    episodes = [
        _episode(
            index,
            independence_unit="local-cluster-secret",
            substrate_hash="a" * 64,
            producer_group="producer-secret",
            task_id="task-secret",
        )
        for index in (0, 1)
    ]
    package, _, _ = _export(
        tmp_path, site_id="site-one", episodes=episodes, linkage_key=linkage
    )
    exported = json.dumps(package)
    receipt_text = "".join(
        path.read_text() for path in (tmp_path / "ledger-site-one").glob("*.json")
    )
    for secret in (
        episodes[0]["identity"]["agent_uuid"],
        "local-cluster-secret",
        "producer-secret",
        "task-secret",
        "a" * 64,
        "risk_probability",
        '"arms"',
    ):
        assert secret not in exported
        assert secret not in receipt_text


def test_every_sub_k_inventory_stratum_is_suppressed() -> None:
    inventory = {
        "schema_version": pilot.INVENTORY_SCHEMA,
        "status": "PILOT_AGGREGATE_ONLY",
        "study_id": contract.STUDY_ID,
        "access_receipt": "a" * 64 + ".json",
        "episode_denominator": 4,
        "pending_episodes": 1,
        "censored_episodes": 0,
        "unscorable_observed_episodes": 0,
        "usable_episodes": 3,
        "usable_primary_adverse_outcomes": 1,
        "usable_primary_adverse_rate": 1 / 3,
        "independence_unit_cluster_sizes": [1, 3],
        "task_cluster_sizes": [4],
        "schedule_class_counts": {"interactive": 3, "scheduled": 1},
        "maturity_stage_counts": {"cold": 1, "self_relative": 3},
        "paired_loss_difference_sd": None,
        "paired_score_outcome_access": "NOT_AUTHORIZED_BY_PILOT_INSTRUMENTATION",
    }
    protected = federation._privacy_protect_inventory(inventory, 2)
    assert protected["pending_episodes"] is None
    assert protected["usable_episodes"] is None
    assert protected["suppressed_status_episode_count"] == 4
    assert protected["usable_primary_adverse_outcomes"] is None
    assert protected["usable_primary_adverse_rate"] is None
    assert protected["primary_outcome_suppressed"] is True
    assert protected["schedule_class_counts"] == {
        "__suppressed__": 1,
        "interactive": 3,
    }
    assert protected["maturity_stage_counts"] == {
        "__suppressed__": 1,
        "self_relative": 3,
    }
    assert "independence_unit_cluster_sizes" not in protected
    assert "task_cluster_sizes" not in protected


def test_tampered_signature_is_rejected(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    episode = _episode(
        0,
        independence_unit="cluster-one",
        substrate_hash="a" * 64,
        producer_group="producer-one",
        task_id="task-one",
    )
    package, public, manifest = _export(
        tmp_path,
        site_id="site-one",
        episodes=[
            episode,
            _episode(
                1,
                independence_unit="cluster-one",
                substrate_hash="a" * 64,
                producer_group="producer-one",
                task_id="task-one",
            ),
        ],
        linkage_key=linkage,
    )
    registry = _registry(manifest, [_site_entry("site-one", public)])
    package["attestation"]["signature_base64"] = "A" * 88
    with pytest.raises(federation.FederationViolation, match="signature is invalid"):
        federation.verify_site_package(package, registry)


def test_registry_snapshot_substitution_is_rejected(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    episode = _episode(
        0,
        independence_unit="cluster-one",
        substrate_hash="a" * 64,
        producer_group="producer-one",
        task_id="task-one",
    )
    package, public, manifest = _export(
        tmp_path,
        site_id="site-one",
        episodes=[
            episode,
            _episode(
                1,
                independence_unit="cluster-one",
                substrate_hash="a" * 64,
                producer_group="producer-one",
                task_id="task-one",
            ),
        ],
        linkage_key=linkage,
    )
    registry = _registry(manifest, [_site_entry("site-one", public)])
    registry["registry_version"] = "registry-test-v2"
    with pytest.raises(federation.FederationViolation, match="registry snapshot"):
        federation.verify_site_package(package, registry)


def test_revoked_key_rejected_before_local_read(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    root, manifest = _prepare_store(tmp_path, "site-one", [])
    private, public = _site_keys(tmp_path, "site-one")
    site = _site_entry("site-one", public)
    site["key_status"] = "revoked"
    registry = _registry(manifest, [site])
    ledger = tmp_path / "ledger"
    with pytest.raises(federation.FederationViolation, match="not active"):
        federation.export_site_package(
            root,
            registry=registry,
            site_id="site-one",
            export_sequence=1,
            read_id="revoked-key",
            ledger_dir=ledger,
            signing_key_path=private,
            linkage_key_path=linkage,
            output_path=tmp_path / "package.json",
            requested_at=None,
        )
    assert not ledger.exists()


def test_combine_links_cross_site_substrates_and_tasks(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    episodes_a = [
        _episode(
            index,
            independence_unit="cluster-a",
            substrate_hash="a" * 64,
            producer_group="producer-a",
            task_id="shared-task",
        )
        for index in (0, 1)
    ]
    episodes_b = [
        _episode(
            index,
            independence_unit="cluster-b",
            substrate_hash="a" * 64,
            producer_group="producer-b",
            task_id="shared-task",
        )
        for index in (2, 3)
    ]
    root_a, manifest = _prepare_store(tmp_path, "site-one", episodes_a)
    root_b, _ = _prepare_store(tmp_path, "site-two", episodes_b)
    private_a, public_a = _site_keys(tmp_path, "site-one")
    private_b, public_b = _site_keys(tmp_path, "site-two")
    registry = _registry(
        manifest,
        [_site_entry("site-one", public_a), _site_entry("site-two", public_b)],
    )
    packages = []
    for site_id, root, private in (
        ("site-one", root_a, private_a),
        ("site-two", root_b, private_b),
    ):
        packages.append(
            federation.export_site_package(
                root,
                registry=registry,
                site_id=site_id,
                export_sequence=1,
                read_id=f"export-{site_id}",
                ledger_dir=tmp_path / f"ledger-{site_id}",
                signing_key_path=private,
                linkage_key_path=linkage,
                output_path=tmp_path / f"package-{site_id}.json",
                requested_at=None,
            )
        )
    report = federation.combine_site_packages(
        registry,
        packages,
        read_id="combine-two-sites",
        ledger_dir=tmp_path / "combine-ledger",
        output_path=tmp_path / "combined.json",
        requested_at=None,
    )
    assert report["totals"]["episode_denominator"] == 4
    assert report["independence_unit_cluster_sizes"] == [4]
    assert report["task_cluster_sizes"] == [4]
    assert report["cross_site_linkage"]["independence_token_unions"] == 1
    assert report["cross_site_linkage"]["task_token_unions"] == 1
    assert report["decision_authority"] == "NONE"
    assert "paired_loss_difference_sd" not in report
    receipt = contract.load_json(
        tmp_path / "combine-ledger" / report["combine_receipt"]
    )
    assert (
        receipt["report_sha256"]
        == hashlib.sha256((tmp_path / "combined.json").read_bytes()).hexdigest()
    )


def test_shared_state_domain_unions_suppressed_singletons(tmp_path: Path) -> None:
    packages = [
        {
            "site_id": site,
            "payload_sha256": token,
            "linkage": {
                "independence_clusters": [],
                "suppressed_independence_episode_count": 1,
            },
        }
        for site, token in (("site-one", "a" * 64), ("site-two", "b" * 64))
    ]
    sites = {
        site: {"shared_state_domain": "shared-host"}
        for site in ("site-one", "site-two")
    }
    sizes, _, domain_links = federation._merge_cluster_sizes(packages, sites)
    assert sizes == [2]
    assert domain_links == 1


def test_unique_site_domain_preserves_visible_local_clusters() -> None:
    package = {
        "site_id": "site-one",
        "payload_sha256": "a" * 64,
        "linkage": {
            "independence_clusters": [
                {
                    "cluster_token": "a" * 64,
                    "episode_count": 2,
                    "substrate_tokens": ["b" * 64],
                    "producer_tokens": ["c" * 64],
                },
                {
                    "cluster_token": "d" * 64,
                    "episode_count": 3,
                    "substrate_tokens": ["e" * 64],
                    "producer_tokens": ["f" * 64],
                },
            ],
            "suppressed_independence_episode_count": 0,
        },
    }
    sizes, _, domain_links = federation._merge_cluster_sizes(
        [package], {"site-one": {"shared_state_domain": "unique-domain"}}
    )
    assert sizes == [2, 3]
    assert domain_links == 0


def test_empty_required_site_is_explicit(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    package, public, manifest = _export(
        tmp_path, site_id="site-one", episodes=[], linkage_key=linkage
    )
    registry = _registry(manifest, [_site_entry("site-one", public)])
    report = federation.combine_site_packages(
        registry,
        [package],
        read_id="combine-empty",
        ledger_dir=tmp_path / "combine-ledger",
        output_path=tmp_path / "combined.json",
        requested_at=None,
    )
    assert report["totals"]["episode_denominator"] == 0
    assert report["independence_unit_cluster_sizes"] == []
    assert report["concentration"]["maximum_site_episode_share"] is None


def test_combine_requires_every_required_site(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    root, manifest = _prepare_store(tmp_path, "site-one", [])
    private_a, public_a = _site_keys(tmp_path, "site-one")
    _, public_b = _site_keys(tmp_path, "site-two")
    registry = _registry(
        manifest,
        [_site_entry("site-one", public_a), _site_entry("site-two", public_b)],
    )
    package = federation.export_site_package(
        root,
        registry=registry,
        site_id="site-one",
        export_sequence=1,
        read_id="export-site-one",
        ledger_dir=tmp_path / "site-ledger",
        signing_key_path=private_a,
        linkage_key_path=linkage,
        output_path=tmp_path / "package-site-one.json",
    )
    with pytest.raises(federation.FederationViolation, match="required sites"):
        federation.combine_site_packages(
            registry,
            [package],
            read_id="combine-missing",
            ledger_dir=tmp_path / "combine-ledger",
            output_path=tmp_path / "combined.json",
        )


def test_exact_replay_is_rejected_by_durable_ledger(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    package, public, manifest = _export(
        tmp_path, site_id="site-one", episodes=[], linkage_key=linkage
    )
    registry = _registry(manifest, [_site_entry("site-one", public)])
    ledger = tmp_path / "combine-ledger"
    federation.combine_site_packages(
        registry,
        [package],
        read_id="combine-first",
        ledger_dir=ledger,
        output_path=tmp_path / "first.json",
        requested_at=None,
    )
    with pytest.raises(federation.FederationViolation, match="exact replay"):
        federation.combine_site_packages(
            registry,
            [package],
            read_id="combine-second",
            ledger_dir=ledger,
            output_path=tmp_path / "second.json",
            requested_at=None,
        )


def test_split_coordinators_are_outside_single_ledger_guarantee(
    tmp_path: Path,
) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    package, public, manifest = _export(
        tmp_path, site_id="site-one", episodes=[], linkage_key=linkage
    )
    registry = _registry(manifest, [_site_entry("site-one", public)])
    for suffix in ("a", "b"):
        report = federation.combine_site_packages(
            registry,
            [package],
            read_id=f"combine-ledger-{suffix}",
            ledger_dir=tmp_path / f"combine-ledger-{suffix}",
            output_path=tmp_path / f"combined-{suffix}.json",
        )
        assert report["decision_authority"] == "NONE"


def test_unknown_or_tampered_ledger_artifact_fails_closed(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    package, public, manifest = _export(
        tmp_path, site_id="site-one", episodes=[], linkage_key=linkage
    )
    registry = _registry(manifest, [_site_entry("site-one", public)])
    ledger = tmp_path / "combine-ledger"
    ledger.mkdir()
    _write_json(ledger / "tampered.json", {"schema_version": "unknown"})
    with pytest.raises(federation.FederationViolation, match="unknown artifact"):
        federation.combine_site_packages(
            registry,
            [package],
            read_id="combine-tampered-ledger",
            ledger_dir=ledger,
            output_path=tmp_path / "combined.json",
        )


def test_cadence_uses_coordinator_acceptance_time() -> None:
    manifest = contract.load_json(pilot.DEFAULT_MANIFEST_PATH)
    registry = _registry(
        manifest,
        [
            {
                **_site_entry(
                    "site-one",
                    {
                        "key_id": "key-site-one",
                        "public_key_base64": "A" * 44,
                    },
                ),
                "public_key_base64": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
            }
        ],
    )
    accepted_at = datetime.now(timezone.utc)
    prior = {
        "schema_version": federation.RECEIPT_SCHEMA,
        "coordinator_ledger_id": registry["coordinator_ledger_id"],
        "federation_id": registry["federation_id"],
        "pilot_run_id": registry["pilot_run_id"],
        "registry_sha256": federation._sha256_bytes(
            federation._canonical_json(registry)
        ),
        "requested_at": accepted_at.isoformat(),
        "packages": [
            {
                "site_id": "site-one",
                "package_sha256": "a" * 64,
                "payload_sha256": "b" * 64,
                "package_nonce": "c" * 32,
                "export_sequence": 1,
                "generated_at": "2020-01-01T00:00:00Z",
            }
        ],
    }
    current = {
        "site_id": "site-one",
        "package_sha256": "d" * 64,
        "payload_sha256": "e" * 64,
        "package_nonce": "f" * 32,
        "export_sequence": 2,
        "generated_at": "2030-01-01T00:00:00Z",
    }
    errors = federation._replay_errors(
        registry,
        [{}],
        [current],
        [prior],
        accepted_at=accepted_at,
    )
    assert "minimum export interval" in "; ".join(errors)


def test_second_release_is_rejected_even_with_fresh_sequence(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    root, manifest = _prepare_store(tmp_path, "site-one", [])
    private, public = _site_keys(tmp_path, "site-one")
    registry = _registry(manifest, [_site_entry("site-one", public)])
    packages = []
    for sequence, suffix in enumerate(("first", "second"), start=1):
        packages.append(
            federation.export_site_package(
                root,
                registry=registry,
                site_id="site-one",
                export_sequence=sequence,
                read_id=f"export-{suffix}",
                ledger_dir=tmp_path / "site-ledger",
                signing_key_path=private,
                linkage_key_path=linkage,
                output_path=tmp_path / f"package-{suffix}.json",
                requested_at=None,
            )
        )
    ledger = tmp_path / "combine-ledger"
    federation.combine_site_packages(
        registry,
        [packages[0]],
        read_id="combine-first",
        ledger_dir=ledger,
        output_path=tmp_path / "first.json",
        requested_at=None,
    )
    with pytest.raises(federation.FederationViolation, match="single-release"):
        federation.combine_site_packages(
            registry,
            [packages[1]],
            read_id="combine-second",
            ledger_dir=ledger,
            output_path=tmp_path / "second.json",
            requested_at=None,
        )


def test_registry_rejects_duplicate_key_identity(tmp_path: Path) -> None:
    _, public = _site_keys(tmp_path, "site-one")
    manifest = contract.load_json(pilot.DEFAULT_MANIFEST_PATH)
    registry = _registry(
        manifest,
        [_site_entry("site-one", public), _site_entry("site-two", public)],
    )
    registry["sites"][1]["key_id"] = "key-site-two"
    with pytest.raises(federation.FederationViolation, match="public keys"):
        federation.validate_registry(registry)


def test_report_schema_rejects_scientific_inference_field(tmp_path: Path) -> None:
    schema = contract.load_json(federation.REPORT_SCHEMA_PATH)
    report = {
        key: value.get("const")
        for key, value in schema["properties"].items()
        if "const" in value
    }
    report["p_value"] = 0.01
    assert any(
        "p_value" in error for error in contract.schema_errors(report, schema=schema)
    )


@given(st.lists(st.integers(min_value=2, max_value=100), min_size=1, max_size=20))
def test_union_preserves_total_episode_count(cluster_sizes: list[int]) -> None:
    package = {
        "site_id": "site-one",
        "payload_sha256": "a" * 64,
        "linkage": {
            "independence_clusters": [
                {
                    "cluster_token": f"{index:064x}",
                    "episode_count": count,
                    "substrate_tokens": [],
                    "producer_tokens": [],
                }
                for index, count in enumerate(cluster_sizes, start=1)
            ],
            "suppressed_independence_episode_count": 0,
        },
    }
    merged, _, _ = federation._merge_cluster_sizes(
        [package], {"site-one": {"shared_state_domain": "unique-domain"}}
    )
    assert sum(merged) == sum(cluster_sizes)


def test_token_collision_is_rejected_semantically() -> None:
    package = {
        "inventory": {
            "episode_denominator": 4,
            "pending_episodes": 0,
            "censored_episodes": 0,
            "unscorable_observed_episodes": 0,
            "usable_episodes": 4,
            "suppressed_status_episode_count": 0,
            "usable_primary_adverse_outcomes": 0,
            "usable_primary_adverse_rate": 0.0,
            "primary_outcome_suppressed": False,
            "schedule_class_counts": {"scheduled": 4},
            "maturity_stage_counts": {"closed": 4},
            "access_receipt": "receipt.json",
        },
        "local_read": {"receipt_name": "receipt.json"},
        "reporting_window": {
            "first_prediction_at": "2026-08-27T00:00:00Z",
            "last_prediction_at": "2026-08-27T01:00:00Z",
            "latest_outcome_close_at": None,
        },
        "linkage": {
            "minimum_cell_count": 2,
            "independence_clusters": [
                {
                    "cluster_token": token,
                    "episode_count": 2,
                    "substrate_tokens": [],
                    "producer_tokens": [],
                }
                for token in ("a" * 64, "a" * 64)
            ],
            "suppressed_independence_episode_count": 0,
            "task_clusters": [{"task_token": "b" * 64, "episode_count": 4}],
            "suppressed_task_episode_count": 0,
        },
        "payload_sha256": "0" * 64,
    }
    assert "tokens are not unique" in "; ".join(
        federation.site_package_semantic_errors(package)
    )


def test_world_readable_linkage_key_fails_closed(tmp_path: Path) -> None:
    linkage = tmp_path / "linkage.key"
    federation.generate_linkage_key(linkage)
    linkage.chmod(0o644)
    with pytest.raises(federation.FederationViolation, match="group/world"):
        federation._load_linkage_key(linkage)

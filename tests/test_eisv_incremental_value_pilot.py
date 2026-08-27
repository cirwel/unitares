"""Tests for the disabled-by-default EISV instrumentation-pilot store."""

from __future__ import annotations

import copy
import json
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.analysis import eisv_incremental_value_contract as contract
from scripts.analysis import eisv_incremental_value_pilot as pilot


OBSERVED_AT = datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def manifest() -> dict[str, Any]:
    return contract.load_json(pilot.DEFAULT_MANIFEST_PATH)


@pytest.fixture
def episode() -> dict[str, Any]:
    return contract.load_json(contract.DEFAULT_EXAMPLE_PATH)


def _write_json(path: Path, value: dict[str, Any]) -> Path:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    return path


def _enabled_manifest(tmp_path: Path, manifest: dict[str, Any]) -> Path:
    enabled = copy.deepcopy(manifest)
    enabled["collection_enabled"] = True
    enabled["pilot_authorization"] = {
        "authorization_id": "test-authorization-only",
        "start_not_before": "2026-08-27T11:00:00Z",
    }
    return _write_json(tmp_path / "enabled-manifest.json", enabled)


def _different_episode(episode: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(episode)
    result["episode_id"] = "ee96f5fc-3127-4baa-9c12-9af493d352a8"
    result["prediction"]["prediction_id"] = "eb659c1f-a64e-4b5d-a2fb-90cd0205bd7a"
    for index, event in enumerate(result["outcome"]["events"]):
        event["event_id"] = f"second-event-{index}"
        event["label_provenance"]["source_record_id"] = f"second-source-{index}"
    return result


def test_checked_in_manifest_is_valid_bound_and_disabled(
    manifest: dict[str, Any],
) -> None:
    pilot.validate_manifest(manifest)
    assert manifest["lifecycle_status"] == "pilot_provisional"
    assert manifest["collection_enabled"] is False
    assert manifest["confirmatory_freeze"]["status"] == "not_frozen"


def test_disabled_manifest_initializes_but_rejects_emission(
    tmp_path: Path, episode: dict[str, Any]
) -> None:
    pilot.initialize_store(tmp_path)

    with pytest.raises(pilot.PilotStoreViolation, match="collection is disabled"):
        pilot.emit_episode(tmp_path, episode, observed_at=OBSERVED_AT)


def test_manifest_is_create_only(
    tmp_path: Path, manifest: dict[str, Any]
) -> None:
    pilot.initialize_store(tmp_path)
    other = copy.deepcopy(manifest)
    other["versions"]["config_version"] = "pilot.2"
    different = _write_json(tmp_path / "different.json", other)

    with pytest.raises(pilot.PilotStoreViolation, match="immutable and differs"):
        pilot.initialize_store(tmp_path, different)


def test_enabled_manifest_commits_private_atomic_bundle(
    tmp_path: Path, manifest: dict[str, Any], episode: dict[str, Any]
) -> None:
    manifest_path = _enabled_manifest(tmp_path, manifest)
    root = tmp_path / "store"
    pilot.initialize_store(root, manifest_path)

    bundle = pilot.emit_episode(root, episode, observed_at=OBSERVED_AT)

    assert bundle.name == episode["episode_id"]
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    assert stat.S_IMODE((bundle / "episode.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((bundle / "structural.json").stat().st_mode) == 0o600
    sidecar_text = (bundle / "structural.json").read_text()
    assert "risk_probability" not in sidecar_text
    assert '"arms"' not in sidecar_text
    assert not list(bundle.parent.glob(".tmp-*"))


def test_version_mismatch_fails_without_partial_bundle(
    tmp_path: Path, manifest: dict[str, Any], episode: dict[str, Any]
) -> None:
    root = tmp_path / "store"
    pilot.initialize_store(root, _enabled_manifest(tmp_path, manifest))
    episode["study_config"]["scorer_bundle_version"] = "pilot.other"

    with pytest.raises(pilot.PilotStoreViolation, match="scorer_bundle_version"):
        pilot.emit_episode(root, episode, observed_at=OBSERVED_AT)

    assert not list((root / contract.DATASET_NAMESPACE / "episodes").iterdir())


def test_duplicate_prediction_and_event_ids_fail_closed(
    tmp_path: Path, manifest: dict[str, Any], episode: dict[str, Any]
) -> None:
    root = tmp_path / "store"
    pilot.initialize_store(root, _enabled_manifest(tmp_path, manifest))
    pilot.emit_episode(root, episode, observed_at=OBSERVED_AT)
    duplicate = _different_episode(episode)
    duplicate["prediction"]["prediction_id"] = episode["prediction"]["prediction_id"]
    duplicate["outcome"]["events"][0]["event_id"] = episode["outcome"]["events"][0]["event_id"]

    with pytest.raises(
        pilot.PilotStoreViolation, match="duplicate prediction_id.*duplicate event_id"
    ):
        pilot.emit_episode(root, duplicate, observed_at=OBSERVED_AT)


@pytest.mark.parametrize("shared_key", ["substrate_id_hash", "producer_group_id"])
def test_independence_mapping_is_checked_across_bundles(
    tmp_path: Path,
    manifest: dict[str, Any],
    episode: dict[str, Any],
    shared_key: str,
) -> None:
    root = tmp_path / "store"
    pilot.initialize_store(root, _enabled_manifest(tmp_path, manifest))
    pilot.emit_episode(root, episode, observed_at=OBSERVED_AT)
    other = _different_episode(episode)
    other["identity"]["independence_unit_id"] = "different-unit"
    other["identity"][shared_key] = episode["identity"][shared_key]
    unrelated = {"substrate_id_hash", "producer_group_id"}.difference({shared_key}).pop()
    other["identity"][unrelated] = (
        "d" * 64 if unrelated == "substrate_id_hash" else "different-producer-group"
    )

    with pytest.raises(pilot.PilotStoreViolation, match=f"{shared_key} maps to both"):
        pilot.emit_episode(root, other, observed_at=OBSERVED_AT)


def test_inventory_records_receipt_and_uses_full_denominator(
    tmp_path: Path, manifest: dict[str, Any], episode: dict[str, Any]
) -> None:
    root = tmp_path / "store"
    ledger = tmp_path / "ledger"
    pilot.initialize_store(root, _enabled_manifest(tmp_path, manifest))
    pilot.emit_episode(root, episode, observed_at=OBSERVED_AT)

    report = pilot.inventory(
        root,
        read_id="pilot-inventory-test-001",
        ledger_dir=ledger,
        requested_at=datetime.now(timezone.utc),
    )

    assert report["status"] == "PILOT_AGGREGATE_ONLY"
    assert report["episode_denominator"] == 1
    assert report["pending_episodes"] == 0
    assert report["usable_primary_adverse_outcomes"] == 1
    assert report["paired_loss_difference_sd"] is None
    receipt = ledger / report["access_receipt"]
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    receipt_body = json.loads(receipt.read_text())
    assert receipt_body["access_classes"] == ["outcomes", "structural"]


def test_inventory_read_id_is_immutable(
    tmp_path: Path, manifest: dict[str, Any]
) -> None:
    root = tmp_path / "store"
    ledger = tmp_path / "ledger"
    pilot.initialize_store(root, _enabled_manifest(tmp_path, manifest))
    now = datetime.now(timezone.utc)
    pilot.inventory(root, read_id="pilot-inventory-once", ledger_dir=ledger, requested_at=now)

    with pytest.raises(contract.AccessDenied, match="already has a receipt"):
        pilot.inventory(root, read_id="pilot-inventory-once", ledger_dir=ledger, requested_at=now)


def test_pending_episode_stays_in_denominator_but_not_usable(
    tmp_path: Path, manifest: dict[str, Any], episode: dict[str, Any]
) -> None:
    root = tmp_path / "store"
    ledger = tmp_path / "ledger"
    pilot.initialize_store(root, _enabled_manifest(tmp_path, manifest))
    pending = copy.deepcopy(episode)
    pending["outcome"] = {
        "status": "pending",
        "window_closed_at": None,
        "observable_events_seen": 0,
        "primary_adverse_outcome": None,
        "secondary_operational_negative": None,
        "task_success": None,
        "quality_score": None,
        "censor_reason": None,
        "events": [],
    }
    pilot.emit_episode(root, pending, observed_at=OBSERVED_AT)

    report = pilot.inventory(
        root,
        read_id="pilot-inventory-pending",
        ledger_dir=ledger,
        requested_at=datetime.now(timezone.utc),
    )

    assert report["episode_denominator"] == 1
    assert report["pending_episodes"] == 1
    assert report["usable_episodes"] == 0
    assert report["usable_primary_adverse_rate"] is None

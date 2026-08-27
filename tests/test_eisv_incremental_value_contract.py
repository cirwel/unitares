"""Contract and contamination-firewall tests for the prospective EISV study."""

from __future__ import annotations

import copy
import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.analysis import eisv_incremental_value_contract as contract


NOW = datetime.now(timezone.utc)


@pytest.fixture
def schema() -> dict[str, Any]:
    return contract.load_json(contract.DEFAULT_SCHEMA_PATH)


@pytest.fixture
def episode() -> dict[str, Any]:
    return contract.load_json(contract.DEFAULT_EXAMPLE_PATH)


def _request(**overrides: Any) -> contract.ReadRequest:
    values: dict[str, Any] = {
        "read_id": "pilot-structural-001",
        "namespace": contract.DATASET_NAMESPACE,
        "phase": "pilot",
        "purpose": "pilot_instrumentation",
        "read_protocol": "pilot_instrumentation",
        "access_classes": frozenset({"structural"}),
        "requested_at": NOW,
    }
    values.update(overrides)
    return contract.ReadRequest(**values)


def _confirmatory_episode(
    episode: dict[str, Any],
    *,
    episode_id: str,
    side: str,
    fold_id: str = "fold-1",
) -> dict[str, Any]:
    result = copy.deepcopy(episode)
    result["episode_id"] = episode_id
    result["phase"] = "confirmatory"
    result["study_config"]["preregistration_sha256"] = "a" * 64
    result["study_config"]["independence_registry_sha256"] = "b" * 64
    result["eligibility"]["analysis_population"] = "confirmatory"
    result["evaluation_assignment"]["assignment"] = side
    result["evaluation_assignment"]["fold_id"] = fold_id
    return result


def test_example_schema_and_semantics_pass(
    schema: dict[str, Any], episode: dict[str, Any]
) -> None:
    contract.validate_episode(episode, schema=schema)


def test_malformed_shape_fails_without_entering_semantic_checks(
    schema: dict[str, Any], episode: dict[str, Any]
) -> None:
    episode["study_config"] = []

    with pytest.raises(contract.ContractViolation, match="not of type 'object'"):
        contract.validate_episode(episode, schema=schema)


def test_invalid_schema_definition_fails_closed(episode: dict[str, Any]) -> None:
    invalid_schema = {"type": "definitely-not-a-json-schema-type"}

    assert contract.schema_errors(episode, schema=invalid_schema)[0].startswith(
        "schema definition is invalid:"
    )


def test_exact_arm_registry_is_a_semantic_invariant(episode: dict[str, Any]) -> None:
    episode["arms"][0]["arm_id"] = episode["arms"][1]["arm_id"]

    errors = contract.semantic_errors(episode)

    assert "arms contain duplicate arm_id values" in errors
    assert any("arms differ from registry" in error for error in errors)


def test_all_arms_must_use_the_frozen_snapshot(episode: dict[str, Any]) -> None:
    episode["arms"][4]["input_snapshot_sha256"] = "f" * 64

    assert (
        "arms/4: input snapshot hash differs from episode"
        in contract.semantic_errors(episode)
    )


def test_primary_governance_derived_label_is_rejected(
    schema: dict[str, Any], episode: dict[str, Any]
) -> None:
    event = episode["outcome"]["events"][1]
    event["label_provenance"] = {
        "producer_class": "governance_policy",
        "independence_class": "governance_derived",
        "governance_input_overlap": True,
        "source_record_id": "verdict-001",
    }

    assert contract.schema_errors(episode, schema=schema)
    semantic = contract.semantic_errors(episode)
    assert any("primary producer is not allowed" in error for error in semantic)
    assert any("primary label is not independent" in error for error in semantic)
    assert any(
        "primary label overlaps governance inputs" in error for error in semantic
    )


def test_passing_instrument_cannot_hide_failed_no_op_control(
    schema: dict[str, Any], episode: dict[str, Any]
) -> None:
    episode["instrument_validation"]["no_op_mutation_status"] = "fail"

    assert contract.schema_errors(episode, schema=schema)


def test_formula_discontinuity_requires_a_structured_code(
    schema: dict[str, Any], episode: dict[str, Any]
) -> None:
    context = episode["observation_context"]
    context["formula_discontinuity_since_previous_observation"] = True
    context["discontinuity_codes"] = []

    assert contract.schema_errors(episode, schema=schema)


def test_eligible_unassessed_episode_fails_closed(
    schema: dict[str, Any], episode: dict[str, Any]
) -> None:
    validation = episode["instrument_validation"]
    validation["status"] = "unassessed"
    validation["requested_arm_status"] = "unassessed"
    validation["assessed_at"] = None
    validation["evidence_sha256"] = None
    validation["unassessed_reasons"] = ["requested arm did not execute"]

    with pytest.raises(contract.ContractViolation):
        contract.validate_episode(episode, schema=schema)
    assert (
        "eligible episode has non-passing instrument validation"
        in contract.semantic_errors(episode)
    )


def test_outcome_events_must_stay_inside_the_frozen_window(
    episode: dict[str, Any],
) -> None:
    episode["outcome"]["events"][1]["occurred_at"] = "2026-08-28T10:16:00Z"
    episode["outcome"]["window_closed_at"] = "2026-08-28T10:15:00Z"

    errors = contract.semantic_errors(episode)

    assert "outcome window exceeds the 24-hour horizon" in errors
    assert any("event is after window close" in error for error in errors)


def test_applied_enforcement_requires_request_and_actuation_provenance(
    schema: dict[str, Any], episode: dict[str, Any]
) -> None:
    policy = episode["production_policy"]
    policy["enforcement_applied"] = True

    assert contract.schema_errors(episode, schema=schema)
    semantic = contract.semantic_errors(episode)
    assert "applied enforcement was not requested" in semantic
    assert "applied enforcement lacks actuation provenance" in semantic


@pytest.mark.parametrize("shared_key", ["substrate_id_hash", "producer_group_id"])
def test_independence_registry_unions_shared_producers(
    episode: dict[str, Any], shared_key: str
) -> None:
    other = copy.deepcopy(episode)
    other["episode_id"] = "episode-other"
    other["identity"]["independence_unit_id"] = "different-unit"
    other["identity"][shared_key] = episode["identity"][shared_key]

    errors = contract.independence_registry_errors([episode, other])

    assert any(shared_key in error and "maps to both" in error for error in errors)


@pytest.mark.parametrize("group_key", ["independence_unit_id", "task_id"])
def test_split_assignment_rejects_group_leakage(
    episode: dict[str, Any], group_key: str
) -> None:
    train = _confirmatory_episode(episode, episode_id="train-1", side="train")
    test = _confirmatory_episode(episode, episode_id="test-1", side="test")
    other_key = {"independence_unit_id", "task_id"}.difference({group_key}).pop()
    test["identity"][other_key] = f"different-{other_key}"

    errors = contract.split_assignment_errors([train, test])

    assert any(group_key in error and "leaks across" in error for error in errors)


def test_pilot_episode_cannot_be_reused_in_confirmation(
    episode: dict[str, Any],
) -> None:
    confirmatory = _confirmatory_episode(
        episode,
        episode_id=episode["episode_id"],
        side="test",
    )

    errors = contract.split_assignment_errors([episode, confirmatory])

    assert any("appears in both pilot and confirmatory" in error for error in errors)


def test_temporal_split_requires_full_horizon_purge() -> None:
    valid = {
        "method": "rolling-origin-purged.v1",
        "group_keys": ["independence_unit_id", "task_id"],
        "purge_seconds": 86_400,
        "folds": [
            {
                "train_end": "2026-10-01T00:00:00Z",
                "test_start": "2026-10-02T00:00:00Z",
                "test_end": "2026-10-09T00:00:00Z",
            }
        ],
    }
    assert contract.temporal_split_errors(valid) == []

    invalid = copy.deepcopy(valid)
    invalid["purge_seconds"] = 3_600
    invalid["folds"][0]["test_start"] = "2026-10-01T01:00:00Z"
    errors = contract.temporal_split_errors(invalid)
    assert "temporal split purge must cover the 24-hour horizon" in errors
    assert "folds/0: purge gap is too short" in errors


def test_pilot_firewall_allows_unpaired_instrumentation_reads() -> None:
    contract.authorize_read(_request(access_classes=frozenset({"structural"})))
    contract.authorize_read(_request(access_classes=frozenset({"predictions"})))
    contract.authorize_read(_request(access_classes=frozenset({"outcomes"})))


def test_pilot_firewall_denies_paired_predictions_and_outcomes() -> None:
    request = _request(access_classes=frozenset({"predictions", "outcomes"}))

    with pytest.raises(contract.AccessDenied, match="cannot pair"):
        contract.authorize_read(request)


def test_firewall_denies_cross_study_namespace() -> None:
    with pytest.raises(contract.AccessDenied, match="cross-study"):
        contract.authorize_read(_request(namespace="historical-stop-rule"))


def test_firewall_denies_backdated_or_future_dated_request() -> None:
    with pytest.raises(contract.AccessDenied, match="trusted clock"):
        contract.authorize_read(_request(requested_at=NOW + timedelta(days=1)))


def test_registered_read_requires_frozen_boundaries_and_hashes() -> None:
    valid = _request(
        read_id="registered-confirmatory-001",
        phase="confirmatory",
        purpose="registered_analysis",
        read_protocol="registered",
        access_classes=frozenset({"predictions", "outcomes"}),
        as_of=NOW - timedelta(days=1),
        not_before=NOW - timedelta(hours=1),
        config_sha256="a" * 64,
        preregistration_sha256="b" * 64,
    )
    contract.authorize_read(valid)

    early = copy.copy(valid)
    object.__setattr__(early, "not_before", NOW + timedelta(hours=1))
    with pytest.raises(contract.AccessDenied, match="early"):
        contract.authorize_read(early)

    missing_hash = copy.copy(valid)
    object.__setattr__(missing_hash, "config_sha256", None)
    with pytest.raises(contract.AccessDenied, match="config_sha256"):
        contract.authorize_read(missing_hash)


@pytest.mark.parametrize("field", ["requested_at", "not_before", "as_of"])
def test_firewall_rejects_naive_datetimes(field: str) -> None:
    overrides: dict[str, Any] = {
        "read_id": "registered-confirmatory-naive",
        "phase": "confirmatory",
        "purpose": "registered_analysis",
        "read_protocol": "registered",
        "access_classes": frozenset({"predictions", "outcomes"}),
        "as_of": NOW - timedelta(days=1),
        "not_before": NOW - timedelta(hours=1),
        "config_sha256": "a" * 64,
        "preregistration_sha256": "b" * 64,
    }
    overrides[field] = datetime(2026, 12, 2, 11)

    with pytest.raises(contract.AccessDenied, match=f"{field} must be timezone-aware"):
        contract.authorize_read(_request(**overrides))


def test_reproduction_requires_explicit_contamination_acknowledgement() -> None:
    request = _request(
        read_id="reproduction-001",
        purpose="reproduction",
        read_protocol="reproduction",
        access_classes=frozenset({"predictions", "outcomes"}),
    )
    with pytest.raises(contract.AccessDenied, match="acknowledgement"):
        contract.authorize_read(request)

    acknowledged = copy.copy(request)
    object.__setattr__(acknowledged, "contamination_acknowledged", True)
    contract.authorize_read(acknowledged)


def test_access_receipt_is_private_and_immutable(tmp_path: Path) -> None:
    request = _request(read_id="pilot-receipt-001")

    receipt_path = contract.record_access_receipt(request, ledger_dir=tmp_path)

    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600
    receipt = json.loads(receipt_path.read_text())
    assert receipt["namespace"] == contract.DATASET_NAMESPACE
    assert receipt["access_classes"] == ["structural"]
    assert receipt["authorized_at"]
    assert not ({"password", "token", "credential"} & receipt.keys())
    with pytest.raises(contract.AccessDenied, match="already has a receipt"):
        contract.record_access_receipt(request, ledger_dir=tmp_path)


def test_missing_jsonschema_is_unassessed_not_success(
    monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any], episode: dict[str, Any]
) -> None:
    monkeypatch.setattr(contract, "Draft202012Validator", None)
    monkeypatch.setattr(contract, "FormatChecker", None)

    with pytest.raises(contract.ContractViolation, match="UNASSESSED"):
        contract.validate_episode(episode, schema=schema)

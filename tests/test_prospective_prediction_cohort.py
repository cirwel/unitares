from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from scripts.analysis.eisv_skeptic_report import OutcomeRow
from scripts.analysis import prospective_prediction_cohort as cohort_module
from scripts.analysis.prospective_prediction_cohort import (
    CONTRACT_VERSION,
    CohortContractError,
    OutcomeFunnel,
    ProspectiveCohortSummary,
    ReadinessThresholds,
    build_cohort_contract,
    build_cohort_summary,
    build_summary_from_db,
    evaluate_readiness,
    format_cohort_report,
    main_async,
    parse_args,
    read_cohort_contract,
    write_cohort_contract,
)
from scripts.utils.date_utils import now_utc


FROZEN_AS_OF = now_utc().replace(microsecond=0)


def _row(
    idx: int,
    *,
    bad: bool = False,
    prediction_id: str | None = None,
    binding: str | None = None,
    harness: str | None = None,
    prior_state: bool = True,
    verification_source: str | None = "external_signal",
    snapshot_present: bool = True,
    snapshot_missing: bool = False,
    controlled_fixture: bool = False,
) -> OutcomeRow:
    detail = {}
    if prediction_id:
        detail["prediction_id"] = prediction_id
    if binding:
        detail["prediction_binding"] = binding
    if harness:
        detail["harness"] = harness
    if snapshot_missing:
        detail["snapshot_missing"] = True
    if controlled_fixture:
        detail["synthetic_calibration_fixture"] = True
    return OutcomeRow(
        ts=now_utc().replace(microsecond=0) + timedelta(minutes=idx),
        agent_id=f"agent-{idx % 2}",
        outcome_type="task_failed" if bad else "task_completed",
        is_bad=bad,
        outcome_score=0.0 if bad else 1.0,
        verification_source=verification_source,
        reported_confidence=None,
        reported_complexity=None,
        detail=detail,
        prior_state_age_seconds=30.0 if prior_state else None,
        prior_risk=0.8 if bad and prior_state else (0.2 if prior_state else None),
        prior_phi=None,
        prior_verdict=None,
        prior_coherence=None,
        prior_e=None,
        prior_i=None,
        prior_s=None,
        prior_v=None,
        snapshot_verdict=None,
        snapshot_e=0.1 if snapshot_present else None,
        snapshot_i=None,
        snapshot_s=None,
        snapshot_v=None,
        snapshot_phi=None,
        snapshot_coherence=None,
    )


def test_build_cohort_summary_counts_only_registry_prediction_bound_rows():
    rows = [
        _row(0, prediction_id="pred-1", binding="registry", prior_state=True),
        _row(1, bad=True, prediction_id="pred-2", binding="registry", harness="beam", prior_state=False),
        _row(2, prediction_id="pred-3", binding="prev_confidence_fallback"),
        _row(3),
    ]

    summary = build_cohort_summary(rows, scope="task", window_days=90, lead_minutes=30)

    assert summary.total_outcomes == 4
    assert summary.prediction_bound == 2
    assert summary.prediction_coverage == 0.5
    assert summary.prediction_bound_bad == 1
    assert summary.prediction_bound_prior_state == 1
    assert summary.by_harness_lane == {"beam": 1, "substrate": 1}


def test_build_cohort_summary_uses_only_trusted_joinable_outcomes():
    rows = [
        _row(0, prediction_id="pred-trusted", binding="registry"),
        _row(
            1,
            prediction_id="pred-no-state",
            binding="registry",
            snapshot_present=False,
        ),
        _row(
            2,
            prediction_id="pred-self-observed",
            binding="registry",
            verification_source="server_observation",
        ),
        _row(
            3,
            prediction_id="pred-soft",
            binding="registry",
            verification_source="agent_reported_tool_result",
        ),
        _row(
            4,
            prediction_id="pred-explicitly-missing",
            binding="registry",
            snapshot_missing=True,
        ),
    ]

    summary = build_cohort_summary(
        rows,
        scope="task",
        window_days=90,
        lead_minutes=30,
    )

    assert summary.total_outcomes == 1
    assert summary.prediction_bound == 1
    assert summary.prediction_coverage == 1.0


def test_build_cohort_summary_exposes_monotonic_persisted_outcome_funnel():
    rows = [
        _row(
            0,
            prediction_id="pred-fixture",
            binding="registry",
            controlled_fixture=True,
        ),
        _row(
            1,
            prediction_id="pred-untrusted",
            binding="registry",
            verification_source="server_observation",
        ),
        _row(2),
        _row(3, prediction_id="pred-missing", binding="missing_prediction"),
        _row(4, prediction_id="pred-no-prior", binding="registry", prior_state=False),
        _row(5, prediction_id="pred-accepted", binding="registry", prior_state=True),
    ]

    summary = build_cohort_summary(
        rows,
        scope="task",
        window_days=90,
        lead_minutes=30,
    )

    assert summary.funnel == OutcomeFunnel(
        fetched_outcomes=6,
        nonfixture_outcomes=5,
        prediction_id_presented=4,
        registry_bound=3,
        trusted_registry_bound=2,
        prior_state_available=1,
        accepted_outcome_attested=1,
    )
    assert summary.funnel.counts == tuple(
        sorted(summary.funnel.counts, reverse=True)
    )


def test_build_summary_from_db_retains_fixture_rows_for_visible_attrition(
    monkeypatch: pytest.MonkeyPatch,
):
    as_of = FROZEN_AS_OF
    observed: dict[str, object] = {}

    async def fake_fetch_rows(*_args: object, **kwargs: object) -> list[OutcomeRow]:
        observed.update(kwargs)
        return [_row(0)]

    monkeypatch.setattr(cohort_module, "fetch_rows", fake_fetch_rows)

    summary = asyncio.run(
        build_summary_from_db(
            "postgresql://example.invalid/db",
            scope="task",
            window_days=90,
            lead_minutes=30,
            as_of=as_of,
        )
    )

    assert summary.funnel.fetched_outcomes == 1
    assert observed["exclude_controlled_fixtures"] is False
    assert observed["include_identity_metadata"] is False
    assert observed["as_of"] == as_of


def test_trusted_outcome_explicitly_disables_soft_anchors(
    monkeypatch: pytest.MonkeyPatch,
):
    observed: dict[str, object] = {}

    def fake_is_anchorable(*_args: object, **kwargs: object) -> bool:
        observed.update(kwargs)
        return True

    monkeypatch.setattr(cohort_module, "is_anchorable", fake_is_anchorable)

    assert cohort_module.is_trusted_outcome(_row(0)) is True
    assert observed["include_soft"] is False


def test_format_cohort_report_keeps_holdout_language_and_lane_counts():
    rows = [
        _row(0, prediction_id="pred-1", binding="registry", prior_state=True),
        _row(1, bad=True, prediction_id="pred-2", binding="registry", harness="beam", prior_state=False),
    ]
    summary = build_cohort_summary(rows, scope="task", window_days=90, lead_minutes=30)

    report = format_cohort_report(summary)

    assert report.startswith("# Outcome-Attested Prediction-Binding Cohort")
    assert "scope: task" in report
    assert "prediction_bound: 2" in report
    assert "prediction_coverage: 1.000" in report
    assert "prediction_bound_prior_state: 1/2" in report
    assert "harness_lanes: beam=1,substrate=1" in report
    assert "not accepted as prospective validation" in report
    assert "online agent-state estimation (agent proprioception)" in report
    assert "not an outcome oracle or bad-verdict dispenser" in report
    assert "external labels still own outcome truth" in report


def test_format_cohort_report_labels_unreconstructible_prediction_creation():
    summary = build_cohort_summary(
        [_row(0, prediction_id="pred-1", binding="registry")],
        scope="task",
        window_days=90,
        lead_minutes=30,
    )

    report = format_cohort_report(summary)

    assert "outcome_funnel_contract: " + CONTRACT_VERSION in report
    assert "fetched_outcomes: 1" in report
    assert "nonfixture_outcomes: 1" in report
    assert "trusted_outcomes: 1" in report
    assert "prediction_id_presented: 1" in report
    assert "registry_bound: 1" in report
    assert "trusted_registry_bound: 1" in report
    assert "prior_state_available: 1" in report
    assert "accepted_outcome_attested: 1" in report
    assert "prediction_created: unavailable" in report
    assert "prediction registration is in-memory and not durably reconstructible" in report


def test_format_cohort_report_keeps_side_statistics_outside_monotonic_funnel():
    rows = [
        _row(0),
        _row(1, verification_source="server_observation"),
        _row(
            2,
            prediction_id="pred-untrusted",
            binding="registry",
            verification_source="server_observation",
        ),
    ]
    report = format_cohort_report(
        build_cohort_summary(
            rows,
            scope="task",
            window_days=90,
            lead_minutes=30,
        )
    )

    funnel_text, side_text = report.split("## Side statistics", maxsplit=1)
    assert "## Outcome binding funnel" in funnel_text
    assert "trusted_outcomes:" not in funnel_text
    assert "trusted_outcomes: 1" in side_text

    funnel_counts = [
        int(line.rsplit(":", maxsplit=1)[1].strip())
        for line in funnel_text.splitlines()
        if line.split(":", maxsplit=1)[0]
        in {
            "fetched_outcomes",
            "nonfixture_outcomes",
            "prediction_id_presented",
            "registry_bound",
            "trusted_registry_bound",
            "prior_state_available",
            "accepted_outcome_attested",
        }
    ]
    assert funnel_counts == sorted(funnel_counts, reverse=True)


def test_report_does_not_claim_unreconstructible_prediction_timing():
    report = format_cohort_report(
        build_cohort_summary(
            [_row(0, prediction_id="pred-1", binding="registry")],
            scope="task",
            window_days=90,
            lead_minutes=30,
        )
    )

    assert "existed before outcomes" not in report
    assert "does not establish when the prediction was created" in report
    assert "self-contained consistency checksum" in report


def test_legacy_summary_constructor_remains_additive_without_funnel():
    summary = ProspectiveCohortSummary(
        scope="task",
        window_days=90,
        lead_minutes=30,
        total_outcomes=1,
        prediction_bound=0,
        prediction_bound_bad=0,
        prediction_bound_prior_state=0,
        by_harness_lane={},
    )

    assert summary.funnel is None
    assert "outcome_funnel: unavailable_for_legacy_summary" in format_cohort_report(
        summary
    )


def test_evaluate_readiness_reports_strong_when_thresholds_are_met():
    rows = [
        _row(0, prediction_id="pred-1", binding="registry", prior_state=True),
        _row(1, bad=True, prediction_id="pred-2", binding="registry", prior_state=True),
        _row(2, prediction_id="pred-3", binding="registry", prior_state=False),
        _row(3),
    ]
    summary = build_cohort_summary(rows, scope="task", window_days=90, lead_minutes=30)
    thresholds = ReadinessThresholds(
        min_prediction_bound=3,
        min_prediction_bound_bad=1,
        min_prediction_coverage=0.5,
        min_prediction_prior_state_coverage=0.6,
    )

    readiness = evaluate_readiness(summary, thresholds)

    assert readiness.status == "strong"
    assert readiness.reasons == ()


def test_evaluate_readiness_explains_weak_dataset_gaps():
    rows = [
        _row(0, prediction_id="pred-1", binding="registry", prior_state=False),
        _row(1),
        _row(2),
        _row(3),
    ]
    summary = build_cohort_summary(rows, scope="task", window_days=90, lead_minutes=30)
    thresholds = ReadinessThresholds(
        min_prediction_bound=3,
        min_prediction_bound_bad=1,
        min_prediction_coverage=0.5,
        min_prediction_prior_state_coverage=0.8,
    )

    readiness = evaluate_readiness(summary, thresholds)

    assert readiness.status == "not_ready"
    assert "prediction_bound 1 < 3" in readiness.reasons
    assert "prediction_bound_bad 0 < 1" in readiness.reasons
    assert "prediction_coverage 0.250 < 0.500" in readiness.reasons
    assert "prediction_prior_state_coverage 0.000 < 0.800" in readiness.reasons


def test_format_cohort_report_includes_readiness_gate():
    rows = [
        _row(0, prediction_id="pred-1", binding="registry", prior_state=False),
        _row(1),
    ]
    summary = build_cohort_summary(rows, scope="task", window_days=90, lead_minutes=30)
    thresholds = ReadinessThresholds(
        min_prediction_bound=2,
        min_prediction_bound_bad=1,
        min_prediction_coverage=0.8,
        min_prediction_prior_state_coverage=0.5,
    )

    report = format_cohort_report(summary, thresholds=thresholds)

    assert "readiness: not_ready" in report
    assert "readiness_reasons:" in report
    assert "- prediction_bound 1 < 2" in report
    assert "readiness_thresholds: min_prediction_bound=2" in report


def _contract(as_of: datetime | None = None) -> dict[str, object]:
    return build_cohort_contract(
        scope="task",
        window_days=90,
        lead_minutes=30.0,
        as_of=as_of or FROZEN_AS_OF,
    )


def test_cohort_contract_freezes_selection_predicates_and_stable_digest():
    first = _contract()
    second = _contract()

    assert first == second
    assert first["schema_version"] == 1
    assert first["contract_version"] == CONTRACT_VERSION
    selection = first["selection"]
    assert isinstance(selection, dict)
    assert selection == {
        "scope": "task",
        "window_days": 90,
        "lead_minutes": 30.0,
        "as_of": cohort_module._canonical_datetime(FROZEN_AS_OF),
        "outcome_types": [
            "test_passed",
            "test_failed",
            "tool_rejected",
            "task_completed",
            "task_failed",
        ],
    }
    predicates = first["predicates"]
    assert isinstance(predicates, dict)
    assert "mutable identity metadata excluded" in predicates["fetched_outcomes"]
    assert "is_controlled_validation_fixture" in predicates["nonfixture_outcomes"]
    assert "include_declared_purpose=True" in predicates["nonfixture_outcomes"]
    assert "is_anchorable" in predicates["trusted_outcomes"]
    assert "prediction_binding" in predicates["registry_bound"]
    digest = first["digest"]
    assert isinstance(digest, dict)
    assert digest["algorithm"] == "sha256"
    assert len(digest["value"]) == 64


def test_checked_in_cohort_contract_matches_current_frozen_selection():
    path = (
        Path(__file__).resolve().parents[1]
        / "config"
        / "evaluation"
        / "prospective_prediction_cohort_v1.json"
    )
    checked_in = read_cohort_contract(path)
    selection = checked_in["selection"]
    assert isinstance(selection, dict)

    rebuilt = build_cohort_contract(
        scope=str(selection["scope"]),
        window_days=int(selection["window_days"]),
        lead_minutes=float(selection["lead_minutes"]),
        as_of=cohort_module._datetime_from_contract(selection["as_of"]),
    )

    assert checked_in == rebuilt


def test_write_cohort_contract_is_create_only(tmp_path: Path):
    path = tmp_path / "cohort.json"
    contract = _contract()

    write_cohort_contract(path, contract)
    original = path.read_bytes()

    with pytest.raises(FileExistsError):
        write_cohort_contract(path, contract)

    assert path.read_bytes() == original
    assert read_cohort_contract(path) == contract


def test_read_cohort_contract_rejects_digest_drift(tmp_path: Path):
    path = tmp_path / "cohort.json"
    contract = _contract()
    write_cohort_contract(path, contract)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["selection"]["window_days"] = 91
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CohortContractError, match="digest"):
        read_cohort_contract(path)


def test_read_cohort_contract_rejects_schema_drift(tmp_path: Path):
    path = tmp_path / "cohort.json"
    payload = _contract()
    payload["schema_version"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CohortContractError, match="schema_version"):
        read_cohort_contract(path)


def test_read_cohort_contract_rejects_cli_parameter_drift(tmp_path: Path):
    path = tmp_path / "cohort.json"
    write_cohort_contract(path, _contract())

    with pytest.raises(CohortContractError, match="CLI parameter drift"):
        read_cohort_contract(path, window_days=365)


def test_read_cohort_contract_rejects_predicate_drift_even_with_valid_digest(
    tmp_path: Path,
):
    path = tmp_path / "cohort.json"
    payload = _contract()
    predicates = payload["predicates"]
    assert isinstance(predicates, dict)
    predicates["registry_bound"] = "detail.prediction_binding in ('registry', 'fallback')"
    payload = cohort_module.with_cohort_contract_digest(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CohortContractError, match="predicate contract drift"):
        read_cohort_contract(path)


def test_parse_args_preserves_default_report_parameters():
    args = parse_args([])

    assert args.scope == "task"
    assert args.window_days == 90
    assert args.lead_minutes == 30.0
    assert args.as_of is None
    assert args.write_cohort_contract is None
    assert args.read_cohort_contract is None


def test_contract_read_mode_uses_frozen_selection_without_default_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    path = tmp_path / "strict-cohort.json"
    as_of = FROZEN_AS_OF
    contract = build_cohort_contract(
        scope="strict",
        window_days=17,
        lead_minutes=5.0,
        as_of=as_of,
    )
    write_cohort_contract(path, contract)
    observed: dict[str, object] = {}

    async def fake_build_summary_from_db(
        _db_url: str,
        **kwargs: object,
    ) -> object:
        observed.update(kwargs)
        return build_cohort_summary(
            [],
            scope=str(kwargs["scope"]),
            window_days=int(kwargs["window_days"]),
            lead_minutes=float(kwargs["lead_minutes"]),
            as_of=kwargs["as_of"],
        )

    monkeypatch.setattr(
        cohort_module,
        "build_summary_from_db",
        fake_build_summary_from_db,
    )

    result = asyncio.run(main_async(parse_args(["--read-cohort-contract", str(path)])))

    assert result == 0
    assert observed == {
        "scope": "strict",
        "window_days": 17,
        "lead_minutes": 5.0,
        "as_of": as_of,
    }
    assert "scope: strict" in capsys.readouterr().out


def test_write_contract_rejects_same_report_output_path_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    path = tmp_path / "cohort.json"
    args = parse_args(
        [
            "--write-cohort-contract",
            str(path),
            "--output",
            str(path),
        ]
    )

    result = asyncio.run(main_async(args))

    assert result == 2
    assert not path.exists()
    assert "must not alias" in capsys.readouterr().err


def test_read_contract_rejects_symlinked_report_output_before_reading(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    contract_path = tmp_path / "cohort.json"
    write_cohort_contract(contract_path, _contract())
    original = contract_path.read_bytes()
    output_alias = tmp_path / "report.md"
    output_alias.symlink_to(contract_path)
    args = parse_args(
        [
            "--read-cohort-contract",
            str(contract_path),
            "--output",
            str(output_alias),
        ]
    )

    result = asyncio.run(main_async(args))

    assert result == 2
    assert contract_path.read_bytes() == original
    assert "must not alias" in capsys.readouterr().err

from __future__ import annotations

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys

import scripts.analysis.eisv_ablation_matrix as matrix_module
from scripts.analysis.eisv_ablation_matrix import (
    AblationMatrixRow,
    build_matrix_row,
    estimate_delta_uncertainty,
    filter_rows_for_validation,
    format_matrix_report,
    split_rows_by_harness_lane,
)
from scripts.analysis.eisv_skeptic_report import ModelScore, OutcomeRow


def _row(
    idx: int, *, bad: bool, risk: float | None, agent: str = "agent-a"
) -> OutcomeRow:
    ts = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(minutes=idx)
    return OutcomeRow(
        ts=ts,
        agent_id=agent,
        outcome_type="task_failed" if bad else "task_completed",
        is_bad=bad,
        outcome_score=0.0 if bad else 1.0,
        verification_source="server_observation",
        reported_confidence=None,
        reported_complexity=None,
        detail={},
        prior_state_age_seconds=30.0 if risk is not None else None,
        prior_risk=risk,
        prior_phi=1.0 - risk if risk is not None else None,
        prior_verdict="high-risk" if risk is not None and risk > 0.7 else "safe",
        prior_coherence=0.5,
        prior_e=0.7,
        prior_i=0.7,
        prior_s=risk if risk is not None else None,
        prior_v=0.0,
        snapshot_verdict=None,
        snapshot_e=None,
        snapshot_i=None,
        snapshot_s=None,
        snapshot_v=None,
        snapshot_phi=None,
        snapshot_coherence=None,
    )


def test_filter_rows_for_validation_excludes_beam_harness_by_default():
    substrate = _row(0, bad=False, risk=0.1)
    beam = _row(1, bad=True, risk=None)
    beam = OutcomeRow(**{**beam.__dict__, "detail": {"harness": "beam"}})

    filtered = filter_rows_for_validation([substrate, beam])

    assert filtered == [substrate]
    assert filter_rows_for_validation([substrate, beam], exclude_harness_lanes=()) == [
        substrate,
        beam,
    ]


def test_filter_rows_for_validation_always_excludes_controlled_demo_perf_rows():
    substrate = _row(0, bad=False, risk=0.1)
    demo = _row(1, bad=True, risk=None)
    demo = OutcomeRow(
        **{
            **demo.__dict__,
            "detail": {"_identity_metadata": {"label": "quick-demo-agent_6d051ff8"}},
        }
    )
    perf = _row(2, bad=False, risk=0.2)
    perf = OutcomeRow(
        **{
            **perf.__dict__,
            "detail": {"_identity_metadata": {"label": "perf-profile-checkin"}},
        }
    )

    assert filter_rows_for_validation([substrate, demo, perf]) == [substrate]
    assert filter_rows_for_validation(
        [substrate, demo, perf],
        exclude_harness_lanes=(),
    ) == [substrate]


def test_split_rows_by_harness_lane_keeps_beam_visible():
    substrate = _row(0, bad=False, risk=0.1)
    beam = _row(1, bad=True, risk=None)
    beam = OutcomeRow(**{**beam.__dict__, "detail": {"harness": "beam"}})

    by_lane = split_rows_by_harness_lane([substrate, beam])

    assert by_lane == {"beam": [beam], "substrate": [substrate]}


def test_build_matrix_from_db_groups_lanes_and_respects_explicit_exclusions(
    monkeypatch,
):
    substrate = _row(0, bad=False, risk=0.1)
    beam = _row(1, bad=True, risk=None)
    beam = OutcomeRow(**{**beam.__dict__, "detail": {"harness": "beam"}})

    async def fake_fetch_rows(*_args, **_kwargs):
        return [substrate, beam]

    monkeypatch.setattr(matrix_module, "fetch_rows", fake_fetch_rows)

    default_rows = asyncio.run(
        matrix_module.build_matrix_from_db(
            "postgresql://unit-test",
            scopes=["task"],
            windows=[90],
            leads=[0],
        )
    )
    assert [(row.harness_lane, row.rows, row.bad) for row in default_rows] == [
        (None, 1, 0)
    ]

    grouped_rows = asyncio.run(
        matrix_module.build_matrix_from_db(
            "postgresql://unit-test",
            scopes=["task"],
            windows=[90],
            leads=[0],
            group_by_harness_lane=True,
            exclude_harness_lanes=(),
        )
    )
    assert [(row.harness_lane, row.rows, row.bad) for row in grouped_rows] == [
        ("beam", 1, 1),
        ("substrate", 1, 0),
    ]

    grouped_excluding_beam = asyncio.run(
        matrix_module.build_matrix_from_db(
            "postgresql://unit-test",
            scopes=["task"],
            windows=[90],
            leads=[0],
            group_by_harness_lane=True,
            exclude_harness_lanes=("beam",),
        )
    )
    assert [row.harness_lane for row in grouped_excluding_beam] == ["substrate"]


def test_parse_args_distinguishes_default_from_explicit_harness_exclusion():
    default_args = matrix_module.parse_args([])
    explicit_args = matrix_module.parse_args(
        ["--group-by-harness-lane", "--exclude-harness-lanes", "beam"]
    )

    assert default_args.exclude_harness_lanes is None
    assert explicit_args.group_by_harness_lane is True
    assert explicit_args.exclude_harness_lanes == ("beam",)


def test_redact_sensitive_report_text_removes_credential_shapes():
    redacted = matrix_module._redact_sensitive_report_text(
        "db=postgresql://reporter:s3cr3t@example.test/governance "
        "token: abc12345 password=letmein"
    )

    assert "s3cr3t" not in redacted
    assert "abc12345" not in redacted
    assert "letmein" not in redacted
    assert "postgresql://reporter:[REDACTED]@example.test/governance" in redacted
    assert "token:[REDACTED]" in redacted
    assert "password=[REDACTED]" in redacted


def test_build_matrix_row_summarizes_baseline_and_best_candidate():
    rows = []
    for idx in range(120):
        bad = idx % 10 in (8, 9)
        risk = 0.9 if bad else 0.1
        rows.append(_row(idx, bad=bad, risk=risk, agent=f"agent-{idx % 6}"))

    row = build_matrix_row(
        rows,
        scope="task",
        window_days=90,
        lead_minutes=30,
        train_fraction=0.7,
        min_feature_rows=10,
        uncertainty_resamples=50,
        uncertainty_seed=7,
    )

    assert row.scope == "task"
    assert row.window_days == 90
    assert row.lead_minutes == 30
    assert row.rows == 120
    assert row.bad == 24
    assert row.prior_state == 120
    assert row.baseline_auc is not None
    assert row.baseline_brier is not None
    assert row.best_candidate in {
        "previous_bad_plus_prior_risk",
        "prior_risk_binned",
        "prior_phi_binned",
        "prior_s_binned",
        "prior_verdict",
    }
    assert row.best_auc_delta is not None
    assert row.best_brier_improvement is not None
    assert row.best_auc_delta_ci is not None
    assert row.best_brier_improvement_ci is not None
    assert row.best_brier_permutation_p is not None
    assert isinstance(row.beats_both, bool)


def _score(
    name: str, probs: tuple[float, ...], auc_scores: tuple[float, ...]
) -> ModelScore:
    y_true = (0, 0, 0, 1, 1, 1)
    keys = tuple(f"row-{idx}" for idx in range(len(y_true)))
    return ModelScore(
        name=name,
        n_train=10,
        n_test=len(y_true),
        n_test_scored=len(y_true),
        auc=None,
        brier=None,
        scored_row_keys=keys,
        y_true=y_true,
        y_prob=probs,
        y_auc_score=auc_scores,
    )


def test_estimate_delta_uncertainty_reports_bootstrap_ci_and_permutation_p():
    baseline = _score(
        "previous_outcome_bad",
        probs=(0.30, 0.30, 0.30, 0.70, 0.70, 0.70),
        auc_scores=(0.30, 0.30, 0.30, 0.70, 0.70, 0.70),
    )
    candidate = _score(
        "prior_risk_binned",
        probs=(0.05, 0.10, 0.20, 0.80, 0.90, 0.95),
        auc_scores=(0.05, 0.10, 0.20, 0.80, 0.90, 0.95),
    )

    uncertainty = estimate_delta_uncertainty(
        baseline,
        candidate,
        resamples=80,
        seed=17,
    )

    assert uncertainty is not None
    assert uncertainty.paired_n == 6
    assert uncertainty.auc_delta_ci is not None
    assert uncertainty.brier_improvement_ci is not None
    assert uncertainty.brier_improvement_ci[0] > 0
    assert 0.0 <= uncertainty.brier_permutation_p <= 1.0


def test_format_matrix_report_contains_skeptical_ablation_table():
    rows = [
        AblationMatrixRow(
            scope="task",
            window_days=90,
            lead_minutes=30,
            rows=120,
            bad=24,
            bad_clusters=6,
            bad_agents=4,
            prior_state=120,
            prior_risk=120,
            baseline_auc=0.70,
            baseline_brier=0.12,
            best_candidate="prior_risk_binned",
            best_auc_delta=0.03,
            best_brier_improvement=0.01,
            best_auc_delta_ci=(0.01, 0.05),
            best_brier_improvement_ci=(0.002, 0.02),
            best_brier_permutation_p=0.04,
            beats_both=True,
            conclusion="KEEP TESTING: synthetic row",
        )
    ]

    report = format_matrix_report(rows, excluded_harness_lanes=("beam",))

    assert report.startswith("# EISV Ablation Matrix")
    assert "Excluded harness lanes: `beam`" in report
    assert (
        "| Scope | Window days | Lead min | Rows | Bad | Bad clusters | Bad agents "
        "| Prior state | Prior risk |"
        in report
    )
    # "Trusted" was a lie: the matrix passes no anchor predicate by default, so
    # the count printed under it was the unanchored population.
    assert "| Trusted |" not in report
    assert "AUC delta 95% CI" in report
    assert "Brier improvement 95% CI" in report
    assert "Brier perm p" in report
    assert "Null max median" in report
    assert "Selective p" in report
    assert "Read `AUC delta` against `Null max median`, never against zero." in report
    assert "Read `Bad` against `Bad clusters`." in report
    assert "| task | 90 | 30 | 120 | 24 | 6 | 4 | 120 | 120 |" in report
    assert "[0.010, 0.050]" in report
    assert "[0.0020, 0.0200]" in report
    assert "0.040" in report
    assert "online agent-state estimation" in report
    assert "not bad-action prevention" in report
    assert "`Bad` means rows labeled `is_bad=true`" in report
    assert "not a moral verdict or a count of prevented outcomes" in report
    assert "outcome oracle" in report
    assert "bad-verdict authority" in report
    assert "bad-agent detector" in report
    assert "prior_risk_binned" in report
    assert "KEEP TESTING" in report


def test_format_matrix_report_labels_grouped_harness_lane_rows():
    rows = [
        AblationMatrixRow(
            scope="task",
            window_days=90,
            lead_minutes=0,
            rows=2,
            bad=1,
            bad_clusters=1,
            bad_agents=1,
            prior_state=0,
            prior_risk=0,
            baseline_auc=None,
            baseline_brier=0.25,
            best_candidate=None,
            best_auc_delta=None,
            best_brier_improvement=None,
            beats_both=False,
            conclusion="BEAM lane needs runtime features",
            harness_lane="beam",
        ),
        AblationMatrixRow(
            scope="task",
            window_days=90,
            lead_minutes=0,
            rows=2,
            bad=0,
            bad_clusters=0,
            bad_agents=0,
            prior_state=2,
            prior_risk=2,
            baseline_auc=None,
            baseline_brier=0.0,
            best_candidate=None,
            best_auc_delta=None,
            best_brier_improvement=None,
            beats_both=False,
            conclusion="substrate lane separate",
            harness_lane="substrate",
        ),
    ]

    report = format_matrix_report(rows)

    assert "Harness lane mode: grouped" in report
    assert "| Lane | Scope | Window days | Lead min |" in report
    assert "| beam | task | 90 | 0 | 2 | 1 | 1 | 1 | 0 | 0 |" in report
    assert "| substrate | task | 90 | 0 | 2 | 0 | 0 | 0 | 2 | 2 |" in report


def test_cli_help_runs_when_invoked_as_a_file():
    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "scripts/analysis/eisv_ablation_matrix.py", "--help"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Run a compact EISV ablation matrix" in result.stdout
    assert "--group-by-harness-lane" in result.stdout


def test_count_bad_clusters_collapses_a_retry_burst_sharing_one_snapshot():
    """Rows sharing a prior-state snapshot are one unit of evidence, not N.

    An edit-test-retry burst emits several failures seconds apart, all joining
    the same prior state. Every candidate feature is then identical across them,
    so within-burst discrimination is 0.5 by construction. Live example: three
    `test_failed` rows six minutes apart from one agent, one snapshot, all at
    prior_s=0.417.
    """
    base = datetime.now(timezone.utc).replace(microsecond=0)
    burst = [
        dataclasses.replace(
            _row(0, bad=True, risk=0.4, agent="agent-burst"),
            ts=base + timedelta(seconds=offset),
            # Same absolute snapshot instant for all three.
            prior_state_age_seconds=3600.0 + offset,
        )
        for offset in (0, 11, 354)
    ]
    other = dataclasses.replace(
        _row(9, bad=True, risk=0.4, agent="agent-other"),
        ts=base,
        prior_state_age_seconds=3600.0,
    )
    good = _row(20, bad=False, risk=0.1, agent="agent-burst")

    clusters, agents = matrix_module.count_bad_clusters([*burst, other, good])

    assert clusters == 2, "three bursty rows plus one other = two independent events"
    assert agents == 2


def test_selective_null_reports_the_distribution_of_the_reported_maximum():
    """The reported statistic is a max over candidates, so its null is not zero.

    With ~7 candidates on a few dozen paired rows, EISV readings that carry no
    information still produce a sizeable best-candidate lift. Reporting the max
    against an implicit zero null is what made a noise-level +0.139 read as a
    signal. Permuting readings between clusters (rather than shuffling labels)
    leaves the previous-outcome baseline identical in every resample, so the
    null isolates the EISV contribution.
    """
    rows = [
        _row(idx, bad=(idx % 9 == 0), risk=0.1 + (idx % 5) / 10.0, agent=f"agent-{idx % 6}")
        for idx in range(180)
    ]

    null = matrix_module.estimate_selective_null(
        rows, observed_best_delta=0.0, resamples=25, seed=7, min_feature_rows=10
    )

    assert null is not None
    assert null.resamples > 0
    assert null.median is not None
    assert null.selective_p is not None
    assert 0.0 < null.selective_p <= 1.0
    # A max over several candidates does not centre on zero under the null.
    assert null.p95 is not None and null.p95 >= null.median


def test_selective_null_reports_that_it_could_not_form_a_null():
    """A slice with no deltas must say so, not silently omit the null."""
    rows = [_row(idx, bad=False, risk=0.2) for idx in range(40)]

    single_class = matrix_module.estimate_selective_null(
        rows, observed_best_delta=0.1, resamples=10
    )
    assert single_class is not None
    assert single_class.resamples == 0
    assert single_class.selective_p is None

    assert (
        matrix_module.estimate_selective_null(
            rows, observed_best_delta=0.1, resamples=0
        )
        is None
    )


def test_selective_null_needs_at_least_three_permutable_clusters():
    """Two clusters cannot produce a null worth printing."""
    base = datetime.now(timezone.utc).replace(microsecond=0)
    rows = [
        dataclasses.replace(
            _row(idx, bad=(idx % 2 == 0), risk=0.3, agent=f"agent-{idx % 2}"),
            ts=base + timedelta(seconds=idx),
            prior_state_age_seconds=60.0 + idx,
        )
        for idx in range(2)
    ]

    null = matrix_module.estimate_selective_null(
        rows, observed_best_delta=0.1, resamples=10
    )

    assert null is not None
    assert null.clusters == 2
    assert null.selective_p is None


def test_validation_slices_keep_rows_whose_identity_self_declared_testing():
    """The agent under measurement must not be able to exclude itself."""
    substrate = _row(0, bad=False, risk=0.1)
    self_declared = OutcomeRow(
        **{
            **_row(1, bad=True, risk=0.9).__dict__,
            "detail": {"_identity_metadata": {"purpose": "testing", "label": "claude-x"}},
        }
    )

    assert filter_rows_for_validation([substrate, self_declared]) == [
        substrate,
        self_declared,
    ]


def test_conclusion_is_downgraded_when_the_selective_null_is_not_cleared():
    """"KEEP TESTING" must not survive a lift the noise floor reproduces.

    `summarize_conclusion` thresholds the best candidate against zero, but the
    reported delta is a maximum over ~7 candidates, so zero is the wrong
    reference. The conclusion string is what gets quoted downstream, so the
    qualification has to live there and not only in a column.
    """
    not_cleared = matrix_module.SelectiveNull(
        resamples=300, clusters=34, median=0.145, p95=0.400, selective_p=0.100
    )
    qualified = matrix_module.qualify_conclusion_with_selective_null(
        "KEEP TESTING: EISV/prior-state features show modest lift",
        best_auc_delta=0.312,
        selective_null=not_cleared,
    )
    assert qualified.startswith("NOISE-LEVEL")
    assert "selective p=0.100" in qualified
    assert "34 permutable clusters" in qualified
    assert "KEEP TESTING" in qualified, "the original verdict stays visible"

    cleared = matrix_module.SelectiveNull(
        resamples=300, clusters=34, median=0.145, p95=0.400, selective_p=0.004
    )
    kept = matrix_module.qualify_conclusion_with_selective_null(
        "KEEP TESTING: EISV/prior-state features show modest lift",
        best_auc_delta=0.6,
        selective_null=cleared,
    )
    assert kept.startswith("KEEP TESTING")
    assert "Clears the selective null" in kept


def test_conclusion_is_untouched_when_no_selective_null_was_computed():
    assert (
        matrix_module.qualify_conclusion_with_selective_null(
            "SKEPTICAL: nothing here",
            best_auc_delta=0.01,
            selective_null=None,
        )
        == "SKEPTICAL: nothing here"
    )

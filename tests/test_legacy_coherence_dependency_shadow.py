from datetime import datetime, timedelta, timezone

import pytest
from datetime import datetime as _dt, timezone as _tz

from scripts.analysis.legacy_coherence_dependency_shadow import (
    MIN_BAD_CLUSTERS,
    SHADOW_OUTCOME_SQL,
    ShadowOutcomeRow,
    build_report,
    summarize_channel,
)
from src.eisv_telemetry import EISV_SHADOW_ABLATIONS_SCHEMA


def _row(
    idx: int,
    *,
    bad: bool,
    deployed: float,
    candidate: float,
    measurement: str | None = None,
    agent: str | None = None,
) -> ShadowOutcomeRow:
    ts = datetime(2026, 8, 12, tzinfo=timezone.utc) + timedelta(minutes=idx)
    return ShadowOutcomeRow(
        ts=ts,
        outcome_id=f"outcome-{idx}",
        agent_id=agent or f"agent-{idx % 5}",
        outcome_type="task_failed" if bad else "task_completed",
        is_bad=bad,
        prior_state_recorded_at=ts - timedelta(minutes=30),
        prior_state_age_seconds=1800.0,
        prior_measurement_id=measurement or f"measurement-{idx}",
        prior_derivation_kind="behavioral_sensor",
        shadow_schema=EISV_SHADOW_ABLATIONS_SCHEMA,
        behavioral_eligible=True,
        deployed_e=deployed,
        candidate_e=candidate,
        deployed_i=deployed,
        candidate_i=candidate,
        confidence_eligible=True,
        deployed_confidence=deployed,
        candidate_confidence=candidate,
    )


def test_query_uses_trusted_external_anchor_and_leak_safe_prior_state():
    assert "o.verification_source = 'external_signal'" in SHADOW_OUTCOME_SQL
    assert "s.synthetic IS NOT TRUE" in SHADOW_OUTCOME_SQL
    assert "s.recorded_at <= o.ts -" in SHADOW_OUTCOME_SQL
    assert "ORDER BY s.recorded_at DESC" in SHADOW_OUTCOME_SQL


def test_outcome_metrics_are_withheld_below_bad_cluster_floor():
    rows = [
        _row(0, bad=False, deployed=0.8, candidate=0.8),
        _row(1, bad=True, deployed=0.2, candidate=0.2),
    ]

    read = summarize_channel(
        rows,
        channel="behavioral_E",
        deployed=lambda row: row.deployed_e,
        candidate=lambda row: row.candidate_e,
        min_bad_clusters=MIN_BAD_CLUSTERS,
        resamples=50,
    )

    assert read.status == "WAIT_SAMPLE_FLOOR"
    assert read.deployed_auc is None
    assert read.candidate_auc is None
    assert read.mean_signed_delta == 0.0


def test_identical_candidate_passes_after_cluster_floor():
    rows = [
        _row(
            idx,
            bad=idx >= 5,
            deployed=0.8 if idx < 5 else 0.2,
            candidate=0.8 if idx < 5 else 0.2,
        )
        for idx in range(10)
    ]

    read = summarize_channel(
        rows,
        channel="behavioral_E",
        deployed=lambda row: row.deployed_e,
        candidate=lambda row: row.candidate_e,
        min_bad_clusters=5,
        resamples=100,
        seed=7,
    )

    assert read.status == "PASS_AUC_NONINFERIORITY"
    assert read.deployed_auc == pytest.approx(1.0)
    assert read.candidate_auc == pytest.approx(1.0)
    assert read.candidate_minus_deployed_auc == pytest.approx(0.0)
    assert read.auc_delta_ci95 == pytest.approx((0.0, 0.0))


def test_cluster_floor_counts_shared_prior_state_once():
    rows = [
        _row(
            0,
            bad=True,
            deployed=0.2,
            candidate=0.2,
            measurement="shared",
            agent="agent-shared",
        ),
        _row(
            1,
            bad=True,
            deployed=0.2,
            candidate=0.2,
            measurement="shared",
            agent="agent-shared",
        ),
        _row(2, bad=False, deployed=0.8, candidate=0.8),
    ]

    read = summarize_channel(
        rows,
        channel="behavioral_I",
        deployed=lambda row: row.deployed_i,
        candidate=lambda row: row.candidate_i,
        min_bad_clusters=2,
        resamples=50,
    )

    assert read.bad_rows == 2
    assert read.bad_clusters == 1
    assert read.status == "WAIT_SAMPLE_FLOOR"


def test_report_names_non_actuation_and_recursive_replay_boundary():
    report = build_report(
        [_row(0, bad=False, deployed=0.8, candidate=0.75)],
        scope="task",
        window_days=365,
        lead_minutes=30.0,
        min_bad_clusters=2,
        resamples=20,
    )

    assert "not an actuator" in report
    assert "Behavioral E/I still require recursive history" in report
    assert "WAIT_SAMPLE_FLOOR" in report


def shadow_module_cutoff():
    from scripts.analysis.legacy_coherence_dependency_shadow import V0_1_AMENDMENT_CUTOFF

    return V0_1_AMENDMENT_CUTOFF


def test_report_header_names_the_fixture_rule_and_its_contract_standing():
    from scripts.analysis.legacy_coherence_dependency_shadow import build_report

    registered = build_report([], scope="task", window_days=21, lead_minutes=30.0, fixture_rule="registered")
    assert "Contract:" not in registered
    assert "Fixture rule: `registered` (the contract's item 2 as registered)" in registered
    v01 = build_report([], scope="task", window_days=21, lead_minutes=30.0, fixture_rule="corrected", contract="v0.1", not_before=shadow_module_cutoff())
    assert "Contract: `v0.1`" in v01 and "Fixture rule: `corrected` (the v0.1 contract's rule)" in v01
    with pytest.raises(ValueError):
        build_report([], scope="task", window_days=21, lead_minutes=30.0, fixture_rule="corrected", contract="v0.1")
    deviation = build_report([], scope="task", window_days=21, lead_minutes=30.0, fixture_rule="corrected")
    assert "Fixture rule: `corrected` (a disclosed deviation from the contract's item 2)" in deviation


def test_cli_threads_one_fixture_rule_into_fetch_and_report(monkeypatch, tmp_path):
    from scripts.analysis import legacy_coherence_dependency_shadow as shadow_module

    calls: list = []

    async def fake_fetch_rows(_db_url, **kwargs):
        # The receipt must already exist when the database is first touched.
        assert list((tmp_path / "ledger").glob("*.json")), "receipt written after database access"
        calls.append(kwargs)
        return []

    monkeypatch.setattr(shadow_module, "fetch_rows", fake_fetch_rows)
    monkeypatch.setattr(shadow_module, "_utcnow", lambda: _dt(2026, 10, 2, tzinfo=_tz.utc))
    out = tmp_path / "shadow.md"
    rc = shadow_module.main(
        [
            "--db-url", "postgresql://unused", "--contract", "v0.1", "--output", str(out),
            "--read-id", "legacy-coherence-dependency-v0.1-1", "--read-ledger-dir", str(tmp_path / "ledger"),
            "--as-of", "2026-10-01T00:00:00Z",
        ]
    )
    assert rc == 0
    # v0.1 reads its own cohort under the corrected rule, then the same window
    # under the registered rule for the provenance block; both carry the cutoff.
    text = out.read_text(encoding="utf-8")
    assert [c["fixture_rule"] for c in calls] == ["corrected", "registered"]
    assert {c["not_before"] for c in calls} == {shadow_module.V0_1_AMENDMENT_CUTOFF}
    # One boundary for both fetches.
    assert calls[0]["as_of"] is not None and calls[0]["as_of"] == calls[1]["as_of"]
    assert "Read ID: `legacy-coherence-dependency-v0.1-1`" in text
    assert "Admitted window: 2026-09-03T00:00:00+00:00 to 2026-10-01T00:00:00+00:00" in text
    text = out.read_text(encoding="utf-8")
    assert "Contract: `v0.1`" in text
    assert "Fixture rule: `corrected` (the v0.1 contract's rule)" in text
    assert "Registered-rule sensitivity (provenance only)" in text


def test_v0_is_pinned_to_the_registered_rule_and_refuses_to_move(monkeypatch, tmp_path, capsys):
    from scripts.analysis import legacy_coherence_dependency_shadow as shadow_module

    seen: dict = {}

    async def fake_fetch_rows(_db_url, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(shadow_module, "fetch_rows", fake_fetch_rows)
    out = tmp_path / "v0.md"
    assert shadow_module.main(["--db-url", "postgresql://unused", "--output", str(out)]) == 0
    assert seen["fixture_rule"] == "registered"
    assert seen["not_before"] is None
    assert seen["as_of"] is None  # v0 keeps its live window, as before
    text = out.read_text(encoding="utf-8")
    assert "Contract:" not in text  # v0 output is unchanged by v0.1's existence
    assert "Fixture rule: `registered` (the contract's item 2 as registered)" in text
    # v0 cannot be run under the corrected rule: that is v0.1, a different read.
    rc = shadow_module.main(["--db-url", "postgresql://unused", "--fixture-rule", "corrected"])
    assert rc == 2
    assert "Run the other contract" in capsys.readouterr().err
    assert shadow_module.contract_fixture_rule("v0") == "registered"
    assert shadow_module.contract_fixture_rule("v0.1") == "corrected"
    with pytest.raises(ValueError):
        shadow_module.contract_fixture_rule("v2")


def test_v0_1_cutoff_excludes_pre_amendment_outcomes(monkeypatch):
    """The v0.1 read admits no outcome from before its amendment instant."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    from scripts.analysis import legacy_coherence_dependency_shadow as shadow_module

    cutoff = shadow_module.V0_1_AMENDMENT_CUTOFF
    assert cutoff == datetime(2026, 9, 3, tzinfo=timezone.utc)
    assert shadow_module.contract_not_before("v0.1") == cutoff
    assert shadow_module.contract_not_before("v0") is None

    class _Record(dict):
        pass

    def record(ts):
        return _Record(ts=ts, detail={"kind": "live"})

    captured = {}

    class _Conn:
        async def fetch(self, *_args):
            return [record(cutoff - timedelta(seconds=1)), record(cutoff), record(cutoff + timedelta(days=1))]

        async def close(self):
            return None

    class _FakeAsyncpg:
        async def connect(self, _dsn):
            return _Conn()

    import sys as _sys

    monkeypatch.setitem(_sys.modules, "asyncpg", _FakeAsyncpg())

    def fake_row_from_record(rec):
        captured.setdefault("rows", []).append(rec["ts"])
        return rec["ts"]

    monkeypatch.setattr(shadow_module, "_row_from_record", fake_row_from_record)
    rows = asyncio.run(
        shadow_module.fetch_rows(
            "postgresql://unused", window_days=365, lead_minutes=30.0, outcome_types=("task_failed",),
            fixture_rule="corrected", not_before=cutoff,
        )
    )
    assert rows == [cutoff, cutoff + timedelta(days=1)]


def _v0_1_argv(tmp_path, read_id="legacy-coherence-dependency-v0.1-1", *extra):
    return [
        "--db-url", "postgresql://unused", "--contract", "v0.1", "--read-id", read_id,
        "--read-ledger-dir", str(tmp_path / "ledger"), "--as-of", "2026-10-01T00:00:00Z", *extra,
    ]


def test_v0_1_is_receipted_one_shot_and_pinned(monkeypatch, tmp_path, capsys):
    import json as _json

    from scripts.analysis import legacy_coherence_dependency_shadow as shadow_module

    async def fake_fetch_rows(_db_url, **kwargs):
        return []

    monkeypatch.setattr(shadow_module, "fetch_rows", fake_fetch_rows)
    monkeypatch.setattr(shadow_module, "_utcnow", lambda: _dt(2026, 10, 2, tzinfo=_tz.utc))
    assert shadow_module.main(_v0_1_argv(tmp_path, "legacy-coherence-dependency-v0.1-1", "--output", str(tmp_path / "a.md"))) == 0
    receipts = list((tmp_path / "ledger").glob("*.json"))
    assert len(receipts) == 1
    receipt = _json.loads(receipts[0].read_text(encoding="utf-8"))
    assert receipt["contract"] == "v0.1" and receipt["fixture_rule"] == "corrected"
    assert receipt["not_before"] == shadow_module.V0_1_AMENDMENT_CUTOFF.isoformat()
    assert receipt["read_id"] == "legacy-coherence-dependency-v0.1-1"
    # The same id cannot read twice.
    assert shadow_module.main(_v0_1_argv(tmp_path, "legacy-coherence-dependency-v0.1-1")) == 2
    assert "already has a receipt" in capsys.readouterr().err
    # Pins and guards.
    for extra, message in [
        (("--scope", "strict"), "pins --scope task"),
        (("--lead-minutes", "5"), "pins --lead-minutes"),
        (("--min-bad-clusters", "10"), "pins --min-bad-clusters"),
    ]:
        assert shadow_module.main(_v0_1_argv(tmp_path, "legacy-coherence-dependency-v0.1-2", *extra)) == 2
        assert message in capsys.readouterr().err
    assert shadow_module.main([
        "--db-url", "postgresql://unused", "--contract", "v0.1", "--read-id", "legacy-coherence-dependency-v0.1-3",
        "--read-ledger-dir", str(tmp_path / "ledger"), "--as-of", "2026-09-01T00:00:00Z",
    ]) == 2
    assert "before its amendment cutoff" in capsys.readouterr().err
    assert shadow_module.main(["--db-url", "postgresql://unused", "--contract", "v0.1", "--read-ledger-dir", str(tmp_path / "ledger"), "--as-of", "2026-10-01T00:00:00Z"]) == 2
    assert "require --read-id" in capsys.readouterr().err
    # No receipt was written by any refused read.
    assert len(list((tmp_path / "ledger").glob("*.json"))) == 1


def test_cutoff_fails_closed_on_missing_ts_and_reads_naive_ts_as_utc(monkeypatch):
    import asyncio
    import sys as _sys
    from datetime import timedelta

    from scripts.analysis import legacy_coherence_dependency_shadow as shadow_module

    cutoff = shadow_module.V0_1_AMENDMENT_CUTOFF
    records = [
        {"ts": None, "detail": {"kind": "live"}},
        {"ts": (cutoff + timedelta(days=1)).replace(tzinfo=None), "detail": {"kind": "live"}},
        {"ts": (cutoff - timedelta(days=1)).replace(tzinfo=None), "detail": {"kind": "live"}},
    ]

    class _Conn:
        async def fetch(self, *_args):
            return records

        async def close(self):
            return None

    class _FakeAsyncpg:
        async def connect(self, _dsn):
            return _Conn()

    monkeypatch.setitem(_sys.modules, "asyncpg", _FakeAsyncpg())
    monkeypatch.setattr(shadow_module, "_row_from_record", lambda rec: rec["ts"])
    rows = asyncio.run(
        shadow_module.fetch_rows(
            "postgresql://unused", window_days=365, lead_minutes=30.0, outcome_types=("task_failed",),
            fixture_rule="corrected", not_before=cutoff,
        )
    )
    # The naive value is admitted as UTC and the row keeps the aware timestamp.
    assert rows == [cutoff + timedelta(days=1)]


def test_v0_1_pins_every_inherited_default_and_its_read_id_namespace(monkeypatch, tmp_path, capsys):
    from datetime import datetime, timezone

    from scripts.analysis import legacy_coherence_dependency_shadow as shadow_module

    async def fake_fetch_rows(_db_url, **kwargs):
        return []

    monkeypatch.setattr(shadow_module, "fetch_rows", fake_fetch_rows)
    base = ["--db-url", "postgresql://unused", "--contract", "v0.1", "--read-ledger-dir", str(tmp_path / "ledger"), "--as-of", "2026-10-01T00:00:00Z"]
    for read_id in ("legacy-coherence-dependency-v0.1-0", "eisv-outcome-grounding-2026-12-01", "shadow-run", "legacy-coherence-dependency-v0.1-01"):
        assert shadow_module.main([*base, "--read-id", read_id]) == 2
        assert "legacy-coherence-dependency-v0.1-<n>" in capsys.readouterr().err
    for extra, message in [
        (("--window-days", "30"), "pins --window-days 365"),
        (("--resamples", "10"), "pins --resamples"),
        (("--seed", "7"), "pins --seed 0"),
    ]:
        assert shadow_module.main([*base, "--read-id", "legacy-coherence-dependency-v0.1-9", *extra]) == 2
        assert message in capsys.readouterr().err
    # A boundary in the future at access time is refused too.
    monkeypatch.setattr(shadow_module, "_utcnow", lambda: datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert shadow_module.main([*base, "--read-id", "legacy-coherence-dependency-v0.1-9"]) == 2
    assert "in the future" in capsys.readouterr().err
    assert not list((tmp_path / "ledger").glob("*.json"))


def test_v0_1_freezes_one_boundary_when_no_as_of_is_given(monkeypatch, tmp_path):
    from datetime import datetime, timezone

    from scripts.analysis import legacy_coherence_dependency_shadow as shadow_module

    calls: list = []

    async def fake_fetch_rows(_db_url, **kwargs):
        calls.append(kwargs)
        return []

    frozen = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(shadow_module, "fetch_rows", fake_fetch_rows)
    monkeypatch.setattr(shadow_module, "_utcnow", lambda: frozen)
    rc = shadow_module.main(
        ["--db-url", "postgresql://unused", "--contract", "v0.1", "--read-id", "legacy-coherence-dependency-v0.1-5",
         "--read-ledger-dir", str(tmp_path / "ledger"), "--output", str(tmp_path / "r.md")]
    )
    assert rc == 0
    assert [c["as_of"] for c in calls] == [frozen, frozen]

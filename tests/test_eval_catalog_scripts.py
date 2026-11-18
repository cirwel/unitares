"""Offline unit & CLI tests for repaired EVALUATION_INDEX scripts."""
from __future__ import annotations

import json
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.analysis import (
    compositionality_metrics,
    eisv_pca_analysis,
    report_calibration,
    validate_theoretical_foundations,
)


class _FakeCursor:
    def __init__(self, handler):
        self._handler = handler
        self._rows = []

    def execute(self, sql, params=None):
        self._rows = list(self._handler(sql, params))

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, handler):
        self._handler = handler
        self.queries: list[str] = []
        self.readonly = False

    def set_session(self, **kwargs):
        self.readonly = bool(kwargs.get("readonly"))

    def cursor(self):
        def _dispatch(sql, params):
            self.queries.append(sql)
            return self._handler(sql, params)

        return _FakeCursor(_dispatch)

    def close(self):
        pass


def test_validate_theoretical_foundations_passes():
    assert validate_theoretical_foundations.main() == 0

    result = validate_theoretical_foundations.ValidationResult()
    validate_theoretical_foundations.check_equations_match(result)
    validate_theoretical_foundations.check_coherence_function(result)
    validate_theoretical_foundations.check_adaptive_control(result)
    validate_theoretical_foundations.check_numerical_integration(result)
    validate_theoretical_foundations.check_self_regulation(result)
    validate_theoretical_foundations.check_bounded_domains(result)
    validate_theoretical_foundations.check_domain_integration(result)
    validate_theoretical_foundations.check_objective_function(result)

    assert len(result.failed) == 0
    assert len(result.warnings) == 0
    assert len(result.passed) >= 40


def test_compositionality_metrics_synthetic_cli(tmp_path, monkeypatch):
    out_dir = tmp_path / "comp"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "compositionality_metrics.py",
            "--synthetic",
            "--n-synthetic",
            "60",
            "--no-plot",
            "--output-dir",
            str(out_dir),
        ],
    )
    compositionality_metrics.main()

    out_file = out_dir / "compositionality_results.json"
    assert out_file.exists()
    data = json.loads(out_file.read_text(encoding="utf-8"))
    assert data["data_source"] == "synthetic"
    assert data["n_records"] == 60
    assert "rho" in data["topographic_similarity"]


def test_report_calibration_help_empty_and_missing_state_file(tmp_path, monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        report_calibration.parse_args(["--help"])
    assert exc.value.code == 0

    monkeypatch.setattr("src.calibration.DEFAULT_STATE_FILE", tmp_path / "missing.json")
    rc = report_calibration.main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No calibration samples found" in out

    empty_file = tmp_path / "empty_cal.json"
    empty_file.write_text("{}", encoding="utf-8")
    rc = report_calibration.main(["--state-file", str(empty_file), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["empty"] is True
    assert payload["total_samples"] == 0

    rc_missing = report_calibration.main(["--state-file", str(tmp_path / "nonexistent.json")])
    assert rc_missing == 2


def test_report_calibration_state_file_and_from_db(tmp_path, monkeypatch, capsys):
    sample_state = {
        "_version": 77,
        "bins": {
            "0.7-0.8": {
                "count": 10,
                "predicted_correct": 10,
                "actual_correct": 8,
                "confidence_sum": 7.5,
            }
        },
        "tactical_bins": {
            "0.7-0.8": {
                "count": 8,
                "predicted_correct": 8,
                "actual_correct": 6,
                "confidence_sum": 6.0,
            }
        },
        "tactical_bins_by_channel": {
            "tests": {
                "0.7-0.8": {
                    "count": 8,
                    "predicted_correct": 8,
                    "actual_correct": 6,
                    "confidence_sum": 6.0,
                }
            }
        },
        "complexity_bins": {},
    }
    state_file = tmp_path / "cal_state.json"
    state_file.write_text(json.dumps(sample_state), encoding="utf-8")

    monkeypatch.setattr("src.calibration.DEFAULT_STATE_FILE", tmp_path / "unused.json")
    assert report_calibration.main(["--state-file", str(state_file)]) == 0
    text_out = capsys.readouterr().out
    assert "Channel `tests` (n=8):" in text_out
    assert "STRATEGIC CALIBRATION" in text_out

    created_conns: list[_FakeConn] = []

    def _connect(_url):
        def _handler(sql, _params):
            assert "audit.outcome_events" not in sql.lower()
            assert "core.calibration" in sql
            return [(sample_state, datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc), 77)]

        conn = _FakeConn(_handler)
        created_conns.append(conn)
        return conn

    assert report_calibration.main(["--from-db", "--json"], connect=_connect) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["empty"] is False
    assert payload["version"] == 77
    assert payload["total_samples"] == 18
    assert payload["sample_counts"] == {
        "strategic": 10,
        "tactical": 8,
        "tactical_by_channel": 8,
        "complexity": 0,
    }
    assert payload["calibration_status"] == "calibrated"
    assert payload["strategic_bins"][0]["count"] == 10
    assert payload["tactical_bins_by_channel"]["tests"][0]["count"] == 8
    assert len(created_conns) == 1

    # Insufficient evidence must report UNASSESSED rather than CALIBRATED, and channel
    # observations must not double-count aggregate tactical samples.
    sparse_state = {
        "bins": {
            "0.7-0.8": {
                "count": 1,
                "predicted_correct": 1,
                "actual_correct": 1,
                "confidence_sum": 0.75,
            }
        },
        "tactical_bins": {},
        "tactical_bins_by_channel": {},
        "complexity_bins": {},
    }
    sparse_file = tmp_path / "sparse_cal.json"
    sparse_file.write_text(json.dumps(sparse_state), encoding="utf-8")
    assert report_calibration.main(["--state-file", str(sparse_file)]) == 0
    sparse_text = capsys.readouterr().out
    assert "Overall Status: UNASSESSED" in sparse_text
    assert "Overall Status: CALIBRATED" not in sparse_text

    channel_dup_state = {
        "bins": {},
        "tactical_bins": {
            "0.7-0.8": {
                "count": 10,
                "predicted_correct": 10,
                "actual_correct": 8,
                "confidence_sum": 7.5,
            }
        },
        "tactical_bins_by_channel": {
            "tests": {
                "0.7-0.8": {
                    "count": 10,
                    "predicted_correct": 10,
                    "actual_correct": 8,
                    "confidence_sum": 7.5,
                }
            }
        },
        "complexity_bins": {},
    }
    channel_file = tmp_path / "channel_dup.json"
    channel_file.write_text(json.dumps(channel_dup_state), encoding="utf-8")
    assert report_calibration.main(["--state-file", str(channel_file), "--json"]) == 0
    channel_payload = json.loads(capsys.readouterr().out)
    assert channel_payload["total_samples"] == 10
    assert channel_payload["calibration_status"] == "unassessed"


def test_eisv_pca_legacy_gate_enforced(tmp_path):
    assert eisv_pca_analysis.main([]) == 2
    assert (
        eisv_pca_analysis.main(["--legacy-sqlite-db", str(tmp_path / "retired.db")]) == 2
    )


def test_eisv_pca_postgres_extracts_e_from_state_json_and_s_from_entropy(capsys):
    assert "state_json->>'E'" in eisv_pca_analysis.POSTGRES_FETCH_SQL
    assert "s.entropy" in eisv_pca_analysis.POSTGRES_FETCH_SQL
    assert "audit.outcome_events" not in eisv_pca_analysis.POSTGRES_FETCH_SQL.lower()

    # Deliberately set E >> S and negatively correlated for physical vs positively correlated for behavioral
    fake_rows = [
        # identity, sensor_class, E (state_json), I, S (entropy), V, coherence, risk
        ("b1", "behavioral", 0.70, 0.75, 0.10, -0.05, 0.48, 0.25),
        ("b1", "behavioral", 0.80, 0.78, 0.20, 0.02, 0.51, 0.20),
        ("b2", "behavioral", 0.90, 0.82, 0.30, 0.08, 0.54, 0.15),
        ("p1", "physical", 0.85, 0.70, 0.12, 0.15, 0.56, 0.22),
        ("p1", "physical", 0.75, 0.72, 0.24, 0.03, 0.52, 0.28),
        # Constant coherence in unrecorded must not trigger numpy divide warnings
        ("u1", "unrecorded", 0.60, 0.60, 0.40, 0.00, 0.50, 0.30),
        ("u1", "unrecorded", 0.65, 0.62, 0.35, 0.03, 0.50, 0.28),
    ]

    matrices = eisv_pca_analysis.extract_postgres_matrices(fake_rows)
    assert list(matrices.keys()) == ["behavioral", "physical", "unrecorded"]
    X_beh, _ids_beh = matrices["behavioral"]
    assert np.allclose(X_beh[:, 0], [0.70, 0.80, 0.90])  # E
    assert np.allclose(X_beh[:, 2], [0.10, 0.20, 0.30])  # S (entropy column)

    def _connect(_url):
        def _handler(sql, _params):
            assert "audit.outcome_events" not in sql.lower()
            if "instrument_id" in sql:
                return [("SN1:testinst1234", len(fake_rows))]
            return fake_rows

        return _FakeConn(_handler)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        rc = eisv_pca_analysis.main(
            ["--postgres", "--era", "SN1", "--json"],
            now=datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc),
            connect=_connect,
        )
    assert rc == 0
    runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
    assert runtime_warnings == []

    report = json.loads(capsys.readouterr().out)
    assert report["era"] == "SN1"
    assert report["total_observations"] == len(fake_rows)
    assert set(report["by_sensor_class"].keys()) == {"behavioral", "physical", "unrecorded"}
    assert "coherence" in report["by_sensor_class"]["unrecorded"]["full_pca"]["constant_features"]
    beh_corrs = {
        (c["a"], c["b"]): c["r"]
        for c in report["by_sensor_class"]["behavioral"]["full_pca"]["high_correlations"]
    }
    assert beh_corrs[("E", "S")] == pytest.approx(1.0, abs=1e-3)


def test_eisv_pca_postgres_refuses_mixed_instrument_era():
    def _connect(_url):
        def _handler(sql, _params):
            if "instrument_id" in sql:
                return [("RK1:a2579740d3bea62a", 5), ("RK1:92a47c19ddf301d8", 5)]
            return []

        return _FakeConn(_handler)

    rc = eisv_pca_analysis.main(
        ["--postgres", "--era", "RR1"],
        now=datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc),
        connect=_connect,
    )
    assert rc == 2

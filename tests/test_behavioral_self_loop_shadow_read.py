"""Synthetic tests for scripts/analysis/behavioral_self_loop_shadow_read.py."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.analysis import behavioral_self_loop_shadow_read as reader
from src import eisv_eras

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[1]


def _shadow_row(
    identity_id: str,
    *,
    label: str | None = None,
    sensor_class: str = "behavioral",
    dec_eligible: bool = True,
    dec_reason: str | None = None,
    dE: float | None = -0.0381,
    dI: float | None = 0.0,
    reg_eligible: bool = True,
    reg_reason: str | None = None,
    dS: float | None = 0.0336,
    instability: float | None = 0.0,
    with_shadows: bool = True,
) -> reader.ShadowRow:
    dec = None
    reg = None
    if with_shadows:
        dec = {
            "schema": "decision_self_loop_ablation.v1",
            "eligible": dec_eligible,
            "eligibility_reason": dec_reason,
            "candidate_minus_deployed": (
                {"E": dE, "I": dI} if dec_eligible and dE is not None and dI is not None else None
            ),
        }
        reg = {
            "schema": "regime_self_loop_ablation.v1",
            "eligible": reg_eligible,
            "eligibility_reason": reg_reason,
            "candidate_minus_deployed": ({"S": dS} if reg_eligible and dS is not None else None),
            "deployed_regime_instability": instability if reg_eligible else None,
        }
    return reader.ShadowRow(
        identity_id=identity_id,
        label=label,
        sensor_class=sensor_class,
        decision_shadow=dec,
        regime_shadow=reg,
    )


def test_never_queries_outcome_events():
    import ast

    source = (REPO_ROOT / "scripts" / "analysis" / "behavioral_self_loop_shadow_read.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    doc_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                doc_ids.add(id(body[0].value))
    for sql in (reader.FETCH_SQL, reader.STAMP_FILTERED_ID_COUNTS_SQL):
        assert "outcome_events" not in sql.lower()
        assert "audit." not in sql.lower()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_ids:
            if "SELECT" in node.value.upper() or "FROM" in node.value.upper():
                assert "outcome_events" not in node.value.lower()


def test_resolve_era_window_single_eras_never_pool():
    as_of = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    rk1 = reader.resolve_era_window("RK1", as_of=as_of)
    assert rk1.start == datetime(2026, 10, 16, 2, 59, 50, tzinfo=UTC)
    assert rk1.end == datetime(2026, 10, 22, 14, 27, 20, tzinfo=UTC)
    assert rk1.stamp_era is None
    assert eisv_eras.boundaries_crossed(rk1.start, rk1.end, stamp_filtered=False) == []

    rr1 = reader.resolve_era_window("RR1", as_of=as_of)
    assert rr1.start == datetime(2026, 10, 22, 14, 28, 19, tzinfo=UTC)
    assert rr1.end == datetime(2026, 11, 5, 22, 56, 34, tzinfo=UTC)
    assert rr1.stamp_era is None
    assert eisv_eras.boundaries_crossed(rr1.start, rr1.end, stamp_filtered=False) == []

    sn1 = reader.resolve_era_window("SN1", as_of=as_of)
    assert sn1.start == datetime(2026, 11, 5, 22, 57, 49, tzinfo=UTC)
    assert sn1.end == as_of
    assert sn1.stamp_era == "SN1"
    assert eisv_eras.boundaries_crossed(sn1.start, sn1.end, stamp_filtered=True) == []

    sh1 = reader.resolve_era_window("SH1", as_of=as_of)
    assert sh1.start == datetime(2026, 11, 5, 22, 57, 49, tzinfo=UTC)
    assert sh1.stamp_era == "SH1"


def test_resolve_era_window_rejects_unknown_and_label_only():
    with pytest.raises(reader.EraResolutionError, match="not in"):
        reader.resolve_era_window("NO_SUCH_ERA")
    with pytest.raises(reader.EraResolutionError, match="label_only"):
        reader.resolve_era_window("TEL")


def test_summarize_splits_by_sensor_class_and_computes_percentiles_and_per_identity():
    rows = []
    # Agent A: 12 eligible rows with varying deltas
    for i in range(12):
        rows.append(
            _shadow_row(
                "id-a",
                label="Sentinel",
                sensor_class="behavioral",
                dE=-0.04 + i * 0.001,
                dI=0.0,
                dS=-0.10 + i * 0.01,
                instability=0.2 if i < 6 else 0.0,
            )
        )
    # Agent B: 5 rows (< 10 threshold, excluded from per_identity_medians)
    for _ in range(5):
        rows.append(
            _shadow_row(
                "id-b",
                label="ephemeral-1",
                sensor_class="behavioral",
                dE=-0.0381,
                dI=0.0,
                dS=0.0336,
            )
        )
    # Physical and unrecorded rows have no shadows
    rows.extend(
        _shadow_row("lumen", label="Lumen", sensor_class="physical", with_shadows=False)
        for _ in range(4)
    )
    rows.extend(
        _shadow_row("anon", sensor_class="unrecorded", with_shadows=False) for _ in range(2)
    )

    summary = reader.summarize_shadow_rows(rows, min_identity_rows=10)
    assert summary["total_rows"] == 23
    assert summary["sensor_class_counts"] == {"behavioral": 17, "physical": 4, "unrecorded": 2}

    beh = summary["by_sensor_class"]["behavioral"]
    dec = beh["decision_self_loop_neutralized"]
    assert dec["rows_with_shadow"] == 17
    assert dec["rows_eligible"] == 17
    assert dec["identities_eligible"] == 2
    assert dec["delta_E"]["n"] == 17
    assert dec["delta_I"]["p50"] == pytest.approx(0.0)
    # Only id-a meets >= 10 rows
    assert [x["identity_id"] for x in dec["per_identity_medians"]] == ["id-a"]
    assert dec["per_identity_medians"][0]["n"] == 12

    reg = beh["regime_self_loop_neutralized"]
    assert reg["delta_S"]["n"] == 17
    assert reg["delta_S"]["p50"] == pytest.approx(-0.02)
    assert reg["delta_S"]["p90"] == pytest.approx(0.0336)
    assert [x["identity_id"] for x in reg["per_identity_medians"]] == ["id-a"]

    assert summary["by_sensor_class"]["physical"]["decision_self_loop_neutralized"]["rows_with_shadow"] == 0
    assert summary["by_sensor_class"]["unrecorded"]["regime_self_loop_neutralized"]["rows_with_shadow"] == 0


def test_summarize_records_sn1_already_neutralized_reasons():
    rows = [
        _shadow_row(
            f"id-{i}",
            dec_eligible=False,
            dec_reason="self_loop_already_neutralized",
            reg_eligible=False,
            reg_reason="self_loop_already_neutralized",
        )
        for i in range(3)
    ]
    summary = reader.summarize_shadow_rows(rows)
    beh = summary["by_sensor_class"]["behavioral"]
    assert beh["decision_self_loop_neutralized"]["rows_with_shadow"] == 3
    assert beh["decision_self_loop_neutralized"]["rows_eligible"] == 0
    assert beh["decision_self_loop_neutralized"]["ineligible_reasons"] == {
        "self_loop_already_neutralized": 3
    }
    assert beh["decision_self_loop_neutralized"]["delta_E"]["n"] == 0


class _FakeCursor:
    def __init__(self, conn: "_FakeConn"):
        self.conn = conn
        self._result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql: str, params):
        self.conn.queries.append((sql, params))
        if "AS instrument_id" in sql:
            self._result = self.conn.id_rows
        else:
            self._result = self.conn.data_rows

    def fetchall(self):
        return self._result


class _FakeConn:
    def __init__(self, id_rows, data_rows):
        self.id_rows = id_rows
        self.data_rows = data_rows
        self.queries = []
        self.session = None
        self.closed = False

    def set_session(self, **kwargs):
        self.session = kwargs

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def test_main_reads_single_era_json_and_refuses_mixed_instrument_ids(capsys):
    as_of = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    dec_payload = json.dumps(
        {
            "eligible": True,
            "eligibility_reason": None,
            "candidate_minus_deployed": {"E": -0.0381, "I": 0.0},
        }
    )
    reg_payload = json.dumps(
        {
            "eligible": True,
            "eligibility_reason": None,
            "candidate_minus_deployed": {"S": 0.0336},
            "deployed_regime_instability": 0.0,
        }
    )
    rows = [("id-1", "Sentinel", "behavioral", dec_payload, reg_payload) for _ in range(10)]
    conn = _FakeConn([("RK1:92a47c19ddf301d8", 10)], rows)

    rc = reader.main(["--era", "RR1", "--json"], now=as_of, connect=lambda _url: conn)
    assert rc == 0
    assert conn.session == {"isolation_level": "REPEATABLE READ", "readonly": True}
    assert conn.closed
    out = json.loads(capsys.readouterr().out)
    assert out["era"] == "RR1"
    beh = out["by_sensor_class"]["behavioral"]
    assert beh["decision_self_loop_neutralized"]["delta_E"]["p50"] == pytest.approx(-0.0381)
    assert beh["regime_self_loop_neutralized"]["delta_S"]["p50"] == pytest.approx(0.0336)

    mixed_conn = _FakeConn([("RK1:a", 5), ("RK1:b", 5)], [])
    assert reader.main(["--era", "RR1"], now=as_of, connect=lambda _url: mixed_conn) == 2
    assert "refused:" in capsys.readouterr().err

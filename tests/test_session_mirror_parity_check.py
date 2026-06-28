"""Unit tests for the session-mirror birth-cohort parity checker (pure logic).

No Redis/PG needed — exercises compare_birth_cohort and _parse_bound_at directly.
See scripts/ops/session_mirror_parity_check.py and
docs/proposals/redis-retirement-phase-1-plan.md.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).parent.parent
_MOD_PATH = _PROJECT_ROOT / "scripts" / "ops" / "session_mirror_parity_check.py"
_spec = importlib.util.spec_from_file_location("session_mirror_parity_check", _MOD_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=timezone.utc)
MIN_AGE = timedelta(minutes=5)
MAX_AGE = timedelta(minutes=60)


def _payload(agent_id, *, age_min):
    bound = (NOW - timedelta(minutes=age_min)).isoformat()
    return {"agent_id": agent_id, "bound_at": bound}


# --- _parse_bound_at ---

def test_parse_bound_at_naive_becomes_utc():
    dt = mod._parse_bound_at({"bound_at": "2026-06-28T11:30:00"})
    assert dt is not None and dt.tzinfo is not None


def test_parse_bound_at_bad_and_missing():
    assert mod._parse_bound_at({"bound_at": "not-a-date"}) is None
    assert mod._parse_bound_at({}) is None
    assert mod._parse_bound_at({"bound_at": 12345}) is None


# --- compare_birth_cohort ---

def _run(redis_items, pg):
    return mod.compare_birth_cohort(redis_items, pg, now=NOW, min_age=MIN_AGE, max_age=MAX_AGE)


def test_full_match_in_cohort():
    redis = {"sk1": _payload("u1", age_min=30), "sk2": _payload("u2", age_min=10)}
    pg = {"sk1": "u1", "sk2": "u2"}
    s = _run(redis, pg)
    assert s["cohort_size"] == 2 and s["matched"] == 2
    assert s["parity_ratio"] == 1.0
    assert s["missing_in_pg"] == 0 and s["uuid_mismatch"] == 0


def test_missing_in_pg_counted():
    redis = {"sk1": _payload("u1", age_min=30)}
    s = _run(redis, {})  # PG has nothing
    assert s["cohort_size"] == 1 and s["matched"] == 0
    assert s["missing_in_pg"] == 1
    assert s["parity_ratio"] == 0.0
    assert s["sample_missing"] == ["sk1"]


def test_uuid_mismatch_counted():
    redis = {"sk1": _payload("u1", age_min=30)}
    pg = {"sk1": "DIFFERENT"}
    s = _run(redis, pg)
    assert s["uuid_mismatch"] == 1 and s["matched"] == 0
    assert s["sample_mismatch"][0]["redis"] == "u1"
    assert s["sample_mismatch"][0]["pg"] == "DIFFERENT"


def test_too_young_and_too_old_excluded():
    redis = {
        "young": _payload("u1", age_min=2),    # < 5 min, mid-flight
        "old": _payload("u2", age_min=120),    # > 60 min, may be reaped
        "incohort": _payload("u3", age_min=30),
    }
    pg = {"incohort": "u3"}  # only the in-cohort one mirrored
    s = _run(redis, pg)
    assert s["cohort_size"] == 1  # young + old excluded -> no spurious divergence
    assert s["matched"] == 1 and s["parity_ratio"] == 1.0


def test_unparseable_bound_at_excluded():
    redis = {"bad": {"agent_id": "u1", "bound_at": "garbage"}}
    s = _run(redis, {})
    assert s["cohort_size"] == 0 and s["parity_ratio"] is None


def test_empty_cohort_ratio_none():
    s = _run({}, {})
    assert s["cohort_size"] == 0 and s["parity_ratio"] is None


# --- evaluate_gate (Codex review #3: flip-decision gate) ---

def _gate(summary, min_cohort=100, min_ratio=0.99):
    return mod.evaluate_gate(summary, min_cohort=min_cohort, min_ratio=min_ratio)


def test_gate_fails_on_inert():
    passed, reasons = _gate({"status": "inert"})
    assert passed is False and any("status" in r for r in reasons)


def test_gate_passes_on_clean_ran():
    passed, reasons = _gate({"status": "ran", "cohort_size": 250, "parity_ratio": 1.0, "uuid_mismatch": 0})
    assert passed is True and reasons == []


def test_gate_fails_on_thin_cohort():
    passed, reasons = _gate({"status": "ran", "cohort_size": 10, "parity_ratio": 1.0, "uuid_mismatch": 0})
    assert passed is False and any("cohort_size" in r for r in reasons)


def test_gate_fails_on_low_parity():
    passed, reasons = _gate({"status": "ran", "cohort_size": 250, "parity_ratio": 0.80, "uuid_mismatch": 0})
    assert passed is False and any("parity_ratio" in r for r in reasons)


def test_gate_fails_on_any_uuid_mismatch():
    passed, reasons = _gate({"status": "ran", "cohort_size": 250, "parity_ratio": 1.0, "uuid_mismatch": 1})
    assert passed is False and any("uuid_mismatch" in r for r in reasons)

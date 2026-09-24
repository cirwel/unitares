"""Adjudication checks under a declared no-operator deployment.

adjudication_feedstock asks whether the queue is fed for an operator to judge.
When a deployment declares it has no human adjudicator
(UNITARES_OPERATOR_ADJUDICATION=off) its WARN can never clear, so it SKIPs and
says why. anchor_all_positive_generator does NOT skip: it audits labels already
in the anchor channel, which the declaration does not remove. The declaration
must be explicit: absence of operator verdicts is never inferred.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "dev" / "unitares_doctor.py"
CHECKS = ("check_adjudication_feedstock",)


@pytest.fixture(scope="module")
def doctor():
    spec = importlib.util.spec_from_file_location("unitares_doctor", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unitares_doctor"] = mod  # Python 3.14 dataclass needs this
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("check", CHECKS)
@pytest.mark.parametrize("value", ["off", "none", "0", "false", "OFF"])
def test_declared_off_skips_and_names_the_declaration(doctor, monkeypatch, check, value):
    monkeypatch.setenv("UNITARES_OPERATOR_ADJUDICATION", value)
    monkeypatch.setattr(doctor, "_psql_row", lambda *a, **k: ["4"])
    monkeypatch.setattr(doctor, "_psql_rows",
                        lambda *a, **k: pytest.fail("the channel query must not run"))
    result = getattr(doctor, check)("postgresql:///x")
    assert result.status is doctor.Status.SKIP
    assert "UNITARES_OPERATOR_ADJUDICATION" in result.message
    # Model verdicts are named as telemetry, never as the channel's verdicts.
    assert "4 model verdict(s) in 7d (telemetry, never anchors)" in result.message


@pytest.mark.parametrize("check", CHECKS)
@pytest.mark.parametrize("value", [None, "", "on", "yes"])
def test_absent_or_on_leaves_the_check_judging(doctor, monkeypatch, check, value):
    """Never inferred: without the declaration the real query runs."""
    if value is None:
        monkeypatch.delenv("UNITARES_OPERATOR_ADJUDICATION", raising=False)
    else:
        monkeypatch.setenv("UNITARES_OPERATOR_ADJUDICATION", value)
    ran = []
    monkeypatch.setattr(doctor, "_psql_rows", lambda *a, **k: ran.append(1) or None)
    monkeypatch.setattr(doctor, "_psql_row", lambda *a, **k: ran.append(1) or None)
    getattr(doctor, check)("postgresql:///x")
    assert ran


def test_unreadable_model_count_still_skips(doctor, monkeypatch):
    monkeypatch.setenv("UNITARES_OPERATOR_ADJUDICATION", "off")
    monkeypatch.setattr(doctor, "_psql_row", lambda *a, **k: None)
    result = doctor.check_adjudication_feedstock("postgresql:///x")
    assert result.status is doctor.Status.SKIP
    assert "model verdict" not in result.message


def test_historical_anchor_audit_still_runs_when_operators_are_off(doctor, monkeypatch):
    """Declaring no FUTURE adjudicator does not remove labels already recorded."""
    monkeypatch.setenv("UNITARES_OPERATOR_ADJUDICATION", "off")
    monkeypatch.setattr(doctor, "_psql_rows",
                        lambda *a, **k: [["sentinel_finding", "17", "0"]])
    result = doctor.check_anchor_all_positive_generator("postgresql:///x")
    assert result.status is doctor.Status.WARN
    assert "sentinel_finding: 17 confirmed, 0 dismissed" in result.message

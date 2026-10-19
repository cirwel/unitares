"""Severity grading for deploy-drift findings.

A checkout trailing origin by a commit or two for a few hours is the normal
state of a manual deploy cadence and must not page the operator; a long or old
lag, a checkout on the wrong branch, or a pinned checkout behind its declared
version must.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "deploy_drift_doctor.py"
_spec = importlib.util.spec_from_file_location("deploy_drift_doctor_severity", MODULE_PATH)
ddd = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(ddd)

NOW = 1_800_000_000.0


def diag(condition: str, behind: int = 0) -> "ddd.Diagnosis":
    return ddd.Diagnosis("surface", condition, "detail", behind=behind)


def test_small_fresh_lag_is_a_warning():
    assert ddd.finding_severity(diag("behind_origin", behind=1), None, NOW) == "warning"
    assert ddd.finding_severity(
        diag("behind_origin", behind=ddd.BEHIND_CRITICAL_COMMITS), NOW - 3600, NOW) == "warning"


def test_large_lag_is_critical_immediately():
    d = diag("behind_origin", behind=ddd.BEHIND_CRITICAL_COMMITS + 1)
    assert ddd.finding_severity(d, None, NOW) == "critical"


def test_lag_that_persists_a_day_escalates_to_critical():
    d = diag("behind_origin", behind=1)
    assert ddd.finding_severity(d, NOW - ddd.BEHIND_CRITICAL_AGE_SECONDS + 60, NOW) == "warning"
    assert ddd.finding_severity(d, NOW - ddd.BEHIND_CRITICAL_AGE_SECONDS, NOW) == "critical"


def test_other_conditions_keep_their_grade():
    assert ddd.finding_severity(diag("restart_pending"), None, NOW) == "warning"
    assert ddd.finding_severity(diag("branch_mismatch", behind=1), None, NOW) == "critical"
    assert ddd.finding_severity(diag("version_lag"), None, NOW) == "critical"


def _doctor(posted: list) -> "ddd.Doctor":
    return ddd.Doctor(io={"post_finding": lambda p: (posted.append(p), ddd.DELIVERED)[1]})


def _at(monkeypatch, t: float) -> None:
    monkeypatch.setattr(ddd.time, "time", lambda: t)


def test_escalate_uses_first_seen_from_the_open_finding(monkeypatch):
    """With the real cooldown: a small lag alerts as a warning, re-warns after the
    cooldown, and is critical once it has stayed open a day."""
    posted: list = []
    doc, open_findings = _doctor(posted), {}
    d = diag("behind_origin", behind=1)

    for t in (NOW, NOW + 3600, NOW + ddd.COOLDOWN_SECONDS, NOW + ddd.BEHIND_CRITICAL_AGE_SECONDS):
        _at(monkeypatch, t)
        doc._escalate(d, "fp", open_findings)

    assert [p["severity"] for p in posted] == ["warning", "warning", "critical"]
    assert open_findings["fp"]["first_seen"] == NOW
    assert open_findings["fp"]["severity"] == "critical"


def test_warning_that_becomes_critical_bypasses_the_cooldown(monkeypatch):
    """A lag that jumps past the commit limit an hour after a warning alerts at once."""
    posted: list = []
    doc, open_findings = _doctor(posted), {}

    _at(monkeypatch, NOW)
    doc._escalate(diag("behind_origin", behind=1), "fp", open_findings)
    _at(monkeypatch, NOW + 3600)
    doc._escalate(diag("behind_origin", behind=ddd.BEHIND_CRITICAL_COMMITS + 1), "fp", open_findings)

    assert [p["severity"] for p in posted] == ["warning", "critical"]


def test_repeated_critical_stays_under_the_cooldown(monkeypatch):
    """Only a rise in severity bypasses the cooldown; an unchanged critical does not,
    and neither does a record written before severity was stored."""
    posted: list = []
    doc = _doctor(posted)
    big = diag("behind_origin", behind=ddd.BEHIND_CRITICAL_COMMITS + 5)

    open_findings: dict = {}
    _at(monkeypatch, NOW)
    doc._escalate(big, "fp", open_findings)
    _at(monkeypatch, NOW + 3600)
    doc._escalate(big, "fp", open_findings)
    assert [p["severity"] for p in posted] == ["critical"]

    legacy = {"fp2": {"surface": "s", "condition": "behind_origin",
                      "first_seen": NOW, "last_alert": NOW}}
    doc._escalate(big, "fp2", legacy)
    assert len(posted) == 1

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


def test_escalate_uses_first_seen_from_the_open_finding(tmp_path, monkeypatch):
    """The second alert for the same lag, a day after first sight, is critical."""
    posted: list = []
    monkeypatch.setattr(ddd, "COOLDOWN_SECONDS", 0)
    doc = ddd.Doctor(io={"post_finding": lambda p: (posted.append(p), ddd.DELIVERED)[1]})
    d = diag("behind_origin", behind=1)
    open_findings: dict = {}

    monkeypatch.setattr(ddd.time, "time", lambda: NOW)
    doc._escalate(d, "fp", open_findings)
    monkeypatch.setattr(ddd.time, "time", lambda: NOW + ddd.BEHIND_CRITICAL_AGE_SECONDS)
    doc._escalate(d, "fp", open_findings)

    assert [p["severity"] for p in posted] == ["warning", "critical"]
    assert open_findings["fp"]["first_seen"] == NOW

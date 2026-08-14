"""The adjudication queue can starve while every liveness check reads green.

`finding_producer_live` asks whether producers are alive; `producer_never_
reported` asks whether they were ever born. Both are answered by the finding
stream itself. The queue does not consume that stream — it consumes a narrow
slice of it (two event types at two severities), so a producer can be loud,
healthy, and contribute nothing, and no liveness signal will say so.

Live state 2026-08-10, which is what prompted the check: 280 findings in 7 days
across 9 producers, 0 queue-eligible. Sentinel alone emitted 203 of them (136 in
one day), all `medium`. Its last eligible finding was 2026-08-01 20:20:20 — 31
seconds after the last real forced lease release, because #1443/#1444/#1459
removed the condition. The zero is FAIR; the invisibility is the defect.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "unitares_doctor.py"


@pytest.fixture(scope="module")
def doctor():
    spec = importlib.util.spec_from_file_location("unitares_doctor", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["unitares_doctor"] = mod  # Python 3.14 dataclass needs this
    spec.loader.exec_module(mod)
    return mod


def _rows(doctor, monkeypatch, rows):
    monkeypatch.setattr(doctor, "_psql_rows", lambda *_a, **_k: rows)


# --- the mirror must not desync from the queue's real definition ---

def test_mirrored_constants_match_the_queue_source(doctor):
    """The doctor duplicates the queue definition on purpose (it runs against a
    deployed DB from a possibly-undeployed checkout, so importing server code
    would measure the wrong thing). Duplication is only safe if it is pinned."""
    source = (REPO_ROOT / "src" / "http_routes" / "sentinel.py").read_text()

    types_match = re.search(
        r"_SENTINEL_FINDING_EVENT_TYPES\s*=\s*\(([^)]*)\)", source
    )
    assert types_match, "could not find _SENTINEL_FINDING_EVENT_TYPES in http_routes/sentinel.py"
    source_types = tuple(re.findall(r'"([^"]+)"', types_match.group(1)))
    assert source_types == doctor.ADJUDICABLE_EVENT_TYPES, (
        f"queue event types changed in http_routes/sentinel.py ({source_types}) but the "
        f"doctor mirror still says {doctor.ADJUDICABLE_EVENT_TYPES}"
    )

    sev_match = re.search(
        r"_SENTINEL_BACKLOG_DEFAULT_SEVERITIES\s*=\s*frozenset\(\{([^}]*)\}\)",
        source,
    )
    assert sev_match, "could not find _SENTINEL_BACKLOG_DEFAULT_SEVERITIES"
    source_sevs = set(re.findall(r'"([^"]+)"', sev_match.group(1)))
    assert source_sevs == set(doctor.ADJUDICABLE_SEVERITIES), (
        f"queue severities changed ({source_sevs}) but the doctor mirror "
        f"still says {set(doctor.ADJUDICABLE_SEVERITIES)}"
    )


# --- the condition this exists to catch ---

def test_warns_when_producers_are_loud_but_nothing_is_eligible(doctor, monkeypatch):
    """The live 2026-08-10 shape."""
    _rows(doctor, monkeypatch, [
        ["sentinel_alarm_finding", "132", "0", "0.1"],
        ["sentinel_finding", "71", "0", "0.5"],
        ["doctor_check_finding", "38", "0", "0.2"],
    ])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert r.status is doctor.Status.WARN
    assert "DRY" in r.message
    assert "241" in r.message           # total across producers
    assert "0 eligible" in r.message


def test_warning_reports_per_producer_coverage(doctor, monkeypatch):
    """The federation payload: which producers can reach the queue at all.
    A single boolean would hide that 8 of 10 are structurally excluded."""
    _rows(doctor, monkeypatch, [
        ["sentinel_alarm_finding", "132", "0", "0.1"],
        ["doctor_check_finding", "38", "0", "0.2"],
    ])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert "sentinel_alarm_finding=0/132" in r.detail
    assert "doctor_check_finding=0/38" in r.detail


def test_warning_forbids_the_tempting_wrong_fix(doctor, monkeypatch):
    """Widening the queue books another producer's finding against Sentinel's
    EISV. The warning has to say so or it invites the regression it warns of."""
    _rows(doctor, monkeypatch, [["sentinel_finding", "80", "0", "0.1"]])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert "attribution" in r.detail.lower()
    assert "widen" in r.detail.lower()


# --- must not cry wolf ---

def test_passes_when_something_is_eligible(doctor, monkeypatch):
    _rows(doctor, monkeypatch, [
        ["sentinel_alarm_finding", "132", "4", "0.1"],
        ["doctor_check_finding", "38", "0", "0.2"],
    ])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert r.status is doctor.Status.PASS
    assert "4/170" in r.message


def test_quiet_fleet_is_not_a_dry_queue(doctor, monkeypatch):
    """Below the volume floor, "0 eligible" is indistinguishable from "quiet
    week" — reporting it as dry would train the operator to ignore this."""
    _rows(doctor, monkeypatch, [["sentinel_finding", "3", "0", "1.0"]])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert r.status is doctor.Status.PASS
    assert "too few" in r.message


def test_no_findings_defers_to_the_liveness_check(doctor, monkeypatch):
    """Zero producers is finding_producer_live's question. Answering it here
    too would double-report one condition as two independent failures."""
    _rows(doctor, monkeypatch, [])
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert r.status is doctor.Status.SKIP


def test_unqueryable_db_skips_rather_than_fails(doctor, monkeypatch):
    _rows(doctor, monkeypatch, None)
    r = doctor.check_adjudication_feedstock("postgresql://x/y")
    assert r.status is doctor.Status.SKIP


# --- wiring ---

def test_check_is_registered(doctor, tmp_path):
    checks = doctor.build_checks(REPO_ROOT, "postgresql://x/y")
    names = [c.name for c in checks]
    assert "adjudication_feedstock" in names
    assert dict((c.name, c.mode) for c in checks)["adjudication_feedstock"] == "operator"

"""Tests for the doctor-findings escalator.

Pins the behaviour that motivated it: unitares_doctor's live operator checks ran
nowhere, so detectors built for silent failure were themselves silent (Sentinel
governance-dark 24h from 2026-07-29; Watcher dead a month). Escalation is only
useful if it fires on a real failure, stays quiet while the condition is already
open, and closes itself when the condition clears.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "doctor_findings.py"
_spec = importlib.util.spec_from_file_location("doctor_findings", MODULE_PATH)
df = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(df)


@dataclass
class FakeStatus:
    value: str


@dataclass
class FakeResult:
    name: str
    status: FakeStatus
    message: str
    detail: str = ""


WARN = FakeStatus("warn")
FAIL = FakeStatus("fail")
PASS = FakeStatus("pass")
SKIP = FakeStatus("skip")


def make(results, posted, *, state=None, dry_run=False):
    """A DoctorFindings whose checks are scripted and whose posts are captured."""
    d = df.DoctorFindings(io={"post_finding": posted.append}, dry_run=dry_run)
    d.state = state if state is not None else {}
    d.collect = lambda: results  # type: ignore[method-assign]
    d._save_state = lambda: None  # type: ignore[method-assign]
    return d


@pytest.fixture(autouse=True)
def _status_enum(monkeypatch):
    """Stand in for unitares_doctor.Status without importing the real module."""
    class _D:
        class Status:
            WARN = WARN
            FAIL = FAIL
            PASS = PASS
            SKIP = SKIP
    import sys
    monkeypatch.setitem(sys.modules, "unitares_doctor", _D)


def test_warn_escalates_as_a_finding():
    posted: list = []
    make([FakeResult("immortal_lease", WARN, "2 lease(s) renewed past any sane TTL")],
         posted).run()
    assert len(posted) == 1
    assert posted[0]["event_type"] == df.FINDING_KIND
    assert posted[0]["severity"] == "warning"
    assert "immortal_lease" in posted[0]["message"]


def test_fail_is_critical():
    posted: list = []
    make([FakeResult("checkin_stream_live", FAIL, "fleet governance-dark")], posted).run()
    assert posted[0]["severity"] == "critical"


def test_pass_escalates_nothing():
    posted: list = []
    make([FakeResult("immortal_lease", PASS, "no immortal leases")], posted).run()
    assert posted == []


def test_detail_is_carried_into_the_message():
    # The actionable half of a doctor result lives in .detail (e.g. how to
    # force-release). A finding without it makes the operator go look it up.
    posted: list = []
    make([FakeResult("immortal_lease", WARN, "1 lease(s)", detail="force-release via ...")],
         posted).run()
    assert "force-release" in posted[0]["message"]


def test_open_condition_is_suppressed_by_cooldown():
    posted: list = []
    r = FakeResult("immortal_lease", WARN, "still 2 leases")
    fp = df.fingerprint("immortal_lease", "warn")
    state = {"open": {fp: {"check": "immortal_lease", "last_alert": 1e18}}}
    make([r], posted, state=state).run()
    assert posted == []


def test_recovery_closes_the_open_finding():
    posted: list = []
    fp = df.fingerprint("immortal_lease", "warn")
    state = {"open": {fp: {"check": "immortal_lease", "last_alert": 0}}}
    d = make([FakeResult("immortal_lease", PASS, "no immortal leases")], posted, state=state)
    d.run()
    assert fp not in d.state["open"]


def test_fingerprint_ignores_volatile_counts():
    # Messages carry counts that move every run; keying on them would mint a new
    # fingerprint constantly and re-alert on noise.
    assert df.fingerprint("immortal_lease", "warn") == df.fingerprint("immortal_lease", "warn")
    assert df.fingerprint("immortal_lease", "warn") != df.fingerprint("immortal_lease", "fail")
    assert df.fingerprint("immortal_lease", "warn") != df.fingerprint("signal_degeneracy", "warn")


def test_dry_run_posts_nothing():
    posted: list = []
    make([FakeResult("immortal_lease", WARN, "2 leases")], posted, dry_run=True).run()
    assert posted == []


def test_post_finding_failure_never_raises(monkeypatch):
    # Escalation is best-effort and runs on a timer; a governance outage must
    # not turn into a crashed watchdog.
    def boom(_payload):
        raise RuntimeError("governance down")
    d = df.DoctorFindings(io={"post_finding": boom})
    d.state = {}
    d.collect = lambda: [FakeResult("immortal_lease", WARN, "2 leases")]  # type: ignore[method-assign]
    d._save_state = lambda: None  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        d.run()  # the injected io seam raises directly; io_post_finding is what swallows


def test_io_post_finding_swallows_exceptions(monkeypatch):
    monkeypatch.setattr(df, "log", lambda _m: None)
    df.io_post_finding({"event_type": "x"})  # missing kwargs -> must not raise


def test_mass_skip_escalates_as_blindness():
    # Under launchd's minimal PATH psql is absent, so every DB-backed check
    # returns SKIP and the sweep would otherwise print "all operator checks
    # pass" while seeing nothing. A skipped check is not a passing one.
    posted: list = []
    results = [FakeResult(f"check_{i}", SKIP, "not queryable") for i in range(8)]
    results.append(FakeResult("local_thing", PASS, "fine"))
    make(results, posted).run()
    assert len(posted) == 1
    assert "doctor_sweep_blind" in posted[0]["message"]
    assert posted[0]["severity"] == "critical"


def test_a_couple_of_skips_is_not_blindness():
    # An uninstalled optional component legitimately skips; that must stay quiet.
    posted: list = []
    results = [FakeResult("a", SKIP, "n/a")] + [
        FakeResult(f"p{i}", PASS, "fine") for i in range(8)
    ]
    make(results, posted).run()
    assert posted == []


TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts" / "ops" / "com.unitares.doctor-findings.plist.template"
)


def test_plist_path_reaches_a_keg_only_psql():
    """The shipped PATH must contain the dir that actually holds psql.

    Homebrew's postgresql@17 is keg-only — brew does NOT symlink it into
    /opt/homebrew/bin; it stays at /opt/homebrew/opt/postgresql@17/bin. The
    first version of this template shipped only /opt/homebrew/bin, so an
    install that followed its own instructions verbatim came up with 8 of 11
    checks SKIPPED (verified 2026-08-01 via --dry-run).

    doctor_sweep_blind did catch it, which is the point of the net — but the
    install looked correct while it happened, and the net should not be the
    thing standing between this detector and another month of silence.
    """
    line = next(
        (ln for ln in TEMPLATE_PATH.read_text().splitlines() if "<key>PATH</key>" in ln),
        None,
    )
    assert line is not None, "template no longer declares a PATH env var"
    entries = line.split("<string>", 1)[1].split("</string>", 1)[0].split(":")
    assert any("postgresql" in entry for entry in entries), (
        "PATH declares no postgresql bin dir, so keg-only psql will not resolve "
        f"and every DB-backed check will SKIP. Got: {entries}"
    )

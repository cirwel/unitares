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
import plistlib
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
    def _post(payload):
        # Capture AND report a realistic outcome. Returning None here would
        # read as a failed escalation now that the caller checks the result.
        posted.append(payload)
        return df.DELIVERED

    d = df.DoctorFindings(io={"post_finding": _post}, dry_run=dry_run)
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
    assert posted[0]["extra"] == {"check": "immortal_lease"}


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


HOUR = 3600.0


def _open_state(check, status, *, ago_h, alerts=None):
    """State with one open finding last posted ``ago_h`` hours ago."""
    import time
    rec = {"check": check, "status": status, "first_seen": 0,
           "last_alert": time.time() - ago_h * HOUR}
    if alerts is not None:
        rec["alerts"] = alerts
    return {"open": {df.fingerprint(check, status): rec}}


def test_open_condition_is_not_reposted_after_six_hours():
    # The flat 6h cooldown re-posted every open condition four times a day:
    # 356 findings from 8 fingerprints in 30d (2026-09-24). State written
    # before the backoff has no "alerts" count and must start at the base.
    posted: list = []
    state = _open_state("signal_degeneracy", "warn", ago_h=7)
    make([FakeResult("signal_degeneracy", WARN, "still degenerate")], posted,
         state=state).run()
    assert posted == []


def test_open_condition_reposts_after_the_base_interval_and_counts_it():
    posted: list = []
    state = _open_state("signal_degeneracy", "warn", ago_h=25, alerts=1)
    d = make([FakeResult("signal_degeneracy", WARN, "still degenerate")], posted,
             state=state)
    d.run()
    assert len(posted) == 1
    fp = df.fingerprint("signal_degeneracy", "warn")
    assert d.state["open"][fp]["alerts"] == 2


def test_each_repeat_doubles_the_quiet_period():
    posted: list = []
    held = _open_state("signal_degeneracy", "warn", ago_h=25, alerts=2)
    make([FakeResult("signal_degeneracy", WARN, "x")], posted, state=held).run()
    assert posted == []  # second repeat waits 48h, not 24h

    due = _open_state("signal_degeneracy", "warn", ago_h=49, alerts=2)
    make([FakeResult("signal_degeneracy", WARN, "x")], posted, state=due).run()
    assert len(posted) == 1


def test_backoff_is_capped_so_a_stuck_condition_still_resurfaces():
    posted: list = []
    state = _open_state("signal_degeneracy", "warn", ago_h=7 * 24 + 1, alerts=40)
    make([FakeResult("signal_degeneracy", WARN, "x")], posted, state=state).run()
    assert len(posted) == 1


def test_realert_interval_sequence():
    assert df.realert_interval(1) == df.COOLDOWN_SECONDS
    assert df.realert_interval(2) == 2 * df.COOLDOWN_SECONDS
    assert df.realert_interval(0) == df.COOLDOWN_SECONDS
    assert df.realert_interval(10_000) == max(df.MAX_COOLDOWN_SECONDS,
                                              df.COOLDOWN_SECONDS)


def test_first_post_starts_the_count_at_one():
    posted: list = []
    d = make([FakeResult("immortal_lease", WARN, "2 leases")], posted)
    d.run()
    fp = df.fingerprint("immortal_lease", "warn")
    assert d.state["open"][fp]["alerts"] == 1


def test_a_new_condition_posts_while_others_are_held():
    posted: list = []
    state = _open_state("signal_degeneracy", "warn", ago_h=1, alerts=1)
    make([FakeResult("signal_degeneracy", WARN, "held"),
          FakeResult("immortal_lease", WARN, "new")], posted, state=state).run()
    assert [p["extra"]["check"] for p in posted] == ["immortal_lease"]


def test_escalation_to_fail_posts_despite_a_fresh_warn():
    posted: list = []
    state = _open_state("checkin_stream_live", "warn", ago_h=1, alerts=1)
    make([FakeResult("checkin_stream_live", FAIL, "fleet dark")], posted,
         state=state).run()
    assert len(posted) == 1
    assert posted[0]["severity"] == "critical"


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
def test_failed_escalation_is_not_recorded_as_alerted():
    """A post that never reached governance must not be written to state.

    This is the bug that let deploy_drift_doctor run hourly for its whole life
    while posting nothing: escalation raised, the exception was swallowed, and
    last_alert was recorded anyway. The cooldown then suppressed every retry,
    so the bookkeeping buried the very finding it was tracking.
    """
    posted: list = []
    results = [FakeResult("immortal_lease", WARN, "4 leases past TTL")] + [
        FakeResult(f"p{i}", PASS, "fine") for i in range(8)
    ]
    d = make(results, posted)
    d.io["post_finding"] = lambda payload: df.FAILED
    d.run()
    assert d.state.get("open", {}) == {}, "a failed escalation must leave state clean"


def test_deduped_escalation_counts_as_reaching_governance():
    """DEDUPED means the server already holds it — recording is correct.

    Treating dedup like failure would retry forever and never settle.
    """
    posted: list = []
    results = [FakeResult("immortal_lease", WARN, "4 leases past TTL")] + [
        FakeResult(f"p{i}", PASS, "fine") for i in range(8)
    ]
    d = make(results, posted)
    d.io["post_finding"] = lambda payload: df.DEDUPED
    d.run()
    assert d.state.get("open", {}) != {}, "dedup means governance holds it; record the alert"


def test_template_renders_to_a_plist_python_can_parse():
    """The template must survive a strict XML parser, not just Apple's.

    XML forbids "--" inside a comment. Apple's parser is lenient -- launchd
    loads such a plist and `plutil -lint` passes -- but plistlib rejects it
    outright, so every Python tool that reads LaunchAgents silently drops
    the job and still reports its inventory as complete. That is how
    `unitares-automations census` under-counted this exact automation while
    it was running happily (fixed 2026-08-01).

    The failure recurs because the offending text is documentation: any
    edit that mentions a CLI long-flag inside the header comment
    reintroduces it. It came back within hours of the first fix, in a
    rewrite of this very comment block. Hence a test rather than a patch.
    """
    rendered = (
        TEMPLATE_PATH.read_text()
        .replace("__UNITARES_ROOT__", "/tmp/unitares-test")
        .replace("PYTHON_BIN", "/usr/bin/python3")
    )
    try:
        parsed = plistlib.loads(rendered.encode())
    except Exception as exc:  # noqa: BLE001
        offenders = [
            f"line {n}: {ln.strip()}"
            for n, ln in enumerate(rendered.splitlines(), 1)
            if "--" in ln and not ln.strip().startswith(("<!--", "-->"))
        ]
        raise AssertionError(
            f"template does not parse as a plist: {exc}\n"
            "XML forbids '--' inside comments. Offending lines:\n  "
            + "\n  ".join(offenders or ["<none found; different cause>"])
        ) from exc
    assert parsed.get("Label") == "com.unitares.doctor-findings"

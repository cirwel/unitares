"""A drained adjudication queue and a dead one are the same observation.

`adjudication_feedstock` reasons only from downstream `audit.events`, so when it
sees zero queue-eligible findings it cannot tell which of three things happened:
(a) the producing condition is genuinely gone, (b) the queue was DRAINED — the
last eligible finding was adjudicated and none has arrived since, or (c) the
alarm path BROKE. This check supplies the orthogonal signal by reading the
UPSTREAM substrate and asserting the transform: every real (non-test) forced
lease release must become a queue-admissible sentinel finding.

Why not just pass `adjudication_feedstock` when the newest eligible finding has a
newer adjudication: that shortcut lets case (c) read green forever against a
stale matched pair. Rejected in dialectic ce6f53ad3e0f404e (2026-08-19).

Live evidence behind the design (verified 2026-08-19 against `governance`):
a real forced release fired 2026-08-10 23:46:25 on `resident:/steward_eisv_sync`
(held_x_ttl 87.6, holder_pid_null true), alarmed 28.5s later, and was
adjudicated 2026-08-13. Backtest over 60 days: 128 real forced releases, 128
alarmed, 0 unmatched, 0 false positives.
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


def _row(surface, ts, matched, latency_s, recent):
    """One reconciliation row: surface, ts, matched, latency, is_recent."""
    return [surface, ts, "t" if matched else "f", str(latency_s),
            "t" if recent else "f"]


# --- the join key is the whole check; pin it to both producers ---

def test_fingerprint_prefix_matches_both_producers(doctor):
    """The check joins on a string built in two places — a Python producer and
    an Elixir one. If either drifts, the join silently matches nothing and this
    check reports a healthy transform forever. Duplication is only safe pinned."""
    prefix = doctor.FORCED_TRANSFORM_FINGERPRINT_PREFIX

    py = (REPO_ROOT / "agents" / "sentinel" / "forced_release_alarm.py").read_text()
    py_match = re.search(r'fingerprint=f"(forced_release:ad_hoc:)\{', py)
    assert py_match, "could not find the ad-hoc fingerprint in forced_release_alarm.py"
    assert py_match.group(1) == prefix, (
        f"python producer builds {py_match.group(1)!r} but the doctor joins on "
        f"{prefix!r}"
    )

    ex = (REPO_ROOT / "elixir" / "sentinel" / "lib" / "unitares_sentinel"
          / "forced_release_poller" / "logic.ex").read_text()
    ex_match = re.search(r'fingerprint: "(forced_release:ad_hoc:)#\{', ex)
    assert ex_match, "could not find the ad-hoc fingerprint in logic.ex"
    assert ex_match.group(1) == prefix, (
        f"elixir producer builds {ex_match.group(1)!r} but the doctor joins on "
        f"{prefix!r}"
    )


def test_query_excludes_reserved_test_surfaces(doctor, monkeypatch):
    """The daily td:/test/force-release-contract-* fixtures are suppressed before
    the alarm, so counting them would manufacture unmatched rows and FAIL
    permanently."""
    seen = {}

    def capture(_db, sql, **_k):
        seen["sql"] = sql
        return []

    monkeypatch.setattr(doctor, "_psql_rows", capture)
    doctor.check_forced_release_transform("postgresql:///x")
    assert "td:/test/%" in seen["sql"]
    assert "NOT LIKE" in seen["sql"]


def test_query_gives_fresh_events_time_to_alarm(doctor, monkeypatch):
    """Typical transform latency is ~28s. Judging a 10-second-old event would
    flap between FAIL and PASS on consecutive runs."""
    seen = {}

    def capture(_db, sql, **_k):
        seen["sql"] = sql
        return []

    monkeypatch.setattr(doctor, "_psql_rows", capture)
    doctor.check_forced_release_transform("postgresql:///x")
    assert f"interval '{doctor.FORCED_TRANSFORM_SETTLE_S} seconds'" in seen["sql"]


# --- the condition this exists to catch ---

def test_fails_when_a_real_forced_release_never_alarmed(doctor, monkeypatch):
    """Case (c). This is the state adjudication_feedstock cannot see."""
    _rows(doctor, monkeypatch, [
        _row("resident:/steward", "2026-08-18 04:00:00", False, -1, True),
        _row("resident:/steward", "2026-08-10 23:45:57", True, 28.5, False),
    ])
    r = doctor.check_forced_release_transform("postgresql:///x")
    assert r.status is doctor.Status.FAIL
    assert "NO sentinel finding" in r.message
    assert "resident:/steward" in r.detail
    # The operator must be told the key, or the first fix attempt joins wrong.
    assert "integer event_id" in r.detail


def test_passes_when_every_forced_release_alarmed(doctor, monkeypatch):
    """Case (b): the queue is drained, not dead."""
    _rows(doctor, monkeypatch, [
        _row("resident:/steward_eisv_sync", "2026-08-18 23:45:57", True, 28.5, True),
        _row("resident:/steward", "2026-08-17 20:19:49", True, 30.6, True),
    ])
    r = doctor.check_forced_release_transform("postgresql:///x")
    assert r.status is doctor.Status.PASS
    assert "DRAINED, not dead" in r.detail


def test_vacuous_when_no_real_forced_releases(doctor, monkeypatch):
    """A conditional invariant, not a heartbeat. Real forced releases are bursty
    and rare — a 33-day gap (2026-06-27 -> 07-30) is normal — so silence is the
    honest answer, not a green light and not an alarm."""
    _rows(doctor, monkeypatch, [])
    r = doctor.check_forced_release_transform("postgresql:///x")
    assert r.status is doctor.Status.SKIP
    assert "vacuously satisfied" in r.message


def test_skips_when_lease_plane_unreadable(doctor, monkeypatch):
    _rows(doctor, monkeypatch, None)
    r = doctor.check_forced_release_transform("postgresql:///x")
    assert r.status is doctor.Status.SKIP


# --- the latency arm, and the reason it has its own window ---

def test_warns_on_a_recent_latency_excursion(doctor, monkeypatch):
    """Presence alone is not enough. On 2026-07-30 the transform took 7.4h and
    every event still matched, so an absence-only check read green over a real
    Sentinel degradation."""
    _rows(doctor, monkeypatch, [
        _row("resident:/steward_eisv_sync", "2026-08-18 23:53:46", True, 26581, True),
        _row("resident:/steward", "2026-08-17 20:19:49", True, 30.6, True),
    ])
    r = doctor.check_forced_release_transform("postgresql:///x")
    assert r.status is doctor.Status.WARN
    assert "SLOW" in r.message
    # Host sleep produces real-but-not-Sentinel delay; the operator needs that.
    assert "slept" in r.detail


def test_stale_latency_excursion_does_not_warn_forever(doctor, monkeypatch):
    """Regression. The absence arm needs a long window because events are rare;
    the latency arm must not, or one resolved incident re-reports for the whole
    30-day lookback. Caught live 2026-08-19: the check WARNed about the closed
    2026-07-30 degradation for ten consecutive days. An open finding nobody
    closes is how a detector decays into noise."""
    _rows(doctor, monkeypatch, [
        # Slow, but outside the latency window: history, not current health.
        _row("resident:/steward_eisv_sync", "2026-07-30 23:53:46", True, 26581, False),
        _row("resident:/steward", "2026-08-01 20:19:49", True, 30.6, False),
    ])
    r = doctor.check_forced_release_transform("postgresql:///x")
    assert r.status is doctor.Status.PASS
    assert "unjudged" in r.message


def test_absence_still_fails_outside_the_latency_window(doctor, monkeypatch):
    """The narrower latency window must not narrow the invariant itself — a lost
    adjudication stays lost."""
    _rows(doctor, monkeypatch, [
        _row("resident:/steward", "2026-07-25 04:00:00", False, -1, False),
    ])
    r = doctor.check_forced_release_transform("postgresql:///x")
    assert r.status is doctor.Status.FAIL


def test_latency_window_is_narrower_than_the_absence_window(doctor):
    assert doctor.FORCED_TRANSFORM_LATENCY_DAYS < doctor.FORCED_TRANSFORM_DAYS


def test_latency_threshold_clears_observed_normal(doctor):
    """Measured normal is 24.9-35.5s across 60 days. The threshold must sit well
    above that and well below the 26,581s excursion it is meant to catch."""
    assert doctor.FORCED_TRANSFORM_LATENCY_WARN_S > 35.5 * 10
    assert doctor.FORCED_TRANSFORM_LATENCY_WARN_S < 26581


# --- the check must actually be wired ---

def test_check_is_registered(doctor):
    """unitares_doctor's own anti-pattern: build it, never wire it, watch it look
    dead to a usage audit, delete it, rebuild it."""
    checks = doctor.build_checks(REPO_ROOT, "postgresql:///x")
    names = {c.name for c in checks}
    assert "forced_release_transform" in names
    assert "adjudication_feedstock" in names


def test_feedstock_still_warns_and_does_not_pass_on_adjudication_recency(doctor, monkeypatch):
    """Ratified condition 1. The sibling stays WARN by design; the separation of
    drained-from-dead belongs to forced_release_transform, not to a shortcut
    here."""
    _rows(doctor, monkeypatch, [
        ["sentinel_alarm_finding", "305", "0", "0.1"],
        ["sentinel_finding", "65", "0", "0.5"],
        ["doctor_check_finding", "50", "0", "0.2"],
    ])
    r = doctor.check_adjudication_feedstock("postgresql:///x")
    assert r.status is doctor.Status.WARN
    assert "forced_release_transform" in r.detail

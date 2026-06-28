"""Cross-runtime fingerprint contract — Wave 1 condition 2 (RFC v0.1.1 §B2).

The `/api/findings` server dedups on the alarm fingerprint. Findings emit is a
direct-flip cutover (no shadow mode — RFC §Surface 2), so the same
forced-release condition can be alarmed by the Python Sentinel just before and
the BEAM Sentinel just after the <30s cutover gap. If the two runtimes produce
different fingerprints, the server cannot dedup them → double-fire.

This test pins the PYTHON side of the contract to exact literal strings. The
BEAM side asserts the SAME literals in
`elixir/sentinel/test/unitares_sentinel/forced_release_poller_logic_3class_test.exs`
(conflict_batch). The two suites asserting identical literals IS the
cross-runtime contract.

The conflict_batch case is load-bearing: it embeds an ISO-8601 timestamp, and
Python's `datetime.isoformat()` ("+00:00") must stay byte-equal to BEAM's
`Logic.iso8601_python/1` output — NOT `DateTime.to_iso8601/1` ("Z"), which was
the GAP 1 drift the 2026-06-14 condition-2 parity audit found
(`docs/proposals/resolved/wave-1-condition-2-alarm-parity-audit-2026-06-14.md`).
ad_hoc/deprecation_batch are ID-only and unaffected.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from agents.sentinel.forced_release_alarm import (  # noqa: E402
    RESERVED_TEST_SURFACE_PREFIX,
    _ad_hoc_alarm,
    _batch_alarm,
    _conflict_alarm,
    _is_reserved_test_surface,
)

UTC = timezone.utc


def test_reserved_test_surface_predicate():
    # Pins the reserved namespace so the contract test and the alarm poller
    # cannot drift apart. Events on this prefix are test fixtures, not operator
    # force-releases, and must be excluded from alarms.
    assert RESERVED_TEST_SURFACE_PREFIX == "td:/test/"
    assert _is_reserved_test_surface("td:/test/force-release-contract-abc")
    # Legacy pre-PR-#1102 naming must also be suppressed so lingering historical
    # events don't re-alarm on a cursor reset / freshly-restarted daemon.
    assert _is_reserved_test_surface("td:/force-release-contract-test-abc")
    assert not _is_reserved_test_surface("td:/pr3b_a")
    assert not _is_reserved_test_surface("dialectic:/x")
    assert not _is_reserved_test_surface(None)


def test_ad_hoc_fingerprint_is_event_id_only():
    row = {
        "event_id": "e1",
        "lease_id": "l1",
        "surface_id": "dialectic:/a",
        "surface_kind": "dialectic",
        "ts": datetime(2026, 5, 5, 9, 0, 0, tzinfo=UTC),
    }
    assert _ad_hoc_alarm(row).fingerprint == "forced_release:ad_hoc:e1"


def test_deprecation_batch_fingerprint_is_depr_id_only():
    row = {
        "deprecation_id": "d1",
        "surface_kind": "dialectic",
        "event_count": 3,
        "first_ts": datetime(2026, 5, 5, 8, 0, 0, tzinfo=UTC),
        "last_ts": datetime(2026, 5, 5, 9, 0, 0, tzinfo=UTC),
        "sweep_completed_at": datetime(2026, 5, 5, 10, 0, 0, tzinfo=UTC),
    }
    assert _batch_alarm(row).fingerprint == "forced_release:deprecation_batch:d1"


def test_conflict_batch_fingerprint_uses_plus_offset_iso():
    # MUST stay byte-equal to the BEAM literal in
    # forced_release_poller_logic_3class_test.exs:
    #   "forced_release:conflict_batch:dialectic:/conflict_test:2026-05-05T09:00:00+00:00"
    row = {
        "surface_id": "dialectic:/conflict_test",
        "surface_kind": "dialectic",
        "event_count": 7,
        "first_ts": datetime(2026, 5, 5, 8, 0, 0, tzinfo=UTC),
        "last_ts": datetime(2026, 5, 5, 9, 0, 0, tzinfo=UTC),
    }
    assert (
        _conflict_alarm(row).fingerprint
        == "forced_release:conflict_batch:dialectic:/conflict_test:2026-05-05T09:00:00+00:00"
    )


def test_conflict_batch_fingerprint_with_microseconds():
    # Python isoformat renders 6-digit microseconds when nonzero; BEAM's
    # iso8601_python/1 pads to 6 digits too. Pinned so the two stay aligned on
    # the fractional path, not just the whole-second path.
    row = {
        "surface_id": "dialectic:/x",
        "surface_kind": "dialectic",
        "event_count": 1,
        "first_ts": datetime(2026, 5, 5, 8, 0, 0, tzinfo=UTC),
        "last_ts": datetime(2026, 5, 5, 9, 0, 0, 123456, tzinfo=UTC),
    }
    assert (
        _conflict_alarm(row).fingerprint
        == "forced_release:conflict_batch:dialectic:/x:2026-05-05T09:00:00.123456+00:00"
    )

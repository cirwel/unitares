"""Tests for _watcher_summary_from_rows — the pure aggregator behind
/v1/watcher/summary. We test the aggregator directly so the test doesn't need
to stand up Starlette or touch the filesystem; endpoint wiring is covered by
the dashboard-allowlist regression and the route-registration smoke."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.http_api import _watcher_findings_path, _watcher_summary_from_rows


class TestFindingsPathMatchesWriter:
    """The reader (src.watcher_state_reader) and the writer
    (agents.watcher._util) must resolve the same findings.jsonl. They
    previously diverged across the dev/deploy checkout split and the dashboard
    read zeroes.

    The two implementations are deliberately import-independent — the server
    must not import resident example code — so this class IS the contract
    holding them together: if either side's resolution drifts, fail here
    instead of silently zeroing the panel."""

    @pytest.fixture(autouse=True)
    def _reset_both_sides(self, monkeypatch, tmp_path):
        import agents.watcher._util as util

        import src.watcher_state_reader as reader

        for mod in (util, reader):
            monkeypatch.setattr(mod, "_state_dir_cache", None)
            monkeypatch.setattr(mod, "_legacy_migration_done", True)  # skip fs work
            monkeypatch.setattr(mod, "_LEGACY_STATE_DIR", tmp_path / "missing-legacy")
        yield

    def test_default_resolution_parity(self, monkeypatch, tmp_path):
        import agents.watcher._util as util

        import src.watcher_state_reader as reader

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("UNITARES_WATCHER_DATA_DIR", raising=False)

        assert reader.watcher_state_dir() == util.watcher_state_dir()
        assert reader.watcher_state_dir() == tmp_path / ".unitares" / "watcher"

    def test_env_override_parity_expands_user(self, monkeypatch, tmp_path):
        import agents.watcher._util as util

        import src.watcher_state_reader as reader

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", "~/elsewhere")

        assert reader.watcher_state_dir() == util.watcher_state_dir()
        assert reader.watcher_state_dir() == tmp_path / "elsewhere"

    def test_reader_path_is_under_shared_state_dir(self, monkeypatch, tmp_path):
        import agents.watcher._util as util

        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(tmp_path / "shared"))

        # Reader and writer resolve the same shared dir from the same env.
        assert _watcher_findings_path() == util.watcher_state_dir() / "findings.jsonl"
        assert _watcher_findings_path() == tmp_path / "shared" / "findings.jsonl"

    def test_falls_back_to_legacy_while_shared_empty(self, monkeypatch, tmp_path):
        import src.watcher_state_reader as reader

        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "findings.jsonl").write_text('{"pattern":"P001"}\n')
        shared = tmp_path / "shared"

        monkeypatch.setattr(reader, "_LEGACY_STATE_DIR", legacy)
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(shared))

        # Shared dir has no data yet → read the live legacy file, not a frozen blank.
        assert _watcher_findings_path() == legacy / "findings.jsonl"

    def test_prefers_shared_once_populated(self, monkeypatch, tmp_path):
        import src.watcher_state_reader as reader

        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "findings.jsonl").write_text("legacy\n")
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "findings.jsonl").write_text("shared\n")

        monkeypatch.setattr(reader, "_LEGACY_STATE_DIR", legacy)
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(shared))

        assert _watcher_findings_path() == shared / "findings.jsonl"

    def test_migration_copies_legacy_state_once(self, monkeypatch, tmp_path):
        import src.watcher_state_reader as reader

        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "findings.jsonl").write_text("from-legacy\n")
        shared = tmp_path / "shared"

        monkeypatch.setattr(reader, "_legacy_migration_done", False)
        monkeypatch.setattr(reader, "_LEGACY_STATE_DIR", legacy)
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(shared))

        reader.migrate_legacy_watcher_state()

        assert (shared / "findings.jsonl").read_text() == "from-legacy\n"
        # Source left untouched so an old-code writer mid-rollout isn't disrupted.
        assert (legacy / "findings.jsonl").exists()


NOW = datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc)


def _row(**kwargs):
    base = {
        "pattern": "P001",
        "severity": "high",
        "detected_at": NOW.isoformat(),
        "status": "surfaced",
    }
    base.update(kwargs)
    return base


class TestStatusAndSeverityCounts:
    def test_empty_input_returns_zeroed_shape(self):
        out = _watcher_summary_from_rows([], now=NOW, window_days=7)
        assert out["total"] == 0
        assert out["by_status"] == {}
        assert out["by_severity_open"] == {}
        assert out["patterns"] == []
        assert len(out["timeline"]) == 7
        assert all(d["detected"] == 0 for d in out["timeline"])

    def test_status_counter_counts_everything(self):
        # "confirmed" is the closed-as-real-bug status; see findings.py
        # VALID_FINDING_STATUSES. Earlier the aggregator looked for "resolved"
        # and silently dropped every confirmed finding.
        rows = [
            _row(status="surfaced"),
            _row(status="surfaced"),
            _row(status="confirmed"),
            _row(status="dismissed"),
        ]
        out = _watcher_summary_from_rows(rows, now=NOW)
        assert out["by_status"] == {"surfaced": 2, "confirmed": 1, "dismissed": 1}
        assert out["total"] == 4

    def test_severity_counts_only_open_findings(self):
        """by_severity_open is the actionable queue; confirmed/dismissed shouldn't
        show up there or the panel implies ongoing severity that isn't real."""
        rows = [
            _row(severity="critical", status="surfaced"),
            _row(severity="high", status="surfaced"),
            _row(severity="critical", status="confirmed"),   # closed — excluded
            _row(severity="critical", status="dismissed"),   # closed — excluded
        ]
        out = _watcher_summary_from_rows(rows, now=NOW)
        assert out["by_severity_open"] == {"critical": 1, "high": 1}

    def test_legacy_resolved_status_falls_through_to_other(self):
        """Defensive: if any pre-existing findings.jsonl rows still carry the
        old 'resolved' status (none in current fixtures, but the file is
        gitignored so an old machine could have them), they're tallied in
        by_status but routed to the pattern bucket's 'other' slot — they
        shouldn't pollute the confirmed/dismissed counts."""
        rows = [_row(pattern="P_LEGACY", status="resolved")]
        out = _watcher_summary_from_rows(rows, now=NOW)
        assert out["by_status"] == {"resolved": 1}
        bucket = out["patterns"][0]
        assert bucket["confirmed"] == 0
        assert bucket["dismissed"] == 0
        assert bucket["other"] == 1
        # Ratio undefined when nothing landed in confirmed/dismissed
        assert bucket["dismiss_ratio"] is None


class TestPatternTable:
    def test_pattern_breakdown_with_dismiss_ratio(self):
        """dismiss_ratio is a descriptive stat, not a noise verdict on its own.
        A high ratio only signals a retirement candidate when the rule has
        *also* never confirmed a real bug — when confirmed > 0 the dismissals
        are the FP-filter pipeline catching known-benign matches (PR #659). The
        panel makes that call via confirmed + dismissed_fp; this test just pins
        the underlying counts the panel reads."""
        rows = [
            _row(pattern="P008", status="dismissed"),
            _row(pattern="P008", status="dismissed"),
            _row(pattern="P008", status="dismissed"),   # 3/3 dismissed, 0 confirmed — retire
            _row(pattern="P001", status="confirmed"),
            _row(pattern="P001", status="confirmed"),   # 0/2 dismissed — signal
            _row(pattern="P001", status="surfaced"),    # 1 still open
        ]
        out = _watcher_summary_from_rows(rows, now=NOW)
        by = {p["pattern"]: p for p in out["patterns"]}
        assert by["P008"]["dismissed"] == 3 and by["P008"]["confirmed"] == 0
        assert by["P008"]["dismiss_ratio"] == pytest.approx(1.0)
        assert by["P001"]["confirmed"] == 2 and by["P001"]["dismissed"] == 0
        assert by["P001"]["dismiss_ratio"] == pytest.approx(0.0)

    def test_dismissed_fp_counts_only_false_positive_reason(self):
        """dismissed_fp isolates dismissals closed as confirmed false positives
        (reason='fp') from other dismissal reasons. This is the signal that
        distinguishes 'FP filters working' from 'rule produces no usable
        signal' — a healthy rule can have a high dismiss ratio made entirely of
        fp closures while still confirming real bugs."""
        rows = [
            # P016: high dismiss ratio, but every dismissal is a caught FP and
            # it still confirms a real bug — the PR #659 "keep it" shape.
            _row(pattern="P016", status="dismissed", resolution_reason="fp"),
            _row(pattern="P016", status="dismissed", resolution_reason="fp"),
            _row(pattern="P016", status="dismissed", resolution_reason="fp"),
            _row(pattern="P016", status="confirmed"),
            # P777: dismissed for non-fp reasons (out of scope / won't fix) —
            # not counted as caught false positives.
            _row(pattern="P777", status="dismissed", resolution_reason="out_of_scope"),
            _row(pattern="P777", status="dismissed", resolution_reason="wont_fix"),
            _row(pattern="P777", status="dismissed"),  # no reason recorded
        ]
        out = _watcher_summary_from_rows(rows, now=NOW)
        by = {p["pattern"]: p for p in out["patterns"]}
        assert by["P016"]["dismissed"] == 3 and by["P016"]["dismissed_fp"] == 3
        assert by["P016"]["confirmed"] == 1  # still earns its keep
        assert by["P777"]["dismissed"] == 3 and by["P777"]["dismissed_fp"] == 0

    def test_dismiss_ratio_is_none_when_nothing_closed(self):
        """With no closed findings for a pattern, the ratio is undefined —
        returning None lets the frontend render a dash instead of 0.0 which
        would falsely imply 'this rule is never dismissed'."""
        out = _watcher_summary_from_rows([_row(pattern="P999", status="surfaced")], now=NOW)
        p = out["patterns"][0]
        assert p["surfaced"] == 1
        assert p["dismiss_ratio"] is None

    def test_patterns_sorted_by_open_count_desc(self):
        rows = [
            _row(pattern="P_LOW", status="surfaced"),
            _row(pattern="P_HIGH", status="surfaced"),
            _row(pattern="P_HIGH", status="surfaced"),
            _row(pattern="P_HIGH", status="surfaced"),
        ]
        out = _watcher_summary_from_rows(rows, now=NOW)
        assert [p["pattern"] for p in out["patterns"]] == ["P_HIGH", "P_LOW"]


class TestTimeline:
    def test_timeline_spans_full_window_with_zeros(self):
        """Even with a single finding, the chart should have a point for every
        day in the window so the line renders continuously, not with gaps."""
        rows = [_row(detected_at=NOW.isoformat())]
        out = _watcher_summary_from_rows(rows, now=NOW, window_days=5)
        assert len(out["timeline"]) == 5
        days_with_data = [d for d in out["timeline"] if d["detected"] > 0]
        assert len(days_with_data) == 1
        assert days_with_data[0]["detected"] == 1

    def test_timeline_buckets_detections_by_day(self):
        day_a = datetime(2026, 4, 20, 10, 0, tzinfo=timezone.utc)
        day_b = datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc)
        rows = [
            _row(detected_at=day_a.isoformat()),
            _row(detected_at=day_a.isoformat()),
            _row(detected_at=day_b.isoformat()),
        ]
        out = _watcher_summary_from_rows(rows, now=NOW, window_days=7)
        by_day = {d["day"]: d for d in out["timeline"]}
        assert by_day["2026-04-20"]["detected"] == 2
        assert by_day["2026-04-22"]["detected"] == 1

    def test_timeline_excludes_findings_outside_window(self):
        old = (NOW - timedelta(days=45)).isoformat()
        out = _watcher_summary_from_rows([_row(detected_at=old)], now=NOW, window_days=30)
        assert all(d["detected"] == 0 for d in out["timeline"])

    def test_timeline_captures_confirmed_and_dismissed_timestamps(self):
        confirmed_day = datetime(2026, 4, 21, 14, 0, tzinfo=timezone.utc)
        dismissed_day = datetime(2026, 4, 22, 14, 0, tzinfo=timezone.utc)
        rows = [
            _row(
                detected_at=(NOW - timedelta(days=1)).isoformat(),
                status="confirmed",
                confirmed_at=confirmed_day.isoformat(),
            ),
            _row(
                detected_at=(NOW - timedelta(days=1)).isoformat(),
                status="dismissed",
                dismissed_at=dismissed_day.isoformat(),
            ),
        ]
        out = _watcher_summary_from_rows(rows, now=NOW, window_days=7)
        by_day = {d["day"]: d for d in out["timeline"]}
        assert by_day["2026-04-21"]["confirmed"] == 1
        assert by_day["2026-04-22"]["dismissed"] == 1


class TestRobustness:
    def test_row_with_trailing_Z_timestamp_parses(self):
        """Watcher writes timestamps like '2026-04-14T10:57:11Z' — Python 3.10
        datetime.fromisoformat rejects the trailing Z, so the aggregator must
        tolerate it."""
        rows = [_row(detected_at="2026-04-23T00:00:00Z")]
        out = _watcher_summary_from_rows(rows, now=NOW, window_days=2)
        by_day = {d["day"]: d for d in out["timeline"]}
        assert by_day["2026-04-23"]["detected"] == 1

    def test_row_with_malformed_timestamp_does_not_crash(self):
        rows = [_row(detected_at="not-a-date")]
        out = _watcher_summary_from_rows(rows, now=NOW)
        # Still counted in totals, just not placed on the timeline
        assert out["total"] == 1
        assert all(d["detected"] == 0 for d in out["timeline"])

    def test_row_without_pattern_falls_under_question_mark(self):
        rows = [_row(pattern=None, status="surfaced")]
        out = _watcher_summary_from_rows(rows, now=NOW)
        assert out["patterns"][0]["pattern"] == "?"

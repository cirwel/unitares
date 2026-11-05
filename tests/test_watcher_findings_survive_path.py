"""A finding must outlive the file it was found in.

Regression coverage for a silent-destruction bug found 2026-08-12. Watcher
scans whatever the editor touches, which routinely means git worktrees and
scratch directories — paths that are deleted as a matter of normal workflow.
``_sweep_stale_quiet`` dropped every finding whose target path was missing,
and ``surface_pending`` (the chime) calls it *before* displaying. So the
surfacing path destroyed the findings it existed to show.

The failure was invisible from the inside: the model self-test passed, the
agent checked in, and high-severity findings still reached the event stream.
Only the operator could observe it, and only as silence. Measured at the time:
12 of 12 recent target files no longer existed, ``findings.jsonl`` held zero
open entries, and both surfacing commands printed nothing.

The fix is that a finding carrying a snapshot of its offending source line is
self-contained — there is still something to evaluate — so it is retained and
flagged ``path_gone`` rather than deleted. This matters beyond notification:
an adjudicated finding produces one ``external_signal`` outcome, so a dropped
finding is a discarded label.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import agents.watcher.findings as F


def _iso_days_ago(days: float) -> str:
    then = datetime.now(timezone.utc) - timedelta(days=days)
    return then.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    """Point the module's file constants at a temp dir, not real state."""
    monkeypatch.setattr(F, "STATE_DIR", tmp_path)
    monkeypatch.setattr(F, "FINDINGS_FILE", tmp_path / "findings.jsonl")
    yield


def _write(rows: list[dict]) -> None:
    with F.FINDINGS_FILE.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _read() -> list[dict]:
    return [json.loads(line) for line in F.FINDINGS_FILE.read_text().splitlines() if line.strip()]


def _finding(**over) -> dict:
    base = {
        "pattern": "P001",
        "file": "/tmp/gone-worktree/src/server.py",
        "line": 251,
        "hint": "fire-and-forget task — store ref or use TaskGroup",
        "severity": "high",
        # Fresh by default, so the retention tests below stay retention tests
        # and never cross PATH_GONE_EXPIRY_DAYS as the calendar moves.
        "detected_at": _iso_days_ago(1),
        "model_used": "gemma4:latest",
        "line_content_hash": "abc123",
        "fingerprint": "deadbeefcafe0001",
        "status": "open",
        "violation_class": "REC",
        "line_content": "asyncio.create_task(self._emit(payload))",
        "path_gone": False,
    }
    base.update(over)
    return base


class TestRetention:
    def test_finding_with_snapshot_survives_a_missing_path(self):
        """The core regression: evidence outlives its file."""
        _write([_finding()])

        dropped = F._sweep_stale_quiet()

        assert dropped == 0, "a self-contained finding must not be dropped"
        rows = _read()
        assert len(rows) == 1
        assert rows[0]["fingerprint"] == "deadbeefcafe0001"
        assert rows[0]["line_content"] == "asyncio.create_task(self._emit(payload))"

    def test_retained_finding_is_flagged_path_gone(self):
        """The reader must be told to judge the snippet, not the path."""
        _write([_finding()])

        F._sweep_stale_quiet()

        assert _read()[0]["path_gone"] is True

    def test_snapshotless_finding_is_still_dropped(self):
        """Without evidence there is nothing to evaluate — old behavior holds."""
        _write([_finding(line_content="")])

        dropped = F._sweep_stale_quiet()

        assert dropped == 1
        assert _read() == []

    def test_existing_path_is_untouched(self, tmp_path):
        """A live target keeps its finding and is never flagged."""
        real = tmp_path / "live.py"
        real.write_text("asyncio.create_task(x())\n")
        _write([_finding(file=str(real))])

        dropped = F._sweep_stale_quiet()

        assert dropped == 0
        assert _read()[0]["path_gone"] is False


class TestFlagPersistence:
    def test_marking_alone_is_written_to_disk(self):
        """Guards a bug this fix nearly shipped with.

        The original early return fired on ``dropped == 0``. With retention
        added, a sweep can change state without dropping anything — so that
        return would compute ``path_gone`` and then discard it, leaving the
        flag unwritten until some unrelated later sweep happened to drop a
        finding. That is the same silent-staleness class the function exists
        to prevent, which is why it is pinned here rather than left to review.
        """
        _write([_finding()])

        F._sweep_stale_quiet()  # drops nothing; only marks

        assert _read()[0]["path_gone"] is True, "flag computed but never persisted"

    def test_marking_is_idempotent(self):
        """A second sweep must not rewrite state that is already correct."""
        _write([_finding(path_gone=True)])

        assert F._sweep_stale_quiet() == 0
        assert _read()[0]["path_gone"] is True


class TestMixedLedger:
    def test_drops_and_retains_in_one_pass(self, tmp_path):
        """Counting must stay correct when all three branches are exercised."""
        live = tmp_path / "live.py"
        live.write_text("x = 1\n")
        _write(
            [
                _finding(fingerprint="aaaa", file=str(live)),          # kept, live
                _finding(fingerprint="bbbb"),                          # kept, snapshot
                _finding(fingerprint="cccc", line_content=""),         # dropped
            ]
        )

        dropped = F._sweep_stale_quiet()

        assert dropped == 1
        kept = {r["fingerprint"]: r for r in _read()}
        assert set(kept) == {"aaaa", "bbbb"}
        assert kept["aaaa"]["path_gone"] is False
        assert kept["bbbb"]["path_gone"] is True


class TestExpiry:
    """Retention is bounded: an unadjudicated finding on a deleted path ages
    out after PATH_GONE_EXPIRY_DAYS instead of staying open forever.

    Without the bound the only exit from the open set was adjudication, so it
    grew whenever nobody adjudicated (2026-11-05: 258 open, 181 on deleted
    paths). Aged out, not deleted: the row and its snapshot stay on disk.
    """

    def test_old_path_gone_finding_ages_out_but_is_kept(self):
        _write([_finding(detected_at=_iso_days_ago(F.PATH_GONE_EXPIRY_DAYS + 1))])

        dropped = F._sweep_stale_quiet()

        assert dropped == 0, "expiry must not delete the row"
        rows = _read()
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "aged_out"
        assert row["resolution_reason"] == "path_gone_unadjudicated"
        assert row["aged_out_at"]
        assert row["path_gone"] is True
        assert row["line_content"] == "asyncio.create_task(self._emit(payload))"

    def test_surfaced_status_expires_too(self):
        _write([_finding(status="surfaced", detected_at=_iso_days_ago(F.PATH_GONE_EXPIRY_DAYS + 5))])

        F._sweep_stale_quiet()

        assert _read()[0]["status"] == "aged_out"

    def test_young_path_gone_finding_stays_open(self):
        _write([_finding(detected_at=_iso_days_ago(F.PATH_GONE_EXPIRY_DAYS - 1))])

        F._sweep_stale_quiet()

        row = _read()[0]
        assert row["status"] == "open"
        assert row["path_gone"] is True

    def test_old_finding_on_live_path_never_expires(self, tmp_path):
        """Expiry is for deleted paths only; a live file keeps its finding."""
        real = tmp_path / "live.py"
        real.write_text("asyncio.create_task(x())\n")
        _write([_finding(file=str(real), detected_at=_iso_days_ago(200))])

        F._sweep_stale_quiet()

        assert _read()[0]["status"] == "open"

    @pytest.mark.parametrize("status", ["confirmed", "dismissed", "aged_out"])
    def test_adjudicated_rows_are_not_rewritten(self, status):
        """A label already recorded is never overwritten by expiry."""
        _write([_finding(status=status, path_gone=True, detected_at=_iso_days_ago(90))])

        F._sweep_stale_quiet()

        row = _read()[0]
        assert row["status"] == status
        assert "resolution_reason" not in row

    @pytest.mark.parametrize("detected_at", ["", "not-a-date", None])
    def test_unparseable_timestamp_fails_open(self, detected_at):
        _write([_finding(detected_at=detected_at, path_gone=True)])

        F._sweep_stale_quiet()

        assert _read()[0]["status"] == "open"

    def test_expiry_alone_is_written_to_disk(self):
        """Same trap as TestFlagPersistence: an already-flagged row that only
        expires drops nothing and marks nothing new, so the early return must
        count the expiry or the change is computed and discarded."""
        _write([_finding(path_gone=True, detected_at=_iso_days_ago(F.PATH_GONE_EXPIRY_DAYS + 1))])

        F._sweep_stale_quiet()

        assert _read()[0]["status"] == "aged_out", "expiry computed but never persisted"

    def test_expired_rows_leave_the_unresolved_listing(self, capsys, tmp_path):
        # Deleted paths inside the scope root, so both rows are in scope.
        gone = str(tmp_path / "removed-worktree" / "src" / "server.py")
        _write(
            [
                _finding(
                    fingerprint="01dc0ffee0000001",
                    file=gone,
                    detected_at=_iso_days_ago(F.PATH_GONE_EXPIRY_DAYS + 1),
                ),
                _finding(fingerprint="7e5c0ffee0000002", file=gone, line=99),
            ]
        )
        F._sweep_stale_quiet()

        F.print_unresolved(scope_root=tmp_path)
        out = capsys.readouterr().out

        assert "Total unresolved: 1" in out
        assert "#7e5c0ffe" in out
        assert "#01dc0ffe" not in out

    def test_cli_sweep_reports_expiry(self, capsys):
        _write([_finding(path_gone=True, detected_at=_iso_days_ago(F.PATH_GONE_EXPIRY_DAYS + 1))])

        F.sweep_stale_findings()

        out = capsys.readouterr().out
        assert "aged out 1" in out
        assert "nothing to sweep" not in out

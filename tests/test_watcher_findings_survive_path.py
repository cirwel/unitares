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

import pytest

import agents.watcher.findings as F


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
        "detected_at": "2026-08-12T14:27:06Z",
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

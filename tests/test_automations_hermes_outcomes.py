"""hermes cron outcomes reach the census.

hermes records every execution in its own SQLite ledger, but the census read
only `jobs.json` — which carries no `last_status` — and so reported every
hermes job as `last=-`, i.e. "no idea". On 2026-08-15 that hid two UNITARES
jobs failing every six hours since 2026-08-12.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

# spec_from_file_location cannot infer a loader for an extensionless script --
# name the loader. Same idiom as test_automations_claude_hooks.py.
_MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "ops" / "unitares-automations"
)
_loader = importlib.machinery.SourceFileLoader("unitares_automations", str(_MODULE_PATH))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
assert _spec
census = importlib.util.module_from_spec(_spec)
sys.modules["unitares_automations"] = census
_loader.exec_module(census)


def _ledger(tmp_path: Path, rows: list[tuple]) -> Path:
    db = tmp_path / ".hermes/cron/executions.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE executions (
             id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
             process_id TEXT NOT NULL, pid INTEGER NOT NULL,
             process_started_at INTEGER, status TEXT NOT NULL,
             claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
             error TEXT)"""
    )
    conn.executemany(
        "INSERT INTO executions (id, job_id, source, process_id, pid, status, "
        "claimed_at, finished_at, error) VALUES (?,?,'cron','p',1,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(census, "HOME", tmp_path)
    return tmp_path


class TestHermesLastRuns:
    def test_latest_execution_per_job_wins(self, home):
        _ledger(home, [
            ("1", "job-a", "completed", "2026-08-12T14:00:00-06:00", "2026-08-12T14:01:00-06:00", None),
            ("2", "job-a", "failed", "2026-08-14T20:00:00-06:00", None, "Script exited with code 1\nmissing repo"),
        ])
        latest = census.load_hermes_last_runs([])
        assert latest["job-a"]["status"] == "failed"
        # First line only — a full traceback in a census row is unreadable.
        assert latest["job-a"]["message"] == "Script exited with code 1"

    def test_completed_maps_to_ok(self, home):
        _ledger(home, [("1", "job-b", "completed", "2026-08-14T01:00:00Z", "2026-08-14T01:02:00Z", None)])
        assert census.load_hermes_last_runs([])["job-b"]["status"] == "ok"

    @pytest.mark.parametrize("raw", ["claimed", "running"])
    def test_inflight_is_not_reported_as_success(self, home, raw):
        """Calling a claimed run `ok` asserts an outcome nobody has observed —
        the same error the census refuses to make when it declines to call an
        unconfirmed job `past due`."""
        _ledger(home, [("1", "job-c", raw, "2026-08-14T01:00:00Z", None, None)])
        assert census.load_hermes_last_runs([])["job-c"]["status"] == "running"

    def test_missing_ledger_degrades_quietly(self, home):
        warnings: list[str] = []
        assert census.load_hermes_last_runs(warnings) == {}
        assert warnings == []

    def test_unreadable_ledger_warns_but_does_not_raise(self, home):
        db = home / ".hermes/cron/executions.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        db.write_text("this is not a sqlite database")
        warnings: list[str] = []
        assert census.load_hermes_last_runs(warnings) == {}
        assert warnings, "an unreadable ledger must be reported, not swallowed"

    def test_opens_read_only(self, home):
        """A census run must never be able to block or corrupt the scheduler
        it reports on."""
        db = _ledger(home, [("1", "job-d", "completed", "2026-08-14T01:00:00Z", None, None)])
        census.load_hermes_last_runs([])
        conn = sqlite3.connect(db)
        # Still writable by its owner afterwards: no lingering lock.
        conn.execute("INSERT INTO executions (id, job_id, source, process_id, pid, status, claimed_at) "
                     "VALUES ('9','job-d','cron','p',1,'completed','2026-08-15T01:00:00Z')")
        conn.commit()
        conn.close()


class TestCollectHermesUsesTheLedger:
    def _jobs(self, home: Path) -> None:
        (home / ".hermes/cron").mkdir(parents=True, exist_ok=True)
        (home / ".hermes/cron/jobs.json").write_text(
            '{"jobs": [{"id": "job-a", "name": "UNITARES dogfood pulse", '
            '"enabled": true, "prompt": "dogfood probe"}]}'
        )

    def test_failed_execution_surfaces_on_the_automation(self, home):
        self._jobs(home)
        _ledger(home, [("1", "job-a", "failed", "2026-08-14T20:00:00-06:00", None, "boom")])
        items = census.collect_hermes([])
        assert [i.last_status for i in items] == ["failed"]

    def test_explicit_jobs_json_status_still_wins(self, home):
        """The ledger is a fallback for a field jobs.json does not carry, not
        an override of one it does."""
        (home / ".hermes/cron").mkdir(parents=True, exist_ok=True)
        (home / ".hermes/cron/jobs.json").write_text(
            '{"jobs": [{"id": "job-a", "name": "x", "enabled": true, '
            '"last_status": "ok"}]}'
        )
        _ledger(home, [("1", "job-a", "failed", "2026-08-14T20:00:00Z", None, "boom")])
        assert census.collect_hermes([])[0].last_status == "ok"

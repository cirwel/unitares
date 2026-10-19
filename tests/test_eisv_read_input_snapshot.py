"""The stop-rule read's input snapshot and reconciliation, against a fake connection.

No database is touched: ``_connect`` is replaced by an in-memory stand-in that
serves fixed row text per table.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import scripts.analysis.eisv_read_input_snapshot as snap

T = datetime(2026, 10, 20, 9, 30, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


def _outcome(ts: str, oid: str, bad: bool = False) -> str:
    return json.dumps({"ts": ts, "outcome_id": oid, "agent_id": "a", "is_bad": bad}, sort_keys=True)


def _state(state_id: int, s: float = 0.2) -> str:
    return json.dumps({"state_id": state_id, "identity_id": 1, "entropy": s}, sort_keys=True)


def _identity(identity_id: int) -> str:
    return json.dumps({"identity_id": identity_id, "agent_id": "a"}, sort_keys=True)


BASE_ROWS = {
    "audit.outcome_events": [
        _outcome("2026-09-01T00:00:00+00:00", "o1"),
        _outcome("2026-10-01T00:00:00+00:00", "o2", bad=True),
    ],
    "core.agent_state": [_state(1), _state(2)],
    "core.identities": [_identity(1)],
}


class _FakeConn:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table
        self.calls = []
        self.closed = False

    @contextlib.asynccontextmanager
    async def _tx(self):
        yield

    def transaction(self, **kwargs):
        self.calls.append(("transaction", kwargs))
        return self._tx()

    async def _cursor(self, rows):
        for row in rows:
            yield (row,)

    def cursor(self, query, boundary, since, prefetch=None):
        self.calls.append(("cursor", boundary, since))
        spec = next(s for s in snap.TABLES if s.query == query)
        return self._cursor(self.rows_by_table[spec.name])

    async def close(self):
        self.closed = True


def _use(monkeypatch, rows_by_table):
    conn = _FakeConn(rows_by_table)

    async def _connect(_db_url):
        return conn

    monkeypatch.setattr(snap, "_connect", _connect)
    return conn


def _export(monkeypatch, tmp_path, rows=BASE_ROWS):
    conn = _use(monkeypatch, rows)
    out = tmp_path / "snapshot"
    manifest = asyncio.run(
        snap.export_snapshot("postgresql://unused", boundary=T, window_days=365, out=out, now=NOW)
    )
    return conn, out, manifest


def test_export_writes_a_verifiable_manifest(monkeypatch, tmp_path):
    conn, out, manifest = _export(monkeypatch, tmp_path)
    assert manifest["boundary"] == T.isoformat()
    assert manifest["outcome_ts_from"] == (T - timedelta(days=366)).isoformat()
    assert {name: t["rows"] for name, t in manifest["tables"].items()} == {
        "audit.outcome_events": 2,
        "core.agent_state": 2,
        "core.identities": 1,
    }
    assert snap.verify_snapshot(out) == manifest
    assert ("transaction", {"isolation": "repeatable_read", "readonly": True}) in conn.calls
    assert conn.closed
    # Every query is bounded strictly before T.
    for spec in snap.TABLES:
        assert "< $1::timestamptz" in spec.query


def test_snapshot_digest_is_deterministic(monkeypatch, tmp_path):
    _, first_dir, first = _export(monkeypatch, tmp_path / "a")
    _, second_dir, second = _export(monkeypatch, tmp_path / "b")
    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    for spec in snap.TABLES:
        assert (first_dir / spec.file).read_bytes() == (second_dir / spec.file).read_bytes()


def test_verify_detects_a_tampered_archive(monkeypatch, tmp_path):
    _, out, _ = _export(monkeypatch, tmp_path)
    path = out / "outcome_events.jsonl.gz"
    path.chmod(0o600)
    path.unlink()
    snap.write_table(path, [BASE_ROWS["audit.outcome_events"][0]])
    with pytest.raises(ValueError, match="does not match"):
        snap.verify_snapshot(out)


def test_export_refuses_a_future_boundary_and_a_used_directory(monkeypatch, tmp_path):
    _use(monkeypatch, BASE_ROWS)
    with pytest.raises(ValueError, match="in the future"):
        asyncio.run(
            snap.export_snapshot(
                "postgresql://unused", boundary=NOW + timedelta(minutes=1), window_days=365,
                out=tmp_path / "x", now=NOW,
            )
        )
    _, out, _ = _export(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="not empty"):
        asyncio.run(snap.export_snapshot("postgresql://unused", boundary=T, window_days=365, out=out, now=NOW))


def test_reconcile_reports_backfills_removals_and_changes(monkeypatch, tmp_path):
    _, out, manifest = _export(monkeypatch, tmp_path)
    live = {
        # o1 kept, o2 relabelled, o0 backfilled with a pre-T timestamp
        "audit.outcome_events": [
            _outcome("2026-09-01T00:00:00+00:00", "o1"),
            _outcome("2026-10-01T00:00:00+00:00", "o2", bad=False),
            _outcome("2026-08-01T00:00:00+00:00", "o0"),
        ],
        # state 1 removed by retention
        "core.agent_state": [_state(2)],
        "core.identities": [_identity(1)],
    }
    _use(monkeypatch, live)
    result = asyncio.run(snap.reconcile_snapshot("postgresql://unused", snapshot=out))
    assert result["snapshot_sha256"] == manifest["snapshot_sha256"]
    assert result["tables"]["audit.outcome_events"] == {
        "snapshot_rows": 2, "live_rows": 3, "matched": 1, "changed": 1,
        "live_only_backfill": 1, "snapshot_only_removed": 0,
    }
    assert result["tables"]["core.agent_state"] == {
        "snapshot_rows": 2, "live_rows": 1, "matched": 1, "changed": 0,
        "live_only_backfill": 0, "snapshot_only_removed": 1,
    }
    assert result["tables"]["core.identities"]["matched"] == 1


def test_cli_help_loads():
    with pytest.raises(SystemExit) as exc:
        snap.parse_args(["--help"])
    assert exc.value.code == 0

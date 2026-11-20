"""Durable record of the fingerprints the observer has already written.

The server keeps a 30-minute in-memory dedup window that a restart clears, so
it cannot be what stops a 5-minute poll from writing each fact again. The
observer keeps its own set, in one SQLite file, and checks it before writing.

With ``--record-outcomes`` the same file also holds the outcome queue
(``pending_outcomes``): graded outcomes waiting on their event's answer or on
their own delivery. See ``outcomes.py``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


class FingerprintStore:
    """Fingerprints already emitted, surviving an observer restart."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS emitted ("
            "fingerprint TEXT PRIMARY KEY, event_type TEXT NOT NULL, recorded_at TEXT NOT NULL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS pending_outcomes ("
            "key TEXT PRIMARY KEY, payload TEXT NOT NULL, accepted INTEGER NOT NULL, "
            "created_at TEXT NOT NULL)"
        )
        self._db.commit()

    def has(self, fingerprint: str) -> bool:
        row = self._db.execute(
            "SELECT 1 FROM emitted WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None

    def add(self, fingerprint: str, event_type: str) -> bool:
        """Record a fingerprint. False when it was already there."""
        cursor = self._db.execute(
            "INSERT OR IGNORE INTO emitted (fingerprint, event_type, recorded_at) VALUES (?, ?, ?)",
            (fingerprint, event_type, datetime.now(timezone.utc).isoformat()),
        )
        self._db.commit()
        return cursor.rowcount == 1

    def pending_state(self, key: str) -> str | None:
        """``queued``, ``accepted`` or None for an outcome key."""
        row = self._db.execute(
            "SELECT accepted FROM pending_outcomes WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        return "accepted" if row[0] else "queued"

    def put_pending(self, key: str, payload: dict, *, accepted: bool) -> None:
        """Queue an outcome, or replace the queued one."""
        self._db.execute(
            "INSERT INTO pending_outcomes (key, payload, accepted, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, accepted = excluded.accepted",
            (key, json.dumps(payload, sort_keys=True), int(accepted), datetime.now(timezone.utc).isoformat()),
        )
        self._db.commit()

    def accept_pending(self, key: str) -> None:
        self._db.execute("UPDATE pending_outcomes SET accepted = 1 WHERE key = ?", (key,))
        self._db.commit()

    def drop_pending(self, key: str) -> None:
        self._db.execute("DELETE FROM pending_outcomes WHERE key = ?", (key,))
        self._db.commit()

    def accepted_pending(self) -> list[tuple[str, dict]]:
        rows = self._db.execute(
            "SELECT key, payload FROM pending_outcomes WHERE accepted = 1 ORDER BY key"
        ).fetchall()
        return [(key, json.loads(payload)) for key, payload in rows]

    def count(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM emitted").fetchone()[0])

    def close(self) -> None:
        self._db.close()


def read_only_fingerprints(path: str | Path) -> frozenset[str]:
    """The fingerprints in an existing store, without creating or changing it.

    Raises ``sqlite3.Error`` when the file is not a fingerprint store.
    """
    path = Path(path)
    if not path.exists():
        return frozenset()
    # ``as_uri`` escapes ``?``, ``#`` and ``%`` in the path.
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return frozenset(row[0] for row in db.execute("SELECT fingerprint FROM emitted"))
    finally:
        db.close()

"""Durable record of the fingerprints the observer has already written.

The server keeps a 30-minute in-memory dedup window that a restart clears, so
it cannot be what stops a 5-minute poll from writing each fact again. The
observer keeps its own set, in one SQLite file, and checks it before writing.
"""

from __future__ import annotations

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

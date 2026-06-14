"""Stateful canary runner for resident validation ticks.

This module sits one layer above ``resident_validation``. It turns a one-tick
measurement contract into a repeatable canary stream by reading a JSONL state
file, choosing the next tick index for a resident, building bounded envelopes,
and appending them back to state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.resident_validation import ResidentProfile, build_tick_envelope


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read JSONL rows from ``path``, skipping blank lines."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        rows.append(json.loads(stripped))
    return rows


def next_tick_index(state_path: Path, cohort_id: str, resident_id: str) -> int:
    """Return the next 1-based tick index for a cohort/resident state stream."""
    latest = 0
    for row in _read_jsonl(state_path):
        if row.get("cohort_id") != cohort_id:
            continue
        resident = row.get("resident") or {}
        if resident.get("id") != resident_id:
            continue
        tick_index = int(row.get("tick_index") or 0)
        latest = max(latest, tick_index)
    return latest + 1


def append_ticks(state_path: Path, ticks: list[dict[str, Any]]) -> None:
    """Append raw tick envelopes to the JSONL state stream."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("a", encoding="utf-8") as handle:
        for tick in ticks:
            handle.write(json.dumps(tick, sort_keys=True) + "\n")


def _ensure_utc(dt: datetime | None) -> datetime:
    """Normalize optional datetimes to timezone-aware UTC."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def build_canary_ticks(
    profile: ResidentProfile,
    *,
    state_path: Path,
    count: int,
    observation: str,
    prediction: str,
    confidence: float,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Build and persist ``count`` sequential canary ticks for ``profile``."""
    if count <= 0:
        raise ValueError("count must be positive")
    observed_at = _ensure_utc(now)
    start = next_tick_index(state_path, profile.cohort_id, profile.resident_id)
    ticks = [
        build_tick_envelope(
            profile,
            tick_index=start + offset,
            observation=observation,
            prediction=prediction,
            confidence=confidence,
            now=observed_at,
        )
        for offset in range(count)
    ]
    append_ticks(state_path, ticks)
    return ticks

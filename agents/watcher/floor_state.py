"""Atomic persistence for the per-(pattern, file_class) precision floor.

State lives at ``data/watcher/pattern_floor.json``. Writes use
tmp + rename (mirroring ``_write_findings_atomic`` in findings.py:234)
so a concurrent reader — the surface hook fires on every
UserPromptSubmit, and Vigil/CLI/scan-hook may all touch the watcher
state dir — never sees a truncated file.

Schema:

    {
      "updated_at": "2026-04-27T00:00:00Z",
      "buckets": {
        "PATTERN|file_class": {
          "weighted_confirmed": 10.5,
          "weighted_dismissed": 2.3,
          "weighted_n": 12.8,
          "ci_lower": 0.45,
          "latest_observation": "2026-04-26T12:34:56Z"
        },
        ...
      }
    }

The "PATTERN|file_class" key uses '|' as the separator because patterns
are uppercase identifiers (P-XYZ, etc.) and file_class values are
lowercase enum values — '|' won't collide with either.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from agents.watcher._util import watcher_state_dir
from agents.watcher.calibration import BucketStats


FLOOR_FILE_NAME = "pattern_floor.json"
DEFAULT_STATE_DIR = watcher_state_dir()
_KEY_SEP = "|"


@dataclass
class FloorState:
    updated_at: str
    buckets: dict[tuple[str, str], BucketStats] = field(default_factory=dict)

    def get(self, pattern: str, file_class: str) -> BucketStats | None:
        return self.buckets.get((pattern, file_class))


def _bucket_to_dict(b: BucketStats) -> dict:
    return {
        "weighted_confirmed": b.weighted_confirmed,
        "weighted_dismissed": b.weighted_dismissed,
        "weighted_n": b.weighted_n,
        "ci_lower": b.ci_lower,
        "latest_observation": b.latest_observation,
    }


def _dict_to_bucket(pattern: str, file_class: str, payload: Mapping[str, object]) -> BucketStats:
    return BucketStats(
        pattern=pattern,
        file_class=file_class,
        weighted_confirmed=float(payload.get("weighted_confirmed", 0.0)),
        weighted_dismissed=float(payload.get("weighted_dismissed", 0.0)),
        weighted_n=float(payload.get("weighted_n", 0.0)),
        ci_lower=(
            float(payload["ci_lower"])
            if isinstance(payload.get("ci_lower"), (int, float))
            else None
        ),
        latest_observation=(
            payload["latest_observation"]
            if isinstance(payload.get("latest_observation"), str)
            else None
        ),
    )


def save_floor(state: FloorState, *, state_dir: Path | None = None) -> None:
    """Atomically persist ``state`` to ``pattern_floor.json``.

    Writes to a unique sibling tmp file (suffixed with PID + monotonic
    nanoseconds) and renames over the target — a crash mid-write leaves
    the previous file intact, AND two concurrent writers (Vigil cycle vs
    ``--recompute-floor`` CLI, or two overlapping Vigil cycles) never
    collide on the same tmp filename. Council-flagged race condition.
    """
    import os
    import time

    target_dir = state_dir or DEFAULT_STATE_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / FLOOR_FILE_NAME
    tmp = target.with_suffix(f"{target.suffix}.tmp.{os.getpid()}.{time.monotonic_ns()}")

    payload = {
        "updated_at": state.updated_at,
        "buckets": {
            f"{pattern}{_KEY_SEP}{file_class}": _bucket_to_dict(bucket)
            for (pattern, file_class), bucket in sorted(state.buckets.items())
        },
    }

    with tmp.open("w") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
    tmp.replace(target)


def load_floor(*, state_dir: Path | None = None) -> FloorState:
    """Load floor state. Missing or corrupt files yield an empty state
    (fail-open: a missing floor means no demotion fires, which is the
    safe default — we surface findings rather than hide them)."""
    target_dir = state_dir or DEFAULT_STATE_DIR
    target = target_dir / FLOOR_FILE_NAME
    if not target.exists():
        return FloorState(updated_at=_epoch_iso(), buckets={})
    try:
        payload = json.loads(target.read_text())
    except (json.JSONDecodeError, OSError):
        return FloorState(updated_at=_epoch_iso(), buckets={})

    raw_buckets = payload.get("buckets")
    if not isinstance(raw_buckets, dict):
        return FloorState(updated_at=str(payload.get("updated_at", _epoch_iso())), buckets={})

    buckets: dict[tuple[str, str], BucketStats] = {}
    for key, bp in raw_buckets.items():
        if not isinstance(key, str) or _KEY_SEP not in key or not isinstance(bp, dict):
            continue
        pattern, file_class = key.split(_KEY_SEP, 1)
        buckets[(pattern, file_class)] = _dict_to_bucket(pattern, file_class, bp)

    return FloorState(
        updated_at=str(payload.get("updated_at", _epoch_iso())),
        buckets=buckets,
    )


def _epoch_iso() -> str:
    return datetime(1970, 1, 1, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def recompute_floor(
    *,
    findings_file: Path | None = None,
    state_dir: Path | None = None,
    half_life_days: float = 30.0,
    min_weighted_n: float = 10.0,
    now: datetime | None = None,
) -> FloorState:
    """Read findings.jsonl, aggregate per-(pattern, file_class), persist.

    Returns the new FloorState. Designed to be called nightly (cron) or
    from the ``--recompute-floor`` CLI for ad-hoc rebuilds.
    """
    from agents.watcher.calibration import precision_by_pattern_and_class
    from agents.watcher.findings import _iter_findings_raw

    if findings_file is None:
        rows = _iter_findings_raw()
    else:
        rows = []
        if findings_file.exists():
            with findings_file.open() as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

    reference = now or datetime.now(timezone.utc)
    buckets = precision_by_pattern_and_class(
        rows,
        now=reference,
        half_life_days=half_life_days,
        min_weighted_n=min_weighted_n,
    )

    state = FloorState(
        updated_at=reference.strftime("%Y-%m-%dT%H:%M:%SZ"),
        buckets=buckets,
    )
    save_floor(state, state_dir=state_dir)
    return state

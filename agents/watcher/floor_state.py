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
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from agents.watcher._util import watcher_state_dir
from agents.watcher.calibration import BucketStats


FLOOR_FILE_NAME = "pattern_floor.json"
DEFAULT_STATE_DIR = watcher_state_dir()
logger = logging.getLogger(__name__)

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

    with tmp.open("w") as fh:
        json.dump(_floor_payload(state), fh, indent=2, sort_keys=True)
    tmp.replace(target)


def _floor_payload(state: FloorState) -> dict:
    return {
        "updated_at": state.updated_at,
        "buckets": {
            f"{pattern}{_KEY_SEP}{file_class}": _bucket_to_dict(bucket)
            for (pattern, file_class), bucket in sorted(state.buckets.items())
        },
    }


def _floor_json(state: FloorState) -> str:
    return json.dumps(_floor_payload(state), indent=2, sort_keys=True)


def save_floor_governed(
    state: FloorState,
    *,
    proposer_uuid: str,
    continuity_token: str,
    session_id: str,
    state_dir: Path | None = None,
    bearer_token: str | None = None,
) -> bool:
    """Persist the floor through a governed ``file_write`` effect — the write is
    audited, rollback-tracked, and lease-coordinated (the lease plane enforces a
    single writer, which is what the tmp+rename race guard approximated locally).

    On ANY failure — plane down, veto, missing identity/bearer — this falls back
    to the local atomic ``save_floor`` so the floor is never lost. Fail-open is
    preserved end to end. Returns True only if the governed path committed.
    """
    import os

    target = (state_dir or DEFAULT_STATE_DIR) / FLOOR_FILE_NAME
    bearer = bearer_token or os.environ.get("LEASE_PLANE_BEARER_TOKEN")
    try:
        if bearer and proposer_uuid and continuity_token:
            # The governed File.write fails (enoent) if the parent is absent —
            # mkdir first, exactly as the atomic save_floor does.
            target.parent.mkdir(parents=True, exist_ok=True)
            from unitares_sdk.lease_plane.client import (
                LeasePlaneClient,
                LeasePlaneClientConfig,
            )

            client = LeasePlaneClient(LeasePlaneClientConfig(bearer_token=bearer))
            resp = client.propose_file_write(
                path=str(target),
                content=_floor_json(state),
                proposer_uuid=proposer_uuid,
                continuity_token=continuity_token,
                session_id=session_id,
                idempotency_key=f"watcher-floor-{state.updated_at}",
            )
            if resp.get("ok"):
                return True
            logger.warning(
                "governed floor write rejected (%s); atomic fallback",
                resp.get("error"),
            )
    except Exception as exc:  # noqa: BLE001 — never lose the floor to a plane error
        logger.warning("governed floor write failed (%s); atomic fallback", exc)

    save_floor(state, state_dir=state_dir)
    return False


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
    governed_identity: dict | None = None,
) -> FloorState:
    """Read findings.jsonl, aggregate per-(pattern, file_class), persist.

    When ``governed_identity`` (``{proposer_uuid, continuity_token, session_id}``)
    is supplied, the floor is persisted through a governed ``file_write`` effect
    (audited + rollback-tracked + lease-coordinated), falling back to the atomic
    local write on any failure. Without it, the local atomic write is used.

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
    if governed_identity:
        save_floor_governed(state, state_dir=state_dir, **governed_identity)
    else:
        save_floor(state, state_dir=state_dir)
    return state

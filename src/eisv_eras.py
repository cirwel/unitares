"""EISV instrument eras: where persisted rows stop being comparable.

The registry (``config/eisv_instrument_eras.json``) lists every known change to
the EISV maths, policy or row population, with the best available effective
time and the interval in which it may have gone live. This module is the read
side used by analysis scripts. It imports nothing from the server, so a
standalone read can use it.

Rules it encodes:

* A window that crosses a ``value`` or ``population`` boundary is not one
  sample. ``require_single_era`` refuses it unless the caller gives a reason,
  and the reason is reported, not treated as making the pooling valid.
  ``era_windows`` splits a window into per-era pieces for stratified output.
* There is no per-field exemption. The action feeds behavioral E through
  ``decision_e`` and behavioral observations are partly built from ODE state,
  so a change that only touches the basin still moves behavioral E/I/V on
  later check-ins (#2831, #2834 golden: E up to +0.084 within 20 check-ins).
* Rows inside a boundary's ambiguous interval belong to neither side.
* Rows written from 2026-10-16 carry ``state_json.eisv_instrument``; filter
  those on the stamp (``era``, or ``id`` for exact configuration) and drop the
  restore transient with ``is_settled``. Time windows are for older rows.

Choosing ``DEFAULT_SETTLED_UPDATES`` is a telemetry threshold, not a finding:
after #2834 the restored ODE closed about 4% of its gap per check-in, so 50
check-ins leaves roughly an eighth of the step.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

REGISTRY_PATH = Path(__file__).resolve().parent.parent / "config" / "eisv_instrument_eras.json"
REGISTRY_SCHEMA = "eisv.instrument_eras.v1"
SPLITTING_KINDS = frozenset({"value", "population"})
ENTRY_KINDS = SPLITTING_KINDS | {"label_only"}
DEFAULT_SETTLED_UPDATES = 50


@dataclass(frozen=True)
class Boundary:
    id: str
    kind: str
    effective_at: Optional[datetime]
    summary: str
    ambiguous_from: Optional[datetime] = None
    ambiguous_until: Optional[datetime] = None

    @property
    def splits(self) -> bool:
        return self.kind in SPLITTING_KINDS

    @property
    def interval(self) -> Optional[tuple[datetime, datetime]]:
        """[earliest, latest] instant at which the change may have taken effect."""
        if self.effective_at is None:
            return None
        return (
            self.ambiguous_from or self.effective_at,
            self.ambiguous_until or self.effective_at,
        )


class EraCrossingError(ValueError):
    """A requested window spans an instrument change."""

    def __init__(self, message: str, crossed: Sequence[Boundary]):
        super().__init__(message)
        self.crossed = tuple(crossed)


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if ts is None:
        return None
    parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"registry time {ts!r} has no timezone")
    return parsed.astimezone(timezone.utc)


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("window bounds must be timezone-aware")
    return ts.astimezone(timezone.utc)


@lru_cache(maxsize=4)
def load_registry(path: Path = REGISTRY_PATH) -> Mapping[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if data.get("schema") != REGISTRY_SCHEMA:
        raise ValueError(f"{path}: schema is {data.get('schema')!r}, expected {REGISTRY_SCHEMA!r}")
    return data


def load_boundaries(path: Path = REGISTRY_PATH) -> tuple[Boundary, ...]:
    return tuple(
        Boundary(
            id=entry["id"],
            kind=entry["kind"],
            effective_at=_parse(entry.get("effective_at")),
            summary=entry.get("summary", ""),
            ambiguous_from=_parse(entry.get("ambiguous_from")),
            ambiguous_until=_parse(entry.get("ambiguous_until")),
        )
        for entry in load_registry(path)["eras"]
    )


def current_era_id(path: Path = REGISTRY_PATH) -> str:
    """The latest splitting entry: the era that new rows belong to."""
    for boundary in reversed(load_boundaries(path)):
        if boundary.splits:
            return boundary.id
    raise ValueError("era registry has no splitting entry")


def boundaries_crossed(
    start: datetime, end: datetime, *, path: Path = REGISTRY_PATH
) -> list[Boundary]:
    """Splitting boundaries that a window ``[start, end)`` does not lie wholly on one side of.

    A boundary with no recorded time (a maths change recorded only by the row
    stamp) counts as crossed when the window reaches past the latest timed
    boundary, since a time window cannot tell where it falls.
    """
    start, end = _utc(start), _utc(end)
    if end <= start:
        raise ValueError("window end must be after its start")
    boundaries = load_boundaries(path)
    latest_timed = max(
        (b.interval[1] for b in boundaries if b.splits and b.interval is not None),
        default=None,
    )
    crossed = []
    for boundary in boundaries:
        if not boundary.splits:
            continue
        if boundary.interval is None:
            if latest_timed is None or end > latest_timed:
                crossed.append(boundary)
            continue
        lo, hi = boundary.interval
        # Entirely before the change, or entirely after it took effect.
        if end <= lo or start >= hi:
            continue
        crossed.append(boundary)
    return crossed


def require_single_era(
    start: datetime,
    end: datetime,
    *,
    allow_cross: Optional[str] = None,
    path: Path = REGISTRY_PATH,
) -> list[Boundary]:
    """Refuse a window that spans an instrument change, unless a reason is given.

    Returns the crossed boundaries (empty when the window is within one era).
    A reason lets the read run; it does not make a pooled number valid, so
    callers should report the crossed ids next to any result.
    """
    crossed = boundaries_crossed(start, end, path=path)
    if crossed and not (allow_cross and allow_cross.strip()):
        listed = ", ".join(
            f"{b.id} ({b.effective_at.isoformat() if b.effective_at else 'stamp only'}: {b.summary})"
            for b in crossed
        )
        raise EraCrossingError(
            f"window {start.isoformat()} .. {end.isoformat()} crosses {len(crossed)} EISV "
            f"instrument change(s): {listed}. Narrow the window to one era, report per era "
            "(src.eisv_eras.era_windows), or pass a written reason to allow the crossing.",
            crossed,
        )
    return crossed


def era_windows(
    start: datetime, end: datetime, *, path: Path = REGISTRY_PATH
) -> list[tuple[str, datetime, datetime]]:
    """Split ``[start, end)`` into per-era pieces, dropping ambiguous intervals.

    Each piece is ``(era_id, lo, hi)``; the era id is the latest splitting
    boundary in effect at ``lo`` (``"pre-E1"`` before the first one). Time
    cannot split stamp-only boundaries, so pieces after them should be split
    again on ``state_json.eisv_instrument.era``.
    """
    start, end = _utc(start), _utc(end)
    timed = [b for b in load_boundaries(path) if b.splits and b.interval is not None]
    pieces = []
    cursor = start
    era = "pre-E1"
    for boundary in timed:
        lo, hi = boundary.interval
        if hi <= start:
            era = boundary.id
            continue
        if lo >= end:
            break
        if lo > cursor:
            pieces.append((era, cursor, lo))
        cursor = max(cursor, hi)
        era = boundary.id
    if cursor < end:
        pieces.append((era, cursor, end))
    return pieces


def require_single_instrument(
    id_counts: Mapping[Optional[str], int], *, allow_cross: Optional[str] = None
) -> list[str]:
    """Refuse rows that carry more than one stamped instrument id.

    ``id_counts`` maps ``state_json.eisv_instrument.id`` (``None`` for an
    unstamped row) to a row count for the selected window. Inside one era the
    id still changes when a runtime threshold, flag or calibration overlay
    does, and a time window cannot see that. Unstamped rows are left to the
    time-based check. Returns the distinct stamped ids, sorted.
    """
    ids = sorted(k for k, n in id_counts.items() if k and n)
    if len(ids) > 1 and not (allow_cross and allow_cross.strip()):
        listed = ", ".join(f"{k} ({id_counts[k]} rows)" for k in ids)
        raise EraCrossingError(
            f"rows carry {len(ids)} different EISV instrument ids: {listed}. Filter on "
            "state_json.eisv_instrument.id, report per id, or pass a written reason "
            "to allow the mix.",
            (),
        )
    return ids


def is_settled(stamp: Optional[Mapping[str, Any]], min_updates: int = DEFAULT_SETTLED_UPDATES) -> bool:
    """Whether a stamped row is past the restore transient of its instrument.

    Rows whose monitor state began under the stamped instrument
    (``carried_from`` null) are settled from the first check-in; rows carrying
    state from before need ``min_updates`` check-ins. Unstamped rows return
    False: their history is unknown.
    """
    if not isinstance(stamp, Mapping):
        return False
    if stamp.get("carried_from") is None:
        return True
    return int(stamp.get("updates_since_change") or 0) >= min_updates

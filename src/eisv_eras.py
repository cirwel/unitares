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
    start: datetime,
    end: datetime,
    *,
    stamp_filtered: bool = False,
    path: Path = REGISTRY_PATH,
) -> list[Boundary]:
    """Splitting boundaries that a window ``[start, end)`` does not lie wholly on one side of.

    A boundary with no recorded time (a maths change recorded only by the row
    stamp) counts as crossed when the window reaches past the latest timed
    boundary, since a time window cannot tell where it falls. A caller that
    keeps only rows stamped with one era passes ``stamp_filtered=True``: the
    stamp, not the time, then separates the untimed boundaries.
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
            if stamp_filtered:
                continue
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
    stamp_filtered: bool = False,
    path: Path = REGISTRY_PATH,
) -> list[Boundary]:
    """Refuse a window that spans an instrument change, unless a reason is given.

    Returns the crossed boundaries (empty when the window is within one era).
    A reason lets the read run; it does not make a pooled number valid, so
    callers should report the crossed ids next to any result.
    """
    crossed = boundaries_crossed(start, end, stamp_filtered=stamp_filtered, path=path)
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


def current_era_window_start(path: Path = REGISTRY_PATH) -> tuple[datetime, Optional[str]]:
    """Where a default "current era" read starts, and the stamp era to filter on.

    For a timed current era: its interval end and ``None``. For an untimed one
    (recorded only by the stamp, typically before its deploy time is written
    down): the latest timed boundary's interval end, and the era id, so the
    caller keeps only rows stamped with it.
    """
    current = current_era_id(path)
    boundaries = load_boundaries(path)
    entry = next(b for b in boundaries if b.id == current)
    if entry.interval is not None:
        return entry.interval[1], None
    timed = [b.interval[1] for b in boundaries if b.splits and b.interval is not None]
    if not timed:
        raise ValueError("era registry has no timed splitting entry")
    return max(timed), current


# ---------------------------------------------------------------------------
# Command-line helpers for window reads
# ---------------------------------------------------------------------------

# Start of a read with no lower bound: before any persisted EISV row.
ALL_TIME_START = datetime(2025, 1, 1, tzinfo=timezone.utc)

CROSS_ERA_HELP = (
    "run even if the window crosses an EISV instrument change "
    "(config/eisv_instrument_eras.json); the reason is printed with the result "
    "and does not make the pooled numbers one sample"
)


def add_cross_era_argument(parser: Any) -> None:
    """Add ``--allow-cross-era REASON`` to an argparse parser."""
    parser.add_argument("--allow-cross-era", metavar="REASON", default=None, help=CROSS_ERA_HELP)


def instrument_id_counts_sql(start_param: str, end_param: str) -> str:
    """Rows per stamped instrument id in a window, for ``require_single_instrument``.

    The placeholders are the caller's driver syntax (``$1::timestamptz`` for
    asyncpg, ``%s`` for psycopg2). The count covers every non-synthetic row in
    the window, a superset of what a filtered read selects, so it can refuse a
    mix the read itself would not have touched; it cannot miss one.
    """
    return (
        "SELECT state_json->'eisv_instrument'->>'id' AS instrument_id, count(*) AS n "
        "FROM core.agent_state "
        f"WHERE recorded_at >= {start_param} AND recorded_at < {end_param} "
        "AND synthetic = false GROUP BY 1"
    )


def check_window(
    start: datetime,
    end: datetime,
    *,
    allow_cross: Optional[str] = None,
    id_counts: Optional[Mapping[Optional[str], int]] = None,
    stamp_filtered: bool = False,
    out: Any = None,
    path: Path = REGISTRY_PATH,
) -> Optional[list[str]]:
    """Era and instrument-id check for a script, as printable provenance lines.

    Returns the Markdown lines a report should print next to its result, or
    ``None`` after printing the refusal to ``out`` (stderr by default); the
    caller then exits 2. ``id_counts`` comes from ``instrument_id_counts_sql``;
    without it only the time check runs and the lines say so.
    """
    import sys

    out = out if out is not None else sys.stderr
    try:
        crossed = require_single_era(
            start, end, allow_cross=allow_cross, stamp_filtered=stamp_filtered, path=path
        )
        stamped = (
            require_single_instrument(id_counts, allow_cross=allow_cross)
            if id_counts is not None
            else None
        )
    except EraCrossingError as exc:
        print(f"refused: {exc}", file=out)
        return None
    lines = [f"EISV instrument window: {start.isoformat()} .. {end.isoformat()}."]
    if crossed:
        lines.append(
            "**Pooled across EISV instrument changes** "
            + ", ".join(b.id for b in crossed)
            + f" (allowed: {allow_cross.strip()}). Rows on either side come from different instruments."
        )
    if stamped is None:
        lines.append("Stamped instrument ids were not checked by this read.")
    else:
        unstamped = int(id_counts.get(None, 0))
        lines.append(
            "Instrument ids: "
            + (", ".join(f"`{k}` ({id_counts[k]} rows)" for k in stamped) or "none stamped")
            + f"; unstamped rows: {unstamped}."
        )
        if len(stamped) > 1:
            lines.append(f"**Pooled across stamped instrument ids** (allowed: {allow_cross.strip()}).")
    return lines


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

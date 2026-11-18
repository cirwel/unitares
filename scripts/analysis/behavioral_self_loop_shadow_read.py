#!/usr/bin/env python3
"""Read-only single-era summary of the behavioral self-loop shadow ablations.

Reads persisted ``eisv_telemetry.shadow_ablations.candidates`` entries for:
- ``decision_self_loop_neutralized`` (#1777)
- ``regime_self_loop_neutralized`` (#2759)

Reads ``core.agent_state`` within ONE EISV instrument era (never pooling across
eras, and never querying ``audit.outcome_events``), splits rows by
``sensor_eisv_source`` (``behavioral``, ``physical``, ``unrecorded``), and
reports availability, ``n``, distinct identities, ``p10`` / ``p50`` / ``p90``
of ``candidate_minus_deployed`` deltas, and per-identity medians for identities
with at least ``--min-identity-rows`` eligible rows (default 10).

Measurement-only scope: both shadows compute one-step observation deltas and
exclude recursive history replay, behavioral EMA replay, V/regime
counterfactuals, policy effects, and future outcomes. Never interpret any delta
as a closed-loop gain.

Usage:
  python3 scripts/analysis/behavioral_self_loop_shadow_read.py --era RR1
  python3 scripts/analysis/behavioral_self_loop_shadow_read.py --era SN1 --json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.analysis import eisv_era_sql  # noqa: E402
from src import eisv_eras  # noqa: E402 — standalone module, no server imports

SCHEMA = "eisv.behavioral_self_loop_shadow_read.v1"
DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)
DEFAULT_MIN_IDENTITY_ROWS = 10
SENSOR_CLASS_ORDER = ("behavioral", "physical", "unrecorded")


class EraResolutionError(ValueError):
    """Requested era cannot be resolved into a valid single-era window."""


@dataclass(frozen=True)
class EraWindow:
    era: str
    start: datetime
    end: datetime
    stamp_era: Optional[str]


@dataclass(frozen=True)
class ShadowRow:
    identity_id: str
    label: Optional[str]
    sensor_class: str
    decision_shadow: Optional[Mapping[str, Any]]
    regime_shadow: Optional[Mapping[str, Any]]


def _percentile(sorted_values: Sequence[float], q: float) -> Optional[float]:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    frac = pos - lo
    return float(sorted_values[lo]) * (1.0 - frac) + float(sorted_values[hi]) * frac


def summarize_distribution(values: Sequence[float]) -> dict[str, Any]:
    """Return n, p10, p50, p90, min, max over finite floats (never bare means)."""
    finite = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not finite:
        return {"n": 0, "p10": None, "p50": None, "p90": None, "min": None, "max": None}
    return {
        "n": len(finite),
        "p10": _percentile(finite, 0.10),
        "p50": _percentile(finite, 0.50),
        "p90": _percentile(finite, 0.90),
        "min": finite[0],
        "max": finite[-1],
    }


def _code_era_for(boundary_id: str, boundaries: Sequence[eisv_eras.Boundary]) -> str:
    """Return the code-era stamp prefix active at ``boundary_id``."""
    idx = next(i for i, b in enumerate(boundaries) if b.id == boundary_id)
    for b in reversed(boundaries[: idx + 1]):
        if b.splits and b.config_hash is None:
            return b.id
    return boundary_id


def resolve_era_window(
    era_id: str,
    *,
    as_of: Optional[datetime] = None,
    path: Path = eisv_eras.REGISTRY_PATH,
) -> EraWindow:
    """Resolve one non-crossing window for ``era_id`` from ``eisv_instrument_eras.json``.

    Timed eras start at ``boundary.interval[1]`` and end before the earliest
    later timed splitting boundary (or ``as_of``). When untimed splitting
    boundaries follow the latest timed era (for instance ``HU1``/``SD1``/``SH1``
    after ``SN1``), ``stamp_era`` is set so callers filter on the row stamp
    ``state_json->'eisv_instrument'->>'era'`` and never pool across eras.
    """
    now = (as_of or datetime.now(timezone.utc)).astimezone(timezone.utc)
    boundaries = eisv_eras.load_boundaries(path)
    target = next((b for b in boundaries if b.id == era_id), None)
    if target is None:
        raise EraResolutionError(f"era {era_id!r} is not in {path}")
    if not target.splits:
        raise EraResolutionError(
            f"era {era_id!r} has kind={target.kind!r} and does not define a separate instrument era"
        )

    untimed_splitting = any(b.splits and b.interval is None for b in boundaries)
    if target.interval is not None:
        start = target.interval[1]
        later_timed = [
            b.interval[0]
            for b in boundaries
            if b.splits and b.interval is not None and b.interval[0] > start
        ]
        end = min([now, *later_timed])
        if end <= start:
            raise EraResolutionError(
                f"era {era_id!r} window is empty ({start.isoformat()} .. {end.isoformat()})"
            )
        stamp_era = _code_era_for(target.id, boundaries) if (not later_timed and untimed_splitting) else None
        return EraWindow(era=target.id, start=start, end=end, stamp_era=stamp_era)

    timed_ends = [b.interval[1] for b in boundaries if b.splits and b.interval is not None]
    if not timed_ends:
        raise EraResolutionError(f"era {era_id!r} has no preceding timed splitting boundary")
    start = max(timed_ends)
    if now <= start:
        raise EraResolutionError(
            f"era {era_id!r} window is empty ({start.isoformat()} .. {now.isoformat()})"
        )
    return EraWindow(era=target.id, start=start, end=now, stamp_era=target.id)


def _float_or_none(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _summarize_decision(
    rows: Sequence[ShadowRow],
    *,
    min_identity_rows: int,
) -> dict[str, Any]:
    with_shadow = [r for r in rows if isinstance(r.decision_shadow, Mapping)]
    eligible_rows = [r for r in with_shadow if r.decision_shadow.get("eligible") is True]
    reasons: Counter[str] = Counter(
        str(r.decision_shadow.get("eligibility_reason") or "unknown")
        for r in with_shadow
        if r.decision_shadow.get("eligible") is not True
    )

    deltas_e: list[float] = []
    deltas_i: list[float] = []
    by_identity: dict[str, dict[str, Any]] = {}

    for row in eligible_rows:
        delta = row.decision_shadow.get("candidate_minus_deployed")
        if not isinstance(delta, Mapping):
            continue
        de = _float_or_none(delta.get("E"))
        di = _float_or_none(delta.get("I"))
        if de is not None:
            deltas_e.append(de)
        if di is not None:
            deltas_i.append(di)
        entry = by_identity.setdefault(
            row.identity_id,
            {"identity_id": row.identity_id, "label": row.label, "de": [], "di": []},
        )
        if de is not None:
            entry["de"].append(de)
        if di is not None:
            entry["di"].append(di)

    per_identity = []
    for entry in by_identity.values():
        n_id = max(len(entry["de"]), len(entry["di"]))
        if n_id < min_identity_rows:
            continue
        e_stats = summarize_distribution(entry["de"])
        i_stats = summarize_distribution(entry["di"])
        per_identity.append(
            {
                "identity_id": entry["identity_id"],
                "label": entry["label"],
                "n": n_id,
                "delta_E_p50": e_stats["p50"],
                "delta_E_p90": e_stats["p90"],
                "delta_E_min": e_stats["min"],
                "delta_E_max": e_stats["max"],
                "delta_I_p50": i_stats["p50"],
                "delta_I_p90": i_stats["p90"],
            }
        )
    per_identity.sort(key=lambda item: (-item["n"], item["identity_id"]))

    return {
        "rows_with_shadow": len(with_shadow),
        "rows_eligible": len(eligible_rows),
        "identities_eligible": len({r.identity_id for r in eligible_rows}),
        "ineligible_reasons": dict(sorted(reasons.items())),
        "delta_E": summarize_distribution(deltas_e),
        "delta_I": summarize_distribution(deltas_i),
        "per_identity_medians": per_identity,
    }


def _summarize_regime(
    rows: Sequence[ShadowRow],
    *,
    min_identity_rows: int,
) -> dict[str, Any]:
    with_shadow = [r for r in rows if isinstance(r.regime_shadow, Mapping)]
    eligible_rows = [r for r in with_shadow if r.regime_shadow.get("eligible") is True]
    reasons: Counter[str] = Counter(
        str(r.regime_shadow.get("eligibility_reason") or "unknown")
        for r in with_shadow
        if r.regime_shadow.get("eligible") is not True
    )

    deltas_s: list[float] = []
    instabilities: list[float] = []
    by_identity: dict[str, dict[str, Any]] = {}

    for row in eligible_rows:
        delta = row.regime_shadow.get("candidate_minus_deployed")
        ds = _float_or_none(delta.get("S")) if isinstance(delta, Mapping) else None
        inst = _float_or_none(row.regime_shadow.get("deployed_regime_instability"))
        if ds is not None:
            deltas_s.append(ds)
        if inst is not None:
            instabilities.append(inst)
        entry = by_identity.setdefault(
            row.identity_id,
            {"identity_id": row.identity_id, "label": row.label, "ds": [], "inst": []},
        )
        if ds is not None:
            entry["ds"].append(ds)
        if inst is not None:
            entry["inst"].append(inst)

    per_identity = []
    for entry in by_identity.values():
        n_id = len(entry["ds"])
        if n_id < min_identity_rows:
            continue
        s_stats = summarize_distribution(entry["ds"])
        inst_stats = summarize_distribution(entry["inst"])
        per_identity.append(
            {
                "identity_id": entry["identity_id"],
                "label": entry["label"],
                "n": n_id,
                "delta_S_p10": s_stats["p10"],
                "delta_S_p50": s_stats["p50"],
                "delta_S_p90": s_stats["p90"],
                "delta_S_min": s_stats["min"],
                "delta_S_max": s_stats["max"],
                "deployed_regime_instability_p50": inst_stats["p50"],
                "deployed_regime_instability_p90": inst_stats["p90"],
            }
        )
    per_identity.sort(key=lambda item: (-item["n"], item["identity_id"]))

    return {
        "rows_with_shadow": len(with_shadow),
        "rows_eligible": len(eligible_rows),
        "identities_eligible": len({r.identity_id for r in eligible_rows}),
        "ineligible_reasons": dict(sorted(reasons.items())),
        "delta_S": summarize_distribution(deltas_s),
        "deployed_regime_instability": summarize_distribution(instabilities),
        "per_identity_medians": per_identity,
    }


def summarize_shadow_rows(
    rows: Sequence[ShadowRow],
    *,
    min_identity_rows: int = DEFAULT_MIN_IDENTITY_ROWS,
) -> dict[str, Any]:
    """Summarize both self-loop shadows split strictly by ``sensor_eisv_source``."""
    grouped: dict[str, list[ShadowRow]] = defaultdict(list)
    for row in rows:
        grouped[row.sensor_class or "unrecorded"].append(row)

    ordered_classes = [c for c in SENSOR_CLASS_ORDER if c in grouped] + sorted(
        c for c in grouped if c not in SENSOR_CLASS_ORDER
    )

    by_sensor_class: dict[str, Any] = {}
    for sensor_class in ordered_classes:
        class_rows = grouped[sensor_class]
        by_sensor_class[sensor_class] = {
            "rows_total": len(class_rows),
            "identities_total": len({r.identity_id for r in class_rows}),
            "decision_self_loop_neutralized": _summarize_decision(
                class_rows, min_identity_rows=min_identity_rows
            ),
            "regime_self_loop_neutralized": _summarize_regime(
                class_rows, min_identity_rows=min_identity_rows
            ),
        }

    return {
        "total_rows": len(rows),
        "total_identities": len({r.identity_id for r in rows}),
        "sensor_class_counts": {c: len(grouped[c]) for c in ordered_classes},
        "by_sensor_class": by_sensor_class,
    }


def _fmt_num(val: Optional[float], digits: int = 4) -> str:
    if val is None:
        return "-"
    return f"{val:+.{digits}f}" if digits == 4 else f"{val:.{digits}f}"


def _fmt_plain(val: Optional[float], digits: int = 4) -> str:
    if val is None:
        return "-"
    return f"{val:.{digits}f}"


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# Behavioral self-loop shadow read — era `{report['era']}`",
        "",
        "One-step observation-delta shadows only (`not_modeled`: recursive history replay, "
        "behavioral EMA replay, V/regime counterfactuals, policy effects, future outcomes). "
        "Does not query `audit.outcome_events` and never measures closed-loop gain.",
        "",
    ]
    for line in report.get("era_lines") or ():
        lines.append(f"- {line}")
    if report.get("stamp_era"):
        lines.append(f"- Row stamp filter: `state_json->'eisv_instrument'->>'era' = '{report['stamp_era']}'`.")
    lines.append(
        f"- Total non-synthetic rows: {report['total_rows']} across {report['total_identities']} identities; "
        f"split by `sensor_eisv_source`: {report['sensor_class_counts']}."
    )
    lines.append("")

    for sensor_class, section in report["by_sensor_class"].items():
        lines.append(
            f"## Sensor class: `{sensor_class}` "
            f"(n={section['rows_total']}, identities={section['identities_total']})"
        )
        lines.append("")

        dec = section["decision_self_loop_neutralized"]
        lines.append(
            f"- **`decision_self_loop_neutralized` (#1777):** "
            f"present={dec['rows_with_shadow']}/{section['rows_total']}, "
            f"eligible={dec['rows_eligible']} (identities={dec['identities_eligible']}), "
            f"ineligible={dec['ineligible_reasons'] or '{}'}"
        )
        de = dec["delta_E"]
        di = dec["delta_I"]
        if de["n"] > 0:
            lines.append(
                f"  - `candidate_minus_deployed.E`: n={de['n']}, "
                f"p10={_fmt_num(de['p10'])}, p50={_fmt_num(de['p50'])}, p90={_fmt_num(de['p90'])}, "
                f"min={_fmt_num(de['min'])}, max={_fmt_num(de['max'])}"
            )
            lines.append(
                f"  - `candidate_minus_deployed.I`: n={di['n']}, "
                f"p50={_fmt_num(di['p50'])}, p90={_fmt_num(di['p90'])}"
            )
        if dec["per_identity_medians"]:
            lines.append("")
            lines.append(
                f"| Identity (n>={report['min_identity_rows']}) | Label | n | dE p50 | dE p90 | dE min | dE max | dI p50 |"
            )
            lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
            for item in dec["per_identity_medians"]:
                lines.append(
                    f"| `{item['identity_id']}` | {item['label'] or '-'} | {item['n']} | "
                    f"{_fmt_num(item['delta_E_p50'])} | {_fmt_num(item['delta_E_p90'])} | "
                    f"{_fmt_num(item['delta_E_min'])} | {_fmt_num(item['delta_E_max'])} | "
                    f"{_fmt_num(item['delta_I_p50'])} |"
                )
        lines.append("")

        reg = section["regime_self_loop_neutralized"]
        lines.append(
            f"- **`regime_self_loop_neutralized` (#2759):** "
            f"present={reg['rows_with_shadow']}/{section['rows_total']}, "
            f"eligible={reg['rows_eligible']} (identities={reg['identities_eligible']}), "
            f"ineligible={reg['ineligible_reasons'] or '{}'}"
        )
        ds = reg["delta_S"]
        inst = reg["deployed_regime_instability"]
        if ds["n"] > 0:
            lines.append(
                f"  - `candidate_minus_deployed.S`: n={ds['n']}, "
                f"p10={_fmt_num(ds['p10'])}, p50={_fmt_num(ds['p50'])}, p90={_fmt_num(ds['p90'])}, "
                f"min={_fmt_num(ds['min'])}, max={_fmt_num(ds['max'])}"
            )
            if inst["n"] > 0:
                lines.append(
                    f"  - `deployed_regime_instability`: n={inst['n']}, "
                    f"p50={_fmt_plain(inst['p50'])}, p90={_fmt_plain(inst['p90'])}, "
                    f"max={_fmt_plain(inst['max'])}"
                )
        if reg["per_identity_medians"]:
            lines.append("")
            lines.append(
                f"| Identity (n>={report['min_identity_rows']}) | Label | n | dS p10 | dS p50 | dS p90 | dS min | dS max | inst p50 | inst p90 |"
            )
            lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
            for item in reg["per_identity_medians"]:
                lines.append(
                    f"| `{item['identity_id']}` | {item['label'] or '-'} | {item['n']} | "
                    f"{_fmt_num(item['delta_S_p10'])} | {_fmt_num(item['delta_S_p50'])} | "
                    f"{_fmt_num(item['delta_S_p90'])} | {_fmt_num(item['delta_S_min'])} | "
                    f"{_fmt_num(item['delta_S_max'])} | "
                    f"{_fmt_plain(item['deployed_regime_instability_p50'])} | "
                    f"{_fmt_plain(item['deployed_regime_instability_p90'])} |"
                )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


FETCH_SQL = """
SELECT
    s.identity_id::text,
    i.metadata->>'label' AS label,
    COALESCE(NULLIF(s.state_json->>'sensor_eisv_source', ''), 'unrecorded') AS sensor_class,
    s.state_json #> '{eisv_telemetry,shadow_ablations,candidates,decision_self_loop_neutralized,behavioral_sensor}' AS decision_shadow,
    s.state_json #> '{eisv_telemetry,shadow_ablations,candidates,regime_self_loop_neutralized,behavioral_sensor}' AS regime_shadow
FROM core.agent_state s
LEFT JOIN core.identities i USING (identity_id)
WHERE s.synthetic IS NOT TRUE
  AND s.recorded_at >= %(start)s
  AND s.recorded_at < %(end)s
  AND (%(stamp_era)s::text IS NULL OR s.state_json->'eisv_instrument'->>'era' = %(stamp_era)s::text)
ORDER BY s.recorded_at
"""

STAMP_FILTERED_ID_COUNTS_SQL = """
SELECT state_json->'eisv_instrument'->>'id' AS instrument_id, count(*) AS n
FROM core.agent_state
WHERE recorded_at >= %s AND recorded_at < %s
  AND synthetic IS NOT TRUE
  AND state_json->'eisv_instrument'->>'era' = %s
GROUP BY 1
"""


def _decode_json(value: Any) -> Optional[Mapping[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, Mapping) else None


def row_from_db(record: Sequence[Any]) -> ShadowRow:
    return ShadowRow(
        identity_id=str(record[0]),
        label=record[1],
        sensor_class=record[2] or "unrecorded",
        decision_shadow=_decode_json(record[3]),
        regime_shadow=_decode_json(record[4]),
    )


def fetch_shadow_rows(conn: Any, window: EraWindow) -> tuple[dict[Optional[str], int], list[ShadowRow]]:
    with conn.cursor() as cur:
        if window.stamp_era is None:
            cur.execute(
                eisv_era_sql.instrument_id_counts_sql("%s", "%s"),
                (window.start, window.end),
            )
        else:
            cur.execute(
                STAMP_FILTERED_ID_COUNTS_SQL,
                (window.start, window.end, window.stamp_era),
            )
        id_counts = {r[0]: int(r[1]) for r in cur.fetchall()}
        cur.execute(
            FETCH_SQL,
            {"start": window.start, "end": window.end, "stamp_era": window.stamp_era},
        )
        rows = [row_from_db(r) for r in cur.fetchall()]
    return id_counts, rows


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--era",
        default=None,
        help="Single EISV instrument era id from config/eisv_instrument_eras.json (default: current era).",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument(
        "--min-identity-rows",
        type=int,
        default=DEFAULT_MIN_IDENTITY_ROWS,
        help="Minimum eligible rows per identity for the per-identity table (default: 10).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    return parser.parse_args(argv)


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    now: Optional[datetime] = None,
    connect: Any = None,
    registry_path: Path = eisv_eras.REGISTRY_PATH,
) -> int:
    args = parse_args(argv)
    era_id = args.era or eisv_eras.current_era_id(registry_path)

    try:
        window = resolve_era_window(era_id, as_of=now, path=registry_path)
    except EraResolutionError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    stamp_filtered = window.stamp_era is not None
    if eisv_eras.check_window(
        window.start, window.end, stamp_filtered=stamp_filtered, path=registry_path
    ) is None:
        return 2

    if connect is None:
        import psycopg2

        connect = psycopg2.connect

    conn = connect(args.db_url)
    conn.set_session(isolation_level="REPEATABLE READ", readonly=True)
    try:
        id_counts, rows = fetch_shadow_rows(conn, window)
    finally:
        conn.close()

    era_lines = eisv_eras.check_window(
        window.start,
        window.end,
        stamp_filtered=stamp_filtered,
        id_counts=id_counts,
        path=registry_path,
    )
    if era_lines is None:
        return 2

    summary = summarize_shadow_rows(rows, min_identity_rows=args.min_identity_rows)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "era": window.era,
        "window": {"start": window.start.isoformat(), "end": window.end.isoformat()},
        "stamp_era": window.stamp_era,
        "era_lines": era_lines,
        "min_identity_rows": args.min_identity_rows,
        **summary,
    }
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else render_markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())

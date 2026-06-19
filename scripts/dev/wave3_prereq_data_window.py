#!/usr/bin/env python3
"""Wave 3 §14 prereq data-window checker.

The Wave 3 RFC gates PR #8b on at least 14 days of
``measurement.lease_plane.request`` rows in ``audit.coordination_measurements``.
This script makes that gate mechanical and prints the evidence an operator needs
to decide whether the window is merely old enough or actually meaningful.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

import asyncpg


DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/governance"
DEFAULT_MEASUREMENT_TYPE = "measurement.lease_plane.request"
DEFAULT_MIN_DAYS = 14.0
DEFAULT_MIN_DAYS_WITH_ROWS = 14
DEFAULT_MAX_LAST_ROW_AGE_HOURS = 24.0

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_WAIT = 2


@dataclass(frozen=True)
class GateSummary:
    measurement_type: str
    row_count: int
    first_row: str | None
    last_row: str | None
    elapsed_days: float
    days_with_rows: int
    last_row_age_hours: float | None
    p50_ms: int | None
    p95_ms: int | None
    p99_ms: int | None
    max_samples_dropped_total: int


@dataclass(frozen=True)
class GateDecision:
    status: str
    exit_code: int
    reasons: tuple[str, ...]


def evaluate_gate(
    summary: GateSummary,
    *,
    min_days: float,
    min_days_with_rows: int,
    max_last_row_age_hours: float,
) -> GateDecision:
    """Classify the data window without touching the DB.

    ``WAIT`` means the measurement channel is working but the precommitted
    window has not matured. ``FAIL`` means the channel is missing/stale enough
    that waiting alone will not fix the gate.
    """
    if summary.row_count <= 0:
        return GateDecision(
            status="FAIL",
            exit_code=EXIT_FAIL,
            reasons=("no measurement rows found",),
        )

    reasons: list[str] = []
    hard_failures: list[str] = []

    if summary.last_row_age_hours is None:
        hard_failures.append("last row timestamp missing")
    elif summary.last_row_age_hours > max_last_row_age_hours:
        hard_failures.append(
            "last row is stale: "
            f"{summary.last_row_age_hours:.2f}h > {max_last_row_age_hours:.2f}h"
        )

    if summary.elapsed_days < min_days:
        reasons.append(
            f"window age {summary.elapsed_days:.2f}d < required {min_days:.2f}d"
        )

    if summary.days_with_rows < min_days_with_rows:
        reasons.append(
            "days with rows "
            f"{summary.days_with_rows} < required {min_days_with_rows}"
        )

    if hard_failures:
        return GateDecision(
            status="FAIL",
            exit_code=EXIT_FAIL,
            reasons=tuple(hard_failures + reasons),
        )

    if reasons:
        return GateDecision(status="WAIT", exit_code=EXIT_WAIT, reasons=tuple(reasons))

    passed_reasons = (
        f"window age {summary.elapsed_days:.2f}d >= required {min_days:.2f}d",
        f"days with rows {summary.days_with_rows} >= required {min_days_with_rows}",
        (
            "last row age "
            f"{summary.last_row_age_hours:.2f}h <= {max_last_row_age_hours:.2f}h"
        ),
    )
    return GateDecision(status="PASS", exit_code=EXIT_PASS, reasons=passed_reasons)


async def _fetch_all(conn: asyncpg.Connection, query: str, *args: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(query, *args)
    return [dict(row) for row in rows]


async def collect_evidence(
    dsn: str,
    *,
    measurement_type: str,
    since: str | None,
) -> tuple[GateSummary, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            summary_row = await conn.fetchrow(
                """
                WITH base AS (
                    SELECT *
                    FROM audit.coordination_measurements
                    WHERE measurement_type = $1
                      AND ($2::timestamptz IS NULL OR recorded_at >= $2::timestamptz)
                )
                SELECT
                    count(*)::bigint AS row_count,
                    min(recorded_at)::text AS first_row,
                    max(recorded_at)::text AS last_row,
                    coalesce(
                        EXTRACT(EPOCH FROM (now() - min(recorded_at))) / 86400.0,
                        0
                    )::float8 AS elapsed_days,
                    count(DISTINCT recorded_at::date)::int AS days_with_rows,
                    CASE
                        WHEN max(recorded_at) IS NULL THEN NULL
                        ELSE EXTRACT(EPOCH FROM (now() - max(recorded_at))) / 3600.0
                    END::float8 AS last_row_age_hours,
                    round(percentile_cont(0.50) WITHIN GROUP (ORDER BY elapsed_ms))::int AS p50_ms,
                    round(percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms))::int AS p95_ms,
                    round(percentile_cont(0.99) WITHIN GROUP (ORDER BY elapsed_ms))::int AS p99_ms,
                    coalesce(
                        max(NULLIF(meta->>'samples_dropped_total', '')::bigint),
                        0
                    )::bigint AS max_samples_dropped_total
                FROM base
                """,
                measurement_type,
                since,
            )
            if summary_row is None:
                raise RuntimeError("summary query returned no row")

            summary = GateSummary(
                measurement_type=measurement_type,
                row_count=int(summary_row["row_count"]),
                first_row=summary_row["first_row"],
                last_row=summary_row["last_row"],
                elapsed_days=float(summary_row["elapsed_days"]),
                days_with_rows=int(summary_row["days_with_rows"]),
                last_row_age_hours=(
                    None
                    if summary_row["last_row_age_hours"] is None
                    else float(summary_row["last_row_age_hours"])
                ),
                p50_ms=summary_row["p50_ms"],
                p95_ms=summary_row["p95_ms"],
                p99_ms=summary_row["p99_ms"],
                max_samples_dropped_total=int(summary_row["max_samples_dropped_total"]),
            )

            endpoint_rows = await _fetch_all(
                conn,
                """
                SELECT
                    endpoint,
                    count(*)::bigint AS rows,
                    min(recorded_at)::text AS first_row,
                    max(recorded_at)::text AS last_row,
                    round(percentile_cont(0.50) WITHIN GROUP (ORDER BY elapsed_ms))::int AS p50_ms,
                    round(percentile_cont(0.95) WITHIN GROUP (ORDER BY elapsed_ms))::int AS p95_ms,
                    round(percentile_cont(0.99) WITHIN GROUP (ORDER BY elapsed_ms))::int AS p99_ms
                FROM audit.coordination_measurements
                WHERE measurement_type = $1
                  AND ($2::timestamptz IS NULL OR recorded_at >= $2::timestamptz)
                GROUP BY endpoint
                ORDER BY rows DESC, endpoint
                """,
                measurement_type,
                since,
            )
            status_rows = await _fetch_all(
                conn,
                """
                SELECT status, count(*)::bigint AS rows
                FROM audit.coordination_measurements
                WHERE measurement_type = $1
                  AND ($2::timestamptz IS NULL OR recorded_at >= $2::timestamptz)
                GROUP BY status
                ORDER BY rows DESC, status
                """,
                measurement_type,
                since,
            )
            daily_rows = await _fetch_all(
                conn,
                """
                SELECT
                    date_trunc('day', recorded_at)::date::text AS day,
                    count(*)::bigint AS rows,
                    round(percentile_cont(0.50) WITHIN GROUP (ORDER BY elapsed_ms))::int AS p50_ms,
                    round(percentile_cont(0.99) WITHIN GROUP (ORDER BY elapsed_ms))::int AS p99_ms,
                    coalesce(
                        max(NULLIF(meta->>'samples_dropped_total', '')::bigint),
                        0
                    )::bigint AS max_samples_dropped_total
                FROM audit.coordination_measurements
                WHERE measurement_type = $1
                  AND ($2::timestamptz IS NULL OR recorded_at >= $2::timestamptz)
                GROUP BY 1
                ORDER BY 1
                """,
                measurement_type,
                since,
            )
            related_rows = await _fetch_all(
                conn,
                """
                SELECT measurement_type, endpoint, status, count(*)::bigint AS rows
                FROM audit.coordination_measurements
                WHERE measurement_type IN (
                    'measurement.governance_mcp.request',
                    'measurement.governance_mcp.503_emission',
                    'measurement.beam_python_boundary.request',
                    'measurement.wave_3a.request'
                )
                  AND ($1::timestamptz IS NULL OR recorded_at >= $1::timestamptz)
                GROUP BY measurement_type, endpoint, status
                ORDER BY measurement_type, rows DESC, endpoint, status
                """,
                since,
            )
            return summary, endpoint_rows, status_rows, daily_rows, related_rows
    finally:
        await pool.close()


def _print_table(title: str, rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> None:
    print()
    print(f"--- {title} ---")
    if not rows:
        print("(none)")
        return
    widths = {
        column: max(len(column), *(len(str(row.get(column, ""))) for row in rows))
        for column in columns
    }
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    print(header)
    print("  ".join("-" * widths[column] for column in columns))
    for row in rows:
        print("  ".join(str(row.get(column, "")).ljust(widths[column]) for column in columns))


def print_text_report(
    *,
    summary: GateSummary,
    decision: GateDecision,
    endpoint_rows: Sequence[dict[str, Any]],
    status_rows: Sequence[dict[str, Any]],
    daily_rows: Sequence[dict[str, Any]],
    related_rows: Sequence[dict[str, Any]],
    min_days: float,
    min_days_with_rows: int,
    max_last_row_age_hours: float,
) -> None:
    print("=== Wave 3 §14 data-window preflight ===")
    print(f"measurement_type: {summary.measurement_type}")
    print(f"required_window_days: {min_days:g}")
    print(f"required_days_with_rows: {min_days_with_rows}")
    print(f"max_last_row_age_hours: {max_last_row_age_hours:g}")
    print()
    print(
        "summary: "
        f"rows={summary.row_count} "
        f"first_row={summary.first_row or '-'} "
        f"last_row={summary.last_row or '-'} "
        f"elapsed_days={summary.elapsed_days:.2f} "
        f"days_with_rows={summary.days_with_rows} "
        f"last_row_age_hours="
        f"{'-' if summary.last_row_age_hours is None else f'{summary.last_row_age_hours:.2f}'} "
        f"p50_ms={summary.p50_ms if summary.p50_ms is not None else '-'} "
        f"p95_ms={summary.p95_ms if summary.p95_ms is not None else '-'} "
        f"p99_ms={summary.p99_ms if summary.p99_ms is not None else '-'} "
        f"max_samples_dropped_total={summary.max_samples_dropped_total}"
    )
    print(f"gate: {decision.status}")
    for reason in decision.reasons:
        print(f"- {reason}")

    _print_table(
        "endpoint latency",
        endpoint_rows,
        ("endpoint", "rows", "first_row", "last_row", "p50_ms", "p95_ms", "p99_ms"),
    )
    _print_table("status breakdown", status_rows, ("status", "rows"))
    _print_table(
        "daily continuity",
        daily_rows,
        ("day", "rows", "p50_ms", "p99_ms", "max_samples_dropped_total"),
    )
    _print_table(
        "related boundary measurements",
        related_rows,
        ("measurement_type", "endpoint", "status", "rows"),
    )


def _json_default(value: Any) -> Any:
    if isinstance(value, tuple):
        return list(value)
    return str(value)


async def async_main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("GOVERNANCE_DATABASE_URL", DEFAULT_DSN),
        help="Postgres DSN (default: GOVERNANCE_DATABASE_URL or local governance DB)",
    )
    parser.add_argument(
        "--measurement-type",
        default=DEFAULT_MEASUREMENT_TYPE,
        help=f"Measurement type to gate on (default: {DEFAULT_MEASUREMENT_TYPE})",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Optional ISO timestamp lower bound for rows included in the report",
    )
    parser.add_argument("--min-days", type=float, default=DEFAULT_MIN_DAYS)
    parser.add_argument(
        "--min-days-with-rows",
        type=int,
        default=DEFAULT_MIN_DAYS_WITH_ROWS,
    )
    parser.add_argument(
        "--max-last-row-age-hours",
        type=float,
        default=DEFAULT_MAX_LAST_ROW_AGE_HOURS,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of the text report",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        summary, endpoint_rows, status_rows, daily_rows, related_rows = await collect_evidence(
            args.dsn,
            measurement_type=args.measurement_type,
            since=args.since,
        )
    except Exception as exc:  # noqa: BLE001 — CLI should print concise failure
        print(f"[wave3-data-window] ERROR: {exc}", file=sys.stderr)
        return EXIT_FAIL

    decision = evaluate_gate(
        summary,
        min_days=args.min_days,
        min_days_with_rows=args.min_days_with_rows,
        max_last_row_age_hours=args.max_last_row_age_hours,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "summary": asdict(summary),
                    "decision": asdict(decision),
                    "endpoint_rows": endpoint_rows,
                    "status_rows": status_rows,
                    "daily_rows": daily_rows,
                    "related_rows": related_rows,
                },
                default=_json_default,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print_text_report(
            summary=summary,
            decision=decision,
            endpoint_rows=endpoint_rows,
            status_rows=status_rows,
            daily_rows=daily_rows,
            related_rows=related_rows,
            min_days=args.min_days,
            min_days_with_rows=args.min_days_with_rows,
            max_last_row_age_hours=args.max_last_row_age_hours,
        )

    return decision.exit_code


def main() -> int:
    return asyncio.run(async_main())


if __name__ == "__main__":
    sys.exit(main())

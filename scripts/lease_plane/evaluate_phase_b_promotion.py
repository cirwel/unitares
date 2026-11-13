#!/usr/bin/env python3
"""
Mechanical evaluator for Phase B promotion eligibility per RFC §6.1.

Runs the six promotion-gate criteria against live telemetry and prints a
deterministic per-criterion verdict plus overall recommendation. Shares the
canonical criterion builders and constants with Core's server-owned
`src.lease_plane_phase_b` module (`GET /v1/lease-plane/phase-b/{surface_kind}`).

Usage:
    python3 scripts/lease_plane/evaluate_phase_b_promotion.py <surface_kind>
    python3 scripts/lease_plane/evaluate_phase_b_promotion.py dialectic --json
    python3 scripts/lease_plane/evaluate_phase_b_promotion.py file --window-days 14
    python3 scripts/lease_plane/evaluate_phase_b_promotion.py resident --accept-drill-evidence

Exit codes:
    0 - All evaluable criteria PASS for the surface_kind (promotable)
    1 - One or more criteria FAIL or are NOT_YET_EVALUABLE (not promotable)
    2 - Database / connection error (cannot evaluate)
    3 - Unknown surface_kind
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import psycopg2

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.lease_plane_phase_b import (  # noqa: E402
    COLLISION_SYMPTOM_RE,
    CriterionResult,
    DRILL_AUDIT_SESSION_PREFIX,
    EvaluationReport,
    KNOWN_SURFACE_KINDS,
    NON_WRITE_SURFACE_KINDS,
    SYNTHETIC_SESSION_PREFIXES as SYNTHETIC_SESSION_PREFIXES,
    UnknownSurfaceKindError,
    build_criterion_1,
    build_criterion_2,
    build_criterion_2_missing_table,
    build_criterion_3,
    build_criterion_4,
    build_criterion_4_drill_pass,
    build_criterion_4_missing_table,
    build_criterion_5,
    build_criterion_5_na,
    build_criterion_6,
    build_criterion_6_na,
    finalize_report,
    organic_audit_session_sql,
)


def _organic_audit_session_sql() -> str:
    return organic_audit_session_sql(escape_percent=True)


def _conn(db_url: str | None):
    url = db_url or os.environ.get(
        "DB_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/governance"
    )
    return psycopg2.connect(url)


class _DictCursor:
    """Wraps a psycopg2 cursor so fetchone() returns a column-name dict."""

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=None):
        self._cursor.execute(sql, params)

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None or isinstance(row, dict):
            return row
        cols = [d[0] for d in self._cursor.description]
        return dict(zip(cols, row))


def _criterion_1_advisory_window(cur, surface_kind: str, window_days: int) -> CriterionResult:
    cur.execute(
        """
        SELECT min(ts) AS earliest, max(ts) AS latest, count(*) AS n
        FROM lease_plane.lease_plane_events
        WHERE advisory_mode = true AND surface_kind = %s
        """,
        (surface_kind,),
    )
    return build_criterion_1(cur.fetchone(), surface_kind, window_days)


def _criterion_2_uptime(cur, window_days: int) -> CriterionResult:
    cur.execute(
        """
        SELECT exists(
          SELECT 1 FROM information_schema.tables
          WHERE table_schema = 'audit' AND table_name = 'events'
        ) AS has_table
        """
    )
    if not cur.fetchone()["has_table"]:
        return build_criterion_2_missing_table()
    cur.execute(
        """
        SELECT count(*) AS n
        FROM audit.events
        WHERE event_type IN ('lease_plane_down', 'service_down')
          AND ts > now() - %s::interval
        """,
        (f"{window_days} days",),
    )
    return build_criterion_2(cur.fetchone()["n"], window_days)


def _criterion_3_type_a_signal(
    cur, surface_kind: str, window_days: int, *, accept_drill_evidence: bool
) -> CriterionResult:
    organic_sql = _organic_audit_session_sql()
    cur.execute(
        f"""
        SELECT count(DISTINCT surface_id) AS distinct_surfaces,
               count(*) AS total_conflicts,
               count(DISTINCT surface_id) FILTER (
                 WHERE {organic_sql}
               ) AS organic_distinct_surfaces,
               count(*) FILTER (
                 WHERE {organic_sql}
               ) AS organic_total_conflicts,
               count(DISTINCT surface_id) FILTER (
                 WHERE payload->>'audit_session' LIKE %s
               ) AS drill_distinct_surfaces,
               count(*) FILTER (
                 WHERE payload->>'audit_session' LIKE %s
               ) AS drill_total_conflicts
        FROM lease_plane.lease_plane_events
        WHERE event_type = 'conflict_held_by_other'
          AND advisory_mode = true
          AND surface_kind = %s
          AND ts > now() - %s::interval
        """,
        (
            f"{DRILL_AUDIT_SESSION_PREFIX}%",
            f"{DRILL_AUDIT_SESSION_PREFIX}%",
            surface_kind,
            f"{window_days} days",
        ),
    )
    return build_criterion_3(cur.fetchone(), accept_drill_evidence=accept_drill_evidence)


def _criterion_4_incident_linkage(
    cur, surface_kind: str, window_days: int, *, accept_drill_evidence: bool
) -> CriterionResult:
    if accept_drill_evidence:
        cur.execute(
            """
            SELECT count(*) AS drill_conflicts,
                   count(DISTINCT surface_id) AS drill_distinct_surfaces,
                   min(surface_id) AS example_surface_id,
                   min(payload->>'audit_session') AS example_audit_session
            FROM lease_plane.lease_plane_events
            WHERE event_type = 'conflict_held_by_other'
              AND advisory_mode = true
              AND surface_kind = %s
              AND ts > now() - %s::interval
              AND payload->>'audit_session' LIKE %s
            """,
            (surface_kind, f"{window_days} days", f"{DRILL_AUDIT_SESSION_PREFIX}%"),
        )
        drill_row = cur.fetchone()
        if drill_row["drill_conflicts"] >= 1:
            return build_criterion_4_drill_pass(drill_row)

    cur.execute(
        """
        SELECT exists(
          SELECT 1 FROM information_schema.tables
          WHERE table_schema = 'knowledge' AND table_name = 'discoveries'
        ) AS has_table
        """
    )
    if not cur.fetchone()["has_table"]:
        return build_criterion_4_missing_table()

    organic_sql = _organic_audit_session_sql()
    cur.execute(
        f"""
        WITH conflicts AS (
          SELECT
            event_id,
            ts,
            surface_id,
            payload->>'audit_session' AS audit_session
          FROM lease_plane.lease_plane_events
          WHERE event_type = 'conflict_held_by_other'
            AND advisory_mode = true
            AND surface_kind = %s
            AND ts > now() - %s::interval
            AND {organic_sql}
        ), linked AS (
          SELECT DISTINCT
            c.event_id,
            c.surface_id,
            d.id AS discovery_id
          FROM conflicts c
          JOIN knowledge.discoveries d
            ON d.created_at BETWEEN c.ts - interval '1 hour' AND c.ts + interval '1 hour'
           AND d.provenance->>'writer_session_id_at_write' = c.audit_session
          WHERE c.audit_session IS NOT NULL
            AND c.audit_session <> ''
            AND concat_ws(
                  E'\n',
                  d.summary,
                  d.details,
                  array_to_string(d.tags, ' ')
                ) ~* %s
        )
        SELECT
          (SELECT count(*) FROM conflicts
           WHERE audit_session IS NOT NULL AND audit_session <> '') AS conflicts_with_audit_session,
          (SELECT count(DISTINCT event_id) FROM linked) AS linked_conflicts,
          (SELECT count(DISTINCT discovery_id) FROM linked) AS linked_discoveries,
          (SELECT min(discovery_id) FROM linked) AS example_discovery_id,
          (SELECT min(surface_id) FROM linked) AS example_surface_id
        """,
        (
            surface_kind,
            f"{window_days} days",
            COLLISION_SYMPTOM_RE,
        ),
    )
    return build_criterion_4(cur.fetchone())


def _criterion_5_coverage_ratio(cur, surface_kind: str, window_days: int) -> CriterionResult:
    if surface_kind in NON_WRITE_SURFACE_KINDS:
        return build_criterion_5_na(surface_kind)
    cur.execute(
        """
        WITH writes AS (
          SELECT count(*) AS n FROM audit.tool_usage
           WHERE tool_name LIKE 'write.%%'
             AND payload->>'surface_id' LIKE %s
             AND ts > now() - %s::interval
        ), acquires AS (
          SELECT count(*) AS n FROM audit.tool_usage
           WHERE tool_name = 'lease.acquire'
             AND payload->>'surface_id' LIKE %s
             AND ts > now() - %s::interval
        )
        SELECT writes.n AS writes, acquires.n AS acquires,
               CASE WHEN writes.n = 0 THEN NULL
                    ELSE acquires.n::float / writes.n
               END AS coverage_ratio
        FROM writes, acquires
        """,
        (
            f"{surface_kind}:%",
            f"{window_days} days",
            f"{surface_kind}:%",
            f"{window_days} days",
        ),
    )
    return build_criterion_5(cur.fetchone())


def _criterion_6_adversarial_bypass(cur, surface_kind: str, window_days: int) -> CriterionResult:
    if surface_kind in NON_WRITE_SURFACE_KINDS:
        return build_criterion_6_na(surface_kind)
    cur.execute(
        """
        SELECT exists(
          SELECT 1 FROM information_schema.tables
          WHERE table_schema = 'lease_plane'
            AND table_name IN ('mtime_observations', 'bypass_observations')
        ) AS has_table
        """
    )
    return build_criterion_6(bool(cur.fetchone()["has_table"]))


def evaluate(
    surface_kind: str,
    window_days: int,
    db_url: str | None = None,
    *,
    accept_drill_evidence: bool = False,
) -> EvaluationReport:
    if surface_kind not in KNOWN_SURFACE_KINDS:
        raise UnknownSurfaceKindError(
            f"unknown surface_kind '{surface_kind}'. "
            f"Known: {', '.join(KNOWN_SURFACE_KINDS)}"
        )
    with _conn(db_url) as conn:
        with conn.cursor() as raw_cur:
            cur = _DictCursor(raw_cur)
            criteria = [
                _criterion_1_advisory_window(cur, surface_kind, window_days),
                _criterion_2_uptime(cur, window_days),
                _criterion_3_type_a_signal(
                    cur,
                    surface_kind,
                    window_days,
                    accept_drill_evidence=accept_drill_evidence,
                ),
                _criterion_4_incident_linkage(
                    cur,
                    surface_kind,
                    window_days,
                    accept_drill_evidence=accept_drill_evidence,
                ),
                _criterion_5_coverage_ratio(cur, surface_kind, window_days),
                _criterion_6_adversarial_bypass(cur, surface_kind, window_days),
            ]
    return finalize_report(
        surface_kind=surface_kind,
        window_days=window_days,
        criteria=criteria,
        accept_drill_evidence=accept_drill_evidence,
    )


def _format_text(report: EvaluationReport) -> str:
    lines = [
        f"Phase B promotion evaluation — surface_kind={report.surface_kind}",
        f"  window: {report.window_days} days",
        f"  accept_drill_evidence: {report.accept_drill_evidence}",
        f"  evaluated_at: {report.evaluated_at}",
        "",
    ]
    for c in report.criteria:
        marker = {
            "PASS": "✓",
            "FAIL": "✗",
            "NOT_APPLICABLE": "—",
            "NOT_YET_EVALUABLE": "?",
        }.get(c.status, " ")
        lines.append(f"  {marker} (§6.1.{c.number}) {c.name}: {c.status}")
        lines.append(f"      {c.detail}")
        if c.measured:
            for k, v in c.measured.items():
                lines.append(f"      {k}: {v}")
        lines.append("")
    lines.append(f"VERDICT: {'PROMOTABLE' if report.promotable else 'NOT PROMOTABLE'}")
    return "\n".join(lines)


def _format_json(report: EvaluationReport) -> str:
    return json.dumps(report.to_dict(), indent=2, default=str)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("surface_kind", help=f"one of: {', '.join(KNOWN_SURFACE_KINDS)}")
    parser.add_argument(
        "--window-days",
        type=int,
        default=14,
        help="advisory-mode window in days (default: 14, per §6.1.1)",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--db-url", help="override DB_POSTGRES_URL")
    parser.add_argument(
        "--accept-drill-evidence",
        action="store_true",
        help=(
            "accept explicitly labeled phase-b-drill conflict events as controlled "
            "rehearsal evidence for criteria 3 and 4"
        ),
    )
    args = parser.parse_args(argv)

    try:
        report = evaluate(
            surface_kind=args.surface_kind,
            window_days=args.window_days,
            db_url=args.db_url,
            accept_drill_evidence=args.accept_drill_evidence,
        )
    except UnknownSurfaceKindError as e:
        print(str(e), file=sys.stderr)
        return 3
    except psycopg2.Error as e:
        print(f"db error: {e}", file=sys.stderr)
        return 2
    print(_format_json(report) if args.json else _format_text(report))
    return 0 if report.promotable else 1


if __name__ == "__main__":
    sys.exit(main())

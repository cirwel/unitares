"""Server-owned Phase B promotion-eligibility evaluator (`GET /v1/lease-plane/phase-b/{surface_kind}`).

Evaluates the six RFC §6.1 promotion-gate criteria over Core's PostgreSQL pool
in a read-only transaction so remote monitoring clients never spawn a local
`psycopg2` subprocess or require direct PostgreSQL access. Also shares the
canonical criterion builders and constants with the operator CLI at
`scripts/lease_plane/evaluate_phase_b_promotion.py`.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


class UnknownSurfaceKindError(ValueError):
    """Raised when an unrecognized surface_kind is passed to evaluate."""


# Per §6.2, ordering when multiple surface_kinds are eligible.
KNOWN_SURFACE_KINDS = ("dialectic", "resident", "file", "capture", "td")

# Per §6.1: write-side criteria are N/A for non-write surface_kinds.
NON_WRITE_SURFACE_KINDS = frozenset({"dialectic", "resident"})

# RFC §6.1.4 intentionally requires a concrete collision symptom, not just
# any KG entry in the same session.
COLLISION_SYMPTOM_RE = (
    r"surface[-_ ]?collision|slot[-_ ]?collision|lease[-_ ]?conflict|"
    r"conflict_held_by_other|overwrite|overwrote|lost[-_ ]?work|"
    r"lost[-_ ]?update|clobber|stomp|merge[-_ ]?conflict|parallel[-_ ]?session"
)
DRILL_AUDIT_SESSION_PREFIX = "phase-b-drill:"

# Session-label prefixes that mark synthetic conflict traffic.
SYNTHETIC_SESSION_PREFIXES = ("codex-http-race", "codex-live-probe")

STATEMENT_TIMEOUT_SQL = "SET LOCAL statement_timeout = '15s'"


def organic_audit_session_sql(*, escape_percent: bool = False) -> str:
    """SQL predicate on `payload->>'audit_session'` selecting organic conflicts.

    Set `escape_percent=True` when embedding inside a `psycopg2` query that runs
    `%`-placeholder substitution (`%%`), and `False` for `asyncpg` `$N` queries.
    """
    pct = "%%" if escape_percent else "%"
    clauses = [
        "coalesce(payload->>'audit_session', '') <> ''",
        f"payload->>'audit_session' NOT LIKE '{DRILL_AUDIT_SESSION_PREFIX}{pct}'",
    ]
    clauses.extend(
        f"payload->>'audit_session' NOT LIKE '{prefix}{pct}'"
        for prefix in SYNTHETIC_SESSION_PREFIXES
    )
    return " AND ".join(clauses)


@dataclass
class CriterionResult:
    number: int
    name: str
    status: str  # PASS, FAIL, NOT_APPLICABLE, NOT_YET_EVALUABLE
    detail: str
    measured: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationReport:
    surface_kind: str
    window_days: int
    evaluated_at: str
    criteria: list[CriterionResult]
    promotable: bool
    accept_drill_evidence: bool = False

    @property
    def summary(self) -> str:
        return (
            f"surface_kind={self.surface_kind} "
            f"window={self.window_days}d "
            f"promotable={'YES' if self.promotable else 'NO'}"
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["criteria"] = [asdict(c) for c in self.criteria]
        return payload


# ---------------------------------------------------------------------------
# Pure criterion builders (shared by asyncpg server route and psycopg2 CLI)
# ---------------------------------------------------------------------------


def build_criterion_1(row: Mapping[str, Any], surface_kind: str, window_days: int) -> CriterionResult:
    earliest = row["earliest"]
    if earliest is None:
        return CriterionResult(
            number=1,
            name="advisory_window",
            status="FAIL",
            detail=f"no advisory traffic for surface_kind='{surface_kind}'",
            measured={"events": 0},
        )
    now = datetime.now(timezone.utc)
    days_observed = (now - earliest).total_seconds() / 86400.0
    measured = {
        "earliest": earliest.isoformat(),
        "latest": row["latest"].isoformat(),
        "events": int(row["n"]),
        "days_observed": round(days_observed, 2),
        "days_required": window_days,
    }
    if days_observed >= window_days:
        return CriterionResult(
            number=1,
            name="advisory_window",
            status="PASS",
            detail=f"{days_observed:.1f}d ≥ {window_days}d",
            measured=measured,
        )
    return CriterionResult(
        number=1,
        name="advisory_window",
        status="FAIL",
        detail=(
            f"only {days_observed:.1f}d of {window_days}d required "
            f"(eligible {(earliest + timedelta(days=window_days)).date().isoformat()})"
        ),
        measured=measured,
    )


def build_criterion_2_missing_table() -> CriterionResult:
    return CriterionResult(
        number=2,
        name="lease_plane_uptime",
        status="NOT_YET_EVALUABLE",
        detail="audit.events table not present; uptime probe wiring pending",
    )


def build_criterion_2(n_down: int, window_days: int) -> CriterionResult:
    if n_down == 0:
        return CriterionResult(
            number=2,
            name="lease_plane_uptime",
            status="PASS",
            detail=f"0 service-availability incidents in {window_days}d window",
            measured={"down_events": 0},
        )
    return CriterionResult(
        number=2,
        name="lease_plane_uptime",
        status="FAIL",
        detail=f"{n_down} service-availability incidents in {window_days}d window",
        measured={"down_events": int(n_down)},
    )


def build_criterion_3(
    row: Mapping[str, Any], *, accept_drill_evidence: bool
) -> CriterionResult:
    distinct = int(row["distinct_surfaces"])
    total_conflicts = int(row["total_conflicts"])
    organic_distinct = int(row.get("organic_distinct_surfaces", distinct))
    organic_total = int(row.get("organic_total_conflicts", total_conflicts))
    drill_distinct = int(row.get("drill_distinct_surfaces", 0))
    drill_total = int(row.get("drill_total_conflicts", 0))
    measured = {
        "distinct_surfaces": distinct,
        "total_conflicts": total_conflicts,
        "organic_distinct_surfaces": organic_distinct,
        "organic_total_conflicts": organic_total,
        "drill_distinct_surfaces": drill_distinct,
        "drill_total_conflicts": drill_total,
        "accept_drill_evidence": accept_drill_evidence,
    }
    if organic_distinct >= 3:
        return CriterionResult(
            number=3,
            name="type_a_conflict_signal",
            status="PASS",
            detail=f"{organic_distinct} organic distinct surface_id with conflicts ≥ 3",
            measured=measured,
        )
    if accept_drill_evidence and drill_distinct >= 3:
        return CriterionResult(
            number=3,
            name="type_a_conflict_signal",
            status="PASS",
            detail=f"{drill_distinct} controlled-drill distinct surface_id with conflicts ≥ 3",
            measured=measured,
        )
    return CriterionResult(
        number=3,
        name="type_a_conflict_signal",
        status="FAIL",
        detail=f"only {organic_distinct} organic distinct surface_id with conflicts; need ≥ 3",
        measured=measured,
    )


def build_criterion_4_drill_pass(drill_row: Mapping[str, Any]) -> CriterionResult:
    return CriterionResult(
        number=4,
        name="incident_kg_linkage",
        status="PASS",
        detail=(
            f"{int(drill_row['drill_conflicts'])} controlled drill conflict(s) "
            "record blocked-caller audit_session without requiring a real incident"
        ),
        measured={
            "drill_conflicts": int(drill_row["drill_conflicts"]),
            "drill_distinct_surfaces": int(drill_row["drill_distinct_surfaces"]),
            "example_surface_id": drill_row["example_surface_id"],
            "example_audit_session": drill_row["example_audit_session"],
            "accepted_instead_of_real_incident": True,
        },
    )


def build_criterion_4_missing_table() -> CriterionResult:
    return CriterionResult(
        number=4,
        name="incident_kg_linkage",
        status="NOT_YET_EVALUABLE",
        detail="knowledge.discoveries table not present; KG linkage cannot be evaluated",
    )


def build_criterion_4(row: Mapping[str, Any]) -> CriterionResult:
    conflicts_with_session = int(row["conflicts_with_audit_session"])
    linked_conflicts = int(row["linked_conflicts"])
    linked_discoveries = int(row["linked_discoveries"])
    measured = {
        "conflicts_with_audit_session": conflicts_with_session,
        "linked_conflicts": linked_conflicts,
        "linked_discoveries": linked_discoveries,
        "example_discovery_id": row["example_discovery_id"],
        "example_surface_id": row["example_surface_id"],
    }
    if conflicts_with_session == 0:
        return CriterionResult(
            number=4,
            name="incident_kg_linkage",
            status="NOT_YET_EVALUABLE",
            detail="no conflict events carry audit_session in payload; cannot join blocked caller to KG",
            measured=measured,
        )
    if linked_conflicts >= 1:
        return CriterionResult(
            number=4,
            name="incident_kg_linkage",
            status="PASS",
            detail=(
                f"{linked_conflicts} conflict event(s) link to "
                f"{linked_discoveries} KG collision discovery(ies)"
            ),
            measured=measured,
        )
    return CriterionResult(
        number=4,
        name="incident_kg_linkage",
        status="FAIL",
        detail="conflicts have audit_session, but none link to a KG collision symptom within ±1h",
        measured=measured,
    )


def build_criterion_5_na(surface_kind: str) -> CriterionResult:
    return CriterionResult(
        number=5,
        name="coverage_ratio",
        status="NOT_APPLICABLE",
        detail=f"surface_kind='{surface_kind}' is non-write per §6.1 carve-out",
    )


def build_criterion_5(row: Mapping[str, Any]) -> CriterionResult:
    writes = int(row["writes"])
    acquires = int(row["acquires"])
    coverage = float(row["coverage_ratio"]) if row["coverage_ratio"] is not None else None
    measured = {"writes": writes, "acquires": acquires, "coverage_ratio": coverage}
    if writes == 0:
        return CriterionResult(
            number=5,
            name="coverage_ratio",
            status="NOT_YET_EVALUABLE",
            detail=(
                "no write.* audit events with surface_id payload yet — "
                "write-class tool emission instrumentation must land before this criterion is evaluable"
            ),
            measured=measured,
        )
    if coverage is not None and coverage >= 0.95:
        return CriterionResult(
            number=5,
            name="coverage_ratio",
            status="PASS",
            detail=f"coverage_ratio={coverage:.3f} ≥ 0.95",
            measured=measured,
        )
    return CriterionResult(
        number=5,
        name="coverage_ratio",
        status="FAIL",
        detail=f"coverage_ratio={coverage:.3f} < 0.95 — unintegrated callers exist",
        measured=measured,
    )


def build_criterion_6_na(surface_kind: str) -> CriterionResult:
    return CriterionResult(
        number=6,
        name="adversarial_bypass_check",
        status="NOT_APPLICABLE",
        detail=(
            f"surface_kind='{surface_kind}' is non-write; "
            "write-side mtime/bypass cross-check is not applicable"
        ),
    )


def build_criterion_6(has_table: bool) -> CriterionResult:
    if not has_table:
        return CriterionResult(
            number=6,
            name="adversarial_bypass_check",
            status="NOT_YET_EVALUABLE",
            detail=(
                "mtime/bypass observation table not present; "
                "adversarial-bypass cross-check pipeline must land before this criterion is evaluable"
            ),
        )
    return CriterionResult(
        number=6,
        name="adversarial_bypass_check",
        status="NOT_YET_EVALUABLE",
        detail=(
            "observation table exists but reconciliation SQL pending — "
            "fill in canonical mtime/lease-window join when pipeline finalizes"
        ),
    )


def finalize_report(
    *,
    surface_kind: str,
    window_days: int,
    criteria: list[CriterionResult],
    accept_drill_evidence: bool,
    evaluated_at: str | None = None,
) -> EvaluationReport:
    promotable = all(c.status in {"PASS", "NOT_APPLICABLE"} for c in criteria)
    return EvaluationReport(
        surface_kind=surface_kind,
        window_days=window_days,
        evaluated_at=evaluated_at or datetime.now(timezone.utc).isoformat(),
        criteria=criteria,
        promotable=promotable,
        accept_drill_evidence=accept_drill_evidence,
    )


# ---------------------------------------------------------------------------
# Asyncpg server execution (`GET /v1/lease-plane/phase-b/{surface_kind}`)
# ---------------------------------------------------------------------------


async def evaluate_phase_b_from_conn(
    conn: Any,
    surface_kind: str,
    window_days: int = 14,
    *,
    accept_drill_evidence: bool = False,
) -> EvaluationReport:
    """Evaluate all six Phase-B promotion criteria on an asyncpg connection."""
    if surface_kind not in KNOWN_SURFACE_KINDS:
        raise UnknownSurfaceKindError(
            f"unknown surface_kind '{surface_kind}'. "
            f"Known: {', '.join(KNOWN_SURFACE_KINDS)}"
        )

    window_delta = timedelta(days=window_days)
    drill_like = f"{DRILL_AUDIT_SESSION_PREFIX}%"
    organic_sql = organic_audit_session_sql(escape_percent=False)

    async with conn.transaction(isolation="repeatable_read", readonly=True):
        await conn.execute(STATEMENT_TIMEOUT_SQL)
        db_now = await conn.fetchval("SELECT now()")
        evaluated_at = db_now.isoformat() if hasattr(db_now, "isoformat") else None

        # Criterion 1
        r1 = await conn.fetchrow(
            """
            SELECT min(ts) AS earliest, max(ts) AS latest, count(*) AS n
            FROM lease_plane.lease_plane_events
            WHERE advisory_mode = true AND surface_kind = $1
            """,
            surface_kind,
        )
        c1 = build_criterion_1(dict(r1 or {}), surface_kind, window_days)

        # Criterion 2
        has_audit = await conn.fetchval(
            """
            SELECT exists(
              SELECT 1 FROM information_schema.tables
              WHERE table_schema = 'audit' AND table_name = 'events'
            )
            """
        )
        if not has_audit:
            c2 = build_criterion_2_missing_table()
        else:
            from src.db.mixins.audit import count_service_down_events_within

            n_down = await count_service_down_events_within(conn, window_delta)
            c2 = build_criterion_2(n_down, window_days)

        # Criterion 3
        r3 = await conn.fetchrow(
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
                     WHERE payload->>'audit_session' LIKE $1
                   ) AS drill_distinct_surfaces,
                   count(*) FILTER (
                     WHERE payload->>'audit_session' LIKE $1
                   ) AS drill_total_conflicts
            FROM lease_plane.lease_plane_events
            WHERE event_type = 'conflict_held_by_other'
              AND advisory_mode = true
              AND surface_kind = $2
              AND ts > now() - $3::interval
            """,
            drill_like,
            surface_kind,
            window_delta,
        )
        c3 = build_criterion_3(dict(r3 or {}), accept_drill_evidence=accept_drill_evidence)

        # Criterion 4
        c4: CriterionResult | None = None
        if accept_drill_evidence:
            drill_row = await conn.fetchrow(
                """
                SELECT count(*) AS drill_conflicts,
                       count(DISTINCT surface_id) AS drill_distinct_surfaces,
                       min(surface_id) AS example_surface_id,
                       min(payload->>'audit_session') AS example_audit_session
                FROM lease_plane.lease_plane_events
                WHERE event_type = 'conflict_held_by_other'
                  AND advisory_mode = true
                  AND surface_kind = $1
                  AND ts > now() - $2::interval
                  AND payload->>'audit_session' LIKE $3
                """,
                surface_kind,
                window_delta,
                drill_like,
            )
            drill_dict = dict(drill_row or {})
            if int(drill_dict.get("drill_conflicts") or 0) >= 1:
                c4 = build_criterion_4_drill_pass(drill_dict)

        if c4 is None:
            has_kg = await conn.fetchval(
                """
                SELECT exists(
                  SELECT 1 FROM information_schema.tables
                  WHERE table_schema = 'knowledge' AND table_name = 'discoveries'
                )
                """
            )
            if not has_kg:
                c4 = build_criterion_4_missing_table()
            else:
                from src.db.mixins.knowledge_graph import (
                    fetch_symptom_discoveries_for_sessions,
                )

                conflict_rows = await conn.fetch(
                    f"""
                    SELECT
                      event_id,
                      ts,
                      surface_id,
                      payload->>'audit_session' AS audit_session
                    FROM lease_plane.lease_plane_events
                    WHERE event_type = 'conflict_held_by_other'
                      AND advisory_mode = true
                      AND surface_kind = $1
                      AND ts > now() - $2::interval
                      AND {organic_sql}
                      AND payload->>'audit_session' IS NOT NULL
                      AND payload->>'audit_session' <> ''
                    """,
                    surface_kind,
                    window_delta,
                )
                conflicts_with_session = len(conflict_rows)
                if conflicts_with_session == 0:
                    r4_dict = {
                        "conflicts_with_audit_session": 0,
                        "linked_conflicts": 0,
                        "linked_discoveries": 0,
                        "example_discovery_id": None,
                        "example_surface_id": None,
                    }
                else:
                    session_ids = sorted(
                        {str(row["audit_session"]) for row in conflict_rows}
                    )
                    min_ts = min(row["ts"] for row in conflict_rows) - timedelta(hours=1)
                    max_ts = max(row["ts"] for row in conflict_rows) + timedelta(hours=1)
                    disc_rows = await fetch_symptom_discoveries_for_sessions(
                        conn,
                        session_ids=session_ids,
                        min_created_at=min_ts,
                        max_created_at=max_ts,
                        symptom_regex=COLLISION_SYMPTOM_RE,
                    )
                    linked_event_ids: set[str] = set()
                    linked_disc_ids: set[str] = set()
                    linked_surfaces: set[str] = set()
                    for crow in conflict_rows:
                        csess = str(crow["audit_session"])
                        cts = crow["ts"]
                        for drow in disc_rows:
                            if (
                                str(drow["writer_session_id"]) == csess
                                and abs(drow["created_at"] - cts) <= timedelta(hours=1)
                            ):
                                linked_event_ids.add(str(crow["event_id"]))
                                linked_disc_ids.add(str(drow["discovery_id"]))
                                linked_surfaces.add(str(crow["surface_id"]))
                    r4_dict = {
                        "conflicts_with_audit_session": conflicts_with_session,
                        "linked_conflicts": len(linked_event_ids),
                        "linked_discoveries": len(linked_disc_ids),
                        "example_discovery_id": min(linked_disc_ids) if linked_disc_ids else None,
                        "example_surface_id": min(linked_surfaces) if linked_surfaces else None,
                    }
                c4 = build_criterion_4(r4_dict)

        # Criteria 5 & 6
        if surface_kind in NON_WRITE_SURFACE_KINDS:
            c5 = build_criterion_5_na(surface_kind)
            c6 = build_criterion_6_na(surface_kind)
        else:
            surface_prefix = f"{surface_kind}:%"
            r5 = await conn.fetchrow(
                """
                WITH writes AS (
                  SELECT count(*) AS n FROM audit.tool_usage
                   WHERE tool_name LIKE 'write.%'
                     AND payload->>'surface_id' LIKE $1
                     AND ts > now() - $2::interval
                ), acquires AS (
                  SELECT count(*) AS n FROM audit.tool_usage
                   WHERE tool_name = 'lease.acquire'
                     AND payload->>'surface_id' LIKE $1
                     AND ts > now() - $2::interval
                )
                SELECT writes.n AS writes, acquires.n AS acquires,
                       CASE WHEN writes.n = 0 THEN NULL
                            ELSE acquires.n::float / writes.n
                       END AS coverage_ratio
                FROM writes, acquires
                """,
                surface_prefix,
                window_delta,
            )
            c5 = build_criterion_5(dict(r5 or {}))

            has_obs = await conn.fetchval(
                """
                SELECT exists(
                  SELECT 1 FROM information_schema.tables
                  WHERE table_schema = 'lease_plane'
                    AND table_name IN ('mtime_observations', 'bypass_observations')
                )
                """
            )
            c6 = build_criterion_6(bool(has_obs))

    return finalize_report(
        surface_kind=surface_kind,
        window_days=window_days,
        criteria=[c1, c2, c3, c4, c5, c6],
        accept_drill_evidence=accept_drill_evidence,
        evaluated_at=evaluated_at,
    )


async def evaluate_phase_b_async(
    surface_kind: str,
    window_days: int = 14,
    *,
    accept_drill_evidence: bool = False,
) -> EvaluationReport:
    """Acquire a connection from Core's pool and run Phase-B promotion evaluation."""
    from src import agent_storage

    db = agent_storage.get_db()
    async with db.acquire() as conn:
        return await evaluate_phase_b_from_conn(
            conn,
            surface_kind=surface_kind,
            window_days=window_days,
            accept_drill_evidence=accept_drill_evidence,
        )

"""S7 provenance JSONB index-readiness policy.

S7 stores lineage snapshots in ``knowledge.discoveries.provenance_chain`` and
S22 write context in ``knowledge.discoveries.provenance.s22_context``. Those
fields should not gain new indexes merely because they exist: the default
read path can use existing discovery filters and evaluate lineage snapshots in
Python. Promote JSONB indexes only when row volume and observed JSONB query
pressure justify the write cost.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from src.db import get_db
from src.db.mixins.knowledge_graph import S7_COUNTS_SQL


DEFAULT_MIN_JSONB_ROWS = 1000
DEFAULT_MIN_JSONB_QUERY_COUNT = 25

DISCOVERY_INDEXES_SQL = """
SELECT indexname, indexdef
FROM pg_indexes
WHERE schemaname = 'knowledge'
  AND tablename = 'discoveries'
ORDER BY indexname
"""

PG_STAT_STATEMENTS_SQL = """
SELECT COALESCE(SUM(calls), 0)::BIGINT
FROM pg_stat_statements
WHERE query ILIKE '%knowledge.discoveries%'
  AND (
      query ILIKE '%provenance_chain%'
      OR query ILIKE '%s22_context%'
      OR query ILIKE '%provenance ?%'
  )
  AND query NOT ILIKE '%COUNT(*) FILTER%'
  AND query NOT ILIKE '%pg_stat_statements%'
"""


@dataclass(frozen=True)
class S7ProvenanceIndexSnapshot:
    """Measured inputs for the S7 indexing decision."""

    total_discoveries: int = 0
    provenance_chain_rows: int = 0
    s7_lineage_link_rows: int = 0
    s22_context_rows: int = 0
    observed_jsonb_query_count: int = 0
    query_observation_source: str = "not_supplied"
    existing_indexes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def collect_s7_provenance_index_snapshot(
    *,
    db: Optional[Any] = None,
    observed_jsonb_query_count: Optional[int] = None,
) -> S7ProvenanceIndexSnapshot:
    """Collect read-only S7 provenance index inputs from PostgreSQL.

    ``observed_jsonb_query_count`` may be supplied from external telemetry. If
    omitted, this tries ``pg_stat_statements`` and falls back to zero when the
    extension is unavailable.
    """
    backend = db or get_db()
    async with backend.acquire() as conn:
        counts_row = await conn.fetchrow(S7_COUNTS_SQL)
        index_rows = await conn.fetch(DISCOVERY_INDEXES_SQL)

        if observed_jsonb_query_count is None:
            query_count, source = await _try_observed_query_count(conn)
        else:
            query_count = observed_jsonb_query_count
            source = "manual"

    return S7ProvenanceIndexSnapshot(
        total_discoveries=_row_int(counts_row, "total_discoveries"),
        provenance_chain_rows=_row_int(counts_row, "provenance_chain_rows"),
        s7_lineage_link_rows=_row_int(counts_row, "s7_lineage_link_rows"),
        s22_context_rows=_row_int(counts_row, "s22_context_rows"),
        observed_jsonb_query_count=max(0, int(query_count or 0)),
        query_observation_source=source,
        existing_indexes=tuple(_format_index(row) for row in index_rows),
    )


def assess_s7_provenance_index_readiness(
    snapshot: S7ProvenanceIndexSnapshot,
    *,
    min_jsonb_rows: int = DEFAULT_MIN_JSONB_ROWS,
    min_jsonb_query_count: int = DEFAULT_MIN_JSONB_QUERY_COUNT,
) -> dict[str, Any]:
    """Return the S7 indexing decision for the measured snapshot."""
    if min_jsonb_rows < 0:
        raise ValueError("min_jsonb_rows must be non-negative")
    if min_jsonb_query_count < 0:
        raise ValueError("min_jsonb_query_count must be non-negative")

    candidate_rows = max(
        snapshot.provenance_chain_rows,
        snapshot.s7_lineage_link_rows,
        snapshot.s22_context_rows,
    )
    indexed_surfaces = _indexed_surfaces(snapshot.existing_indexes)
    surfaces_needed = _surfaces_needed(snapshot)
    missing_surfaces = tuple(
        surface for surface in surfaces_needed if surface not in indexed_surfaces
    )
    candidate_indexes = _candidate_indexes(missing_surfaces)

    result = {
        "decision": "defer",
        "reason": "",
        "thresholds": {
            "min_jsonb_rows": min_jsonb_rows,
            "min_jsonb_query_count": min_jsonb_query_count,
        },
        "snapshot": snapshot.to_dict(),
        "indexed_surfaces": sorted(indexed_surfaces),
        "missing_surfaces": list(missing_surfaces),
        "candidate_indexes": candidate_indexes,
        "recommendations": [],
    }

    if candidate_rows == 0:
        result["reason"] = "no_s7_or_s22_jsonb_rows"
        result["recommendations"].append(
            "Do not add a migration; there are no S7/S22 JSONB rows to serve."
        )
        return result

    if surfaces_needed and not missing_surfaces:
        result["decision"] = "already_indexed"
        result["reason"] = "relevant_jsonb_surfaces_already_indexed"
        result["recommendations"].append(
            "Keep the existing indexes; no additional S7 index migration is needed."
        )
        return result

    if candidate_rows < min_jsonb_rows:
        result["reason"] = "below_row_volume_threshold"
        result["recommendations"].append(
            "Defer JSONB indexes until provenance row volume is material."
        )
        return result

    if snapshot.observed_jsonb_query_count < min_jsonb_query_count:
        result["reason"] = "below_query_pressure_threshold"
        result["recommendations"].append(
            "Defer JSONB indexes until logs or pg_stat_statements show repeated "
            "S7/S22 JSONB predicates."
        )
        return result

    result["decision"] = "candidate"
    result["reason"] = "row_volume_and_query_pressure_crossed_thresholds"
    result["recommendations"].append(
        "Open a migration or operator DDL only for the listed missing surfaces."
    )
    result["recommendations"].append(
        "Prefer partial GIN indexes first; consider generated columns only after "
        "stable equality filters emerge."
    )
    return result


async def _try_observed_query_count(conn: Any) -> tuple[int, str]:
    try:
        value = await conn.fetchval(PG_STAT_STATEMENTS_SQL)
    except Exception:
        return 0, "unavailable"
    return int(value or 0), "pg_stat_statements"


def _row_int(row: Any, key: str) -> int:
    if row is None:
        return 0
    value = row.get(key) if hasattr(row, "get") else row[key]
    return int(value or 0)


def _format_index(row: Any) -> str:
    indexname = row.get("indexname") if hasattr(row, "get") else row["indexname"]
    indexdef = row.get("indexdef") if hasattr(row, "get") else row["indexdef"]
    return f"{indexname}: {indexdef}"


def _indexed_surfaces(existing_indexes: tuple[str, ...]) -> set[str]:
    indexed: set[str] = set()
    for index in existing_indexes:
        lower = index.lower()
        if "gin" not in lower:
            continue
        if "provenance_chain" in lower:
            indexed.add("provenance_chain")
        if "s22_context" in lower or "provenance -> 's22_context'" in lower:
            indexed.add("s22_context")
    return indexed


def _surfaces_needed(snapshot: S7ProvenanceIndexSnapshot) -> tuple[str, ...]:
    surfaces = []
    if snapshot.provenance_chain_rows or snapshot.s7_lineage_link_rows:
        surfaces.append("provenance_chain")
    if snapshot.s22_context_rows:
        surfaces.append("s22_context")
    return tuple(surfaces)


def _candidate_indexes(missing_surfaces: tuple[str, ...]) -> list[dict[str, str]]:
    candidates = []
    if "provenance_chain" in missing_surfaces:
        candidates.append({
            "surface": "provenance_chain",
            "name": "idx_knowledge_discoveries_provenance_chain_s7_gin",
            "sql": (
                "CREATE INDEX CONCURRENTLY "
                "idx_knowledge_discoveries_provenance_chain_s7_gin "
                "ON knowledge.discoveries USING GIN "
                "(provenance_chain jsonb_path_ops) "
                "WHERE provenance_chain IS NOT NULL;"
            ),
        })
    if "s22_context" in missing_surfaces:
        candidates.append({
            "surface": "s22_context",
            "name": "idx_knowledge_discoveries_s22_context_gin",
            "sql": (
                "CREATE INDEX CONCURRENTLY "
                "idx_knowledge_discoveries_s22_context_gin "
                "ON knowledge.discoveries USING GIN "
                "((provenance -> 's22_context') jsonb_path_ops) "
                "WHERE provenance ? 's22_context';"
            ),
        })
    return candidates

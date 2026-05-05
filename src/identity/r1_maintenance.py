"""R1 maintenance sweeps.

Operator-facing helpers for the R1 follow-up work that sits outside the
onboard hot path:

- re-score provisional lineage claims and confirm only plausible ones;
- archive stale public R1 KG nodes after the v3.2-D 30-day TTL.

The promotion sweep intentionally does not remove unsupported lineage edges.
R1 names that as the orphan-archival path, but no destructive lineage-removal
primitive exists yet. Unsupported scores are reported as orphan candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from src.identity.trajectory_continuity import score_trajectory_continuity


ScoreFn = Callable[[str, str], Awaitable[Any]]


@dataclass(frozen=True)
class ProvisionalLineageCandidate:
    successor_id: str
    parent_id: str
    provisional_score_id: Optional[str] = None


async def sweep_provisional_lineage(
    *,
    apply: bool = False,
    limit: Optional[int] = None,
    db: Any = None,
    score_fn: ScoreFn = score_trajectory_continuity,
) -> dict[str, Any]:
    """Re-score provisional lineage claims and optionally confirm plausible ones.

    `score_fn` is the normal R1 primitive, so every evaluation writes the
    audit row and public KG redaction. `apply=False` prevents only the
    `confirm_lineage` mutation.
    """
    backend = db or _get_db()
    candidates = await _load_provisional_lineage_candidates(backend, limit=limit)
    results: list[dict[str, Any]] = []
    counts = {
        "evaluated": 0,
        "confirmed": 0,
        "would_confirm": 0,
        "blocked_inconclusive": 0,
        "orphan_candidates": 0,
        "confirm_failed": 0,
    }

    for candidate in candidates:
        score = await score_fn(candidate.parent_id, candidate.successor_id)
        counts["evaluated"] += 1
        verdict = getattr(score, "verdict", None)
        score_id = getattr(score, "score_id", None)
        item = {
            "successor_id": candidate.successor_id,
            "parent_id": candidate.parent_id,
            "previous_score_id": candidate.provisional_score_id,
            "score_id": score_id,
            "verdict": verdict,
        }

        if verdict == "plausible":
            if apply:
                ok = await backend.confirm_lineage(candidate.successor_id)
                item["action"] = "confirmed" if ok else "confirm_failed"
                counts["confirmed" if ok else "confirm_failed"] += 1
            else:
                item["action"] = "would_confirm"
                counts["would_confirm"] += 1
        elif verdict == "unsupported":
            item["action"] = "orphan_candidate"
            counts["orphan_candidates"] += 1
        else:
            item["action"] = "blocked_inconclusive"
            counts["blocked_inconclusive"] += 1

        results.append(item)

    return {
        "apply": apply,
        "limit": limit,
        "candidate_count": len(candidates),
        **counts,
        "results": results,
        "note": (
            "Evaluation calls score_trajectory_continuity and therefore writes "
            "R1 audit/KG score records; apply only controls confirm_lineage."
        ),
    }


async def archive_stale_public_r1_scores(
    *,
    ttl_days: int = 30,
    dry_run: bool = True,
    limit: Optional[int] = None,
    db: Any = None,
) -> dict[str, Any]:
    """Archive stale public R1 KG nodes.

    The audit table keeps score history. Public KG nodes are the redacted,
    deduped-by-pair projection and are archived after 30 days without re-score.
    """
    backend = db or _get_db()
    if ttl_days <= 0:
        raise ValueError("ttl_days must be positive")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive when provided")

    async with backend.acquire() as conn:
        if dry_run:
            rows = await conn.fetch(
                _stale_public_r1_scores_select_sql(limit=limit),
                ttl_days,
            )
            sample = [_row_to_stale_score(r) for r in rows]
            count = await conn.fetchval(_stale_public_r1_scores_count_sql(), ttl_days)
            return {
                "dry_run": True,
                "ttl_days": ttl_days,
                "would_archive": int(count or 0),
                "sample": sample,
                "limit": limit,
            }

        rows = await conn.fetch(
            _stale_public_r1_scores_archive_sql(limit=limit),
            ttl_days,
        )
        archived = [_row_to_stale_score(r) for r in rows]
        return {
            "dry_run": False,
            "ttl_days": ttl_days,
            "archived": len(archived),
            "sample": archived,
            "limit": limit,
        }


async def _load_provisional_lineage_candidates(
    backend: Any,
    *,
    limit: Optional[int],
) -> list[ProvisionalLineageCandidate]:
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive when provided")
    async with backend.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT agent_id AS successor_id,
                   parent_agent_id AS parent_id,
                   provisional_score_id
            FROM core.identities
            WHERE provisional_lineage = TRUE
              AND parent_agent_id IS NOT NULL
              AND status = 'active'
            ORDER BY provisional_recorded_at NULLS FIRST, created_at
            LIMIT COALESCE($1::int, 2147483647)
            """,
            limit,
        )
    return [
        ProvisionalLineageCandidate(
            successor_id=row["successor_id"],
            parent_id=row["parent_id"],
            provisional_score_id=(
                str(row["provisional_score_id"])
                if _row_get(row, "provisional_score_id") else None
            ),
        )
        for row in rows
    ]


def _stale_public_r1_scores_count_sql() -> str:
    return """
        SELECT COUNT(*)
        FROM knowledge.discoveries
        WHERE type = 'trajectory_continuity_score'
          AND status = 'open'
          AND COALESCE(updated_at, created_at) < now() - ($1::int * INTERVAL '1 day')
    """


def _stale_public_r1_scores_select_sql(*, limit: Optional[int]) -> str:
    return f"""
        SELECT id, agent_id, created_at, updated_at, status
        FROM knowledge.discoveries
        WHERE type = 'trajectory_continuity_score'
          AND status = 'open'
          AND COALESCE(updated_at, created_at) < now() - ($1::int * INTERVAL '1 day')
        ORDER BY COALESCE(updated_at, created_at)
        LIMIT {int(limit) if limit is not None else 20}
    """


def _stale_public_r1_scores_archive_sql(*, limit: Optional[int]) -> str:
    limit_sql = f"LIMIT {int(limit)}" if limit is not None else ""
    return f"""
        WITH stale AS (
            SELECT id
            FROM knowledge.discoveries
            WHERE type = 'trajectory_continuity_score'
              AND status = 'open'
              AND COALESCE(updated_at, created_at) < now() - ($1::int * INTERVAL '1 day')
            ORDER BY COALESCE(updated_at, created_at)
            {limit_sql}
        )
        UPDATE knowledge.discoveries d
        SET status = 'archived',
            resolved_at = now(),
            updated_at = now()
        FROM stale
        WHERE d.id = stale.id
        RETURNING d.id, d.agent_id, d.created_at, d.updated_at, d.status
    """


def _row_to_stale_score(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "created_at": row["created_at"].isoformat() if _row_get(row, "created_at") else None,
        "updated_at": row["updated_at"].isoformat() if _row_get(row, "updated_at") else None,
        "status": _row_get(row, "status"),
    }


def _row_get(row: Any, key: str) -> Any:
    if hasattr(row, "get"):
        return row.get(key)
    try:
        return row[key]
    except Exception:
        return None


def _get_db() -> Any:
    from src.db import get_db
    return get_db()

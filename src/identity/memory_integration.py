"""R5 memory-integration scoring.

R5 is a shadow/advisory signal for "did the successor operate on concrete
memory artifacts from the parent?" It intentionally reads existing KG rows
only and does not affect R2 lineage promotion or demotion.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from src.db import get_db
from src.knowledge_graph import get_knowledge_graph


MemoryIntegrationVerdict = Literal[
    "integrated_candidate",
    "weak_signal",
    "absent",
    "insufficient_parent_memory",
    "inconclusive",
]


STRONG_CONSTRUCTIVE_RESPONSE_TYPES = frozenset(
    {
        "extend",
        "elaboration",
        "correction",
        "supersedes",
        "answer",
        "answers",  # legacy handle_answer_question spelling
    }
)
WEAK_CONSTRUCTIVE_RESPONSE_TYPES = frozenset({"support", "follow_up"})
NON_INTEGRATING_RESPONSE_TYPES = frozenset({"question", "disagree"})
KNOWN_RESPONSE_TYPES = (
    STRONG_CONSTRUCTIVE_RESPONSE_TYPES
    | WEAK_CONSTRUCTIVE_RESPONSE_TYPES
    | NON_INTEGRATING_RESPONSE_TYPES
)
EXCLUDED_MEMORY_STATUSES = frozenset({"archived", "cold"})
LINEAGE_PAIR_STATES = frozenset({"provisional", "confirmed", "all"})


@dataclass(frozen=True)
class MemoryIntegrationScore:
    score_id: str
    parent_id: str
    successor_id: str
    channel: str
    verdict: MemoryIntegrationVerdict
    confidence: float
    parent_discoveries_seen: int
    cited_parent_discoveries: int
    strong_extensions: int
    weak_extensions: int
    successor_discoveries_seen: int
    cited_discovery_ids: list[str] = field(default_factory=list)
    generated_discovery_ids: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    calibration_status: Literal["seeded", "calibrating", "calibrated"] = "seeded"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MemoryIntegrationLineagePair:
    successor_id: str
    parent_id: str
    lineage_state: Literal["provisional", "confirmed"]
    lineage_declared_at: Optional[Any] = None
    confirmed_at: Optional[Any] = None
    chain_obs_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "successor_id": self.successor_id,
            "parent_id": self.parent_id,
            "lineage_state": self.lineage_state,
            "lineage_declared_at": _serialize_value(self.lineage_declared_at),
            "confirmed_at": _serialize_value(self.confirmed_at),
            "chain_obs_count": self.chain_obs_count,
        }


async def score_memory_integration(
    parent_id: str,
    successor_id: str,
    *,
    channel: str = "kg_cite_extend",
    window_days: int = 30,
    min_parent_discoveries: int = 3,
    min_strong_extensions: int = 2,
    min_distinct_parent_targets: int = 2,
    graph: Optional[Any] = None,
    now: Optional[datetime] = None,
    max_discoveries: int = 500,
) -> MemoryIntegrationScore:
    """Score successor use of parent KG memory in shadow mode.

    The v0.1 channel is KG cite-and-extend: parent artifacts are KG
    discoveries authored by the immediate declared parent, and successor
    evidence is a KG discovery authored by the successor with response_to
    pointing at a parent artifact.
    """
    if channel != "kg_cite_extend":
        raise ValueError(f"Unsupported R5 memory integration channel: {channel}")
    if not parent_id or not successor_id:
        raise ValueError("parent_id and successor_id are required")
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    if min_parent_discoveries <= 0:
        raise ValueError("min_parent_discoveries must be positive")
    if min_strong_extensions <= 0:
        raise ValueError("min_strong_extensions must be positive")
    if min_distinct_parent_targets <= 0:
        raise ValueError("min_distinct_parent_targets must be positive")
    if max_discoveries <= 0:
        raise ValueError("max_discoveries must be positive")

    score_id = _new_score_id()
    graph = graph or await get_knowledge_graph()
    observed_at = _ensure_aware(now or datetime.now(timezone.utc))
    window_start = observed_at - timedelta(days=window_days)

    try:
        parent_rows = await _load_agent_discoveries(
            graph,
            parent_id,
            limit=max_discoveries,
        )
        successor_rows = await _load_agent_discoveries(
            graph,
            successor_id,
            limit=max_discoveries,
        )
    except Exception as exc:
        return _build_score(
            score_id=score_id,
            parent_id=parent_id,
            successor_id=successor_id,
            channel=channel,
            verdict="inconclusive",
            confidence=0.0,
            reasons=[f"KG read failed: {type(exc).__name__}: {exc}"],
        )

    parent_discoveries = [
        row
        for row in parent_rows
        if _is_eligible_memory_row(row, window_start=window_start)
    ]
    successor_discoveries = [
        row
        for row in successor_rows
        if _is_eligible_memory_row(row, window_start=window_start)
    ]

    parent_discovery_ids = {
        discovery_id
        for discovery_id in (_get_field(row, "id") for row in parent_discoveries)
        if discovery_id
    }

    citing_rows: list[Any] = []
    cited_parent_ids: list[str] = []
    generated_ids: list[str] = []
    strong_extensions = 0
    weak_extensions = 0
    unclassified_types: set[str] = set()

    for row in successor_discoveries:
        response_to_id = _response_to_id(row)
        if response_to_id not in parent_discovery_ids:
            continue

        citing_rows.append(row)
        cited_parent_ids.append(response_to_id)
        generated_id = _get_field(row, "id")
        if generated_id:
            generated_ids.append(generated_id)

        response_type = _response_type(row)
        if response_type in STRONG_CONSTRUCTIVE_RESPONSE_TYPES:
            strong_extensions += 1
        elif response_type in WEAK_CONSTRUCTIVE_RESPONSE_TYPES:
            weak_extensions += 1
        elif response_type and response_type not in KNOWN_RESPONSE_TYPES:
            unclassified_types.add(response_type)

    cited_discovery_ids = _dedupe(cited_parent_ids)
    generated_discovery_ids = _dedupe(generated_ids)
    reasons = _build_reasons(
        parent_count=len(parent_discoveries),
        successor_count=len(successor_discoveries),
        cited_count=len(cited_discovery_ids),
        strong_extensions=strong_extensions,
        weak_extensions=weak_extensions,
        min_parent_discoveries=min_parent_discoveries,
        min_strong_extensions=min_strong_extensions,
        min_distinct_parent_targets=min_distinct_parent_targets,
        unclassified_types=unclassified_types,
    )

    if len(parent_discoveries) < min_parent_discoveries:
        verdict: MemoryIntegrationVerdict = "insufficient_parent_memory"
    elif (
        strong_extensions >= min_strong_extensions
        and len(cited_discovery_ids) >= min_distinct_parent_targets
    ):
        verdict = "integrated_candidate"
    elif citing_rows:
        verdict = "weak_signal"
    else:
        verdict = "absent"

    confidence = _heuristic_confidence(
        verdict=verdict,
        strong_extensions=strong_extensions,
        weak_extensions=weak_extensions,
        distinct_targets=len(cited_discovery_ids),
        successor_count=len(successor_discoveries),
        min_strong_extensions=min_strong_extensions,
        min_distinct_parent_targets=min_distinct_parent_targets,
    )

    return _build_score(
        score_id=score_id,
        parent_id=parent_id,
        successor_id=successor_id,
        channel=channel,
        verdict=verdict,
        confidence=confidence,
        parent_discoveries_seen=len(parent_discoveries),
        cited_parent_discoveries=len(cited_discovery_ids),
        strong_extensions=strong_extensions,
        weak_extensions=weak_extensions,
        successor_discoveries_seen=len(successor_discoveries),
        cited_discovery_ids=cited_discovery_ids,
        generated_discovery_ids=generated_discovery_ids,
        reasons=reasons,
    )


async def select_memory_integration_lineage_pairs(
    *,
    lineage_state: str = "provisional",
    limit: int = 25,
    db: Optional[Any] = None,
) -> list[MemoryIntegrationLineagePair]:
    """Select lineage pairs for read-only R5 shadow sampling."""
    if lineage_state not in LINEAGE_PAIR_STATES:
        raise ValueError(
            "lineage_state must be one of "
            + ", ".join(sorted(LINEAGE_PAIR_STATES))
        )
    if limit <= 0:
        raise ValueError("limit must be positive")

    backend = db or get_db()
    async with backend.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT agent_id AS successor_id,
                   parent_agent_id AS parent_id,
                   CASE
                     WHEN provisional_lineage = TRUE THEN 'provisional'
                     ELSE 'confirmed'
                   END AS lineage_state,
                   lineage_declared_at,
                   confirmed_at,
                   chain_obs_count
              FROM core.identities
             WHERE parent_agent_id IS NOT NULL
               AND lineage_archived_at IS NULL
               AND lineage_demoted_at IS NULL
               AND (
                    $1 = 'all'
                 OR ($1 = 'provisional' AND provisional_lineage = TRUE)
                 OR ($1 = 'confirmed'
                     AND provisional_lineage = FALSE
                     AND confirmed_at IS NOT NULL)
               )
             ORDER BY
               CASE WHEN provisional_lineage = TRUE THEN 0 ELSE 1 END,
               lineage_last_eval_at NULLS FIRST,
               lineage_declared_at NULLS FIRST,
               confirmed_at NULLS LAST,
               agent_id
             LIMIT $2
            """,
            lineage_state,
            int(limit),
        )

    return [_row_to_lineage_pair(row) for row in rows]


async def score_memory_integration_batch(
    *,
    lineage_state: str = "provisional",
    limit: int = 25,
    db: Optional[Any] = None,
    graph: Optional[Any] = None,
    **score_kwargs: Any,
) -> dict[str, Any]:
    """Run R5 shadow scoring over selected lineage pairs.

    Read-only: pair selection reads core.identities and scoring reads KG rows.
    No audit/KG/R2 state is written.
    """
    pairs = await select_memory_integration_lineage_pairs(
        lineage_state=lineage_state,
        limit=limit,
        db=db,
    )
    verdict_counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []

    for pair in pairs:
        score = await score_memory_integration(
            pair.parent_id,
            pair.successor_id,
            graph=graph,
            **score_kwargs,
        )
        verdict_counts[score.verdict] = verdict_counts.get(score.verdict, 0) + 1
        items.append(
            {
                "pair": pair.to_dict(),
                "score": score.to_dict(),
            }
        )

    return {
        "lineage_state": lineage_state,
        "limit": limit,
        "pair_count": len(pairs),
        "verdict_counts": verdict_counts,
        "items": items,
        "note": (
            "R5 shadow batch is read-only: it selects lineage pairs from "
            "core.identities and scores existing KG response_to links. It "
            "does not write audit rows, KG rows, or R2 lineage state."
        ),
    }


async def _load_agent_discoveries(
    graph: Any,
    agent_id: str,
    *,
    limit: int,
) -> list[Any]:
    if hasattr(graph, "get_agent_discoveries"):
        return list(await graph.get_agent_discoveries(agent_id, limit=limit))
    if hasattr(graph, "query"):
        return list(await graph.query(agent_id=agent_id, limit=limit))
    raise TypeError("KG backend must expose get_agent_discoveries or query")


def _is_eligible_memory_row(row: Any, *, window_start: datetime) -> bool:
    status = str(_get_field(row, "status") or "").lower()
    if status in EXCLUDED_MEMORY_STATUSES:
        return False
    created_at = _row_timestamp(row)
    return created_at is None or created_at >= window_start


def _row_timestamp(row: Any) -> Optional[datetime]:
    value = _get_field(row, "timestamp") or _get_field(row, "created_at")
    if value is None:
        return None
    if isinstance(value, datetime):
        return _ensure_aware(value)
    if isinstance(value, str):
        try:
            return _ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _response_to_id(row: Any) -> Optional[str]:
    response_to = _get_field(row, "response_to")
    if isinstance(response_to, dict):
        return response_to.get("discovery_id") or response_to.get("id")
    if response_to is not None:
        return getattr(response_to, "discovery_id", None)
    return _get_field(row, "response_to_id")


def _response_type(row: Any) -> Optional[str]:
    response_to = _get_field(row, "response_to")
    response_type = None
    if isinstance(response_to, dict):
        response_type = response_to.get("response_type")
    elif response_to is not None:
        response_type = getattr(response_to, "response_type", None)
    response_type = response_type or _get_field(row, "response_type")
    return str(response_type).lower() if response_type else None


def _get_field(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(name)
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        pass
    return getattr(row, name, None)


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _row_to_lineage_pair(row: Any) -> MemoryIntegrationLineagePair:
    lineage_state = str(_get_field(row, "lineage_state") or "")
    if lineage_state not in {"provisional", "confirmed"}:
        raise ValueError(f"Unexpected lineage_state from DB: {lineage_state}")
    return MemoryIntegrationLineagePair(
        successor_id=str(_get_field(row, "successor_id")),
        parent_id=str(_get_field(row, "parent_id")),
        lineage_state=lineage_state,  # type: ignore[arg-type]
        lineage_declared_at=_get_field(row, "lineage_declared_at"),
        confirmed_at=_get_field(row, "confirmed_at"),
        chain_obs_count=int(_get_field(row, "chain_obs_count") or 0),
    )


def _serialize_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _ensure_aware(value).isoformat()
    return value


def _build_reasons(
    *,
    parent_count: int,
    successor_count: int,
    cited_count: int,
    strong_extensions: int,
    weak_extensions: int,
    min_parent_discoveries: int,
    min_strong_extensions: int,
    min_distinct_parent_targets: int,
    unclassified_types: set[str],
) -> list[str]:
    reasons = [
        f"parent_discoveries_seen={parent_count}",
        f"successor_discoveries_seen={successor_count}",
        f"cited_parent_discoveries={cited_count}",
        f"strong_extensions={strong_extensions}",
        f"weak_extensions={weak_extensions}",
    ]
    if parent_count < min_parent_discoveries:
        reasons.append(
            "parent memory corpus below threshold "
            f"({parent_count} < {min_parent_discoveries})"
        )
    elif strong_extensions >= min_strong_extensions and cited_count >= min_distinct_parent_targets:
        reasons.append(
            "successor met strong cite-and-extend thresholds "
            f"({strong_extensions} >= {min_strong_extensions}, "
            f"{cited_count} >= {min_distinct_parent_targets})"
        )
    elif cited_count > 0:
        reasons.append(
            "successor cited parent memory but did not meet strong-extension thresholds"
        )
    else:
        reasons.append("no successor response_to links targeted parent memory")
    if unclassified_types:
        reasons.append(
            "ignored unclassified response_type values: "
            + ", ".join(sorted(unclassified_types))
        )
    return reasons


def _heuristic_confidence(
    *,
    verdict: MemoryIntegrationVerdict,
    strong_extensions: int,
    weak_extensions: int,
    distinct_targets: int,
    successor_count: int,
    min_strong_extensions: int,
    min_distinct_parent_targets: int,
) -> float:
    if verdict == "inconclusive":
        return 0.0
    if verdict == "insufficient_parent_memory":
        return 0.2
    if verdict == "absent":
        return min(0.7, 0.45 + min(successor_count, 5) * 0.03)
    if verdict == "weak_signal":
        return min(0.55, 0.3 + (strong_extensions * 0.08) + (weak_extensions * 0.04))

    strong_ratio = min(2.0, strong_extensions / min_strong_extensions)
    target_ratio = min(2.0, distinct_targets / min_distinct_parent_targets)
    return min(0.9, round(0.45 + strong_ratio * 0.15 + target_ratio * 0.075, 6))


def _build_score(
    *,
    score_id: str,
    parent_id: str,
    successor_id: str,
    channel: str,
    verdict: MemoryIntegrationVerdict,
    confidence: float,
    parent_discoveries_seen: int = 0,
    cited_parent_discoveries: int = 0,
    strong_extensions: int = 0,
    weak_extensions: int = 0,
    successor_discoveries_seen: int = 0,
    cited_discovery_ids: Optional[list[str]] = None,
    generated_discovery_ids: Optional[list[str]] = None,
    reasons: Optional[list[str]] = None,
) -> MemoryIntegrationScore:
    return MemoryIntegrationScore(
        score_id=score_id,
        parent_id=parent_id,
        successor_id=successor_id,
        channel=channel,
        verdict=verdict,
        confidence=round(confidence, 6),
        parent_discoveries_seen=parent_discoveries_seen,
        cited_parent_discoveries=cited_parent_discoveries,
        strong_extensions=strong_extensions,
        weak_extensions=weak_extensions,
        successor_discoveries_seen=successor_discoveries_seen,
        cited_discovery_ids=cited_discovery_ids or [],
        generated_discovery_ids=generated_discovery_ids or [],
        reasons=reasons or [],
    )


def _new_score_id() -> str:
    return f"r5-memory-{uuid4()}"

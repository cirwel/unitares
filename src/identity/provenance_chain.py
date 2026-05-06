"""S7 lineage provenance-chain snapshots.

The snapshot is write-time metadata for KG discoveries. It reads the current
R2 lineage lifecycle state from ``core.identities`` and emits immutable link
records ordered root-to-writer. It does not mutate lineage state.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Optional

from src.db import get_db
from src.identity.lineage_lifecycle import derive_lineage_state


SCHEMA_VERSION = "s7.lineage_link.v1"
AGGREGATION_MODES = {"as_written", "current_valid"}


async def build_lineage_provenance_chain(
    agent_id: str,
    *,
    db: Optional[Any] = None,
    max_depth: int = 16,
) -> list[dict[str, Any]]:
    """Build root-to-writer lineage link snapshots from ``core.identities``.

    Missing ancestors stop traversal with an explicit ``chain_stop_reason`` on
    the visible link. The helper never fabricates links beyond rows it can
    read.
    """
    if not agent_id:
        raise ValueError("agent_id is required")
    if max_depth <= 0:
        raise ValueError("max_depth must be positive")

    backend = db or get_db()
    successor_id = agent_id
    visited = {successor_id}
    writer_to_root: list[dict[str, Any]] = []

    for depth in range(1, max_depth + 1):
        row = await backend.read_lineage_state(successor_id)
        if row is None:
            if writer_to_root:
                writer_to_root[-1]["chain_stop_reason"] = "successor_identity_missing"
            break

        raw_parent_id = row.get("parent_agent_id")
        if not raw_parent_id:
            break
        parent_id = str(raw_parent_id)

        link = _lineage_link_from_row(
            row,
            successor_id=successor_id,
            parent_id=parent_id,
            depth_from_writer=depth,
        )
        writer_to_root.append(link)

        if parent_id in visited:
            link["chain_stop_reason"] = "cycle_detected"
            break
        visited.add(parent_id)

        parent_record = await backend.get_identity(parent_id)
        if parent_record is None:
            link["chain_stop_reason"] = "parent_identity_missing"
            break

        if depth == max_depth:
            link["chain_stop_reason"] = "max_depth_reached"
            break

        successor_id = str(parent_id)

    return list(reversed(writer_to_root))


async def evaluate_lineage_chain_aggregation(
    provenance_chain: Optional[list[dict[str, Any]]],
    *,
    mode: str = "current_valid",
    include_provisional: bool = False,
    db: Optional[Any] = None,
) -> dict[str, Any]:
    """Evaluate whether a write's lineage chain counts for aggregation.

    ``as_written`` trusts the immutable KG snapshot. ``current_valid`` first
    applies the write-time contract, then rereads ``core.identities`` so later
    demotion/archive/parent changes claw back current lineage-attributed counts.
    """
    if mode not in AGGREGATION_MODES:
        raise ValueError(
            f"mode must be one of {sorted(AGGREGATION_MODES)}, got {mode!r}"
        )

    chain = provenance_chain or []
    base = _empty_evaluation(mode, include_provisional, len(chain))
    if not chain:
        base["reason"] = "missing_provenance_chain"
        return base

    written = _evaluate_as_written(chain, mode, include_provisional)
    if not written["eligible"] or mode == "as_written":
        return written

    backend = db or get_db()
    current_failures: list[dict[str, Any]] = []
    current_provisional = False
    for index, link in enumerate(chain):
        parent_id = _normalize_id(link.get("parent_agent_id"))
        successor_id = _normalize_id(link.get("successor_agent_id"))
        if not parent_id or not successor_id:
            current_failures.append({
                "index": index,
                "reason": "missing_link_identity",
            })
            continue

        row = await backend.read_lineage_state(successor_id)
        if row is None:
            current_failures.append({
                "index": index,
                "successor_agent_id": successor_id,
                "reason": "current_successor_missing",
            })
            continue

        current_parent_id = _normalize_id(row.get("parent_agent_id"))
        if current_parent_id != parent_id:
            current_failures.append({
                "index": index,
                "successor_agent_id": successor_id,
                "parent_agent_id": parent_id,
                "current_parent_agent_id": current_parent_id,
                "reason": "current_parent_mismatch",
            })
            continue

        current_state = derive_lineage_state(row) or "unknown"
        if current_state == "confirmed":
            continue
        if include_provisional and current_state == "provisional":
            current_provisional = True
            continue
        current_failures.append({
            "index": index,
            "successor_agent_id": successor_id,
            "parent_agent_id": parent_id,
            "current_lineage_state": current_state,
            "reason": "current_link_not_confirmed",
        })

    if current_failures:
        written["eligible"] = False
        written["reason"] = "current_invalid"
        written["current_failures"] = current_failures
    else:
        written["reason"] = "eligible"
        written["provisional_included"] = (
            written["provisional_included"] or current_provisional
        )
    return written


async def aggregate_lineage_attribution(
    discoveries: Iterable[Any],
    *,
    mode: str = "current_valid",
    include_provisional: bool = False,
    db: Optional[Any] = None,
) -> dict[str, Any]:
    """Aggregate KG discoveries by eligible S7 lineage attribution."""
    totals = {
        "mode": mode,
        "include_provisional": include_provisional,
        "total_discoveries": 0,
        "eligible_discoveries": 0,
        "excluded_discoveries": 0,
        "provisional_included": 0,
        "by_root_agent_id": {},
        "by_direct_parent_agent_id": {},
        "by_lineage_agent_id": {},
        "by_writer_agent_id": {},
        "excluded_reasons": {},
    }

    backend = db or (get_db() if mode == "current_valid" else None)
    for discovery in discoveries:
        totals["total_discoveries"] += 1
        chain = _discovery_field(discovery, "provenance_chain")
        evaluation = await evaluate_lineage_chain_aggregation(
            chain,
            mode=mode,
            include_provisional=include_provisional,
            db=backend,
        )

        if not evaluation["eligible"]:
            totals["excluded_discoveries"] += 1
            reason = evaluation.get("reason", "unknown")
            _increment(totals["excluded_reasons"], reason)
            continue

        totals["eligible_discoveries"] += 1
        if evaluation.get("provisional_included"):
            totals["provisional_included"] += 1

        root_id = evaluation.get("root_agent_id")
        direct_parent_id = evaluation.get("direct_parent_agent_id")
        writer_id = (
            _discovery_field(discovery, "agent_id")
            or evaluation.get("writer_agent_id")
        )
        if root_id:
            _increment(totals["by_root_agent_id"], root_id)
        if direct_parent_id:
            _increment(totals["by_direct_parent_agent_id"], direct_parent_id)
        if writer_id:
            _increment(totals["by_writer_agent_id"], writer_id)
        for agent_id in evaluation.get("lineage_agent_ids", []):
            _increment(totals["by_lineage_agent_id"], agent_id)

    return totals


def _lineage_link_from_row(
    row: dict[str, Any],
    *,
    successor_id: str,
    parent_id: str,
    depth_from_writer: int,
) -> dict[str, Any]:
    state = derive_lineage_state(row) or "unknown"
    return {
        "schema": SCHEMA_VERSION,
        "source": "core.identities",
        "parent_agent_id": parent_id,
        "successor_agent_id": successor_id,
        "relationship": "lineage_parent",
        "lineage_state": state,
        "provisional_lineage": bool(row.get("provisional_lineage")),
        "lineage_declared_at": _serialize_timestamp(row.get("lineage_declared_at")),
        "confirmed_at": _serialize_timestamp(row.get("confirmed_at")),
        "lineage_demoted_at": _serialize_timestamp(row.get("lineage_demoted_at")),
        "lineage_archived_at": _serialize_timestamp(row.get("lineage_archived_at")),
        "chain_obs_count": int(row.get("chain_obs_count") or 0),
        "depth_from_writer": depth_from_writer,
        "aggregation_eligible_at_write": state == "confirmed",
    }


def _evaluate_as_written(
    chain: list[dict[str, Any]],
    mode: str,
    include_provisional: bool,
) -> dict[str, Any]:
    evaluation = _empty_evaluation(mode, include_provisional, len(chain))
    lineage_agent_ids: list[str] = []
    writer_agent_id = None
    provisional_included = False

    for index, link in enumerate(chain):
        parent_id = _normalize_id(link.get("parent_agent_id"))
        successor_id = _normalize_id(link.get("successor_agent_id"))
        if not parent_id or not successor_id:
            evaluation["reason"] = "missing_link_identity"
            evaluation["failed_link_index"] = index
            return evaluation
        if link.get("chain_stop_reason"):
            evaluation["reason"] = "chain_stopped"
            evaluation["chain_stop_reason"] = link["chain_stop_reason"]
            evaluation["failed_link_index"] = index
            return evaluation

        lineage_agent_ids.append(parent_id)
        writer_agent_id = successor_id

        state = link.get("lineage_state")
        explicit_flag = link.get("aggregation_eligible_at_write")
        if state == "confirmed" and explicit_flag is not False:
            continue
        if include_provisional and state == "provisional":
            provisional_included = True
            continue

        evaluation["reason"] = "link_not_eligible_at_write"
        evaluation["failed_link_index"] = index
        evaluation["lineage_state"] = state or "missing"
        return evaluation

    evaluation.update({
        "eligible": True,
        "reason": "eligible",
        "lineage_agent_ids": lineage_agent_ids,
        "root_agent_id": lineage_agent_ids[0],
        "direct_parent_agent_id": lineage_agent_ids[-1],
        "writer_agent_id": writer_agent_id,
        "provisional_included": provisional_included,
    })
    return evaluation


def _empty_evaluation(
    mode: str,
    include_provisional: bool,
    link_count: int,
) -> dict[str, Any]:
    return {
        "eligible": False,
        "reason": "not_evaluated",
        "mode": mode,
        "include_provisional": include_provisional,
        "link_count": link_count,
        "lineage_agent_ids": [],
        "root_agent_id": None,
        "direct_parent_agent_id": None,
        "writer_agent_id": None,
        "provisional_included": False,
    }


def _normalize_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _discovery_field(discovery: Any, field_name: str) -> Any:
    if isinstance(discovery, dict):
        return discovery.get(field_name)
    return getattr(discovery, field_name, None)


def _increment(bucket: dict[str, int], key: str) -> None:
    bucket[key] = bucket.get(key, 0) + 1


def _serialize_timestamp(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat()
    return value

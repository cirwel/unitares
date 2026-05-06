"""S7 lineage provenance-chain snapshots.

The snapshot is write-time metadata for KG discoveries. It reads the current
R2 lineage lifecycle state from ``core.identities`` and emits immutable link
records ordered root-to-writer. It does not mutate lineage state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from src.db import get_db
from src.identity.lineage_lifecycle import derive_lineage_state


SCHEMA_VERSION = "s7.lineage_link.v1"


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


def _serialize_timestamp(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.isoformat()
    return value

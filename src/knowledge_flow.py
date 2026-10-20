"""Cross-agent knowledge flow: who built on whose findings.

Reads the AGE graph through ``query_cross_agent_knowledge_flow``: one row per
(builder, source) agent pair, where the builder authored a discovery whose
RELATED_TO edge points at a discovery the source authored. Agent ids are
resolved to their ``core.agents.label`` for display; an agent without a label
is shown by id. Read-only.
"""

from __future__ import annotations

from typing import Any

from src.db.age_queries import query_cross_agent_knowledge_flow

DEFAULT_LIMIT = 25
MAX_LIMIT = 100


class GraphProbeError(RuntimeError):
    """The graph backend is installed but its probe failed.

    Distinct from an absent graph: the caller must report this as an error,
    never as "no graph backend".
    """


def clamp_limit(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


async def query_knowledge_flow(db: Any, *, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Return the top builder→source pairs by link count.

    ``graph_available`` false means the backend has no AGE graph, which is a
    different finding from a graph with no cross-agent links; the response
    keeps the two apart. A graph probe that failed is neither: it raises
    ``GraphProbeError`` so the route answers with an error.
    """
    status = await db.graph_status()
    if status == "absent":
        return {"success": True, "graph_available": False, "limit": limit, "flows": []}
    if status != "available":
        raise GraphProbeError(f"knowledge graph probe returned {status!r}")

    cypher, params = query_cross_agent_knowledge_flow(limit=limit)
    rows = await db.graph_query(cypher, params)

    flows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        builder, source = row.get("builder"), row.get("source")
        if not builder or not source:
            continue
        flows.append({"builder": str(builder), "source": str(source), "links": int(row.get("links") or 0)})

    labels: dict[str, str] = {}
    ids = sorted({f["builder"] for f in flows} | {f["source"] for f in flows})
    if ids:
        async with db.acquire() as conn:
            label_rows = await conn.fetch(
                "SELECT id, label FROM core.agents WHERE id = ANY($1::text[])",
                ids,
            )
        labels = {r["id"]: r["label"] for r in label_rows if r["label"]}

    for flow in flows:
        flow["builder_label"] = labels.get(flow["builder"])
        flow["source_label"] = labels.get(flow["source"])

    return {"success": True, "graph_available": True, "limit": limit, "flows": flows}

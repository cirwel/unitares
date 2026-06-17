"""Transitive lineage-succession reachability — a de-risk capability for the
AGE-canonical knowledge-graph move, wired in SHADOW mode over the archival
decision.

Single-hop ``_live_lineage_parent_ids`` marks a parent "superseded" when it has
an active, recent child whose ``spawn_reason`` is a SUCCESSION — anything except
``subagent``/``compaction``, which keep the parent live by design (#779). The
flat one-hop pass misses a chain ``P -> M -> C`` where the intermediate ``M``
has exited but a deeper descendant ``C`` is live: ``P``'s lineage continued, but
one hop can't see past the stale ``M``.

This module computes the TRANSITIVE succession-ancestor set:

  * Authoritative source = a recursive CTE over ``core.identities``, applying the
    SAME succession filter as the single-hop pass (``spawn_reason NOT IN
    {subagent, compaction}``). Relational truth, deterministic, no AGE dependency.
  * AGE cross-check = a single-round-trip ``SPAWNED*1..N`` Cypher walk. AGE 1.7.0
    cannot causal-filter a variable-length path (no ``ALL()`` predicate, no
    ``relationships()`` property access — both raise), so the walk is UNFILTERED
    and ADVISORY: it measures whether the graph's reachability TOPOLOGY matches
    the relational truth. That agreement is the evidence the step-1 (discovery
    canonical-collapse) decision needs, and it is the one thing only the graph
    can show cheaply.

Recoverable by construction: every failure path yields an EMPTY set, so a caller
keeps its existing single-hop behavior. The module can only ever ADD succeeded
ancestors; it never removes a caller's protection, and the caller's
``get_live_bindings`` gate stays the hard stop against retiring a still-running
agent. It is wired in SHADOW mode first (measure + log, no mutation) because
amplifying the superseded set on a correctness-critical archival path warrants
live evidence before it drives real archivals.

ACTIVATION PREREQUISITE (do not flip shadow -> active until this holds):
The transitive set enlarges the candidate pool that the liveness gate must
protect. The recurring false-archival bug is, at root, a *liveness-signal* gap:
ephemeral session agents do not spawn through the orchestrator, hold no
``agent:`` lease, and have no reliable runtime liveness signal — so prior fixes
fell back to stale ``agent_state`` and archived live work. The lease plane tracks
liveness for residents but not (yet) for those ephemeral agents. Before this set
drives real archivals, the gate must source liveness from the LEASE PLANE (with
ephemeral-agent liveness actually tracked there), not from ``agent_state``.
Until then, the single-hop ``get_live_bindings`` gate carries archival and this
module only measures.
"""

from __future__ import annotations

from typing import Iterable

from src.db import get_db
from src.logging_utils import get_logger

logger = get_logger(__name__)

# Spawn reasons that keep the parent LIVE by design — a child declaring one is
# NOT a successor (its parent is its still-running dispatcher / same session).
# Mirrors the single-hop exclusion in stuck.py::_live_lineage_parent_ids (#779).
# Every other reason (explicit, dispatch, new_session, fleet_dispatch, ...) — and
# a NULL reason — is treated as a succession edge, exactly as the single-hop pass
# does, so the transitive set is a superset of the single-hop set, not a
# differently-scoped one.
_NON_SUCCESSION_SPAWN_REASONS = ("subagent", "compaction")

# Hard cap on transitive depth; succession chains are shallow in practice
# (the live probe found depth-5 added only 2 nodes beyond depth-1).
_MAX_DEPTH = 5


async def reachable_ancestors(
    live_child_ids: Iterable[str], depth_limit: int = _MAX_DEPTH
) -> set[str]:
    """Transitive succession-ancestor UUIDs of the given live child agents.

    Returns the set of agent UUIDs that are ancestors (1..depth_limit hops) of
    any live child via SUCCESSION lineage, per the authoritative relational CTE.
    Runs the AGE walk alongside purely to log topology agreement. Returns an
    EMPTY set on empty input or ANY error — the caller then keeps its single-hop
    behavior.
    """
    child_ids = {c for c in live_child_ids if c}
    if not child_ids:
        return set()

    try:
        db = get_db()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("lineage_reachability: get_db failed (%s); no expansion", type(e).__name__)
        return set()

    try:
        causal = await _cte_ancestors(db, child_ids, depth_limit)
    except Exception as e:
        logger.warning("lineage_reachability: CTE failed (%s); no expansion", type(e).__name__)
        return set()

    # Advisory AGE cross-check — never drives the returned set.
    try:
        age = await _age_ancestors(db, child_ids, depth_limit)
        _record_reachability_agreement(age, causal)
    except Exception as e:
        logger.debug("lineage_reachability: AGE cross-check unavailable (%s)", type(e).__name__)

    return causal


async def _cte_ancestors(db, child_ids: set[str], depth_limit: int) -> set[str]:
    """Recursive CTE over core.identities — succession-filtered transitive ancestors.

    Succession filter mirrors the single-hop pass: an edge counts unless its
    spawn_reason is subagent/compaction (NULL counts as succession-eligible).
    """
    depth = max(1, min(int(depth_limit), _MAX_DEPTH))
    sql = """
        WITH RECURSIVE ancestors(agent_id, depth) AS (
            SELECT i.parent_agent_id, 1
            FROM core.identities i
            WHERE i.agent_id = ANY($1::text[])
              AND i.parent_agent_id IS NOT NULL
              AND (i.spawn_reason IS NULL OR i.spawn_reason <> ALL($2::text[]))
            UNION ALL
            SELECT i.parent_agent_id, a.depth + 1
            FROM core.identities i
            JOIN ancestors a ON i.agent_id = a.agent_id
            WHERE i.parent_agent_id IS NOT NULL
              AND (i.spawn_reason IS NULL OR i.spawn_reason <> ALL($2::text[]))
              AND a.depth < $3
        )
        SELECT DISTINCT agent_id FROM ancestors WHERE agent_id IS NOT NULL
    """
    async with db.acquire() as conn:
        rows = await conn.fetch(
            sql, list(child_ids), list(_NON_SUCCESSION_SPAWN_REASONS), depth
        )
    return {r["agent_id"] for r in rows if r["agent_id"]}


async def _age_ancestors(db, child_ids: set[str], depth_limit: int) -> set[str]:
    """AGE SPAWNED*1..N walk (UNFILTERED — advisory cross-check only).

    AGE 1.7.0 cannot filter a variable-length path by edge property, so this is
    the full reachability including coincidental new_session edges. Used solely
    to measure topology agreement against the authoritative CTE — never to drive
    archival.
    """
    depth = max(1, min(int(depth_limit), _MAX_DEPTH))
    cypher = (
        f"MATCH (ancestor:Agent)-[:SPAWNED*1..{depth}]->(child:Agent) "
        "WHERE child.id IN ${child_ids} AND ancestor.id <> child.id "
        "RETURN DISTINCT {ancestor: ancestor.id}"
    )
    rows = await db.graph_query(cypher, {"child_ids": sorted(child_ids)})
    out: set[str] = set()
    for row in rows or []:
        if isinstance(row, dict) and row.get("ancestor"):
            out.add(row["ancestor"])
    return out


def _record_reachability_agreement(age: set[str], causal: set[str]) -> None:
    """Log AGE-vs-CTE reachability agreement — evidence for the step-1 decision.

    AGE (unfiltered) should be a SUPERSET of the causal CTE: the extra entries are
    reachable only via non-succession (coincidental) edges, and any causal entry
    AGE misses indicates a stale/missing SPAWNED edge (write-path lag).
    """
    if age == causal:
        logger.info("lineage_reachability: AGE==CTE topology match (%d ancestors)", len(causal))
        return
    coincidental_only = age - causal
    missing_in_age = causal - age
    logger.info(
        "lineage_reachability: AGE/CTE divergence — causal=%d age=%d "
        "coincidental_only=%d missing_in_age=%d",
        len(causal), len(age), len(coincidental_only), len(missing_in_age),
    )


async def measure_transitive_expansion(
    live_child_ids: Iterable[str],
    single_hop_parent_ids: Iterable[str],
    depth_limit: int = _MAX_DEPTH,
) -> dict:
    """SHADOW measurement: what transitive succession-reachability WOULD add to
    the single-hop superseded set (and the AGE/CTE agreement logged inside
    ``reachable_ancestors``). Read-only — logs a summary and returns it, never
    mutates the archival decision.
    """
    single_hop = {p for p in single_hop_parent_ids if p}
    ancestors = await reachable_ancestors(live_child_ids, depth_limit)
    new_beyond_single_hop = ancestors - single_hop
    summary = {
        "single_hop": len(single_hop),
        "transitive_total": len(ancestors),
        "new_beyond_single_hop": len(new_beyond_single_hop),
    }
    if new_beyond_single_hop:
        logger.info(
            "lineage_reachability SHADOW: transitive would add +%d ancestor(s) "
            "beyond single-hop %s",
            len(new_beyond_single_hop),
            summary,
        )
    return summary

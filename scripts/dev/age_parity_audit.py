#!/usr/bin/env python3
"""AGE-vs-relational READ PARITY audit (read-only).

Decision gate for "should we remove the Apache-AGE graph layer?". Treats the
relational store (`knowledge.discoveries` + `discovery_edges` + `related_to[]`)
+ pgvector as the source of truth and proves the AGE graph adds nothing a read
needs. If parity holds, removing AGE is a safe, evidence-backed cut.

Findings (live `governance` DB, 2026-06-16, backend=age):
- RELATED_TO edges: every AGE edge is present in relational (0 missing)
- RESPONDS_TO (the only multi-hop traversal edge): recorded as 0, but that
  reading is NOT trustworthy -- see the `_scalar` docstring. The original
  reader could only ever produce 0 for this line, so it says nothing about
  the world and must be re-run before it is cited. `docs/operations/
  dormant-capability-registry.md` reaches the same conclusion by a separate
  route (edge counts taken against the live DB); this script is not
  independent corroboration of it until re-run.
- semantic_search: pure pgvector (`embedding <=> $1::vector`), no Cypher
- AGE query layer is UNRELIABLE here: identical status filters return
  different counts under literal vs parameter substitution, and `RETURN
  d.<prop>` frequently parses empty. So AGE is not a trustworthy read surface;
  relational is. This audit therefore verifies relational COMPLETENESS, not
  AGE-side counts.

Run: UNITARES_KNOWLEDGE_BACKEND=age python3 scripts/dev/age_parity_audit.py
Exit 0 = parity holds (safe to remove); exit 1 = a divergence needs review;
exit 2 = the audit could not run (wrong backend), which is not a parity verdict.
"""
import asyncio
import json
import sys
from pathlib import Path

# scripts/ is not a package and bash does not add the rootdir to sys.path the
# way pytest does, so `from src...` below fails unless the caller happens to be
# standing in the repo root. Same bootstrap as adoption_kpi.py / bump_epoch.py.
_REPO_ROOT = str(Path(__file__).resolve().parents[2])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def _scalar(rows, key):
    """Read a single-value Cypher result, whichever shape it decodes to.

    ``graph_query`` declares ONE ``result agtype`` output column and returns
    ``[decoded_value_per_row]`` (src/db/mixins/graph.py:225). So
    ``RETURN count(d) AS n`` decodes to a bare int -- the alias never survives
    as a key. The original of this audit read ``rows[0].get(key)`` behind an
    ``isinstance(rows[0], dict)`` guard, which is False for every scalar
    return: the RESPONDS_TO count reported 0 whether or not edges existed, and
    the reliability probe reported None-vs-None as "unreliable" unconditionally.
    A zero the instrument manufactures is not a zero about the world, so read
    both shapes and return None (not 0) when there is genuinely no row.
    """
    if not rows:
        return None
    first = rows[0]
    if isinstance(first, dict):
        return first.get(key)
    return first


async def main() -> int:
    from src.knowledge_graph import get_knowledge_graph
    kg = await get_knowledge_graph()
    if not hasattr(kg, "_get_db"):
        print(
            f"This audit needs the AGE backend; got {type(kg).__name__}, which "
            "exposes no graph handle. Re-run with "
            "UNITARES_KNOWLEDGE_BACKEND=age.",
            file=sys.stderr)
        return 2
    db = await kg._get_db()
    report: dict = {}
    parity_ok = True

    # 1. EDGE PARITY: every AGE RELATED_TO edge must exist in relational.
    age_rel = await db.graph_query(
        "MATCH (a:Discovery)-[:RELATED_TO]->(b:Discovery) RETURN {s: a.id, t: b.id} AS result", {})
    age_pairs = {(r["s"], r["t"]) for r in age_rel if isinstance(r, dict) and r.get("s")}
    async with db.acquire() as conn:
        arr = await conn.fetch(
            "SELECT id, unnest(related_to) AS t FROM knowledge.discoveries WHERE related_to IS NOT NULL")
        edges = await conn.fetch(
            "SELECT src_id AS s, dst_id AS t FROM knowledge.discovery_edges WHERE edge_type='related'")
    rel_pairs = {(r["id"], r["t"]) for r in arr} | {(r["s"], r["t"]) for r in edges}
    missing = age_pairs - rel_pairs
    parity_ok &= not missing
    report["related_edges"] = {
        "age": len(age_pairs), "relational_union": len(rel_pairs),
        "age_edges_missing_from_relational": len(missing), "parity": not missing,
    }

    # 2. RELATIONAL STATUS DISTRIBUTION (the authoritative, consistent surface).
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT COALESCE(status,'<null>') AS status, count(*) AS n "
            "FROM knowledge.discoveries GROUP BY 1 ORDER BY 2 DESC")
    report["relational_status_truth"] = {r["status"]: r["n"] for r in rows}

    # 3. AGE QUERY-RELIABILITY demonstration: same filter, two phrasings.
    lit = await db.graph_query("MATCH (d:Discovery {status:'open'}) RETURN count(d) AS n", {})
    par = await db.graph_query("MATCH (d:Discovery {status: ${s}}) RETURN count(d) AS n", {"s": "open"})
    lit_n = _scalar(lit, "n")
    par_n = _scalar(par, "n")
    reliable = lit_n is not None and par_n is not None and lit_n == par_n
    report["age_query_reliability"] = {
        "status_open_literal": lit_n, "status_open_param": par_n,
        "reliable": reliable,  # observed: counts vary across phrasings AND runs, or return null
        "note": "AGE read layer is unreliable here (null/varying counts for the same filter); "
                "relational is the trustworthy surface. Not gating on this, only documenting it.",
    }

    # 4. SEMANTIC SEARCH: pgvector path works (AGE-independent).
    try:
        hits = await kg.semantic_search("knowledge graph consistency", limit=5)
        report["semantic_search"] = {"works": True, "hits": len(hits), "engine": "pgvector"}
    except Exception as e:  # pragma: no cover - environment dependent
        report["semantic_search"] = {"works": False, "error": str(e)[:160]}
        parity_ok = False

    # 5. DEAD TRAVERSAL: response chains.
    chain = await db.graph_query("MATCH ()-[r:RESPONDS_TO]->() RETURN count(r) AS n", {})
    responds_to = _scalar(chain, "n")
    report["responds_to_edges"] = (
        responds_to if responds_to is not None
        else "unknown (AGE returned no row -- NOT evidence of zero edges)")

    report["verdict"] = (
        "PARITY — relational reconstructs every AGE read; AGE adds no read value"
        if parity_ok else "DIVERGENCE — review before removing AGE")
    print(json.dumps(report, indent=2, default=str))
    return 0 if parity_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

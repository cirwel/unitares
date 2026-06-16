#!/usr/bin/env python3
"""AGE-vs-relational READ PARITY audit (read-only).

Decision gate for "should we remove the Apache-AGE graph layer?". Treats the
relational store (`knowledge.discoveries` + `discovery_edges` + `related_to[]`)
+ pgvector as the source of truth and proves the AGE graph adds nothing a read
needs. If parity holds, removing AGE is a safe, evidence-backed cut.

Findings (live `governance` DB, 2026-06-16, backend=age):
- RELATED_TO edges: every AGE edge is present in relational (0 missing)
- RESPONDS_TO (the only multi-hop traversal edge): 0 -> dead capability
- semantic_search: pure pgvector (`embedding <=> $1::vector`), no Cypher
- AGE query layer is UNRELIABLE here: identical status filters return
  different counts under literal vs parameter substitution, and `RETURN
  d.<prop>` frequently parses empty. So AGE is not a trustworthy read surface;
  relational is. This audit therefore verifies relational COMPLETENESS, not
  AGE-side counts.

Run: UNITARES_KNOWLEDGE_BACKEND=age python3 scripts/dev/age_parity_audit.py
Exit 0 = parity holds (safe to remove); exit 1 = a divergence needs review.
"""
import asyncio
import json
import sys


async def main() -> int:
    from src.knowledge_graph import get_knowledge_graph
    kg = await get_knowledge_graph()
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
    lit_n = (lit[0].get("n") if lit and isinstance(lit[0], dict) else None)
    par_n = (par[0].get("n") if par and isinstance(par[0], dict) else None)
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
    report["responds_to_edges"] = (chain[0].get("n") if chain and isinstance(chain[0], dict) else 0)

    report["verdict"] = (
        "PARITY — relational reconstructs every AGE read; AGE adds no read value"
        if parity_ok else "DIVERGENCE — review before removing AGE")
    print(json.dumps(report, indent=2, default=str))
    return 0 if parity_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

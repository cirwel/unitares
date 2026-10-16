#!/usr/bin/env python3
"""Freeze read-only retrieval inputs, then replay the actual search handler offline.

The private snapshot contains KG text and vectors. Only aggregate/rank evidence
belongs in git. No embeddings are computed and no production settings change.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import sys
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.eval.kg_embedding_compare import digest, known_item_metrics, write_json  # noqa: E402


def read_labels(path):
    pairs = json.loads(Path(path).read_text())["pairs"]
    if not pairs or len({p["query_id"] for p in pairs}) != len(pairs):
        raise ValueError("empty or duplicate query labels")
    return pairs


async def capture(args):
    import asyncpg
    import numpy as np
    from src.db.mixins.knowledge_graph import _apply_operator

    if Path(args.output).exists():
        raise ValueError("refusing to overwrite a frozen snapshot")
    labels = read_labels(args.labels)
    meta = json.loads(Path(args.vectors).with_suffix(".json").read_text())
    if meta["labels_sha256"] != digest(args.labels) or meta["model"] != "bge":
        raise ValueError("expected BGE query vectors for these exact labels")
    with np.load(args.vectors, allow_pickle=False) as data:
        if len(data["queries"]) != len(labels):
            raise ValueError("query vector count does not match labels")
        query_vectors = data["queries"].tolist()

    conn = await asyncpg.connect(database=args.database, command_timeout=120)
    out = {"labels_sha256": digest(args.labels), "query_vectors_sha256": digest(args.vectors),
           "query_vector_metadata": meta, "query_vectors": query_vectors, "queries": []}
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            out["captured_at"] = (await conn.fetchval("SELECT transaction_timestamp()")).isoformat()
            # Match asyncpg's UTC timestamp serialization in the serving backend.
            await conn.execute("SET LOCAL TIME ZONE 'UTC'")
            await conn.execute("SET LOCAL search_path = ag_catalog, public")
            rows = await conn.fetch("""
              SELECT row_to_json(d)::text AS document, e.embedding::text AS embedding
              FROM knowledge.discoveries d
              LEFT JOIN core.discovery_embeddings_bge_m3 e ON e.discovery_id=d.id
              ORDER BY d.id
            """)
            out["documents"] = [dict(json.loads(r["document"]), bge_vector=r["embedding"]) for r in rows]
            counts = await conn.fetch("""
              WITH rel AS (SELECT e.end_id, count(DISTINCT e.start_id) n FROM governance_graph."RELATED_TO" e
                JOIN governance_graph."Discovery" s ON s.id=e.start_id GROUP BY e.end_id),
              resp AS (SELECT e.end_id, count(DISTINCT e.start_id) n FROM governance_graph."RESPONDS_TO" e
                JOIN governance_graph."Discovery" s ON s.id=e.start_id GROUP BY e.end_id),
              sup AS (SELECT e.end_id, count(DISTINCT e.start_id) n FROM governance_graph."SUPERSEDES" e
                JOIN governance_graph."Discovery" s ON s.id=e.start_id GROUP BY e.end_id)
              SELECT d.properties::text::jsonb->>'id' AS id,
                     coalesce(rel.n,0) AS related, coalesce(resp.n,0) AS responds,
                     coalesce(sup.n,0) AS superseded_by
              FROM governance_graph."Discovery" d
              LEFT JOIN rel ON rel.end_id=d.id LEFT JOIN resp ON resp.end_id=d.id
              LEFT JOIN sup ON sup.end_id=d.id
            """)
            out["connectivity_counts"] = [dict(r) for r in counts]
            out["hnsw_ef_search"] = await conn.fetchval("SHOW hnsw.ef_search")
            out["hnsw_iterative_scan"] = await conn.fetchval("SHOW hnsw.iterative_scan")
            sql = """SELECT discovery_id AS id, 1-(embedding <=> $1::vector) AS score
              FROM core.discovery_embeddings_bge_m3
              WHERE embedding IS NOT NULL AND 1-(embedding <=> $1::vector)>=0.3
              ORDER BY embedding <=> $1::vector LIMIT 50"""
            for i, pair in enumerate(labels):
                vector = json.dumps(query_vectors[i])
                if i == 0:
                    out["ann_plan"] = [r[0] for r in await conn.fetch("EXPLAIN " + sql, vector)]
                ann = [dict(r) for r in await conn.fetch(sql, vector)]
                lexical = {}
                for op in ("AND", "OR"):
                    found = await conn.fetch("""
                      SELECT id, ts_rank_cd(search_vector,websearch_to_tsquery('english',$1),32) AS score
                      FROM knowledge.discoveries
                      WHERE search_vector @@ websearch_to_tsquery('english',$1)
                      ORDER BY score DESC, created_at DESC, id
                    """, _apply_operator(pair["query"], op))
                    lexical[op] = [dict(r) for r in found]
                out["queries"].append({"query_id": pair["query_id"], "ann": ann, "fts": lexical})
    finally:
        await conn.close()
    out["git_sha"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    write_json(args.output, out)
    print(json.dumps({"snapshot": args.output, "rows": len(out["documents"]),
                      "queries": len(labels), "sha256": digest(args.output)}))


VARIANTS = {
    "dense_visible": dict(early_visible=True, connectivity=0, age=False, status=False, fusion=False, authority=False),
    "dense_late_visible": dict(connectivity=0, age=False, status=False, fusion=False, authority=False),
    "connectivity_only": dict(connectivity=.3, age=False, status=False, fusion=False, authority=False),
    "status_only": dict(connectivity=0, age=False, status=True, fusion=False, authority=False),
    "age_only": dict(connectivity=0, age=True, status=False, fusion=False, authority=False),
    "all_backend_weights": dict(fusion=False, authority=False),
    "weights_auto_fusion": dict(authority=False),
    "production_exact": {},
    "production_ann": dict(ann=True),
    "early_visibility": dict(early_visible=True),
    "without_status": dict(status=False),
    "without_age": dict(age=False),
    "without_connectivity": dict(connectivity=0),
    "without_backend_weights": dict(connectivity=0, age=False, status=False),
    "without_backend_weights_early_visible": dict(connectivity=0, age=False, status=False, early_visible=True),
    "without_fusion": dict(fusion=False),
    "handler_current": dict(ann=True),
    "handler_current_recency30": dict(ann=True, recency_half_life_days=30),
}


def visible(row):
    return row["status"] not in {"archived", "cold"}


async def replay(args):
    import numpy as np
    from src.storage.knowledge_graph_age import KnowledgeGraphAGE
    from src.mcp_handlers.knowledge import handlers

    snapshot = json.loads(Path(args.snapshot).read_text())
    pairs = read_labels(args.labels)
    if snapshot["labels_sha256"] != digest(args.labels):
        raise ValueError("labels changed after snapshot")
    if [q["query_id"] for q in snapshot["queries"]] != [p["query_id"] for p in pairs]:
        raise ValueError("snapshot query order mismatch")
    documents = {d["id"]: d for d in snapshot["documents"]}
    if len(documents) != len(snapshot["documents"]):
        raise ValueError("duplicate corpus IDs")
    for p in pairs:
        if not p["relevant_ids"] or any(d not in documents or not visible(documents[d]) for d in p["relevant_ids"]):
            raise ValueError(f"missing/out-of-scope target for {p['query_id']}")
    vector_rows = [d for d in snapshot["documents"] if d.get("bge_vector")]
    vectors = np.asarray([json.loads(d["bge_vector"]) for d in vector_rows], dtype=np.float64)
    queries = np.asarray(snapshot["query_vectors"], dtype=np.float64)
    if (not np.isfinite(vectors).all() or not np.isfinite(queries).all()
            or np.any(np.linalg.norm(vectors, axis=1) == 0)
            or np.any(np.linalg.norm(queries, axis=1) == 0)):
        raise ValueError("invalid vectors")
    similarities = (queries / np.linalg.norm(queries, axis=1, keepdims=True)) @ (
        vectors / np.linalg.norm(vectors, axis=1, keepdims=True)).T
    frozen_now = datetime.fromisoformat(snapshot["captured_at"])
    local_now = frozen_now.astimezone(ZoneInfo(args.server_timezone)).replace(tzinfo=None)

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            # Existing backend subtracts naive local now from a UTC timestamp
            # with its offset removed; preserve that behavior in this replay.
            return frozen_now.astimezone(tz) if tz else local_now

    class FrozenDB:
        async def graph_available(self):
            return True

        async def graph_query(self, query, params):
            return [r for r in snapshot["connectivity_counts"] if r["id"] in params["ids"]]

    class FrozenGraph(KnowledgeGraphAGE):
        def __init__(self, index, options):
            super().__init__()
            self.index, self.options = index, options

        async def _get_db(self):
            return FrozenDB()

        def node(self, docid):
            return self._dict_to_discovery(documents[docid])

        async def semantic_search(self, query, limit=50, min_similarity=.3, **kwargs):
            if self.options.get("ann"):
                ranked = [(r["id"], r["score"]) for r in snapshot["queries"][self.index]["ann"]]
            else:
                order = np.argsort(-similarities[self.index], kind="stable")
                ranked = [(vector_rows[int(i)]["id"], float(similarities[self.index, i])) for i in order
                          if similarities[self.index, i] >= min_similarity]
            if self.options.get("early_visible"):
                ranked = [(d, s) for d, s in ranked if visible(documents[d])]
            raw = [(self.node(d), s) for d, s in ranked[:limit] if d in documents]
            return await self._blend_with_connectivity(
                raw, connectivity_weight=self.options.get("connectivity", kwargs.get("connectivity_weight", .3)),
                exclude_orphans=False, limit=limit,
                temporal_decay=self.options.get("age", kwargs.get("temporal_decay", True)),
                status_weight=self.options.get("status", kwargs.get("status_weight", True)),
            )

        async def full_text_search(self, query, limit=50, operator="AND", **kwargs):
            rows = snapshot["queries"][self.index]["fts"][operator]
            if self.options.get("early_visible"):
                rows = [r for r in rows if visible(documents[r["id"]])]
            result = []
            for r in rows[:limit]:
                node = self.node(r["id"])
                node.relevance = r["score"]
                result.append(node)
            return result

    output = {"snapshot_sha256": digest(args.snapshot), "labels_sha256": digest(args.labels),
              "captured_at": snapshot["captured_at"], "corpus_rows": len(documents),
              "visible_rows": sum(visible(d) for d in documents.values()),
              "git_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
              "hnsw_ef_search": snapshot["hnsw_ef_search"],
              "pgvector_plan": [re.sub(r"'\[[^]]+\]'::vector", "'<query vector>'::vector", line)
                                for line in snapshot["ann_plan"]],
              "server_timezone": args.server_timezone,
              "source_sha256": {p: digest(ROOT / p) for p in (
                  "src/mcp_handlers/knowledge/handlers.py", "src/storage/knowledge_graph_age.py",
                  "src/knowledge_authority.py", "src/retrieval.py", "scripts/eval/kg_serving_ablation.py")},
              "variants": {}}
    with patch("src.storage.knowledge_graph_age.datetime", FrozenClock), \
         patch("src.mcp_handlers.knowledge.handlers.datetime", FrozenClock), \
         patch("src.retrieval.hybrid_enabled", return_value=True), \
         patch("src.retrieval.graph_expansion_enabled", return_value=False), \
         patch("src.reranker.reranker_enabled", return_value=False):
        for name, options in VARIANTS.items():
            # Preserve pre-fix production weights as the experimental baseline.
            # Only handler_current follows the actual checked-out caller flags.
            if not name.startswith("handler_current"):
                options = {"connectivity": .3, "age": True, "status": True, **options}
            per_query = []
            for index, pair in enumerate(pairs):
                # Keep authority's 50-row fetch window fixed even when its
                # ranking is removed: that isolates scoring from pool size.
                request = handlers._parse_knowledge_search_request({
                    "query": pair["query"], "limit": 20,
                    "search_mode": "semantic" if options.get("fusion") is False else "auto",
                    "recency_half_life_days": options.get("recency_half_life_days"),
                })
                graph = FrozenGraph(index, options)
                state = handlers._KnowledgeSearchState(request, graph)
                with patch("src.mcp_handlers.knowledge.handlers.rank_by_authority",
                           side_effect=(lambda docs, **kw: (list(docs), False)) if options.get("authority") is False
                           else handlers.rank_by_authority):
                    await handlers._run_text_search(state)
                    await handlers._apply_semantic_fts_fallback(state)
                ranking = [d.id for d in state.results]
                per_query.append({"query_id": pair["query_id"], "topic": pair["topic"],
                                  "origin": pair["origin"], "mode": state.search_mode,
                                  "candidate_count": len(state.candidates),
                                  "visible_candidates": sum(d.status not in {"archived", "cold"} for d in state.candidates),
                                  **known_item_metrics(ranking, pair["relevant_ids"]), "top20": ranking})
            aggregate = {}
            for origin in ("all", "logged", "authored_paraphrase"):
                group = [r for r in per_query if origin == "all" or r["origin"] == origin]
                if not group:
                    continue
                aggregate[origin] = {"n": len(group), **{
                    metric: sum(r[metric] for r in group) / len(group)
                    for metric in ("hit@1", "hit@5", "hit@10", "recall@20", "mrr@10", "ndcg@10")}}
            output["variants"][name] = {"options": options, "aggregate": aggregate, "per_query": per_query}
            print(name, json.dumps(aggregate["all"]), flush=True)
    write_json(args.output, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    cap = sub.add_parser("capture")
    cap.add_argument("--database", default="governance")
    cap.add_argument("--vectors", required=True)
    cap.add_argument("--labels", required=True)
    cap.add_argument("--output", required=True)
    rep = sub.add_parser("replay")
    rep.add_argument("--snapshot", required=True)
    rep.add_argument("--labels", required=True)
    rep.add_argument("--output", required=True)
    rep.add_argument("--server-timezone", default="UTC", help="IANA timezone used by the serving process's naive clock")
    args = parser.parse_args()
    asyncio.run(capture(args) if args.command == "capture" else replay(args))


if __name__ == "__main__":
    main()

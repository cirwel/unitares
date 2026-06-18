#!/usr/bin/env python3
"""
KG retrieval quality eval — measures nDCG@10, Recall@20, MRR, latency against
a labeled (query, relevant_ids) corpus. Objective floor for the Phase 2-5 rebuild
in docs/plans/2026-04-20-kg-retrieval-rebuild.md.

Usage:
    python scripts/eval/retrieval_eval.py
    python scripts/eval/retrieval_eval.py --labels tests/retrieval_eval/labels.json
    python scripts/eval/retrieval_eval.py --json > /tmp/baseline.json
    python scripts/eval/retrieval_eval.py --k 10 --recall-k 20 --limit-queries 5

Requires live Postgres + embeddings backend.
"""

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.eval.metrics import dcg, mrr, ndcg_at_k, recall_at_k  # noqa: F401  (dcg re-exported for callers)
from src.knowledge_graph import get_knowledge_graph


async def run_query(
    graph,
    query: str,
    top_k: int,
    rerank: bool = False,
    rerank_pool_size: int = 50,
    hybrid: bool = False,
    graph_expand: bool = False,
) -> tuple[List[str], List[float], float]:
    """Run a single query against the current retrieval stack.

    Returns (ids, scores, latency_ms). Supports:
    - `hybrid=True`: fan out semantic + FTS, fuse via RRF (k=60). Phase 4.
    - `graph_expand=True` (requires hybrid): pull 1-hop typed-edge neighbors
      from top seeds into the pool at a discounted RRF score. Phase 5.
    - `rerank=True`: apply cross-encoder to top `rerank_pool_size` candidates. Phase 3.
    - Combined: hybrid fuse → graph expand → rerank the fused/expanded pool.
    """
    import asyncio as _asyncio
    t0 = time.perf_counter()

    # First-stage retrieval
    if hybrid:
        from src.retrieval import rrf_fuse, expand_with_neighbors
        fetch = max(top_k, rerank_pool_size if rerank else 50)
        sem_task = graph.semantic_search(query, limit=fetch, min_similarity=0.0)
        fts_task = graph.full_text_search(query, limit=fetch)
        sem_res, fts_res = await _asyncio.gather(sem_task, fts_task)
        sem_ids = [d.id for d, _ in sem_res]
        fts_ids = [d.id for d in fts_res]
        fused = rrf_fuse([sem_ids, fts_ids], k=60)
        pool = {d.id: d for d, _ in sem_res}
        for d in fts_res:
            pool.setdefault(d.id, d)
        if graph_expand:
            seed_neighbors: Dict[str, set] = {}
            for seed_id, _ in fused[:10]:
                seed_doc = pool.get(seed_id)
                if seed_doc is None:
                    continue
                nbrs: set = set()
                nbrs.update(seed_doc.related_to or [])
                nbrs.update(getattr(seed_doc, "responses_from", None) or [])
                if seed_doc.response_to:
                    nbrs.add(seed_doc.response_to.discovery_id)
                nbrs.discard(seed_id)
                seed_neighbors[seed_id] = nbrs
            fused = expand_with_neighbors(
                fused, seed_neighbors, edge_weight=0.5, max_seeds=10,
            )
        fused_docs = [pool[did] for did, _ in fused if did in pool]
    else:
        pool_size = max(top_k, rerank_pool_size) if rerank else top_k
        first_stage = await graph.semantic_search(query, limit=pool_size, min_similarity=0.0)
        fused_docs = [d for d, _ in first_stage]
        fused = [(d.id, s) for d, s in first_stage]

    # Optional rerank on the first-stage top
    if rerank and fused_docs:
        from src.reranker import rerank as _rerank
        pairs = [
            (d.id, f"{d.summary}\n{(d.details or '')[:2000]}")
            for d in fused_docs
        ]
        reranked = await _rerank(query, pairs, top_k=top_k, max_rerank_size=rerank_pool_size)
        ranked_ids = [doc_id for doc_id, _ in reranked]
        scores = [float(score) for _, score in reranked]
    else:
        ranked_ids = [did for did, _ in fused[:top_k]]
        scores = [float(s) for _, s in fused[:top_k]]

    dt_ms = (time.perf_counter() - t0) * 1000.0
    return ranked_ids, scores, dt_ms


async def evaluate(
    labels_path: Path,
    ndcg_k: int = 10,
    recall_k: int = 20,
    top_k_fetch: int = 20,
    limit_queries: int | None = None,
    rerank: bool = False,
    rerank_pool_size: int = 50,
    hybrid: bool = False,
    graph_expand: bool = False,
) -> Dict[str, Any]:
    with labels_path.open() as f:
        corpus = json.load(f)

    pairs = corpus["pairs"]
    if limit_queries:
        pairs = pairs[:limit_queries]

    graph = await get_knowledge_graph()

    per_query: List[Dict[str, Any]] = []
    ndcgs, recalls, mrrs, latencies = [], [], [], []

    for pair in pairs:
        query = pair["query"]
        relevant = set(pair["relevant_ids"])
        ranked, scores, dt_ms = await run_query(
            graph,
            query,
            max(top_k_fetch, recall_k),
            rerank=rerank,
            rerank_pool_size=rerank_pool_size,
            hybrid=hybrid,
            graph_expand=graph_expand,
        )
        ndcg = ndcg_at_k(ranked, relevant, ndcg_k)
        rec = recall_at_k(ranked, relevant, recall_k)
        m = mrr(ranked, relevant)
        top_score = scores[0] if scores else 0.0
        first_hit_rank = next(
            (i + 1 for i, rid in enumerate(ranked) if rid in relevant), None
        )
        per_query.append({
            "query": query,
            "relevant_ids": sorted(relevant),
            "top_ranked_ids": ranked[:5],
            "top_scores": [round(s, 3) for s in scores[:5]],
            "ndcg@10": round(ndcg, 3),
            f"recall@{recall_k}": round(rec, 3),
            "mrr": round(m, 3),
            "first_hit_rank": first_hit_rank,
            "top_score": round(top_score, 3),
            "latency_ms": round(dt_ms, 1),
        })
        ndcgs.append(ndcg)
        recalls.append(rec)
        mrrs.append(m)
        latencies.append(dt_ms)

    def agg(values: List[float]) -> Dict[str, float]:
        if not values:
            return {}
        return {
            "mean": round(statistics.fmean(values), 3),
            "median": round(statistics.median(values), 3),
            "min": round(min(values), 3),
            "max": round(max(values), 3),
        }

    def percentiles(values: List[float]) -> Dict[str, float]:
        if not values:
            return {}
        s = sorted(values)
        def pct(p: float) -> float:
            idx = min(int(p * len(s)), len(s) - 1)
            return s[idx]
        return {
            "p50": round(pct(0.50), 1),
            "p95": round(pct(0.95), 1),
            "max": round(max(s), 1),
        }

    # Report the path relative to the repo root so pinned baselines don't
    # capture worktree-specific absolute paths.
    repo_root = Path(__file__).resolve().parents[2]
    try:
        corpus_rel_path = str(labels_path.resolve().relative_to(repo_root))
    except ValueError:
        corpus_rel_path = str(labels_path)

    return {
        "corpus": {
            "path": corpus_rel_path,
            "pair_count": len(pairs),
            "schema_version": corpus.get("schema_version"),
        },
        "config": {
            "ndcg_k": ndcg_k,
            "recall_k": recall_k,
            "top_k_fetch": top_k_fetch,
            "rerank": rerank,
            "rerank_pool_size": rerank_pool_size if rerank else None,
            "hybrid": hybrid,
            "graph_expand": graph_expand,
        },
        "aggregate": {
            f"ndcg@{ndcg_k}": agg(ndcgs),
            f"recall@{recall_k}": agg(recalls),
            "mrr": agg(mrrs),
            "latency_ms": percentiles(latencies),
        },
        "per_query": per_query,
    }


def print_human(result: Dict[str, Any]) -> None:
    agg = result["aggregate"]
    cfg = result["config"]
    ndcg_key = f"ndcg@{cfg['ndcg_k']}"
    recall_key = f"recall@{cfg['recall_k']}"
    ndcg = agg[ndcg_key]
    rec = agg[recall_key]
    mrr_agg = agg["mrr"]
    lat = agg["latency_ms"]

    print(f"\nKG retrieval eval — {result['corpus']['pair_count']} queries\n")
    print(f"  {ndcg_key:<10} mean {ndcg['mean']:.3f}  median {ndcg['median']:.3f}")
    print(f"  {recall_key:<10} mean {rec['mean']:.3f}  median {rec['median']:.3f}")
    print(f"  MRR        mean {mrr_agg['mean']:.3f}  median {mrr_agg['median']:.3f}")
    print(f"  Latency    p50 {lat['p50']}ms  p95 {lat['p95']}ms  max {lat['max']}ms\n")

    print("Per-query detail:")
    print(f"  {'query':<42}  ndcg  recall  mrr    rank  top_score  latency")
    for q in result["per_query"]:
        rank = q["first_hit_rank"] if q["first_hit_rank"] is not None else "—"
        print(
            f"  {q['query'][:42]:<42}  "
            f"{q['ndcg@10']:.2f}  "
            f"{q[recall_key]:.2f}    "
            f"{q['mrr']:.2f}   "
            f"{str(rank):<4}  "
            f"{q['top_score']:.3f}      "
            f"{q['latency_ms']}ms"
        )


def main():
    parser = argparse.ArgumentParser(description="KG retrieval quality eval")
    parser.add_argument("--labels", type=Path,
                        default=Path(__file__).resolve().parents[2] / "tests/retrieval_eval/labels.json")
    parser.add_argument("--k", "--ndcg-k", dest="ndcg_k", type=int, default=10)
    parser.add_argument("--recall-k", type=int, default=20)
    parser.add_argument("--top-k-fetch", type=int, default=20)
    parser.add_argument("--limit-queries", type=int, default=None)
    parser.add_argument("--rerank", action="store_true",
                        help="Apply cross-encoder reranker to the first-stage top-K")
    parser.add_argument("--rerank-pool-size", type=int, default=50)
    parser.add_argument("--hybrid", action="store_true",
                        help="Run hybrid RRF fusion (semantic + FTS)")
    parser.add_argument("--graph-expand", action="store_true",
                        help="After RRF, pull 1-hop typed-edge neighbors into the pool (requires --hybrid)")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of human-readable output")
    args = parser.parse_args()

    result = asyncio.run(evaluate(
        labels_path=args.labels,
        ndcg_k=args.ndcg_k,
        recall_k=args.recall_k,
        top_k_fetch=max(args.top_k_fetch, args.recall_k),
        limit_queries=args.limit_queries,
        rerank=args.rerank,
        rerank_pool_size=args.rerank_pool_size,
        hybrid=args.hybrid,
        graph_expand=args.graph_expand,
    ))

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print_human(result)


if __name__ == "__main__":
    main()

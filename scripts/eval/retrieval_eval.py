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
from contextlib import contextmanager
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from scripts.eval.metrics import dcg, mrr, ndcg_at_k, recall_at_k  # noqa: F401  (dcg re-exported for callers)
from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph


def _parse_handler_response(result: Any) -> Dict[str, Any]:
    """Parse a handler TextContent response into a JSON dict."""
    if isinstance(result, (list, tuple)):
        result = result[0]
    return json.loads(result.text)


@contextmanager
def _temporary_env(overrides: Dict[str, str]):
    """Temporarily apply env flags so CLI knobs hit the serving handler path."""
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _serving_search(
    query: str,
    limit: int,
    *,
    rerank: bool = False,
    hybrid: bool = False,
    graph_expand: bool = False,
) -> Dict[str, Any]:
    """Run the same search handler used by knowledge(action='search')."""
    arguments: Dict[str, Any] = {
        "query": query,
        "limit": limit,
    }
    env: Dict[str, str] = {}

    if hybrid or graph_expand:
        arguments["search_mode"] = "hybrid"
        env["UNITARES_ENABLE_HYBRID"] = "1"
    if graph_expand:
        env["UNITARES_ENABLE_GRAPH_EXPANSION"] = "1"
    if rerank:
        env["UNITARES_ENABLE_RERANKER"] = "1"

    with _temporary_env(env):
        payload = _parse_handler_response(await handle_search_knowledge_graph(arguments))

    if not payload.get("success"):
        raise RuntimeError(payload.get("error") or payload.get("message") or "knowledge search failed")
    return payload


async def run_query(
    query: str,
    top_k: int,
    rerank: bool = False,
    rerank_pool_size: int = 50,
    hybrid: bool = False,
    graph_expand: bool = False,
) -> tuple[List[str], List[float], float]:
    """Run a single query against the current serving retrieval stack.

    Returns (ids, scores, latency_ms). Supports:
    - default: live knowledge(action='search') routing for the active backend.
    - `hybrid=True`: force the handler's hybrid mode.
    - `graph_expand=True` (requires hybrid): enable the serving graph-expansion flag.
    - `rerank=True`: enable the serving reranker flag.
    """
    t0 = time.perf_counter()

    # The old eval called graph.semantic_search() directly, which only exists
    # on the AGE backend. The default backend is PostgreSQL FTS, and the live
    # tool surface routes through handle_search_knowledge_graph(), so the eval
    # must measure that path instead of a backend-private method.
    payload = await _serving_search(
        query,
        limit=max(top_k, rerank_pool_size if rerank else top_k),
        rerank=rerank,
        hybrid=hybrid,
        graph_expand=graph_expand,
    )
    ranked_ids = [
        str(discovery["id"])
        for discovery in payload.get("discoveries", [])[:top_k]
        if discovery.get("id") is not None
    ]

    score_map = (
        payload.get("rerank_scores")
        or payload.get("rrf_scores")
        or payload.get("similarity_scores")
        or {}
    )
    # PostgreSQL FTS does not expose a calibrated score through the handler.
    # Metrics use rank only, so use reciprocal rank as a display placeholder.
    scores = [
        float(score_map.get(doc_id, 1.0 / (idx + 1)))
        for idx, doc_id in enumerate(ranked_ids)
    ]

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

    per_query: List[Dict[str, Any]] = []
    ndcgs, recalls, mrrs, latencies = [], [], [], []

    for pair in pairs:
        query = pair["query"]
        relevant = set(pair["relevant_ids"])
        ranked, scores, dt_ms = await run_query(
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

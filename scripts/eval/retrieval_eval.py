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
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
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


@dataclass
class QueryRun:
    """Backward-compatible 3-value query result plus source diagnostics."""

    ranked_ids: List[str]
    scores: List[float]
    latency_ms: float
    source_diagnostics: Dict[str, Any]

    def __iter__(self):
        # Preserve existing ``ranked, scores, latency = await run_query(...)``.
        yield self.ranked_ids
        yield self.scores
        yield self.latency_ms


def _source_partition(discovery: Dict[str, Any]) -> str:
    """Classify a result by its explicit source tag, or the native KG lane."""
    for tag in discovery.get("tags") or []:
        tag = str(tag)
        if tag.startswith("source-"):
            return tag
    return "native"


def _source_diagnostics(discoveries: List[Dict[str, Any]]) -> Dict[str, Any]:
    counts = Counter(_source_partition(item) for item in discoveries)
    total = sum(counts.values())
    dominant = max(counts.values(), default=0)
    return {
        "partition_counts": dict(sorted(counts.items())),
        "unique_partitions": len(counts),
        "dominant_partition_share": round(dominant / total, 3) if total else 0.0,
        "claude_memory_share": round(
            counts.get("source-claude-memory", 0) / total, 3
        ) if total else 0.0,
    }


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
    include_archived: bool = False,
    include_cold: bool = False,
) -> Dict[str, Any]:
    """Run the same search handler used by knowledge(action='search')."""
    arguments: Dict[str, Any] = {
        "query": query,
        "limit": limit,
    }
    # Must travel with the scope classification in label_scope_report, or a
    # widened scope would mark a label reachable that the search still drops.
    if include_archived:
        arguments["include_archived"] = True
    if include_cold:
        arguments["include_cold"] = True
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


# A labeled row that lifecycle policy has moved out of the scope being measured
# cannot be retrieved by definition, so scoring it as a ranking miss is a lie.
# These are the statuses the default search scope drops.
_OUT_OF_SCOPE_STATUS = {"archived": "include_archived", "cold": "include_cold"}


async def label_scope_report(
    pairs: List[Dict[str, Any]],
    *,
    include_archived: bool = False,
    include_cold: bool = False,
) -> Dict[str, Any]:
    """Classify every labeled id by whether the measured scope can reach it.

    The eval scores against ground truth hand-labeled at a point in time, but
    the labeled rows are ordinary discoveries that lifecycle policy keeps
    archiving. When a target leaves scope the query becomes unscorable, not a
    retrieval failure, and reporting it as a flat miss manufactures a quality
    regression out of routine housekeeping. Observed 2026-09-09: two of five
    queries lost their only target to archival on 2026-09-08, and the gate's
    recall fell 0.567 -> 0.167 with no change to the retrieval stack.

    Uses the configured backend, so this reads whatever store is actually
    serving (``UNITARES_KNOWLEDGE_BACKEND``), not a store we assume.
    """
    from src.knowledge_graph import get_knowledge_graph

    reachable = {"open", "resolved", "superseded", "closed"}
    if include_archived:
        reachable.add("archived")
    if include_cold:
        reachable.add("cold")

    graph = await get_knowledge_graph()
    statuses: Dict[str, str] = {}
    for label_id in sorted({i for pair in pairs for i in pair["relevant_ids"]}):
        try:
            node = await graph.get_discovery(label_id)
            if node is None:
                status = "missing"
            else:
                raw = getattr(node, "status", None)
                status = raw if isinstance(raw, str) and raw else "unknown"
        except Exception:
            status = "lookup_failed"
        statuses[label_id] = status

    # Fail OPEN. Excluding a query is a strong action, and a lookup that broke
    # is not evidence that a row left scope. Only a status we actually read and
    # know to be unreachable may drop a query from the aggregate.
    undetermined = {"lookup_failed", "unknown"}
    decayed = {
        label_id: status
        for label_id, status in statuses.items()
        if status not in reachable and status not in undetermined
    }
    by_status: Dict[str, int] = {}
    for status in statuses.values():
        by_status[status] = by_status.get(status, 0) + 1

    return {
        "labeled_id_count": len(statuses),
        "by_status": dict(sorted(by_status.items())),
        "reachable_in_scope": sorted(reachable),
        "out_of_scope": dict(sorted(decayed.items())),
        "out_of_scope_count": len(decayed),
        "statuses": statuses,
    }


async def run_query(
    query: str,
    top_k: int,
    rerank: bool = False,
    rerank_pool_size: int = 50,
    hybrid: bool = False,
    graph_expand: bool = False,
    include_archived: bool = False,
    include_cold: bool = False,
) -> QueryRun:
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
        include_archived=include_archived,
        include_cold=include_cold,
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
    ranked_discoveries = [
        discovery
        for discovery in payload.get("discoveries", [])[:top_k]
        if isinstance(discovery, dict) and discovery.get("id") is not None
    ]
    return QueryRun(
        ranked_ids=ranked_ids,
        scores=scores,
        latency_ms=dt_ms,
        source_diagnostics=_source_diagnostics(ranked_discoveries),
    )


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
    include_archived: bool = False,
    include_cold: bool = False,
) -> Dict[str, Any]:
    with labels_path.open() as f:
        corpus = json.load(f)

    pairs = corpus["pairs"]
    if limit_queries:
        pairs = pairs[:limit_queries]

    label_health = await label_scope_report(
        pairs, include_archived=include_archived, include_cold=include_cold
    )
    out_of_scope = label_health["out_of_scope"]

    per_query: List[Dict[str, Any]] = []
    ndcgs, recalls, mrrs, latencies = [], [], [], []
    source_partition_counts: List[float] = []
    dominant_source_shares: List[float] = []
    claude_memory_shares: List[float] = []

    unscorable: List[Dict[str, Any]] = []

    for pair in pairs:
        query = pair["query"]
        relevant = set(pair["relevant_ids"])
        in_scope = relevant - set(out_of_scope)
        if not in_scope:
            # Every target has left the measured scope. Scoring this as a miss
            # would attribute lifecycle housekeeping to the ranker.
            unscorable.append({
                "query": query,
                "relevant_ids": sorted(relevant),
                "reason": "all labeled targets are out of the measured scope",
                "target_status": {i: out_of_scope[i] for i in sorted(relevant)},
            })
            continue
        query_run = await run_query(
            query,
            max(top_k_fetch, recall_k),
            rerank=rerank,
            rerank_pool_size=rerank_pool_size,
            hybrid=hybrid,
            graph_expand=graph_expand,
            include_archived=include_archived,
            include_cold=include_cold,
        )
        ranked, scores, dt_ms = query_run
        source_diag = getattr(query_run, "source_diagnostics", {})
        # Score against the reachable targets only. A partially decayed query
        # still measures ranking; it just measures it over what is retrievable.
        ndcg = ndcg_at_k(ranked, in_scope, ndcg_k)
        rec = recall_at_k(ranked, in_scope, recall_k)
        m = mrr(ranked, in_scope)
        top_score = scores[0] if scores else 0.0
        first_hit_rank = next(
            (i + 1 for i, rid in enumerate(ranked) if rid in in_scope), None
        )
        per_query.append({
            "query": query,
            "relevant_ids": sorted(relevant),
            "scored_against_ids": sorted(in_scope),
            "out_of_scope_ids": sorted(relevant - in_scope),
            "top_ranked_ids": ranked[:5],
            "top_scores": [round(s, 3) for s in scores[:5]],
            "ndcg@10": round(ndcg, 3),
            f"recall@{recall_k}": round(rec, 3),
            "mrr": round(m, 3),
            "first_hit_rank": first_hit_rank,
            "flat_miss": first_hit_rank is None,
            "top_score": round(top_score, 3),
            "latency_ms": round(dt_ms, 1),
            "source_partitions": source_diag,
        })
        ndcgs.append(ndcg)
        recalls.append(rec)
        mrrs.append(m)
        latencies.append(dt_ms)
        if source_diag:
            source_partition_counts.append(float(source_diag["unique_partitions"]))
            dominant_source_shares.append(float(source_diag["dominant_partition_share"]))
            claude_memory_shares.append(float(source_diag["claude_memory_share"]))

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

    flat_miss_count = sum(1 for item in per_query if item["flat_miss"])

    return {
        "corpus": {
            "path": corpus_rel_path,
            "pair_count": len(pairs),
            "scored_pair_count": len(per_query),
            "unscorable_pair_count": len(unscorable),
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
            "include_archived": include_archived,
            "include_cold": include_cold,
        },
        # Read this before reading `aggregate`. A rising out_of_scope_count is
        # the label set decaying, not retrieval getting worse, and the two are
        # indistinguishable in the metrics alone.
        "label_health": label_health,
        "unscorable": unscorable,
        "aggregate": {
            f"ndcg@{ndcg_k}": agg(ndcgs),
            f"recall@{recall_k}": agg(recalls),
            "mrr": agg(mrrs),
            "latency_ms": percentiles(latencies),
            "flat_miss_count": flat_miss_count,
            "flat_miss_rate": round(flat_miss_count / len(per_query), 3) if per_query else 0.0,
            "source_diversity": {
                "mean_unique_partitions": round(
                    statistics.fmean(source_partition_counts), 3
                ) if source_partition_counts else None,
                "mean_dominant_partition_share": round(
                    statistics.fmean(dominant_source_shares), 3
                ) if dominant_source_shares else None,
                "mean_claude_memory_share": round(
                    statistics.fmean(claude_memory_shares), 3
                ) if claude_memory_shares else None,
            },
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

    corpus = result["corpus"]
    health = result.get("label_health", {})
    unscorable = result.get("unscorable", [])

    print(
        f"\nKG retrieval eval — {corpus['pair_count']} queries "
        f"({corpus.get('scored_pair_count', corpus['pair_count'])} scored)\n"
    )

    # Loud, and above the metrics, because a decayed label set makes the
    # numbers below look like a retrieval regression when nothing regressed.
    if health.get("out_of_scope_count"):
        print(
            f"  !! LABEL DECAY: {health['out_of_scope_count']} of "
            f"{health['labeled_id_count']} labeled rows are outside the measured scope."
        )
        for label_id, status in health["out_of_scope"].items():
            flag = _OUT_OF_SCOPE_STATUS.get(status)
            hint = f" (rescore with --{flag.replace('_', '-')})" if flag else ""
            print(f"       {label_id}  {status}{hint}")
        if unscorable:
            print(
                f"  !! {len(unscorable)} quer{'y is' if len(unscorable) == 1 else 'ies are'} "
                "unscorable and excluded from the aggregate:"
            )
            for item in unscorable:
                print(f"       {item['query']}")
        print("     These are not retrieval failures. Do not read them as a trend.\n")
    print(f"  {ndcg_key:<10} mean {ndcg['mean']:.3f}  median {ndcg['median']:.3f}")
    print(f"  {recall_key:<10} mean {rec['mean']:.3f}  median {rec['median']:.3f}")
    print(f"  MRR        mean {mrr_agg['mean']:.3f}  median {mrr_agg['median']:.3f}")
    print(
        f"  Flat miss  {agg['flat_miss_count']}/{corpus.get('scored_pair_count', corpus['pair_count'])} "
        f"({agg['flat_miss_rate']:.1%})"
    )
    print(f"  Latency    p50 {lat['p50']}ms  p95 {lat['p95']}ms  max {lat['max']}ms\n")
    source = agg["source_diversity"]
    if source["mean_unique_partitions"] is not None:
        print(
            "  Sources    mean unique "
            f"{source['mean_unique_partitions']:.2f}; dominant share "
            f"{source['mean_dominant_partition_share']:.1%}; Claude-memory share "
            f"{source['mean_claude_memory_share']:.1%}\n"
        )

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
    parser.add_argument("--include-archived", action="store_true",
                        help="Widen the measured scope to archived rows, so labels "
                             "archived since labeling stay scorable")
    parser.add_argument("--include-cold", action="store_true",
                        help="Widen the measured scope to cold-storage rows")
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
        include_archived=args.include_archived,
        include_cold=args.include_cold,
    ))

    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print_human(result)


if __name__ == "__main__":
    main()

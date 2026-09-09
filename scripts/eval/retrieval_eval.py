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


# The serving handler excludes exactly these two statuses when no explicit
# status filter is given (src/mcp_handlers/knowledge/handlers.py, the
# _candidate_status_visible predicate), each with its own opt-in. This is a
# DENYLIST, and mirroring it here is deliberate: the first cut of this code
# kept a parallel allowlist of reachable statuses, which meant any status
# nobody had thought of (a new lifecycle state, a case variant) was treated as
# unreachable. That is the same class of false signal this reporting exists to
# expose, only quieter, and there was no flag to recover from it.
_OUT_OF_SCOPE_STATUS = {"archived": "include_archived", "cold": "include_cold"}


async def label_scope_report(
    pairs: List[Dict[str, Any]],
    *,
    include_archived: bool = False,
    include_cold: bool = False,
) -> Dict[str, Any]:
    """Report which labeled rows the measured scope can still reach.

    This is a DIAGNOSTIC. It changes no score and drops no query. Ground truth
    is defined by reference into a store that keeps mutating underneath it, so
    a labeled row can be archived long after it was labeled; when that happens
    the scores stop meaning what a reader assumes, and the only honest fix is
    to say so out loud rather than to quietly adjust the denominator.

    An earlier version of this function did adjust the denominator, excluding
    decayed queries from the aggregate. Review killed it, correctly: on the
    weekly gate's 5-query sample it would have cut n from 5 to 3 and left every
    survivor with a single target, so recall and nDCG would have RISEN with no
    change to the ranker, appended to the same series file. Trading a false
    regression for a false improvement is not a fix. Run with
    --include-archived --include-cold to keep a constant denominator instead.
    """
    from src.knowledge_graph import get_knowledge_graph

    opted_in = {"include_archived": include_archived, "include_cold": include_cold}

    graph = await get_knowledge_graph()
    # Fail loudly on a programming error rather than 21 times quietly. The
    # per-label except below has to be broad, because a real backend outage
    # must not abort the run; that same breadth would swallow a renamed or
    # removed method and report every label as unresolvable, which reads as a
    # decayed corpus instead of as broken code.
    if not hasattr(graph, "get_discovery"):
        raise RuntimeError(
            f"{type(graph).__name__} has no get_discovery(); label scope cannot be read"
        )
    statuses: Dict[str, str] = {}
    for label_id in sorted({str(i) for pair in pairs for i in pair.get("relevant_ids") or []}):
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

    # Only a status we actually read AND that the serving predicate excludes
    # counts as decay. Everything else — a status we do not recognise, a failed
    # lookup, a row that has vanished — is undetermined, and undetermined is
    # never treated as decay. `missing` in particular is a broken label or the
    # wrong database, not a lifecycle decision, and reporting a whole corpus of
    # them as "decayed" would let a catastrophically misconfigured run read as
    # an orderly one.
    decayed = {
        label_id: status
        for label_id, status in statuses.items()
        if status in _OUT_OF_SCOPE_STATUS and not opted_in[_OUT_OF_SCOPE_STATUS[status]]
    }
    undetermined = {
        label_id: status
        for label_id, status in statuses.items()
        if status in {"missing", "unknown", "lookup_failed"}
    }
    by_status: Dict[str, int] = {}
    for status in statuses.values():
        by_status[status] = by_status.get(status, 0) + 1

    return {
        "labeled_id_count": len(statuses),
        "by_status": dict(sorted(by_status.items())),
        "out_of_scope": dict(sorted(decayed.items())),
        "out_of_scope_count": len(decayed),
        "undetermined": dict(sorted(undetermined.items())),
        "undetermined_count": len(undetermined),
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

    for pair in pairs:
        query = pair["query"]
        relevant = {str(i) for i in pair["relevant_ids"]}
        # Every labeled target stays in the denominator, decayed or not. See
        # label_scope_report: narrowing it per query made the aggregate rise
        # for a bookkeeping reason, which is a worse lie than the one it fixed.
        decayed_here = sorted(relevant & set(out_of_scope))
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
            # Named so a miss on this query can be read as decay rather than
            # as the ranker failing, without the score having moved.
            "out_of_scope_ids": decayed_here,
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
        "aggregate": {
            f"ndcg@{ndcg_k}": agg(ndcgs),
            f"recall@{recall_k}": agg(recalls),
            "mrr": agg(mrrs),
            "latency_ms": percentiles(latencies),
            "flat_miss_count": flat_miss_count,
            "flat_miss_rate": round(flat_miss_count / len(per_query), 3) if per_query else None,
            # Carried inside `aggregate` on purpose. The weekly gate logs this
            # dict and nothing else, so decay reported anywhere outside it
            # reaches no reader that exists.
            "labels_out_of_scope": label_health["out_of_scope_count"],
            "labels_undetermined": label_health["undetermined_count"],
            "labels_total": label_health["labeled_id_count"],
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
    corpus = result["corpus"]
    health = result.get("label_health", {})
    scored = corpus.get("scored_pair_count", corpus["pair_count"])

    print(f"\nKG retrieval eval — {corpus['pair_count']} queries ({scored} scored)\n")

    # Nothing scored means no metrics exist, not that they are zero. The first
    # cut printed straight into the aggregate here and died with KeyError on an
    # empty labels file or a corpus whose every label had decayed; worse, the
    # --json path stayed silent and emitted flat_miss_rate 0.0, so a run
    # against the wrong database recorded as a clean one.
    if not scored:
        print("  NO QUERIES SCORED — no metrics are available for this run.")
        print(f"  Labels: {health.get('labeled_id_count', 0)} total, "
              f"{health.get('undetermined_count', 0)} undetermined, "
              f"{health.get('out_of_scope_count', 0)} out of scope.")
        if health.get("undetermined_count"):
            print("  Undetermined labels usually mean the wrong database or backend,")
            print("  not a corpus that decayed. Check which store this process read.")
        return

    if health.get("undetermined_count"):
        print(
            f"  !! {health['undetermined_count']} of {health['labeled_id_count']} labels "
            "could not be resolved (missing / unknown / lookup failed)."
        )
        for label_id, status in list(health["undetermined"].items())[:10]:
            print(f"       {label_id}  {status}")
        print("     Scores below still count these as relevant. Treat them as suspect.\n")

    if health.get("out_of_scope_count"):
        print(
            f"  !! LABEL DECAY: {health['out_of_scope_count']} of "
            f"{health['labeled_id_count']} labeled rows are outside the measured scope."
        )
        for label_id, status in list(health["out_of_scope"].items())[:10]:
            flag = _OUT_OF_SCOPE_STATUS.get(status)
            hint = f" (rescore with --{flag.replace('_', '-')})" if flag else ""
            print(f"       {label_id}  {status}{hint}")
        print("     A miss on these is lifecycle, not ranking. Rescore with both")
        print("     scope flags for a constant denominator.\n")

    ndcg = agg[ndcg_key]
    rec = agg[recall_key]
    mrr_agg = agg["mrr"]
    lat = agg["latency_ms"]

    print(f"  {ndcg_key:<10} mean {ndcg['mean']:.3f}  median {ndcg['median']:.3f}")
    print(f"  {recall_key:<10} mean {rec['mean']:.3f}  median {rec['median']:.3f}")
    print(f"  MRR        mean {mrr_agg['mean']:.3f}  median {mrr_agg['median']:.3f}")
    print(f"  Flat miss  {agg['flat_miss_count']}/{scored} ({agg['flat_miss_rate']:.1%})")
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
        decayed = f"  [{len(q['out_of_scope_ids'])} decayed]" if q.get("out_of_scope_ids") else ""
        print(
            f"  {q['query'][:42]:<42}  "
            f"{q['ndcg@10']:.2f}  "
            f"{q[recall_key]:.2f}    "
            f"{q['mrr']:.2f}   "
            f"{str(rank):<4}  "
            f"{q['top_score']:.3f}      "
            f"{q['latency_ms']}ms{decayed}"
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

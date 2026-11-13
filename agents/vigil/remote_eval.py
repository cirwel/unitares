"""Member-clean remote KG retrieval evaluation for Vigil (`--with-eval`).

Evaluates `labels.json` against Core's live `POST /v1/tools/call` (`knowledge`
`action="search"`) endpoint via `SyncGovernanceClient(transport="rest")` so
Vigil never launches `scripts/eval/retrieval_eval.py` (which imports `src.*`
for server-local ranker ablations) and never opens a direct PostgreSQL
connection.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from agents.common.http_client import resolve_governance_base_url
from unitares_sdk.sync_client import SyncGovernanceClient


def dcg(relevances: list[int], k: int) -> float:
    """Discounted cumulative gain at k for binary relevance."""
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances[:k]))


def ndcg_at_k(ranked_ids: list[str], relevant_set: set[str], k: int) -> float:
    """nDCG@k with binary relevance."""
    rels = [1 if rid in relevant_set else 0 for rid in ranked_ids[:k]]
    ideal = dcg([1] * min(len(relevant_set), k), k)
    if ideal == 0:
        return 0.0
    return dcg(rels, k) / ideal


def recall_at_k(ranked_ids: list[str], relevant_set: set[str], k: int) -> float:
    """Fraction of `relevant_set` appearing in `ranked_ids[:k]`."""
    if not relevant_set:
        return 0.0
    hits = sum(1 for rid in ranked_ids[:k] if rid in relevant_set)
    return hits / len(relevant_set)


def mrr(ranked_ids: Iterable[str], relevant_set: set[str]) -> float:
    """Reciprocal rank of first relevant hit (1-indexed); 0.0 if none."""
    for i, rid in enumerate(ranked_ids):
        if rid in relevant_set:
            return 1.0 / (i + 1)
    return 0.0


def _agg(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {
        "mean": round(statistics.fmean(values), 3),
        "median": round(statistics.median(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def _percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)

    def pct(p: float) -> float:
        idx = min(int(p * len(ordered)), len(ordered) - 1)
        return ordered[idx]

    return {
        "p50": round(pct(0.50), 1),
        "p95": round(pct(0.95), 1),
        "max": round(max(ordered), 1),
    }


def run_remote_retrieval_eval(
    *,
    labels_path: Path,
    base_url: str | None = None,
    ndcg_k: int = 10,
    recall_k: int = 20,
    top_k_fetch: int = 20,
    search_fn: Callable[[str, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run retrieval evaluation against Core's REST tool surface."""
    corpus = json.loads(labels_path.read_text())
    pairs = corpus.get("pairs") or []
    limit = max(top_k_fetch, recall_k)

    if search_fn is None:
        resolved_base = resolve_governance_base_url(base_url)
        sdk_client = SyncGovernanceClient(
            mcp_url=f"{resolved_base}/mcp/",
            rest_url=f"{resolved_base}/v1/tools/call",
            transport="rest",
            timeout=30.0,
        )

        def _default_search(query: str, k: int) -> dict[str, Any]:
            return sdk_client._rest_call(
                "knowledge",
                {"action": "search", "query": query, "limit": k},
            )

        search_fn = _default_search

    per_query: list[dict[str, Any]] = []
    ndcgs: list[float] = []
    recalls: list[float] = []
    mrrs: list[float] = []
    latencies: list[float] = []

    for pair in pairs:
        query = str(pair["query"])
        relevant = {str(item) for item in pair.get("relevant_ids") or []}
        t0 = time.perf_counter()
        payload = search_fn(query, limit)
        dt_ms = (time.perf_counter() - t0) * 1000.0

        if not isinstance(payload, dict) or payload.get("success") is False:
            err = payload.get("error") if isinstance(payload, dict) else payload
            raise RuntimeError(f"knowledge search failed for {query!r}: {err}")

        discoveries = payload.get("discoveries") or []
        ranked = [
            str(d["id"])
            for d in discoveries[:limit]
            if isinstance(d, dict) and d.get("id") is not None
        ]
        ndcg = ndcg_at_k(ranked, relevant, ndcg_k)
        rec = recall_at_k(ranked, relevant, recall_k)
        m = mrr(ranked, relevant)
        first_hit_rank = next(
            (i + 1 for i, rid in enumerate(ranked) if rid in relevant), None
        )

        per_query.append(
            {
                "query": query,
                "relevant_ids": sorted(relevant),
                "top_ranked_ids": ranked[:5],
                "ndcg@10": round(ndcg, 3),
                f"recall@{recall_k}": round(rec, 3),
                "mrr": round(m, 3),
                "first_hit_rank": first_hit_rank,
                "flat_miss": first_hit_rank is None,
                "latency_ms": round(dt_ms, 1),
            }
        )
        ndcgs.append(ndcg)
        recalls.append(rec)
        mrrs.append(m)
        latencies.append(dt_ms)

    flat_miss_count = sum(1 for item in per_query if item["flat_miss"])
    return {
        "corpus": {
            "path": labels_path.name,
            "pair_count": len(pairs),
            "scored_pair_count": len(per_query),
            "schema_version": corpus.get("schema_version"),
        },
        "config": {
            "ndcg_k": ndcg_k,
            "recall_k": recall_k,
            "top_k_fetch": top_k_fetch,
            "transport": "remote_rest",
        },
        "aggregate": {
            f"ndcg@{ndcg_k}": _agg(ndcgs),
            f"recall@{recall_k}": _agg(recalls),
            "mrr": _agg(mrrs),
            "latency_ms": _percentiles(latencies),
            "flat_miss_count": flat_miss_count,
            "flat_miss_rate": (
                round(flat_miss_count / len(per_query), 3) if per_query else None
            ),
        },
        "per_query": per_query,
    }

"""Member-clean remote KG retrieval evaluation for Vigil (`--with-eval`).

Evaluates `labels.json` against Core's live `POST /v1/tools/call` (`knowledge`
`action="search"`) endpoint over HTTP so Vigil never launches
`scripts/eval/retrieval_eval.py` (which imports `src.*` for server-local ranker
ablations) and never opens a direct PostgreSQL connection.

Also probes Core's `GET /health/deep` (`checks.knowledge_graph.retrieval_config`)
and records `search_mode_used` from search responses so Vigil selects the
baseline matching Core's active ranker rather than Vigil's local shell env.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from agents.common.http_client import (
    resolve_governance_base_url,
    send_with_bearer_fallback,
)


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


def _unwrap_tool_call_response(tool_name: str, data: Any) -> dict[str, Any]:
    """Unwrap Core's `POST /v1/tools/call` envelope into a tool payload dict."""
    if not isinstance(data, dict) or not data.get("success", False):
        error = data.get("error", "Unknown error") if isinstance(data, dict) else data
        raise RuntimeError(f"Tool {tool_name} failed: {error}")

    result = data.get("result")
    if result is None:
        raise RuntimeError(f"No result from {tool_name}")

    if isinstance(result, dict) and result.get("isError"):
        content = result.get("content", [])
        error_text = " ".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and "text" in item
        )
        raise RuntimeError(
            f"Tool {tool_name} returned error: {error_text or 'unknown'}"
        )

    if isinstance(result, str):
        try:
            parsed = json.loads(result)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {"text": result, "raw": True}

    if isinstance(result, dict):
        content = result.get("content")
        if content and isinstance(content, list):
            merged: dict[str, Any] = {}
            for item in content:
                if isinstance(item, dict) and "text" in item:
                    try:
                        parsed = json.loads(item["text"])
                        if isinstance(parsed, dict):
                            merged.update(parsed)
                    except json.JSONDecodeError:
                        pass
            return merged if merged else result
        return result

    raise RuntimeError(f"Unexpected result type from {tool_name}: {type(result)}")


def _probe_core_retrieval_config(
    client: httpx.Client,
    resolved_base: str,
    *,
    api_token: str | None = None,
) -> dict[str, Any] | None:
    """Best-effort fetch of Core's active KG ranker config from `/health/deep`."""
    try:
        resp = send_with_bearer_fallback(
            lambda hdrs: client.get(
                f"{resolved_base}/health/deep",
                headers=hdrs,
                timeout=5.0,
            ),
            explicit_token=api_token,
            base_url=resolved_base,
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        if not isinstance(body, dict):
            return None
        checks = body.get("checks")
        if not isinstance(checks, dict):
            return None
        kg_check = checks.get("knowledge_graph")
        if not isinstance(kg_check, dict):
            return None
        cfg = kg_check.get("retrieval_config")
        return dict(cfg) if isinstance(cfg, dict) else None
    except Exception:
        return None


def run_remote_retrieval_eval(
    *,
    labels_path: Path,
    base_url: str | None = None,
    api_token: str | None = None,
    ndcg_k: int = 10,
    recall_k: int = 20,
    top_k_fetch: int = 20,
    search_fn: Callable[[str, int], dict[str, Any]] | None = None,
    config_fn: Callable[[], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Run retrieval evaluation against Core's REST tool surface."""
    corpus = json.loads(labels_path.read_text())
    pairs = corpus.get("pairs") or []
    limit = max(top_k_fetch, recall_k)

    owned_client: httpx.Client | None = None
    core_cfg: dict[str, Any] | None = None

    try:
        if config_fn is not None:
            raw_cfg = config_fn()
            core_cfg = dict(raw_cfg) if isinstance(raw_cfg, dict) else None

        if search_fn is None:
            resolved_base = resolve_governance_base_url(base_url)
            owned_client = httpx.Client(timeout=30.0)
            if config_fn is None:
                core_cfg = _probe_core_retrieval_config(
                    owned_client,
                    resolved_base,
                    api_token=api_token,
                )
            rest_url = f"{resolved_base}/v1/tools/call"

            def _default_search(query: str, k: int) -> dict[str, Any]:
                assert owned_client is not None
                body = {
                    "name": "knowledge",
                    "arguments": {"action": "search", "query": query, "limit": k},
                }
                for attempt in range(2):
                    resp = send_with_bearer_fallback(
                        lambda hdrs: owned_client.post(
                            rest_url,
                            json=body,
                            headers=hdrs,
                            timeout=30.0,
                        ),
                        explicit_token=api_token,
                        base_url=resolved_base,
                        json_body=True,
                    )
                    if resp.status_code == 503 and attempt == 0:
                        retry_hdr = resp.headers.get("Retry-After", "")
                        try:
                            delay = min(max(float(retry_hdr), 0.1), 5.0)
                        except ValueError:
                            delay = 1.0
                        time.sleep(delay)
                        continue
                    if resp.status_code >= 400:
                        raise RuntimeError(
                            f"POST /v1/tools/call (knowledge search) failed: "
                            f"HTTP {resp.status_code} {resp.text[:200]}"
                        )
                    return _unwrap_tool_call_response("knowledge", resp.json())
                raise RuntimeError("POST /v1/tools/call exhausted retries")

            search_fn = _default_search

        per_query: list[dict[str, Any]] = []
        ndcgs: list[float] = []
        recalls: list[float] = []
        mrrs: list[float] = []
        latencies: list[float] = []
        search_modes_observed: list[str] = []

        for pair in pairs:
            query = str(pair["query"])
            relevant = {str(item) for item in pair.get("relevant_ids") or []}
            t0 = time.perf_counter()
            payload = search_fn(query, limit)
            dt_ms = (time.perf_counter() - t0) * 1000.0

            if not isinstance(payload, dict) or payload.get("success") is False:
                err = payload.get("error") if isinstance(payload, dict) else payload
                raise RuntimeError(f"knowledge search failed for {query!r}: {err}")

            mode_used = payload.get("search_mode_used")
            if isinstance(mode_used, str) and mode_used and mode_used not in search_modes_observed:
                search_modes_observed.append(mode_used)

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

        if core_cfg is None and config_fn is None and owned_client is not None:
            core_cfg = _probe_core_retrieval_config(
                owned_client,
                resolved_base,
                api_token=api_token,
            )
    finally:
        if owned_client is not None:
            owned_client.close()

    observed_hybrid = any("hybrid" in mode for mode in search_modes_observed)
    observed_graph = any("graph" in mode for mode in search_modes_observed)
    observed_rerank = any("rerank" in mode for mode in search_modes_observed)
    config_source = (
        "core"
        if core_cfg is not None
        else ("observed_search_mode" if search_modes_observed else "unavailable")
    )
    config_block: dict[str, Any] = {
        "ndcg_k": ndcg_k,
        "recall_k": recall_k,
        "top_k_fetch": top_k_fetch,
        "transport": "remote_rest",
        "embedding_model": core_cfg.get("embedding_model") if core_cfg is not None else None,
        "hybrid": (
            bool(core_cfg["hybrid"])
            if (core_cfg is not None and "hybrid" in core_cfg)
            else (True if observed_hybrid else (False if search_modes_observed else None))
        ),
        "graph_expand": (
            bool(core_cfg["graph_expand"])
            if (core_cfg is not None and "graph_expand" in core_cfg)
            else (True if observed_graph else (False if search_modes_observed else None))
        ),
        "rerank": (
            bool(core_cfg["rerank"])
            if (core_cfg is not None and "rerank" in core_cfg)
            else (True if observed_rerank else (False if search_modes_observed else None))
        ),
        "search_modes_observed": search_modes_observed,
        "config_source": config_source,
    }

    flat_miss_count = sum(1 for item in per_query if item["flat_miss"])
    return {
        "corpus": {
            "path": labels_path.name,
            "pair_count": len(pairs),
            "scored_pair_count": len(per_query),
            "schema_version": corpus.get("schema_version"),
        },
        "config": config_block,
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

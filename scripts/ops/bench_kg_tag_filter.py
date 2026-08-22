#!/usr/bin/env python3
"""Read-only benchmark for tagged knowledge search (fix for finding bdb18ee4e52ee7d6).

Runs the search handler's retrieval stage in-process (no audit writes)
against the local governance Postgres, using the env the gov-mcp plist
gives the server. Run it once on master and once on the branch to compare:

    python3 scripts/ops/bench_kg_tag_filter.py <checkout-root> [runs]

Operator tooling: reads ~/Library/LaunchAgents/com.unitares.governance-mcp.plist.
"""
import asyncio
import json
import os
import plistlib
import statistics
import sys
import time

ROOT = os.path.abspath(sys.argv[1])
RUNS = int(sys.argv[2]) if len(sys.argv) > 2 else 10
sys.path.insert(0, ROOT)
os.chdir(ROOT)

plist = plistlib.load(open(os.path.expanduser("~/Library/LaunchAgents/com.unitares.governance-mcp.plist"), "rb"))
for key, value in plist.get("EnvironmentVariables", {}).items():
    os.environ.setdefault(key, value)
os.environ["PYTHONPATH"] = ROOT
os.environ["UNITARES_ENABLE_HYBRID"] = "1"
os.environ.pop("UNITARES_ENABLE_GRAPH_EXPANSION", None)
os.environ.pop("UNITARES_ENABLE_RERANKER", None)

from src.knowledge_graph import get_knowledge_graph  # noqa: E402
from src.mcp_handlers.knowledge.handlers import (  # noqa: E402
    _KnowledgeSearchState,
    _parse_knowledge_search_request,
    _run_text_search,
)

CASES = [
    ("untagged baseline", {"query": "identity serialization leak", "limit": 20}),
    ("common tag + query", {"query": "identity serialization leak", "limit": 20, "tags": ["identity"]}),
    ("sparse tag + matching query", {"query": "identity serialization leak four handles", "limit": 20, "tags": ["cross-repo"]}),
    ("sparse tag + weak query", {"query": "quarterly budget forecast", "limit": 20, "tags": ["cross-repo"]}),
    ("nonexistent tag (probe)", {"query": "open finding", "limit": 20, "tags": ["__probe_tag_missing_bench__"]}),
]


async def run_case(graph, args, runs):
    latencies = []
    last = None
    for _ in range(runs):
        request = _parse_knowledge_search_request(dict(args))
        state = _KnowledgeSearchState(request=request, graph=graph)
        t0 = time.perf_counter()
        await _run_text_search(state)
        latencies.append((time.perf_counter() - t0) * 1000)
        last = state
    wanted = set(args.get("tags") or [])
    ids = [d.id for d in last.results]
    untagged = sum(1 for d in last.results if wanted and not (wanted & set(d.tags or [])))
    ordered = sorted(latencies)
    return {
        "n": runs,
        "p50_ms": round(statistics.median(latencies), 1),
        "p95_ms": round(ordered[int(0.95 * (runs - 1))], 1),
        "max_ms": round(ordered[-1], 1),
        "mode": last.search_mode,
        "count": len(ids),
        "untagged_returned": untagged,
        "tag_filter_dropped": getattr(last, "tag_filter_dropped", None),
        "top3": ids[:3],
    }


async def main():
    graph = await get_knowledge_graph()
    await run_case(graph, CASES[0][1], 1)  # warm-up: embedder load, pool open
    out = {}
    for name, args in CASES:
        out[name] = await run_case(graph, args, RUNS)
    print(json.dumps({"root": ROOT, "backend": graph.__class__.__name__, "results": out}, indent=2))


asyncio.run(main())

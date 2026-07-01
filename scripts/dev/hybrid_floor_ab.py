#!/usr/bin/env python3
"""Validate lowering the hybrid semantic floor 0.3 -> 0.15 end-to-end.

Drives the REAL handler (handle_search_knowledge_graph, search_mode=hybrid) so
RRF fusion + tag boost + low_confidence flagging all run. For each gold item it
locks the target by a distinctive anchor, then compares the target's rank in the
fused results at min_similarity=0.3 (old default) vs 0.15 (new default). A win is
a real answer that the 0.3 floor filtered out now appearing; a regression is a
previously-good rank pushed down by added noise.

Run:  UNITARES_KNOWLEDGE_BACKEND=age UNITARES_EMBEDDING_MODEL=bge-m3 \
        python scripts/dev/hybrid_floor_ab.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.knowledge_graph import get_knowledge_graph
from src.mcp_handlers.knowledge.handlers import handle_search_knowledge_graph

GOLD = [
    ("ephemeral liveness lease agent uuid check-in path false-archival",
     "how do we stop concurrent sessions from archiving each other"),
    ("sonification entropy chromaticism drift carrier valence flat",
     "which EISV dimension actually carries the early-warning signal in audio"),
    ("empirical adoption audit unprompted calls agents do not voluntarily",
     "do agents ever call governance on their own without being told"),
    ("calibration check strongest empirical accuracy evidence channel bins",
     "is the calibration accuracy number something I can actually trust"),
    ("AGE relational canonical advisory variable-length path causal filter",
     "should the knowledge graph treat the AGE store as the source of truth"),
    ("reserved prefix mcp auto-mint anonymous tool_usage incident gate",
     "why were anonymous callers failing every gated tool call"),
    ("Sentinel paused 18h silent swallow AGENT_PAUSED behavioral z-score",
     "what caused the resident agent to go dark for most of a day"),
    ("agent proliferation uninitialized counting artifact participated view",
     "why does the dashboard say half the agents are uninitialized"),
    ("strict identity 425 prefix-bind hijack fingerprint cache free-ride",
     "can a session impersonate another by reusing a cached binding"),
    ("orchestrator vouched UDS peer-cred strong cross-process ephemeral",
     "how can a short-lived agent get a strong identity it can prove"),
    ("dialectic independent reasoner heterogeneous reviewer rubber-stamp",
     "why was the dialectic review just rubber-stamping itself"),
    ("lease plane file lease leak remote_heartbeat TTL reaper self-heal",
     "what happens to a file lock when the session holding it dies"),
]


def parse(result):
    return json.loads(result[0].text)


async def lock_target(graph, anchor):
    res = await graph.full_text_search(anchor, limit=3, operator="OR")
    return res[0].id if res else None


async def rank_in_hybrid(target_id, paraphrase, floor):
    res = await handle_search_knowledge_graph({
        "query": paraphrase,
        "search_mode": "hybrid",
        "min_similarity": floor,
        "limit": 20,
    })
    data = parse(res)
    ids = [d["id"] for d in data.get("discoveries", [])]
    for i, did in enumerate(ids, 1):
        if did == target_id:
            return i
    return None


async def main():
    graph = await get_knowledge_graph()
    print(f"\nHybrid floor A/B (0.3 → 0.15) end-to-end, n={len(GOLD)}, "
          f"backend={os.getenv('UNITARES_KNOWLEDGE_BACKEND','?')}\n")
    print(f"{'paraphrase':<52} {'@0.3':>5} {'@0.15':>6}  {'Δ':>6}")
    print("-" * 74)
    improved = worsened = same = 0
    for anchor, paraphrase in GOLD:
        target = await lock_target(graph, anchor)
        if not target:
            print(f"{paraphrase[:50]:<52} no-anchor")
            continue
        r3 = await rank_in_hybrid(target, paraphrase, 0.3)
        r15 = await rank_in_hybrid(target, paraphrase, 0.15)
        s3 = str(r3) if r3 else "miss"
        s15 = str(r15) if r15 else "miss"
        if r3 and r15:
            d = r3 - r15
            delta = f"+{d}" if d > 0 else (str(d) if d < 0 else "0")
            improved += d > 0; worsened += d < 0; same += d == 0
        elif r15 and not r3:
            delta = "RESCUED"; improved += 1
        elif r3 and not r15:
            delta = "LOST"; worsened += 1
        else:
            delta = "—"; same += 1
        print(f"{paraphrase[:50]:<52} {s3:>5} {s15:>6}  {delta:>6}")
    print("-" * 74)
    print(f"improved={improved}  worsened={worsened}  unchanged/both-miss={same}")


if __name__ == "__main__":
    asyncio.run(main())

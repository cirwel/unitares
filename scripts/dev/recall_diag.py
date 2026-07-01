#!/usr/bin/env python3
"""First-stage recall diagnostic: WHERE do paraphrase queries lose the target?

The reranker A/B (2026-06-20) showed 6/12 gold targets never entered the top-50
pool, so reranking can't help — recall is the bottleneck. This script localizes
the failure: for each gold item it retrieves a DEEP pool (min_similarity=0.0) and
reports the target's true rank + raw cosine under the natural-language paraphrase.

Reading the output:
  - rank 1-50      : in-pool (reranker could act; recall fine)
  - rank 51-200    : JUST outside the pool -> pool-size / SQL-threshold lever
  - rank 200+/none : deep miss -> embedding doesn't connect paraphrase->doc;
                     needs query expansion or a better query-side encoder
  - cosine column  : how strong the best semantic tie is at all (bge-m3 good
                     matches ~0.40-0.47; a target sitting at 0.15 is a true
                     embedding miss, not a thresholding artifact)

Also prints the locked-target summary so the gold set itself can be eyeballed
(an anchor that locks the wrong doc shows up as a nonsense target line).

Run:  UNITARES_EMBEDDING_MODEL=bge-m3 python scripts/dev/recall_diag.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.knowledge_graph import get_knowledge_graph

DEEP_POOL = 200  # how far down we look for the target

# Reuse the reranker A/B gold set (anchor → paraphrase).
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


async def lock_target(graph, anchor):
    res = await graph.full_text_search(anchor, limit=3, operator="OR")
    if not res:
        sem = await graph.semantic_search(anchor, limit=3, min_similarity=0.2)
        res = [d for d, _ in sem]
    return (res[0].id, res[0].summary) if res else (None, None)


async def main():
    graph = await get_knowledge_graph()
    print(f"\nFirst-stage recall diagnostic — n={len(GOLD)}, deep_pool={DEEP_POOL}, "
          f"embedder={os.getenv('UNITARES_EMBEDDING_MODEL','minilm')}\n")
    print(f"{'paraphrase':<52} {'rank':>6} {'cos@1':>6} {'cos@tgt':>8}  bucket")
    print("-" * 92)

    buckets = {"in-pool(≤50)": 0, "near(51-200)": 0, "deep-miss(>200)": 0}
    for anchor, paraphrase in GOLD:
        target_id, target_sum = await lock_target(graph, anchor)
        if target_id is None:
            print(f"{paraphrase[:50]:<52} {'—':>6} {'—':>6} {'—':>8}  NO-ANCHOR")
            continue
        sem = await graph.semantic_search(paraphrase, limit=DEEP_POOL, min_similarity=0.0)
        if isinstance(sem, tuple):
            print(f"{paraphrase[:50]:<52} semantic degraded")
            continue
        ordered = [(d.id, score) for d, score in sem]
        cos_at1 = ordered[0][1] if ordered else 0.0
        rank = next((i for i, (did, _) in enumerate(ordered, 1) if did == target_id), None)
        cos_tgt = next((s for did, s in ordered if did == target_id), None)

        if rank and rank <= 50:
            bucket = "in-pool(≤50)"
        elif rank and rank <= 200:
            bucket = "near(51-200)"
        else:
            bucket = "deep-miss(>200)"
        buckets[bucket] += 1

        rank_s = str(rank) if rank else f">{DEEP_POOL}"
        cos_tgt_s = f"{cos_tgt:.3f}" if cos_tgt is not None else "—"
        print(f"{paraphrase[:50]:<52} {rank_s:>6} {cos_at1:>6.3f} {cos_tgt_s:>8}  {bucket}")

    print("-" * 92)
    for k, v in buckets.items():
        print(f"  {k:<16} {v}")
    print("\nVERDICT GUIDE: many 'near(51-200)' -> raise pool / lower SQL floor "
          "(cheap). Many 'deep-miss' with low cos@tgt -> query-side expansion or "
          "a stronger query encoder (the real recall work).")


if __name__ == "__main__":
    asyncio.run(main())

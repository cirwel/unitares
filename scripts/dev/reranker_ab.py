#!/usr/bin/env python3
"""Reranker A/B: does the bge-reranker-v2-m3 cross-encoder improve KG retrieval?

Methodology (known-item retrieval with paraphrase, to avoid lexical leakage):
  1. For each gold item, LOCK the target discovery id via a distinctive-keyword
     query (high-precision anchor the operator can eyeball).
  2. EVALUATE retrieval on a *different* natural-language paraphrase of the same
     information need — the phrasing an agent actually types.
  3. First stage: semantic_search(paraphrase, limit=POOL). Record rank of target.
  4. Rerank the same pool with the cross-encoder. Record new rank.
  5. Aggregate MRR / mean-rank / recall@5 before vs after, plus rerank latency.

Honesty caveats (same class as the trajectory-discrimination pilot): the gold
set is hand-curated, small (n≈12), and within-corpus. This measures re-ranking
of a fixed first-stage pool, not end-to-end hybrid+graph. Treat as a directional
signal for the enable/skip decision, not a benchmark.

Run:  UNITARES_EMBEDDING_MODEL=bge-m3 python scripts/dev/reranker_ab.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.knowledge_graph import get_knowledge_graph
from src.reranker import rerank
from src.mcp_handlers.knowledge.limits import EMBED_DETAILS_WINDOW

POOL = 50  # first-stage pool size handed to the reranker

# (anchor_query → locks target id, paraphrase → what we actually evaluate).
# Anchors are distinctive keyword phrases; paraphrases are natural questions
# that deliberately avoid the anchor's exact terms.
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


def build_text(d) -> str:
    details = (d.details or "")[:EMBED_DETAILS_WINDOW]
    return f"{d.summary or ''}\n{details}".strip()


def rank_of(target_id, ordered_ids):
    """1-based rank of target in ordered list, or None if absent."""
    for i, did in enumerate(ordered_ids, 1):
        if did == target_id:
            return i
    return None


async def lock_target(graph, anchor):
    """Lock the gold target id as the top hit of the distinctive anchor query."""
    res = await graph.full_text_search(anchor, limit=5, operator="OR")
    if not res:
        res = await graph.semantic_search(anchor, limit=5, min_similarity=0.2)
        res = [d for d, _ in res]
    return res[0].id if res else None


async def main():
    graph = await get_knowledge_graph()
    rows = []
    rerank_latencies = []

    for anchor, paraphrase in GOLD:
        target = await lock_target(graph, anchor)
        if target is None:
            rows.append((paraphrase, None, None, None, "no target locked"))
            continue

        # First-stage pool via semantic search on the natural-language paraphrase.
        sem = await graph.semantic_search(paraphrase, limit=POOL, min_similarity=0.0)
        if isinstance(sem, tuple):  # degraded
            rows.append((paraphrase, target, None, None, "semantic degraded"))
            continue
        pool = [d for d, _ in sem]
        first_ids = [d.id for d in pool]
        rank_before = rank_of(target, first_ids)

        # Rerank the same pool.
        cands = [(d.id, build_text(d)) for d in pool]
        t0 = time.perf_counter()
        reranked = await rerank(paraphrase, cands, top_k=POOL, max_rerank_size=POOL)
        rerank_latencies.append((time.perf_counter() - t0) * 1000)
        rank_after = rank_of(target, [rid for rid, _ in reranked])

        rows.append((paraphrase, target, rank_before, rank_after, ""))

    # ---- report ----
    def mrr(ranks):
        vals = [1.0 / r for r in ranks if r]
        return sum(vals) / len(GOLD)

    def recall_at(ranks, k):
        return sum(1 for r in ranks if r and r <= k) / len(GOLD)

    before = [r[2] for r in rows]
    after = [r[3] for r in rows]

    print(f"\nReranker A/B — n={len(GOLD)} gold items, pool={POOL}, "
          f"embedder={os.getenv('UNITARES_EMBEDDING_MODEL', 'minilm')}\n")
    print(f"{'paraphrase query':<58} {'before':>7} {'after':>7}  {'Δ':>4}")
    print("-" * 82)
    improved = worsened = same = 0
    for q, _t, rb, ra, note in rows:
        if note:
            print(f"{q[:56]:<58} {note}")
            continue
        sb = str(rb) if rb else "miss"
        sa = str(ra) if ra else "miss"
        delta = ""
        if rb and ra:
            d = rb - ra
            delta = f"+{d}" if d > 0 else (str(d) if d < 0 else "0")
            improved += d > 0; worsened += d < 0; same += d == 0
        elif ra and not rb:
            delta = "rescued"; improved += 1
        elif rb and not ra:
            delta = "lost"; worsened += 1
        print(f"{q[:56]:<58} {sb:>7} {sa:>7}  {delta:>4}")

    print("-" * 82)
    print(f"MRR        before={mrr(before):.3f}   after={mrr(after):.3f}   "
          f"Δ={mrr(after) - mrr(before):+.3f}")
    print(f"recall@5   before={recall_at(before,5):.2f}     after={recall_at(after,5):.2f}")
    print(f"recall@1   before={recall_at(before,1):.2f}     after={recall_at(after,1):.2f}")
    print(f"items improved={improved}  worsened={worsened}  unchanged={same}")
    if rerank_latencies:
        avg = sum(rerank_latencies) / len(rerank_latencies)
        print(f"rerank latency: avg={avg:.0f}ms  max={max(rerank_latencies):.0f}ms "
              f"(pool={POOL}, per-search added cost)")


if __name__ == "__main__":
    asyncio.run(main())

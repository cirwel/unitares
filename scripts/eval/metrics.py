"""Pure ranking-quality metrics for the KG retrieval eval.

These are the scoring functions the eval (`retrieval_eval.py`) reports against a
labeled (query, relevant_ids) corpus. They are deliberately kept free of any
backend dependency: the runner needs live Postgres + embeddings, but the math
that decides whether a ranking change is an improvement must be trustworthy and
exercisable in CI on its own. `tests/test_retrieval_eval_metrics.py` pins them.

All metrics use **binary** relevance (a doc is relevant or it is not), matching
the label format in `tests/retrieval_eval/labels.json`.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Set


def dcg(relevances: List[int], k: int) -> float:
    """Discounted cumulative gain at k for a binary-relevance gain list.

    Position i (0-indexed) contributes ``rel_i / log2(i + 2)`` so rank 1 has
    discount log2(2)=1, rank 2 has log2(3), etc. Only the first ``k`` positions
    count.
    """
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances[:k]))


def ndcg_at_k(ranked_ids: List[str], relevant_set: Set[str], k: int) -> float:
    """nDCG@k with binary relevance.

    The ideal ranking places every relevant doc first, so ideal-DCG is the DCG
    of ``min(len(relevant_set), k)`` ones. Returns 0.0 when no relevant doc
    exists (nothing is achievable, so nothing is forfeited).
    """
    rels = [1 if rid in relevant_set else 0 for rid in ranked_ids[:k]]
    ideal = dcg([1] * min(len(relevant_set), k), k)
    if ideal == 0:
        return 0.0
    return dcg(rels, k) / ideal


def recall_at_k(ranked_ids: List[str], relevant_set: Set[str], k: int) -> float:
    """Fraction of the relevant set that appears in the top-k of the ranking.

    Returns 0.0 when there are no relevant docs (undefined recall, reported as
    0 so it never inflates an aggregate).
    """
    if not relevant_set:
        return 0.0
    hits = sum(1 for rid in ranked_ids[:k] if rid in relevant_set)
    return hits / len(relevant_set)


def mrr(ranked_ids: Iterable[str], relevant_set: Set[str]) -> float:
    """Reciprocal rank of the first relevant hit (1-indexed); 0.0 if none."""
    for i, rid in enumerate(ranked_ids):
        if rid in relevant_set:
            return 1.0 / (i + 1)
    return 0.0

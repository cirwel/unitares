"""
Cross-Encoder Reranker for Knowledge Graph Retrieval

Second stage of the retrieval pipeline: given a set of candidate discoveries
retrieved by the first-stage retriever (dense, FTS, or hybrid), score each
(query, candidate) pair jointly with a cross-encoder and reorder.

Cross-encoders trade latency for quality: they attend jointly to the query
and document, which collapses the semantic distinctions a bi-encoder's fixed
embedding can miss.

Model registry (UNITARES_RERANKER_MODEL env var):
- `bge-m3` (default) — BAAI/bge-reranker-v2-m3, 568M params, multilingual

Enable via UNITARES_ENABLE_RERANKER=1. When disabled, `rerank()` returns the
input unchanged — safe no-op.

Phase 3 of docs/plans/2026-04-20-kg-retrieval-rebuild.md.
"""

from __future__ import annotations

import asyncio
import os
from typing import Dict, List, Optional, Tuple

from src.logging_utils import get_logger

logger = get_logger(__name__)


KNOWN_RERANKERS: Dict[str, Dict[str, object]] = {
    "bge-m3": {
        "hf_name": "BAAI/bge-reranker-v2-m3",
    },
}

DEFAULT_RERANKER_KEY = os.getenv("UNITARES_RERANKER_MODEL", "bge-m3").strip().lower()
if DEFAULT_RERANKER_KEY not in KNOWN_RERANKERS:
    logger.warning(
        f"Unknown UNITARES_RERANKER_MODEL={DEFAULT_RERANKER_KEY!r}; "
        f"falling back to 'bge-m3'. Known: {list(KNOWN_RERANKERS)}"
    )
    DEFAULT_RERANKER_KEY = "bge-m3"


def _flag_enabled(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def reranker_enabled() -> bool:
    """True when the reranker should run. Controlled by UNITARES_ENABLE_RERANKER.

    The cross-encoder itself is content-scoring and carries no usage feedback,
    so enabling it as shipped does not change the KG's selection dynamics.

    Training or tuning one on `knowledge_read` logs would. Retrieval ranking
    today has no usage term at all, which is what stops the KG from selecting
    on its own past output; a read-log-trained reranker makes a discovery
    easier to retrieve because it was retrieved before. Treat that as a
    decision about propagation rights rather than a retrieval improvement — see
    `graph_expansion_enabled()` in `src/retrieval.py` for the same loop by a
    shorter route, and `src/storage/kg_write_budget.py` for the write-side
    constraint.
    """
    return _flag_enabled("UNITARES_ENABLE_RERANKER", default=False)


try:
    from sentence_transformers import CrossEncoder
    CROSS_ENCODER_AVAILABLE = True
except ImportError:
    CROSS_ENCODER_AVAILABLE = False
    logger.warning(
        "sentence-transformers CrossEncoder not available; reranker disabled."
    )


class CrossEncoderReranker:
    """Lazy-loaded cross-encoder reranker, async-safe via run_in_executor."""

    def __init__(self, model_key: str = DEFAULT_RERANKER_KEY):
        if model_key not in KNOWN_RERANKERS:
            model_key = "bge-m3"
        entry = KNOWN_RERANKERS[model_key]
        self.model_key = model_key
        self.model_name: str = str(entry["hf_name"])
        self._model: Optional[CrossEncoder] = None
        self._load_lock: Optional[asyncio.Lock] = None

    async def _ensure_model(self) -> CrossEncoder:
        if not CROSS_ENCODER_AVAILABLE:
            raise RuntimeError("sentence-transformers CrossEncoder not installed")

        if self._model is not None:
            return self._model

        if self._load_lock is None:
            self._load_lock = asyncio.Lock()

        async with self._load_lock:
            if self._model is not None:
                return self._model

            loop = asyncio.get_running_loop()

            def _load():
                logger.info(f"Loading cross-encoder reranker: {self.model_name} (key={self.model_key})")
                model = CrossEncoder(self.model_name)
                logger.info(f"Cross-encoder loaded: {self.model_name}")
                return model

            self._model = await loop.run_in_executor(None, _load)
            return self._model

    async def score_pairs(
        self,
        query: str,
        docs: List[str],
        batch_size: int = 32,
    ) -> List[float]:
        """Return relevance scores (one per doc, order-preserving)."""
        if not docs:
            return []
        model = await self._ensure_model()
        loop = asyncio.get_running_loop()

        def _score():
            pairs = [(query, d) for d in docs]
            scores = model.predict(
                pairs,
                batch_size=batch_size,
                show_progress_bar=len(pairs) > 100,
            )
            return [float(s) for s in scores]

        return await loop.run_in_executor(None, _score)


# Global singleton
_reranker: Optional[CrossEncoderReranker] = None
_reranker_lock: Optional[asyncio.Lock] = None


async def get_reranker() -> CrossEncoderReranker:
    global _reranker, _reranker_lock
    if _reranker is not None:
        return _reranker
    if _reranker_lock is None:
        _reranker_lock = asyncio.Lock()
    async with _reranker_lock:
        if _reranker is None:
            _reranker = CrossEncoderReranker()
        return _reranker


async def rerank(
    query: str,
    candidates: List[Tuple[str, str]],
    top_k: int = 10,
    max_rerank_size: int = 50,
) -> List[Tuple[str, float]]:
    """Rerank `(doc_id, doc_text)` pairs by cross-encoder score.

    Args:
        query: The query string (same text passed to first-stage retrieval).
        candidates: First-stage candidates as (id, text) pairs. Text should be
                    the same join of summary+details used for embedding.
        top_k: Number of results to return after reranking.
        max_rerank_size: Cap on how many pairs go to the cross-encoder, to
                        bound latency. Sort-order stability relies on this
                        being applied BEFORE scoring, not after.

    Returns:
        [(doc_id, rerank_score)] sorted by rerank_score descending, length
        min(top_k, len(candidates)).
    """
    if not candidates:
        return []
    if not CROSS_ENCODER_AVAILABLE:
        # No-op when reranker unavailable; return input order as-is with 0 scores.
        return [(d, 0.0) for d, _ in candidates[:top_k]]

    pool = candidates[:max_rerank_size]
    ids = [d for d, _ in pool]
    texts = [t for _, t in pool]

    reranker = await get_reranker()
    try:
        scores = await reranker.score_pairs(query, texts)
    except Exception as e:
        logger.warning(f"Reranker failed, returning first-stage order: {e}")
        return [(d, 0.0) for d, _ in pool[:top_k]]

    scored = list(zip(ids, scores))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]


def reranker_available() -> bool:
    return CROSS_ENCODER_AVAILABLE

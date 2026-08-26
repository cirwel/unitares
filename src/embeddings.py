"""
Embeddings Service for Semantic Search

Provides text embeddings using sentence-transformers for semantic similarity
queries over the knowledge graph.

Model selection (UNITARES_EMBEDDING_MODEL env var):
- `minilm` (default) — sentence-transformers/all-MiniLM-L6-v2, 384d
- `bge-m3`           — BAAI/bge-m3, 1024d

Both load via SentenceTransformer. BGE-M3 is symmetric (no query/passage
asymmetry), so the same encode path is used for both sides.

Usage:
    from src.embeddings import get_embeddings_service

    service = await get_embeddings_service()
    embedding = await service.embed("circuit breaker triggered")
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from src.logging_utils import get_logger

logger = get_logger(__name__)


# Model registry. Add entries here when introducing new embedders.
# `table_suffix` is appended to `core.discovery_embeddings` when a model
# differs in dimension; empty string means the default table.
KNOWN_MODELS: Dict[str, Dict[str, object]] = {
    "minilm": {
        "hf_name": "sentence-transformers/all-MiniLM-L6-v2",
        "dim": 384,
        "table_suffix": "",
    },
    "bge-m3": {
        "hf_name": "BAAI/bge-m3",
        "dim": 1024,
        "table_suffix": "_bge_m3",
    },
}

DEFAULT_MODEL_KEY = os.getenv("UNITARES_EMBEDDING_MODEL", "minilm").strip().lower()
if DEFAULT_MODEL_KEY not in KNOWN_MODELS:
    logger.warning(
        f"Unknown UNITARES_EMBEDDING_MODEL={DEFAULT_MODEL_KEY!r}; "
        f"falling back to 'minilm'. Known: {list(KNOWN_MODELS)}"
    )
    DEFAULT_MODEL_KEY = "minilm"

_DEFAULT_ENTRY = KNOWN_MODELS[DEFAULT_MODEL_KEY]
DEFAULT_MODEL: str = str(_DEFAULT_ENTRY["hf_name"])
EMBEDDING_DIM: int = int(_DEFAULT_ENTRY["dim"])
EMBEDDINGS_TABLE: str = f"core.discovery_embeddings{_DEFAULT_ENTRY['table_suffix']}"


# NumPy is a direct core dependency and is used by similarity/ranking even when
# no embedding model is loaded.  sentence-transformers is intentionally only
# imported inside _ensure_model(): importing it here used to pull torch,
# transformers, and sklearn into every process that merely checked embedding
# capability.  On GitHub runners that added 8-10 seconds and hundreds of MB to
# each pytest shard before a model was ever requested.
import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer
else:
    SentenceTransformer = Any

SENTENCE_TRANSFORMERS_AVAILABLE = (
    importlib.util.find_spec("sentence_transformers") is not None
)
if not SENTENCE_TRANSFORMERS_AVAILABLE:
    logger.warning(
        "sentence-transformers not available. Install with: "
        "pip install sentence-transformers"
    )


class EmbeddingsService:
    """
    Embeddings service with lazy model loading.

    Thread-safe, async-compatible via run_in_executor.
    Model loaded on first use to avoid startup overhead.
    """

    def __init__(self, model_key: str = DEFAULT_MODEL_KEY):
        if model_key not in KNOWN_MODELS:
            logger.warning(f"Unknown model_key={model_key!r}; using 'minilm'")
            model_key = "minilm"
        entry = KNOWN_MODELS[model_key]
        self.model_key = model_key
        self.model_name: str = str(entry["hf_name"])
        self.dim: int = int(entry["dim"])
        self.table_name: str = f"core.discovery_embeddings{entry['table_suffix']}"
        self._model: Optional[SentenceTransformer] = None
        self._load_lock: Optional[asyncio.Lock] = None

    async def _ensure_model(self) -> SentenceTransformer:
        """Lazy load model on first use."""
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise RuntimeError(
                "sentence-transformers not installed. "
                "Install with: pip install sentence-transformers"
            )

        if self._model is not None:
            return self._model

        # Create lock lazily to avoid event loop binding issues
        if self._load_lock is None:
            self._load_lock = asyncio.Lock()

        async with self._load_lock:
            # Double-check after acquiring lock
            if self._model is not None:
                return self._model

            # Load model in executor (blocking I/O)
            loop = asyncio.get_running_loop()

            def _load_model():
                from sentence_transformers import SentenceTransformer

                logger.info(f"Loading embedding model: {self.model_name} (key={self.model_key}, dim={self.dim})")
                model = SentenceTransformer(self.model_name)
                logger.info(f"Embedding model loaded: {self.model_name}")
                return model

            self._model = await loop.run_in_executor(None, _load_model)
            return self._model

    async def embed(self, text: str) -> List[float]:
        """Generate embedding for a single text."""
        model = await self._ensure_model()
        loop = asyncio.get_running_loop()

        def _encode():
            embedding = model.encode(text, normalize_embeddings=True)
            return embedding.tolist()

        return await loop.run_in_executor(None, _encode)

    async def embed_batch(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        """Generate embeddings for multiple texts."""
        if not texts:
            return []

        model = await self._ensure_model()
        loop = asyncio.get_running_loop()

        def _encode_batch():
            embeddings = model.encode(
                texts,
                batch_size=batch_size,
                normalize_embeddings=True,
                show_progress_bar=len(texts) > 100
            )
            return [emb.tolist() for emb in embeddings]

        return await loop.run_in_executor(None, _encode_batch)

    async def similarity(self, embedding1: List[float], embedding2: List[float]) -> float:
        """Cosine similarity via dot product on normalized vectors."""
        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise RuntimeError("numpy not available")
        arr1 = np.array(embedding1)
        arr2 = np.array(embedding2)
        return float(np.dot(arr1, arr2))

    async def rank_by_similarity(
        self,
        query_embedding: List[float],
        candidate_embeddings: List[Tuple[str, List[float]]],
        top_k: int = 10
    ) -> List[Tuple[str, float]]:
        """Rank candidates by similarity to query."""
        if not candidate_embeddings:
            return []

        if not SENTENCE_TRANSFORMERS_AVAILABLE:
            raise RuntimeError("numpy not available")

        loop = asyncio.get_running_loop()

        def _rank():
            query = np.array(query_embedding)
            scores = []
            for doc_id, emb in candidate_embeddings:
                if emb is None:
                    continue
                candidate = np.array(emb)
                if candidate.ndim == 0:
                    continue
                score = float(np.dot(query, candidate))
                scores.append((doc_id, score))
            scores.sort(key=lambda x: x[1], reverse=True)
            return scores[:top_k]

        return await loop.run_in_executor(None, _rank)

    def is_available(self) -> bool:
        """Check if embeddings service is available."""
        return SENTENCE_TRANSFORMERS_AVAILABLE


# Global singleton
_embeddings_service: Optional[EmbeddingsService] = None
_service_lock: Optional[asyncio.Lock] = None


async def get_embeddings_service() -> EmbeddingsService:
    """Get global embeddings service singleton."""
    global _embeddings_service, _service_lock

    if _embeddings_service is not None:
        return _embeddings_service

    if _service_lock is None:
        _service_lock = asyncio.Lock()

    async with _service_lock:
        if _embeddings_service is None:
            _embeddings_service = EmbeddingsService()
        return _embeddings_service


def embeddings_available() -> bool:
    """Check if embeddings are available (sentence-transformers installed)."""
    return SENTENCE_TRANSFORMERS_AVAILABLE


def get_active_table_name() -> str:
    """Return the PG table storing embeddings for the active model.

    Exposed so the storage layer can select the right table without hard-coding
    `core.discovery_embeddings`. Different embedding dimensions live in
    different tables so pgvector's per-column dimension is respected.
    """
    return EMBEDDINGS_TABLE

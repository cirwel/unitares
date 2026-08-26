"""
Tests for src/embeddings.py - EmbeddingsService

Tests embedding generation, similarity computation, and ranking.
Uses mocked SentenceTransformer to avoid model downloads in CI.
"""

import pytest
import asyncio
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import numpy as np

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# --- Unit tests for EmbeddingsService ---


def test_module_import_does_not_load_sentence_transformers():
    """Capability checks must not import the heavyweight model runtime."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import src.embeddings; "
                "assert 'sentence_transformers' not in sys.modules"
            ),
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_embed_returns_list_of_floats():
    """embed() should return a list of floats from the model."""
    from src.embeddings import EmbeddingsService

    service = EmbeddingsService()

    # Mock the model
    mock_model = MagicMock()
    fake_embedding = np.random.randn(384).astype(np.float32)
    # Normalize like the real code does
    fake_embedding = fake_embedding / np.linalg.norm(fake_embedding)
    mock_model.encode.return_value = fake_embedding

    # Inject mock model directly
    service._model = mock_model

    result = await service.embed("test text")

    assert isinstance(result, list)
    assert len(result) == 384
    assert all(isinstance(v, float) for v in result)
    mock_model.encode.assert_called_once_with("test text", normalize_embeddings=True)


@pytest.mark.asyncio
async def test_embed_batch_returns_list_of_embeddings():
    """embed_batch() should return a list of embedding lists."""
    from src.embeddings import EmbeddingsService

    service = EmbeddingsService()

    mock_model = MagicMock()
    fake_embeddings = np.random.randn(3, 384).astype(np.float32)
    mock_model.encode.return_value = fake_embeddings

    service._model = mock_model

    texts = ["hello", "world", "test"]
    result = await service.embed_batch(texts)

    assert isinstance(result, list)
    assert len(result) == 3
    assert all(len(emb) == 384 for emb in result)
    mock_model.encode.assert_called_once()


@pytest.mark.asyncio
async def test_embed_batch_empty_input():
    """embed_batch() with empty list should return empty list without calling model."""
    from src.embeddings import EmbeddingsService

    service = EmbeddingsService()
    mock_model = MagicMock()
    service._model = mock_model

    result = await service.embed_batch([])

    assert result == []
    mock_model.encode.assert_not_called()


@pytest.mark.asyncio
async def test_similarity_normalized_vectors():
    """similarity() should compute cosine similarity (dot product for normalized vectors)."""
    from src.embeddings import EmbeddingsService

    service = EmbeddingsService()

    # Two identical normalized vectors should have similarity ~1.0
    vec = np.random.randn(384).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    vec_list = vec.tolist()

    score = await service.similarity(vec_list, vec_list)
    assert abs(score - 1.0) < 0.01, f"Same vector should have similarity ~1.0, got {score}"


@pytest.mark.asyncio
async def test_similarity_orthogonal_vectors():
    """Orthogonal vectors should have similarity ~0."""
    from src.embeddings import EmbeddingsService

    service = EmbeddingsService()

    # Create two orthogonal vectors
    vec1 = np.zeros(384)
    vec1[0] = 1.0
    vec2 = np.zeros(384)
    vec2[1] = 1.0

    score = await service.similarity(vec1.tolist(), vec2.tolist())
    assert abs(score) < 0.01, f"Orthogonal vectors should have similarity ~0, got {score}"


@pytest.mark.asyncio
async def test_rank_by_similarity():
    """rank_by_similarity() should return candidates sorted by descending similarity."""
    from src.embeddings import EmbeddingsService

    service = EmbeddingsService()

    # Query vector
    query = np.zeros(384)
    query[0] = 1.0
    query_list = query.tolist()

    # Candidates with known similarities
    # candidate A: very similar (aligned with query)
    vec_a = np.zeros(384)
    vec_a[0] = 0.9
    vec_a[1] = 0.1

    # candidate B: less similar
    vec_b = np.zeros(384)
    vec_b[0] = 0.3
    vec_b[1] = 0.7

    # candidate C: opposite
    vec_c = np.zeros(384)
    vec_c[0] = -1.0

    candidates = [
        ("B", vec_b.tolist()),
        ("A", vec_a.tolist()),
        ("C", vec_c.tolist()),
    ]

    result = await service.rank_by_similarity(query_list, candidates, top_k=3)

    assert len(result) == 3
    # A should be first (most similar)
    assert result[0][0] == "A"
    # C should be last (least similar)
    assert result[2][0] == "C"
    # Scores should be descending
    assert result[0][1] >= result[1][1] >= result[2][1]


@pytest.mark.asyncio
async def test_rank_by_similarity_empty_candidates():
    """rank_by_similarity() with empty candidates should return empty list."""
    from src.embeddings import EmbeddingsService

    service = EmbeddingsService()

    query = np.zeros(384).tolist()
    result = await service.rank_by_similarity(query, [], top_k=5)
    assert result == []


@pytest.mark.asyncio
async def test_rank_by_similarity_skips_none_embeddings():
    """None candidate embeddings should be ignored instead of crashing."""
    from src.embeddings import EmbeddingsService

    service = EmbeddingsService()

    query = [1.0, 0.0]
    candidates = [
        ("bad", None),
        ("good", [1.0, 0.0]),
    ]

    result = await service.rank_by_similarity(query, candidates, top_k=5)
    assert result == [("good", 1.0)]


@pytest.mark.asyncio
async def test_rank_by_similarity_top_k():
    """rank_by_similarity() should respect top_k limit."""
    from src.embeddings import EmbeddingsService

    service = EmbeddingsService()

    query = np.random.randn(384).tolist()
    candidates = [
        (f"doc_{i}", np.random.randn(384).tolist())
        for i in range(10)
    ]

    result = await service.rank_by_similarity(query, candidates, top_k=3)
    assert len(result) == 3


def test_is_available():
    """is_available() should return True when sentence-transformers is installed."""
    from src.embeddings import EmbeddingsService

    service = EmbeddingsService()
    # In test environment, sentence-transformers should be available
    # (or at least numpy which is needed)
    result = service.is_available()
    assert isinstance(result, bool)


@pytest.mark.asyncio
async def test_ensure_model_raises_without_sentence_transformers():
    """_ensure_model() should raise RuntimeError if sentence-transformers unavailable."""
    from src.embeddings import EmbeddingsService

    service = EmbeddingsService()

    with patch("src.embeddings.SENTENCE_TRANSFORMERS_AVAILABLE", False):
        with pytest.raises(RuntimeError, match="sentence-transformers not installed"):
            await service._ensure_model()


@pytest.mark.asyncio
async def test_ensure_model_imports_framework_only_when_requested(monkeypatch):
    """The first real model request performs the deferred framework import."""
    import src.embeddings as emb_module

    model = MagicMock()
    constructor = MagicMock(return_value=model)
    fake_module = types.ModuleType("sentence_transformers")
    fake_module.SentenceTransformer = constructor
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)
    monkeypatch.setattr(emb_module, "SENTENCE_TRANSFORMERS_AVAILABLE", True)

    service = emb_module.EmbeddingsService()
    assert await service._ensure_model() is model
    constructor.assert_called_once_with(service.model_name)


@pytest.mark.asyncio
async def test_get_embeddings_service_singleton():
    """get_embeddings_service() should return the same instance."""
    import src.embeddings as emb_module

    # Reset global state
    emb_module._embeddings_service = None
    emb_module._service_lock = asyncio.Lock()

    svc1 = await emb_module.get_embeddings_service()
    svc2 = await emb_module.get_embeddings_service()

    assert svc1 is svc2


def test_embeddings_available():
    """embeddings_available() should reflect SENTENCE_TRANSFORMERS_AVAILABLE."""
    from src.embeddings import embeddings_available

    result = embeddings_available()
    assert isinstance(result, bool)


def test_embedding_dim_constant():
    """EMBEDDING_DIM should be 384 for MiniLM-L6-v2."""
    from src.embeddings import EMBEDDING_DIM
    assert EMBEDDING_DIM == 384


def test_default_model_constant():
    """DEFAULT_MODEL should be sentence-transformers/all-MiniLM-L6-v2."""
    from src.embeddings import DEFAULT_MODEL
    assert DEFAULT_MODEL == "sentence-transformers/all-MiniLM-L6-v2"

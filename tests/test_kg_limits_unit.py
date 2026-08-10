"""Unit tests for src/mcp_handlers/knowledge/limits.py and its consumers.

Covers:
- Limits module constants and their relative ordering
- DiscoveryNode.to_dict(include_details=False) preview generation
- Embed-text construction in knowledge_graph_age.py uses the wider window
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.knowledge_graph import DiscoveryNode
from src.mcp_handlers.knowledge.limits import (
    DETAILS_PREVIEW_CHARS,
    EMBED_DETAILS_WINDOW,
    MAX_DETAILS_LEN,
    MAX_SUMMARY_LEN,
)


class TestLimitsSanity:
    def test_summary_cap_positive(self):
        assert MAX_SUMMARY_LEN > 0

    def test_details_cap_larger_than_summary(self):
        assert MAX_DETAILS_LEN > MAX_SUMMARY_LEN

    def test_details_cap_supports_medium_documents(self):
        assert MAX_DETAILS_LEN == 64 * 1024

    def test_embed_window_within_details_cap(self):
        assert EMBED_DETAILS_WINDOW <= MAX_DETAILS_LEN

    def test_preview_smaller_than_embed_window(self):
        assert DETAILS_PREVIEW_CHARS < EMBED_DETAILS_WINDOW

    def test_embed_window_fits_bge_m3_budget(self):
        # BGE-M3 tokenizer max = 8192 tokens ~= 30000 chars for English.
        # Leave headroom for the summary field.
        assert EMBED_DETAILS_WINDOW <= 30000 - MAX_SUMMARY_LEN


class TestDiscoveryNodePreview:
    def _discovery(self, details: str) -> DiscoveryNode:
        return DiscoveryNode(
            id="test-id", agent_id="agent-1", type="insight",
            summary="short summary", details=details,
        )

    def test_include_details_true_returns_full_details(self):
        d = self._discovery("x" * (DETAILS_PREVIEW_CHARS + 100))
        result = d.to_dict(include_details=True)
        assert result["details"] == d.details
        assert "details_preview" not in result

    def test_no_details_omits_preview_fields(self):
        d = self._discovery("")
        result = d.to_dict(include_details=False)
        assert "details_preview" not in result
        assert "has_details" not in result

    def test_short_details_returns_unellipsed_preview(self):
        short = "x" * (DETAILS_PREVIEW_CHARS - 10)
        d = self._discovery(short)
        result = d.to_dict(include_details=False)
        assert result["has_details"] is True
        assert result["details_preview"] == short
        assert result["has_more_details"] is False
        assert "..." not in result["details_preview"]

    def test_long_details_returns_ellipsed_preview(self):
        long = "y" * (DETAILS_PREVIEW_CHARS + 200)
        d = self._discovery(long)
        result = d.to_dict(include_details=False)
        assert result["has_details"] is True
        assert result["has_more_details"] is True
        assert result["details_length"] == len(long)
        assert result["details_preview"].endswith("...")
        assert len(result["details_preview"]) == DETAILS_PREVIEW_CHARS + 3

    def test_exact_boundary_no_ellipsis(self):
        at_limit = "z" * DETAILS_PREVIEW_CHARS
        d = self._discovery(at_limit)
        result = d.to_dict(include_details=False)
        # length == preview_chars, so `> preview_chars` is False → no ellipsis
        assert result["has_more_details"] is False
        assert result["details_preview"] == at_limit


class TestEmbedTextConstruction:
    """Verify embed uses the wider window, not the legacy 500-char slice."""

    @pytest.mark.asyncio
    async def test_store_embedding_uses_embed_window(self):
        from src.storage.knowledge_graph_age import KnowledgeGraphAGE

        long_details = "D" * (EMBED_DETAILS_WINDOW + 1000)
        discovery = DiscoveryNode(
            id="disc-embed-1", agent_id="agent-x", type="insight",
            summary="summary text", details=long_details,
        )

        graph = KnowledgeGraphAGE.__new__(KnowledgeGraphAGE)

        # Fake embeddings service capturing the text it receives
        captured = {}

        async def fake_embed(text):
            captured["text"] = text
            return [0.0] * 1024

        mock_service = MagicMock()
        mock_service.embed = fake_embed

        with patch.object(graph, "_pgvector_available",
                          new=AsyncMock(return_value=True)), \
             patch.object(graph, "_store_embedding",
                          new=AsyncMock(return_value=None)), \
             patch("src.embeddings.get_embeddings_service",
                   new=AsyncMock(return_value=mock_service)), \
             patch("src.embeddings.embeddings_available", return_value=True):

            # Directly exercise the embed-text construction by invoking the
            # refresh path (no DB writes required beyond the stubbed helpers).
            with patch.object(graph, "get_discovery",
                              new=AsyncMock(return_value=discovery)):
                await graph._refresh_embedding("disc-embed-1")

        # Text must include much more than the legacy 500 chars.
        assert "text" in captured, "embed was not called"
        assert len(captured["text"]) > 500 + len(discovery.summary)
        # But capped at EMBED_DETAILS_WINDOW + summary + newline.
        assert len(captured["text"]) <= (
            len(discovery.summary) + 1 + EMBED_DETAILS_WINDOW
        )

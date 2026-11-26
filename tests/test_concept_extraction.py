"""Tests for ConceptExtractor — Option D concept graph pipeline."""

from collections import defaultdict
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from src.concept_extraction import ConceptExtractor, UnionFind


class TestUnionFind:
    def test_basic_union(self):
        uf = UnionFind(["a", "b", "c"])
        uf.union("a", "b")
        assert uf.find("a") == uf.find("b")
        assert uf.find("c") != uf.find("a")

    def test_groups(self):
        uf = UnionFind(["a", "b", "c", "d"])
        uf.union("a", "b")
        uf.union("c", "d")
        groups = uf.groups()
        assert len(groups) == 2
        for members in groups.values():
            assert len(members) == 2


class TestMergeSimilarTags:
    def test_high_cosine_and_cooccurrence_merges(self):
        """Two tags with high cosine + co-occurrence merge into one Concept."""
        extractor = ConceptExtractor()
        # Nearly identical embeddings
        tag_embeddings = {
            "python": np.array([1.0, 0.0, 0.0], dtype=np.float32),
            "python3": np.array([0.99, 0.1, 0.0], dtype=np.float32),
            "javascript": np.array([0.0, 1.0, 0.0], dtype=np.float32),
        }
        co_occurrence = {("python", "python3"): 5}

        clusters = extractor._merge_similar_tags(tag_embeddings, co_occurrence)

        # python and python3 should be merged; javascript separate
        merged = False
        for members in clusters.values():
            if "python" in members and "python3" in members:
                merged = True
        assert merged, "python and python3 should merge"

        # javascript should be separate
        for members in clusters.values():
            if "javascript" in members:
                assert len(members) == 1

    def test_low_cosine_no_merge(self):
        """Orthogonal tags should not merge."""
        extractor = ConceptExtractor()
        tag_embeddings = {
            "python": np.array([1.0, 0.0, 0.0], dtype=np.float32),
            "rust": np.array([0.0, 1.0, 0.0], dtype=np.float32),
        }
        co_occurrence = {("python", "rust"): 10}

        clusters = extractor._merge_similar_tags(tag_embeddings, co_occurrence)
        assert len(clusters) == 2

    def test_low_cooccurrence_no_merge(self):
        """High cosine but low co-occurrence should not merge."""
        extractor = ConceptExtractor()
        tag_embeddings = {
            "python": np.array([1.0, 0.0, 0.0], dtype=np.float32),
            "python3": np.array([0.99, 0.1, 0.0], dtype=np.float32),
        }
        co_occurrence = {("python", "python3"): 1}  # below threshold of 2

        clusters = extractor._merge_similar_tags(tag_embeddings, co_occurrence)
        assert len(clusters) == 2


class TestSplitBroadTag:
    def test_split_distinct_clusters(self):
        """Tag with bimodal discoveries splits into sub-Concepts."""
        extractor = ConceptExtractor(split_min_discoveries=4)

        # Two distinct clusters in embedding space
        embeddings = {}
        for i in range(5):
            embeddings[f"d_a_{i}"] = np.array([1.0, 0.0, 0.0], dtype=np.float32) + np.random.randn(3) * 0.01
        for i in range(5):
            embeddings[f"d_b_{i}"] = np.array([0.0, 1.0, 0.0], dtype=np.float32) + np.random.randn(3) * 0.01

        tag_discoveries = {
            "broad_tag": [f"d_a_{i}" for i in range(5)] + [f"d_b_{i}" for i in range(5)],
        }

        clusters = {"broad_tag": ["broad_tag"]}
        result = extractor._split_broad_tags(clusters, tag_discoveries, embeddings)

        # Should produce more than one cluster
        assert len(result) >= 2, f"Expected split but got {len(result)} clusters"

    def test_no_split_when_few_discoveries(self):
        """Tag with few discoveries should not split."""
        extractor = ConceptExtractor(split_min_discoveries=10)

        embeddings = {f"d_{i}": np.random.randn(3).astype(np.float32) for i in range(3)}
        tag_discoveries = {"small_tag": [f"d_{i}" for i in range(3)]}

        clusters = {"small_tag": ["small_tag"]}
        result = extractor._split_broad_tags(clusters, tag_discoveries, embeddings)

        assert len(result) == 1
        assert result[0] == ["small_tag"]


class TestCoOccurrence:
    def test_correct_co_occurrence(self):
        """Co-occurrence counts shared discoveries between tag pairs."""
        extractor = ConceptExtractor()
        tag_discoveries = {
            "python": ["d1", "d2", "d3"],
            "async": ["d2", "d3", "d4"],
            "rust": ["d5"],
        }

        co = extractor._compute_co_occurrence(tag_discoveries)

        # python and async share d2, d3
        assert co.get(("async", "python"), 0) == 2
        # rust shares nothing
        assert co.get(("python", "rust"), 0) == 0
        assert co.get(("async", "rust"), 0) == 0


class TestBuildConcepts:
    def test_concept_id_deterministic(self):
        """Same tags produce same concept_id."""
        extractor = ConceptExtractor()
        tag_discoveries = {"a": ["d1", "d2"], "b": ["d2", "d3"]}

        concepts1 = extractor._build_concepts([["a", "b"]], tag_discoveries)
        concepts2 = extractor._build_concepts([["b", "a"]], tag_discoveries)

        assert concepts1[0]["concept_id"] == concepts2[0]["concept_id"]

    def test_label_is_most_frequent(self):
        """Label should be the tag with most discoveries."""
        extractor = ConceptExtractor()
        tag_discoveries = {
            "rare": ["d1"],
            "common": ["d1", "d2", "d3"],
        }

        concepts = extractor._build_concepts([["rare", "common"]], tag_discoveries)
        assert concepts[0]["label"] == "common"


class TestNoEmbeddingsSkipsGracefully:
    @pytest.mark.asyncio
    async def test_no_embeddings_returns_skipped(self):
        """Missing embeddings don't crash — returns skipped status."""
        extractor = ConceptExtractor()

        mock_db = MagicMock()
        # graph_query returns tag-discovery pairs but no embeddings exist
        mock_db.graph_query = AsyncMock(return_value=[
            {"tag_name": "python", "discovery_id": "d1"},
            {"tag_name": "python", "discovery_id": "d2"},
        ])

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=[])  # no embeddings

        mock_db.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("src.concept_extraction.get_db", return_value=mock_db):
            result = await extractor.run()

        assert result["status"] == "skipped"
        assert "no embeddings" in result["reason"]


class TestEndToEndMock:
    @pytest.mark.asyncio
    async def test_full_pipeline_with_mocked_db(self):
        """Full pipeline with mocked DB returns expected Concepts."""
        extractor = ConceptExtractor(
            merge_cosine_threshold=0.75,
            merge_co_occurrence_min=2,
            split_min_discoveries=100,  # high threshold to avoid splits
        )

        # Phase 1: Tags with discoveries
        graph_rows = [
            {"tag_name": "python", "discovery_id": "d1"},
            {"tag_name": "python", "discovery_id": "d2"},
            {"tag_name": "python", "discovery_id": "d3"},
            {"tag_name": "py3", "discovery_id": "d1"},
            {"tag_name": "py3", "discovery_id": "d2"},
            {"tag_name": "rust", "discovery_id": "d4"},
            {"tag_name": "rust", "discovery_id": "d5"},
        ]

        # Phase 2: Embeddings — python and py3 are similar, rust is different
        embedding_rows = [
            {"discovery_id": "d1", "embedding": "[1.0,0.0,0.0]"},
            {"discovery_id": "d2", "embedding": "[0.98,0.1,0.0]"},
            {"discovery_id": "d3", "embedding": "[0.95,0.15,0.0]"},
            {"discovery_id": "d4", "embedding": "[0.0,1.0,0.0]"},
            {"discovery_id": "d5", "embedding": "[0.0,0.95,0.1]"},
        ]

        mock_db = MagicMock()
        mock_db.graph_query = AsyncMock(side_effect=[
            graph_rows,  # Phase 1: tag query
            [],  # Phase 7: concept node 1
            [], [], [],  # Phase 7: ABOUT edges for concept 1
            [],  # Phase 7: concept node 2
            [], [],  # Phase 7: ABOUT edges for concept 2
            [],  # Phase 8: concept id listing (the two concepts share no discovery)
        ])

        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=embedding_rows)

        mock_db.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("src.concept_extraction.get_db", return_value=mock_db):
            result = await extractor.run()

        assert result["status"] == "completed"
        assert result["concepts_created"] >= 1
        assert result["tags_processed"] >= 2
        assert result["stale_prune_skipped"] is None
        assert result["stale_prune_failures"] == 0


class TestTagQueryShape:
    def test_tag_query_returns_single_map_expression(self):
        """graph_query declares one ``result`` column; a two-column RETURN
        failed every run with a column-arity error, so no Concept was ever
        written."""
        from src.db.age_queries import query_tags_with_discoveries

        cypher, params = query_tags_with_discoveries()
        assert "RETURN {tag_name: t.name, discovery_id: d.id}" in cypher
        assert " AS tag_name" not in cypher
        assert params == {}

    def test_relates_to_merge_does_not_key_on_strength(self):
        """Strength changes between runs; keying MERGE on it would add a
        duplicate edge every day instead of updating the existing one."""
        from src.db.age_queries import create_concept_relates_to_edge

        cypher, params = create_concept_relates_to_edge("c1", "c2", strength=0.5)
        assert "MERGE (c1)-[r:RELATES_TO]->(c2)" in cypher
        assert "SET r.strength = ${strength}" in cypher
        assert params["strength"] == 0.5


class TestWriteFailuresAreSurfaced:
    @pytest.mark.asyncio
    async def test_write_failures_counted_and_warned_once(self):
        extractor = ConceptExtractor()
        concepts = [
            {"concept_id": "c1", "label": "a", "source_tags": ["a"], "discovery_ids": ["d1", "d2"]},
            {"concept_id": "c2", "label": "b", "source_tags": ["b"], "discovery_ids": ["d2"]},
        ]
        mock_db = MagicMock()
        mock_db.graph_query = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("src.concept_extraction.get_db", return_value=mock_db), \
                patch("src.concept_extraction.logger") as mock_logger:
            written = await extractor._write_to_graph(concepts, {})

        assert written["concepts"] == 0
        assert written["failures"] == {"concept_node": 2, "relates_to_edge": 1}
        # One warning per failure kind, not one per failed write.
        assert mock_logger.warning.call_count == 2
        assert "boom" in mock_logger.warning.call_args_list[0].args[0]

    @pytest.mark.asyncio
    async def test_partial_failure_reports_completed_with_errors(self):
        extractor = ConceptExtractor(split_min_discoveries=100)
        graph_rows = [
            {"tag_name": "python", "discovery_id": "d1"},
            {"tag_name": "python", "discovery_id": "d2"},
        ]
        embedding_rows = [
            {"discovery_id": "d1", "embedding": "[1.0,0.0,0.0]"},
            {"discovery_id": "d2", "embedding": "[0.98,0.1,0.0]"},
        ]
        mock_db = MagicMock()
        mock_db.graph_query = AsyncMock(side_effect=[
            graph_rows,  # Phase 1
            [],  # concept node
            [],  # ABOUT d1
            RuntimeError("edge refused"),  # ABOUT d2
        ])
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=embedding_rows)
        mock_db.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))

        with patch("src.concept_extraction.get_db", return_value=mock_db):
            result = await extractor.run()

        assert result["status"] == "completed_with_errors"
        assert result["concepts_created"] == 1
        assert result["about_edges_created"] == 1
        assert result["write_failures"] == {"about_edge": 1}
        # A failed edge write means the run's own output is incomplete, so the
        # previous run's concepts are left alone and the graph is not listed.
        assert mock_db.graph_query.await_count == 4
        assert result["stale_prune_skipped"] == "a write failed"
        assert result["stale_prune_failures"] == 0
        assert result["stale_concepts_deleted"] == 0


class TestBackgroundTaskSurfacesErrors:
    @pytest.mark.asyncio
    async def test_run_failure_logged_at_warning(self):
        import asyncio

        from src import background_tasks

        sleeps = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        failing = MagicMock()
        failing.return_value.run = AsyncMock(side_effect=RuntimeError("column arity"))

        with patch.object(background_tasks.asyncio, "sleep", sleeps), \
                patch("src.concept_extraction.ConceptExtractor", failing), \
                patch.object(background_tasks, "logger") as mock_logger:
            await background_tasks.concept_extraction_background_task()

        mock_logger.warning.assert_called_once()
        message = mock_logger.warning.call_args.args[0]
        assert "RuntimeError" in message and "column arity" in message
        mock_logger.debug.assert_not_called()


class TestStaleConceptsArePruned:
    """A regrouped cluster gets a new id; the run must remove the old vertex."""

    @staticmethod
    def _db(graph_side_effect, embedding_rows):
        mock_db = MagicMock()
        mock_db.graph_query = AsyncMock(side_effect=graph_side_effect)
        mock_conn = AsyncMock()
        mock_conn.fetch = AsyncMock(return_value=embedding_rows)
        mock_db.acquire = MagicMock(return_value=AsyncMock(
            __aenter__=AsyncMock(return_value=mock_conn),
            __aexit__=AsyncMock(return_value=False),
        ))
        return mock_db

    GRAPH_ROWS = [
        {"tag_name": "python", "discovery_id": "d1"},
        {"tag_name": "python", "discovery_id": "d2"},
    ]
    EMBEDDINGS = [
        {"discovery_id": "d1", "embedding": "[1.0,0.0,0.0]"},
        {"discovery_id": "d2", "embedding": "[0.98,0.1,0.0]"},
    ]

    @pytest.mark.asyncio
    async def test_deletes_only_concepts_this_run_did_not_produce(self):
        extractor = ConceptExtractor(split_min_discoveries=100)
        current_id = extractor._build_concepts(
            [["python"]], {"python": ["d1", "d2"]}
        )[0]["concept_id"]
        calls = []

        async def graph_query(cypher, params=None):
            calls.append((cypher, params))
            if "TAGGED" in cypher:
                return self.GRAPH_ROWS
            if "DETACH DELETE" in cypher:
                return [params["concept_id"]]
            if "RETURN c.id" in cypher:
                return [current_id, "stale-a", "stale-b"]
            return []

        mock_db = self._db(graph_query, self.EMBEDDINGS)
        with patch("src.concept_extraction.get_db", return_value=mock_db):
            result = await extractor.run()

        deleted = [p["concept_id"] for c, p in calls if "DETACH DELETE" in c]
        assert deleted == ["stale-a", "stale-b"]
        assert result["status"] == "completed"
        assert result["stale_concepts_deleted"] == 2
        assert result["stale_prune_skipped"] is None

    @pytest.mark.asyncio
    async def test_no_delete_when_a_planned_concept_was_not_written(self):
        extractor = ConceptExtractor(split_min_discoveries=100)
        calls = []

        async def graph_query(cypher, params=None):
            calls.append(cypher)
            if "TAGGED" in cypher:
                return self.GRAPH_ROWS
            if "MERGE (c:Concept" in cypher:
                raise RuntimeError("graph refused")
            return ["stale-a"]

        mock_db = self._db(graph_query, self.EMBEDDINGS)
        with patch("src.concept_extraction.get_db", return_value=mock_db):
            result = await extractor.run()

        assert not any("RETURN c.id" in c for c in calls)
        assert result["stale_concepts_deleted"] == 0
        assert result["stale_prune_skipped"] == "not every planned concept was written"

    @pytest.mark.asyncio
    async def test_delete_failure_is_counted_and_reported(self):
        extractor = ConceptExtractor(split_min_discoveries=100)

        async def graph_query(cypher, params=None):
            if "TAGGED" in cypher:
                return self.GRAPH_ROWS
            if "DETACH DELETE" in cypher:
                if params["concept_id"] == "stale-a":
                    raise RuntimeError("delete refused")
                return [params["concept_id"]]
            if "RETURN c.id" in cypher:
                return ["stale-a", "stale-b"]
            return []

        mock_db = self._db(graph_query, self.EMBEDDINGS)
        with patch("src.concept_extraction.get_db", return_value=mock_db):
            result = await extractor.run()

        assert result["status"] == "completed_with_errors"
        assert result["stale_concepts_deleted"] == 1
        assert result["stale_prune_failures"] == 1

    @pytest.mark.asyncio
    async def test_unexpected_listing_shape_deletes_nothing(self):
        extractor = ConceptExtractor(split_min_discoveries=100)
        calls = []

        async def graph_query(cypher, params=None):
            calls.append(cypher)
            if "TAGGED" in cypher:
                return self.GRAPH_ROWS
            if "DETACH DELETE" in cypher:
                return [params["concept_id"]]
            if "RETURN c.id" in cypher:
                return ["stale-a", {"id": "stale-b"}]
            return []

        mock_db = self._db(graph_query, self.EMBEDDINGS)
        with patch("src.concept_extraction.get_db", return_value=mock_db):
            result = await extractor.run()

        assert not any("DETACH DELETE" in c for c in calls)
        assert result["status"] == "completed_with_errors"
        assert result["stale_prune_skipped"] == "concept listing had an unexpected shape"
        assert result["stale_concepts_deleted"] == 0

    def test_concept_id_listing_decodes_to_bare_strings(self):
        """The prune compares listed ids with planned ids as plain strings."""
        from src.db.mixins.graph import GraphMixin

        decoded = GraphMixin._decode_agtype_value('"1facbd2ffe47f5ff"')
        assert decoded == "1facbd2ffe47f5ff"

    def test_delete_query_removes_the_vertex_with_its_edges(self):
        from src.db.age_queries import query_delete_concept

        cypher, params = query_delete_concept("abc")
        assert "DETACH DELETE" in cypher
        assert "RETURN c.id" in cypher
        assert "${concept_id}" in cypher
        assert params == {"concept_id": "abc"}

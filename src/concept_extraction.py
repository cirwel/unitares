"""
Concept Extraction — Option D (Tags Seed, Embeddings Refine)

Reads Tag→Discovery relationships from AGE, fetches embeddings from
core.discovery_embeddings, and produces Concept nodes by:
  1. Merging similar tags (co-occurrence + embedding cosine > threshold)
  2. Splitting broad tags into sub-Concepts (agglomerative clustering)
  3. Writing Concept vertices and ABOUT/RELATES_TO edges to AGE
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from typing import Any, Dict, List, Set

import numpy as np

from src.db import get_db
from src.db.age_queries import (
    create_about_edge,
    create_concept_node,
    create_concept_relates_to_edge,
    query_tags_with_discoveries,
)
from src.logging_utils import get_logger

logger = get_logger(__name__)

# Thresholds
MERGE_COSINE_THRESHOLD = 0.75
MERGE_CO_OCCURRENCE_MIN = 2
SPLIT_MIN_DISCOVERIES = 10
SPLIT_DISTANCE_THRESHOLD = 0.5
MIN_TAG_DISCOVERIES = 2


class UnionFind:
    """Simple union-find for tag merging."""

    def __init__(self, items: List[str]):
        self.parent: Dict[str, str] = {x: x for x in items}
        self.rank: Dict[str, int] = {x: 0 for x in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1

    def groups(self) -> Dict[str, List[str]]:
        clusters: Dict[str, List[str]] = defaultdict(list)
        for item in self.parent:
            clusters[self.find(item)].append(item)
        return dict(clusters)


class ConceptExtractor:
    """Extracts Concepts from Tags using hybrid co-occurrence + embedding similarity."""

    def __init__(
        self,
        merge_cosine_threshold: float = MERGE_COSINE_THRESHOLD,
        merge_co_occurrence_min: int = MERGE_CO_OCCURRENCE_MIN,
        split_min_discoveries: int = SPLIT_MIN_DISCOVERIES,
        split_distance_threshold: float = SPLIT_DISTANCE_THRESHOLD,
    ):
        self.merge_cosine_threshold = merge_cosine_threshold
        self.merge_co_occurrence_min = merge_co_occurrence_min
        self.split_min_discoveries = split_min_discoveries
        self.split_distance_threshold = split_distance_threshold

    async def run(self) -> Dict[str, Any]:
        """Full pipeline. Returns summary stats."""
        # Phase 1: Fetch tag→discovery mappings from AGE
        tag_discoveries = await self._fetch_tags_with_discoveries()
        if not tag_discoveries:
            return {"status": "skipped", "reason": "no tags found"}

        # Phase 2: Fetch embeddings from SQL
        all_discovery_ids = set()
        for ids in tag_discoveries.values():
            all_discovery_ids.update(ids)
        embeddings = await self._fetch_embeddings(all_discovery_ids)
        if not embeddings:
            return {"status": "skipped", "reason": "no embeddings found"}

        # Phase 3: Compute per-tag mean embeddings
        tag_embeddings = self._compute_tag_embeddings(tag_discoveries, embeddings)
        if not tag_embeddings:
            return {"status": "skipped", "reason": "no tags with sufficient embeddings"}

        # Phase 4: Merge similar tags (Union-Find)
        co_occurrence = self._compute_co_occurrence(tag_discoveries)
        clusters = self._merge_similar_tags(tag_embeddings, co_occurrence)

        # Phase 5: Split broad tags/clusters
        split_clusters = self._split_broad_tags(
            clusters, tag_discoveries, embeddings
        )

        # Phase 6: Build concept definitions
        concepts = self._build_concepts(split_clusters, tag_discoveries)

        # Phase 7: Write to graph
        written = await self._write_to_graph(concepts, tag_discoveries)

        return {
            "status": "completed",
            "tags_processed": len(tag_embeddings),
            "concepts_created": written["concepts"],
            "about_edges_created": written["about_edges"],
            "relates_to_edges_created": written["relates_to_edges"],
        }

    async def _fetch_tags_with_discoveries(self) -> Dict[str, List[str]]:
        """Phase 1: Get {tag_name: [discovery_id, ...]} from AGE."""
        db = get_db()
        q, p = query_tags_with_discoveries()
        rows = await db.graph_query(q, p)

        tag_map: Dict[str, List[str]] = defaultdict(list)
        for row in rows:
            tag_name = row.get("tag_name")
            discovery_id = row.get("discovery_id")
            if tag_name and discovery_id:
                tag_map[tag_name].append(discovery_id)

        # Filter: only tags with enough discoveries
        return {
            tag: ids
            for tag, ids in tag_map.items()
            if len(ids) >= MIN_TAG_DISCOVERIES
        }

    async def _fetch_embeddings(
        self, discovery_ids: Set[str]
    ) -> Dict[str, np.ndarray]:
        """Fetch embeddings from the active pgvector table."""
        if not discovery_ids:
            return {}

        from src.embeddings import get_active_table_name
        table = get_active_table_name()

        db = get_db()
        result: Dict[str, np.ndarray] = {}

        # Batch fetch in chunks of 500
        id_list = list(discovery_ids)
        for i in range(0, len(id_list), 500):
            chunk = id_list[i : i + 500]
            placeholders = ", ".join(f"${j + 1}" for j in range(len(chunk)))
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    f"SELECT discovery_id, embedding::text FROM {table} "
                    f"WHERE discovery_id IN ({placeholders})",
                    *chunk,
                )
            for row in rows:
                vec_str = row["embedding"]
                # Parse pgvector text format: "[0.1,0.2,...]"
                vec = np.fromstring(vec_str.strip("[]"), sep=",", dtype=np.float32)
                result[row["discovery_id"]] = vec

        return result

    def _compute_tag_embeddings(
        self,
        tag_discoveries: Dict[str, List[str]],
        embeddings: Dict[str, np.ndarray],
    ) -> Dict[str, np.ndarray]:
        """Phase 3: Mean embedding per tag."""
        tag_embs: Dict[str, np.ndarray] = {}
        for tag, disc_ids in tag_discoveries.items():
            vecs = [embeddings[d] for d in disc_ids if d in embeddings]
            if len(vecs) >= MIN_TAG_DISCOVERIES:
                tag_embs[tag] = np.mean(vecs, axis=0)
        return tag_embs

    def _compute_co_occurrence(
        self, tag_discoveries: Dict[str, List[str]]
    ) -> Dict[tuple[str, str], int]:
        """Compute co-occurrence: number of discoveries shared between tag pairs."""
        # Build reverse index: discovery -> set of tags
        disc_to_tags: Dict[str, Set[str]] = defaultdict(set)
        for tag, disc_ids in tag_discoveries.items():
            for d in disc_ids:
                disc_to_tags[d].add(tag)

        co: Dict[tuple[str, str], int] = defaultdict(int)
        for tags in disc_to_tags.values():
            tag_list = sorted(tags)
            for i in range(len(tag_list)):
                for j in range(i + 1, len(tag_list)):
                    co[(tag_list[i], tag_list[j])] += 1

        return dict(co)

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        """Cosine similarity between two vectors."""
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))

    def _merge_similar_tags(
        self,
        tag_embeddings: Dict[str, np.ndarray],
        co_occurrence: Dict[tuple[str, str], int],
    ) -> Dict[str, List[str]]:
        """Phase 4: Merge tags via Union-Find based on cosine + co-occurrence."""
        tags = list(tag_embeddings.keys())
        uf = UnionFind(tags)

        for i in range(len(tags)):
            for j in range(i + 1, len(tags)):
                a, b = tags[i], tags[j]
                pair = (min(a, b), max(a, b))
                cos = self._cosine_similarity(tag_embeddings[a], tag_embeddings[b])
                co = co_occurrence.get(pair, 0)
                if cos > self.merge_cosine_threshold and co >= self.merge_co_occurrence_min:
                    uf.union(a, b)

        return uf.groups()

    def _split_broad_tags(
        self,
        clusters: Dict[str, List[str]],
        tag_discoveries: Dict[str, List[str]],
        embeddings: Dict[str, np.ndarray],
    ) -> List[List[str]]:
        """Phase 5: Split clusters with many discoveries into sub-Concepts."""
        result: List[List[str]] = []

        for _root, tag_group in clusters.items():
            # Collect all discoveries for this cluster
            disc_ids = set()
            for tag in tag_group:
                disc_ids.update(tag_discoveries.get(tag, []))

            if len(disc_ids) < self.split_min_discoveries:
                result.append(tag_group)
                continue

            # Get embeddings for these discoveries
            disc_list = [d for d in disc_ids if d in embeddings]
            if len(disc_list) < self.split_min_discoveries:
                result.append(tag_group)
                continue

            # Try agglomerative clustering
            sub_clusters = self._agglomerative_split(disc_list, embeddings)
            if len(sub_clusters) <= 1:
                result.append(tag_group)
                continue

            # Map sub-clusters back to tags
            # Each sub-cluster gets the tags whose discoveries overlap most
            for sc_disc_ids in sub_clusters:
                sc_set = set(sc_disc_ids)
                sub_tags = []
                for tag in tag_group:
                    tag_set = set(tag_discoveries.get(tag, []))
                    overlap = len(tag_set & sc_set)
                    if overlap > 0:
                        sub_tags.append(tag)
                if sub_tags:
                    result.append(sub_tags)
                else:
                    # Shouldn't happen, but safety: keep original group
                    result.append(tag_group)
                    break

        return result

    def _agglomerative_split(
        self,
        disc_ids: List[str],
        embeddings: Dict[str, np.ndarray],
    ) -> List[List[str]]:
        """Agglomerative clustering using scipy if available, else simple threshold."""
        vecs = np.array([embeddings[d] for d in disc_ids])

        try:
            from scipy.cluster.hierarchy import fcluster, linkage
            from scipy.spatial.distance import pdist

            distances = pdist(vecs, metric="cosine")
            Z = linkage(distances, method="average")
            labels = fcluster(Z, t=self.split_distance_threshold, criterion="distance")

            clusters: Dict[int, List[str]] = defaultdict(list)
            for idx, label in enumerate(labels):
                clusters[int(label)].append(disc_ids[idx])
            return list(clusters.values())

        except ImportError:
            # Fallback: simple pairwise threshold-based clustering
            return self._simple_threshold_split(disc_ids, vecs)

    def _simple_threshold_split(
        self, disc_ids: List[str], vecs: np.ndarray
    ) -> List[List[str]]:
        """Fallback clustering when scipy is unavailable."""
        n = len(disc_ids)
        assigned = [False] * n
        clusters: List[List[str]] = []

        for i in range(n):
            if assigned[i]:
                continue
            cluster = [disc_ids[i]]
            assigned[i] = True
            for j in range(i + 1, n):
                if assigned[j]:
                    continue
                cos = self._cosine_similarity(vecs[i], vecs[j])
                if cos > (1.0 - self.split_distance_threshold):
                    cluster.append(disc_ids[j])
                    assigned[j] = True
            clusters.append(cluster)

        return clusters

    def _build_concepts(
        self,
        tag_clusters: List[List[str]],
        tag_discoveries: Dict[str, List[str]],
    ) -> List[Dict[str, Any]]:
        """Phase 6: Build concept definitions from tag clusters."""
        concepts = []
        for tag_group in tag_clusters:
            sorted_tags = sorted(tag_group)
            concept_id = hashlib.sha256(
                "|".join(sorted_tags).encode()
            ).hexdigest()[:16]

            # Label: most frequent tag (by discovery count)
            best_tag = max(
                tag_group,
                key=lambda t: len(tag_discoveries.get(t, [])),
            )

            # Collect all discovery IDs
            disc_ids = set()
            for tag in tag_group:
                disc_ids.update(tag_discoveries.get(tag, []))

            concepts.append(
                {
                    "concept_id": concept_id,
                    "label": best_tag,
                    "source_tags": sorted_tags,
                    "discovery_ids": list(disc_ids),
                }
            )

        return concepts

    async def _write_to_graph(
        self,
        concepts: List[Dict[str, Any]],
        tag_discoveries: Dict[str, List[str]],
    ) -> Dict[str, int]:
        """Phase 7: Write Concepts, ABOUT edges, and RELATES_TO edges to AGE."""
        db = get_db()
        concept_count = 0
        about_count = 0
        relates_count = 0

        # Create Concept nodes and ABOUT edges
        for concept in concepts:
            try:
                q, p = create_concept_node(
                    concept["concept_id"],
                    concept["label"],
                    source_tags=concept["source_tags"],
                )
                await db.graph_query(q, p)
                concept_count += 1

                # ABOUT edges from discoveries to concept
                for disc_id in concept["discovery_ids"]:
                    try:
                        q, p = create_about_edge(disc_id, concept["concept_id"])
                        await db.graph_query(q, p)
                        about_count += 1
                    except Exception as e:
                        logger.debug(f"ABOUT edge failed for {disc_id}: {e}")
            except Exception as e:
                logger.debug(f"Concept node failed for {concept['concept_id']}: {e}")

        # RELATES_TO edges between concepts sharing discoveries
        for i in range(len(concepts)):
            for j in range(i + 1, len(concepts)):
                shared = set(concepts[i]["discovery_ids"]) & set(
                    concepts[j]["discovery_ids"]
                )
                if shared:
                    strength = len(shared) / min(
                        len(concepts[i]["discovery_ids"]),
                        len(concepts[j]["discovery_ids"]),
                    )
                    try:
                        q, p = create_concept_relates_to_edge(
                            concepts[i]["concept_id"],
                            concepts[j]["concept_id"],
                            strength=round(strength, 3),
                        )
                        await db.graph_query(q, p)
                        relates_count += 1
                    except Exception as e:
                        logger.debug(f"RELATES_TO edge failed: {e}")

        return {
            "concepts": concept_count,
            "about_edges": about_count,
            "relates_to_edges": relates_count,
        }

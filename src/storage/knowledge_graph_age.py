"""
AGE-backed Knowledge Graph Implementation

Apache AGE implementation of the knowledge graph interface.
Uses PostgreSQL + AGE for native graph queries.
"""

from __future__ import annotations

import asyncio
import functools
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from src.logging_utils import get_logger
from src.knowledge_graph import (
    DiscoveryNode,
    ResponseTo,  # noqa: F401
    apply_closure_reopen_rule,
    closure_evidence_from_stored,
    closure_evidence_to_json,
)
from src.mcp_handlers.knowledge.limits import EMBED_DETAILS_WINDOW
import src.db as db_module
from src.storage.knowledge_graph_age_search import KnowledgeGraphAGESearchMixin
from src.db.age_queries import (
    create_discovery_node,
    create_agent_node,
    create_authored_edge,
    create_responds_to_edge,
    create_related_to_edge,
    create_tagged_edge,
    create_supersedes_edge,  # noqa: F401
)

logger = get_logger(__name__)

# Once-per-process flag for the embedding-skip warning (#2293). A discovery
# written while sentence-transformers is missing lands in the graph but never
# gets a pgvector row, and nothing retries it once the extra is installed. The
# import-time warning in src.embeddings fires far from the write and is easy to
# miss in a server log, so the first skipped write says so here, naming the
# consequence and the remedy; later skips stay at debug to keep the log quiet.
_embedding_skip_warned = False


def _log_embedding_skipped(discovery_id: str, op: str) -> None:
    """Log an embedding write skipped because embeddings are unavailable.

    WARNING on the first skip in this process, DEBUG afterwards.
    """
    global _embedding_skip_warned
    if _embedding_skip_warned:
        logger.debug(
            f"Embedding {op} skipped for {discovery_id}: embeddings unavailable"
        )
        return
    _embedding_skip_warned = True
    logger.warning(
        f"Embedding {op} skipped for {discovery_id}: sentence-transformers is not "
        "installed, so semantic search will not find this entry until it is "
        "re-embedded (FTS still works). Install the embeddings extra "
        '(pip install -e ".[full,embeddings]" -c constraints.txt; the Docker '
        "image needs sentence-transformers uncommented in requirements-docker.txt), "
        "then backfill with python scripts/migration/reembed_corpus.py "
        "(fills only rows with no vector by default), run with the server's UNITARES_EMBEDDING_MODEL in the "
        "environment (or pass --model) so it fills the same table (a skipped "
        "refresh leaves the entry's old vector in place, which the default "
        "run will not rewrite; pass --rebuild to re-embed every row). "
        "Further skips log at debug."
    )


# A failure while embeddings ARE available is a fault, not the install's
# default state, so each one logs at WARNING with the discovery id (#2293).
# These used to log at debug, which hid a row left without a vector (store) or
# with a stale one (refresh) exactly as the unavailable branch once did.
_BACKFILL_MISSING_HINT = (
    "semantic search will not find it until it is re-embedded "
    "(python scripts/migration/reembed_corpus.py fills rows with no vector)"
)
_BACKFILL_STALE_HINT = (
    "its previous vector, if any, stays in place "
    "(python scripts/migration/reembed_corpus.py --rebuild rewrites it)"
)


def _log_store_task_failure(discovery_id: str, task: "asyncio.Task") -> None:
    """Done-callback for a background _store_embedding task.

    _store_embedding logs its own insert failures; this catches anything raised
    before the insert (resolving the table or model). A cancelled task, at
    shutdown for instance, is not a failure and must not raise from
    task.exception().
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            f"Embedding store failed for {discovery_id}: {exc}; "
            f"{_BACKFILL_MISSING_HINT}"
        )


class KnowledgeGraphAGE(KnowledgeGraphAGESearchMixin):
    """
    AGE-backed knowledge graph implementation.
    
    Uses Apache AGE for native graph queries while maintaining compatibility
    with the existing KnowledgeGraph interface.
    """

    def __init__(self, graph_name: str = "governance_graph"):
        # Note: the actual AGE graph name used at query time is owned by the DB backend
        # (see PostgresBackend._age_graph). We keep a local copy for SQL operations
        # that reference the graph schema (e.g., CREATE INDEX ON <graph>.Label(...)).
        self.graph_name = graph_name
        self._db = None
        self._indexes_created = False
        self.rate_limit_stores_per_hour = 20  # Max stores per agent per hour
        # Background embedding refreshes, one pass at a time per discovery
        # (_schedule_embedding_refresh).
        self._embedding_refresh_running: set[str] = set()
        self._embedding_refresh_again: set[str] = set()

    @staticmethod
    def _parse_optional_datetime(value: Any) -> Optional[datetime]:
        """Parse ISO-like timestamps from discovery payloads."""
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value.replace('Z', '+00:00'))
            except Exception:
                return None
        return None

    @staticmethod
    def _build_discovery_metadata(discovery: DiscoveryNode) -> Optional[Dict[str, Any]]:
        """Build metadata payload stored on the AGE discovery node."""
        metadata: Dict[str, Any] = {}

        if discovery.related_to:
            metadata["related_to"] = discovery.related_to
        if discovery.references_files:
            metadata["references_files"] = discovery.references_files
        if discovery.confidence is not None:
            metadata["confidence"] = discovery.confidence
        if discovery.provenance:
            metadata["provenance"] = discovery.provenance
        if discovery.provenance_chain:
            metadata["provenance_chain"] = discovery.provenance_chain
        if discovery.response_to:
            metadata["response_to"] = {
                "discovery_id": discovery.response_to.discovery_id,
                "response_type": discovery.response_to.response_type,
            }

        return metadata or None

    async def _persist_discovery_row(
        self,
        conn,
        discovery: DiscoveryNode,
        *,
        created_at: datetime,
        resolved_at: Optional[datetime],
    ) -> None:
        """Persist discovery into durable PostgreSQL knowledge tables."""
        from config.governance_config import GovernanceConfig

        updated_at = self._parse_optional_datetime(discovery.updated_at)
        response_to_id = None
        response_type = None
        if discovery.response_to:
            response_to_id = discovery.response_to.discovery_id
            response_type = discovery.response_to.response_type

        await conn.execute(
            """
            INSERT INTO knowledge.discoveries (
                id, agent_id, type, severity, status,
                created_at, updated_at, resolved_at,
                summary, details, tags, references_files, related_to,
                response_to_id, response_type, provenance, epoch,
                closure_class, closure_evidence
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8,
                $9, $10, $11, $12, $13,
                $14, $15, $16, $17,
                $18, $19
            )
            ON CONFLICT (id) DO UPDATE SET
                agent_id = EXCLUDED.agent_id,
                type = EXCLUDED.type,
                severity = EXCLUDED.severity,
                status = EXCLUDED.status,
                created_at = EXCLUDED.created_at,
                updated_at = EXCLUDED.updated_at,
                resolved_at = EXCLUDED.resolved_at,
                summary = EXCLUDED.summary,
                details = EXCLUDED.details,
                tags = EXCLUDED.tags,
                references_files = EXCLUDED.references_files,
                related_to = EXCLUDED.related_to,
                response_to_id = EXCLUDED.response_to_id,
                response_type = EXCLUDED.response_type,
                provenance = EXCLUDED.provenance,
                epoch = EXCLUDED.epoch,
                closure_class = EXCLUDED.closure_class,
                closure_evidence = EXCLUDED.closure_evidence
            """,
            discovery.id,
            discovery.agent_id,
            discovery.type,
            discovery.severity or "low",
            discovery.status or "open",
            created_at,
            updated_at,
            resolved_at,
            discovery.summary,
            discovery.details or "",
            discovery.tags or [],
            discovery.references_files or [],
            discovery.related_to or [],
            response_to_id,
            response_type,
            json.dumps(discovery.provenance) if discovery.provenance else None,
            GovernanceConfig.CURRENT_EPOCH,
            # The row carries the pair the AGE vertex gets, so a classified
            # node round-trips (graph unavailable, or rebuilt from SQL). Like
            # every other column, an upsert takes the incoming values, status
            # included, so a re-add that reopens a row clears its class
            # rather than tripping discoveries_closure_class_requires_closed.
            discovery.closure_class,
            closure_evidence_to_json(discovery.closure_evidence),
        )

        await self._sync_discovery_tags(conn, discovery.id, discovery.tags or [])
        await self._sync_discovery_edges(conn, discovery, created_at)

    async def _sync_discovery_tags(self, conn, discovery_id: str, tags: List[str]) -> None:
        """Sync normalized tag rows for one discovery."""
        await conn.execute(
            "DELETE FROM knowledge.discovery_tags WHERE discovery_id = $1",
            discovery_id,
        )
        if tags:
            await conn.executemany(
                """
                INSERT INTO knowledge.discovery_tags (discovery_id, tag)
                VALUES ($1, $2)
                ON CONFLICT (discovery_id, tag) DO NOTHING
                """,
                [(discovery_id, tag) for tag in tags],
            )

    async def _sync_discovery_edges(
        self,
        conn,
        discovery: DiscoveryNode,
        created_at: datetime,
    ) -> None:
        """Sync durable edge rows sourced from a discovery payload."""
        await conn.execute(
            """
            DELETE FROM knowledge.discovery_edges
            WHERE src_id = $1 AND edge_type IN ('related', 'responds_to')
            """,
            discovery.id,
        )

        edge_rows = []
        if discovery.response_to:
            edge_rows.append(
                (
                    discovery.id,
                    discovery.response_to.discovery_id,
                    "responds_to",
                    discovery.response_to.response_type,
                    1.0,
                    created_at,
                    discovery.agent_id,
                    None,
                )
            )

        for related_id in discovery.related_to:
            edge_rows.append(
                (
                    discovery.id,
                    related_id,
                    "related",
                    None,
                    1.0,
                    created_at,
                    discovery.agent_id,
                    None,
                )
            )

        if edge_rows:
            # Filter out dst_ids that have no row in knowledge.discoveries.
            # AGE and the PG table can drift (AGE→PG canonical flip on
            # 2026-05-04 left 4 AGE-only Discovery nodes; find_similar
            # returned those IDs into related_to, and the unguarded INSERT
            # below tripped the discovery_edges_dst_id_fkey FK constraint,
            # rolling back the entire tagged write. Tags landed empty even
            # on the underlying discovery row that did get inserted earlier
            # in the transaction (KG 2026-05-10T00:58:42 by d0832eaf). The
            # orphans on the live DB were cleaned up out-of-band; this
            # guard makes future drift survivable instead of write-fatal.
            dst_ids = {row[1] for row in edge_rows}
            existing = await conn.fetch(
                """
                SELECT id FROM knowledge.discoveries
                WHERE id = ANY($1::text[])
                """,
                list(dst_ids),
            )
            existing_set = {r["id"] for r in existing}
            missing = dst_ids - existing_set
            if missing:
                logger.warning(
                    "_sync_discovery_edges: dropping %d edge(s) to missing "
                    "dst_id(s) for src_id=%r — drift between graph backend and "
                    "knowledge.discoveries. Missing: %s",
                    len(missing),
                    discovery.id,
                    sorted(missing),
                )
                edge_rows = [row for row in edge_rows if row[1] in existing_set]
            if edge_rows:
                await conn.executemany(
                    """
                    INSERT INTO knowledge.discovery_edges (
                        src_id, dst_id, edge_type, response_type, weight,
                        created_at, created_by, metadata
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (src_id, dst_id, edge_type) DO UPDATE SET
                        response_type = EXCLUDED.response_type,
                        weight = EXCLUDED.weight,
                        created_at = EXCLUDED.created_at,
                        created_by = EXCLUDED.created_by,
                        metadata = EXCLUDED.metadata
                    """,
                    edge_rows,
                )

    async def _sync_updated_discovery_row(
        self,
        conn,
        discovery_id: str,
        updates: Dict[str, Any],
    ) -> None:
        """Sync AGE discovery updates into durable PostgreSQL tables."""
        set_parts = []
        params: List[Any] = []
        updates = apply_closure_reopen_rule(updates)

        field_map = {
            "status": "status",
            "severity": "severity",
            "type": "type",
            "summary": "summary",
            "details": "details",
            "closure_class": "closure_class",
        }

        for key, column in field_map.items():
            if key in updates:
                params.append(updates[key])
                set_parts.append(f"{column} = ${len(params)}")

        if "closure_evidence" in updates:
            params.append(closure_evidence_to_json(updates["closure_evidence"]))
            set_parts.append(f"closure_evidence = ${len(params)}")

        for key in ("resolved_at", "updated_at"):
            if key in updates:
                params.append(self._parse_optional_datetime(updates[key]))
                set_parts.append(f"{key} = ${len(params)}")

        if "tags" in updates:
            params.append(updates["tags"])
            set_parts.append(f"tags = ${len(params)}")

        if set_parts:
            params.append(discovery_id)
            await conn.execute(
                f"""
                UPDATE knowledge.discoveries
                SET {', '.join(set_parts)}
                WHERE id = ${len(params)}
                """,
                *params,
            )

        if "tags" in updates:
            await self._sync_discovery_tags(conn, discovery_id, updates["tags"] or [])

    async def _sync_age_tag_edges(
        self,
        db,
        conn,
        discovery_id: str,
        tags: List[str],
    ) -> None:
        """Replace one discovery's derived TAGGED relationships.

        ``knowledge.discoveries.tags`` is canonical. Replacing the projection
        inside the caller's transaction handles both halves of an edit: newly
        added tags become searchable and removed tags stop matching.
        """
        delete_cypher = """
            MATCH (d:Discovery {id: ${discovery_id}})-[r:TAGGED]->(t:Tag)
            DELETE r
            RETURN count(r)
        """
        await db.graph_query(
            delete_cypher,
            {"discovery_id": discovery_id},
            conn=conn,
        )
        for tag in tags:
            tagged_cypher, tagged_params = create_tagged_edge(
                discovery_id=discovery_id,
                tag_name=tag,
            )
            await db.graph_query(tagged_cypher, tagged_params, conn=conn)

    async def _delete_orphan_age_tags(self, db, conn) -> int:
        """Delete Tag vertices left with no incoming TAGGED relationship."""
        cypher = """
            MATCH (t:Tag)
            OPTIONAL MATCH (d:Discovery)-[:TAGGED]->(t)
            WITH t, count(d) AS inbound
            WHERE inbound = 0
            DELETE t
            RETURN count(t)
        """
        result = await db.graph_query(cypher, {}, conn=conn)
        if result and isinstance(result[0], (int, float)):
            return int(result[0])
        return 0

    async def _find_duplicate_age_tag_groups(
        self,
        db,
        *,
        conn=None,
    ) -> List[Dict[str, Any]]:
        """Return same-name Tag groups with deterministic vertex-id ordering."""
        cypher = """
            MATCH (t:Tag)
            WITH t
            ORDER BY id(t)
            WITH t.name AS name, collect(id(t)) AS ids
            WHERE size(ids) > 1
            RETURN {name: name, ids: ids}
            ORDER BY name
        """
        rows = await db.graph_query(cypher, {}, conn=conn)
        groups: List[Dict[str, Any]] = []
        for row in rows or []:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("name"), str)
                or not row["name"]
            ):
                raise RuntimeError(f"Malformed duplicate AGE Tag group: {row!r}")
            try:
                ids = sorted({int(vertex_id) for vertex_id in row.get("ids", [])})
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Malformed duplicate AGE Tag vertex ids: {row!r}"
                ) from exc
            if len(ids) > 1:
                groups.append({"name": str(row["name"]), "ids": ids})
        return sorted(groups, key=lambda group: (group["name"], group["ids"]))

    async def _consolidate_duplicate_age_tags(
        self,
        db,
        conn,
        groups: List[Dict[str, Any]],
    ) -> int:
        """Rewire and remove duplicate Tag vertices inside one transaction.

        The lowest AGE vertex id is the canonical node. All incoming TAGGED
        relationships are MERGEd onto it before the duplicate's relationships
        and vertex are deleted. A postcondition fails the transaction if any
        same-name duplicate remains, so later tag MERGEs never run against an
        ambiguous match set.
        """
        consolidated = 0
        for group in groups:
            ids = sorted({int(vertex_id) for vertex_id in group.get("ids", [])})
            if len(ids) < 2:
                continue
            canonical_id = ids[0]
            for duplicate_id in ids[1:]:
                await db.graph_query(
                    """
                    MATCH (d:Discovery)-[:TAGGED]->(duplicate:Tag)
                    MATCH (canonical:Tag)
                    WHERE id(duplicate) = ${duplicate_id}
                      AND id(canonical) = ${canonical_id}
                    MERGE (d)-[:TAGGED]->(canonical)
                    RETURN count(d)
                    """,
                    {
                        "canonical_id": canonical_id,
                        "duplicate_id": duplicate_id,
                    },
                    conn=conn,
                )
                await db.graph_query(
                    """
                    MATCH (:Discovery)-[r:TAGGED]->(duplicate:Tag)
                    WHERE id(duplicate) = ${duplicate_id}
                    DELETE r
                    RETURN count(r)
                    """,
                    {"duplicate_id": duplicate_id},
                    conn=conn,
                )
                deleted = await db.graph_query(
                    """
                    MATCH (duplicate:Tag)
                    WHERE id(duplicate) = ${duplicate_id}
                    DELETE duplicate
                    RETURN count(duplicate)
                    """,
                    {"duplicate_id": duplicate_id},
                    conn=conn,
                )
                deleted_count = (
                    int(deleted[0])
                    if deleted and isinstance(deleted[0], (int, float))
                    else 0
                )
                if deleted_count != 1:
                    raise RuntimeError(
                        "AGE duplicate Tag consolidation did not delete exactly "
                        f"one vertex (name={group['name']!r}, id={duplicate_id})"
                    )
                consolidated += 1

        remaining = await self._find_duplicate_age_tag_groups(db, conn=conn)
        if remaining:
            raise RuntimeError(
                "AGE duplicate Tag consolidation postcondition failed: "
                f"{len(remaining)} same-name group(s) remain"
            )
        return consolidated

    async def _get_db(self):
        """Get database backend (lazy initialization)."""
        if self._db is None:
            self._db = db_module.get_db()
            await self._db.init()

            # Best-effort: align our graph_name with backend config
            try:
                if hasattr(self._db, "_age_graph"):
                    self.graph_name = getattr(self._db, "_age_graph") or self.graph_name
                elif hasattr(self._db, "_postgres") and getattr(self._db, "_postgres_available", False):
                    pg = getattr(self._db, "_postgres")
                    if hasattr(pg, "_age_graph"):
                        self.graph_name = getattr(pg, "_age_graph") or self.graph_name
            except Exception:
                pass
            
            # Create indexes on first use
            if not self._indexes_created:
                await self._create_indexes()
                self._indexes_created = True
        
        return self._db

    async def _create_indexes(self):
        """
        Create AGE indexes for efficient queries.

        Note: AGE stores properties in a JSON-like 'properties' column, not as
        individual columns. Standard SQL indexes on property names don't apply.
        We use GIN indexes on the properties column instead.
        """
        db = await self._get_db()
        if not await db.graph_available():
            logger.warning("AGE not available, skipping index creation")
            return

        # AGE-compatible GIN indexes on properties column
        gin_indexes = [
            f'CREATE INDEX IF NOT EXISTS idx_discovery_props ON {self.graph_name}."Discovery" USING GIN (properties)',
            f'CREATE INDEX IF NOT EXISTS idx_agent_props ON {self.graph_name}."Agent" USING GIN (properties)',
            f'CREATE INDEX IF NOT EXISTS idx_tag_props ON {self.graph_name}."Tag" USING GIN (properties)',
        ]

        for sql in gin_indexes:
            try:
                await self._execute_age_sql(sql)
                logger.debug(f"Created GIN index: {sql[:60]}...")
            except Exception as e:
                # GIN index may already exist or properties column uses unsupported type
                logger.debug(f"GIN index creation skipped: {e}")

    async def _execute_age_sql(self, sql: str) -> None:
        """
        Execute a SQL statement against Postgres (used for AGE DDL like CREATE INDEX).

        Uses acquire() for proper pool orphan protection.
        """
        db = await self._get_db()
        async with db.acquire() as conn:
            await conn.execute("LOAD 'age'")
            await conn.execute("SET search_path = ag_catalog, core, audit, public")
            await conn.execute(sql)

    async def add_discovery(
        self,
        discovery: DiscoveryNode,
    ) -> None:
        """
        Add a discovery to the graph.

        Args:
            discovery: DiscoveryNode to add

        NOTE: Temporal/similarity linking is now query-time, not write-time.
        Use get_related_discoveries(id, temporal_window=300) or find_similar(id) at query time.
        """
        # Normalize tags before storage for consistent search
        from src.knowledge_graph import normalize_tags
        if discovery.tags:
            discovery.tags = normalize_tags(discovery.tags)

        db = await self._get_db()

        if not await db.graph_available():
            raise RuntimeError("AGE graph not available. Check PostgreSQL AGE extension.")

        # Extract EISV fields if this is a self_observation
        eisv_e = None
        eisv_i = None
        eisv_s = None
        eisv_v = None
        regime = None
        coherence = None

        if discovery.type == "self_observation" and discovery.provenance:
            prov = discovery.provenance
            eisv_e = prov.get("E") or prov.get("eisv_e")
            eisv_i = prov.get("I") or prov.get("eisv_i")
            eisv_s = prov.get("S") or prov.get("eisv_s")
            eisv_v = prov.get("V") or prov.get("eisv_v")
            regime = prov.get("regime")
            coherence = prov.get("coherence")

        # Parse timestamp
        timestamp = self._parse_optional_datetime(discovery.timestamp) or datetime.now()
        resolved_at = self._parse_optional_datetime(discovery.resolved_at)

        metadata = self._build_discovery_metadata(discovery)

        # Create discovery node
        cypher, params = create_discovery_node(
            discovery_id=discovery.id,
            agent_id=discovery.agent_id,
            discovery_type=discovery.type,
            summary=discovery.summary,
            details=discovery.details,
            severity=discovery.severity,
            status=discovery.status,
            timestamp=timestamp,
            resolved_at=resolved_at,
            eisv_e=eisv_e,
            eisv_i=eisv_i,
            eisv_s=eisv_s,
            eisv_v=eisv_v,
            regime=regime,
            coherence=coherence,
            tags=discovery.tags,
            metadata=metadata,
            closure_class=discovery.closure_class,
            closure_evidence=closure_evidence_to_json(discovery.closure_evidence),
        )

        # Execute rate limit + all graph operations in a single transaction
        async with db.transaction() as conn:
            # Rate limiting inside transaction — if limit exceeded, entire txn rolls back
            await self._check_rate_limit(discovery.agent_id, conn=conn)
            await self._persist_discovery_row(
                conn,
                discovery,
                created_at=timestamp,
                resolved_at=resolved_at,
            )
            await db.graph_query(cypher, params, conn=conn)

            # Create/update agent node
            agent_cypher, agent_params = create_agent_node(
                agent_id=discovery.agent_id,
                created_at=timestamp,
                updated_at=timestamp,
            )
            await db.graph_query(agent_cypher, agent_params, conn=conn)

            # Create AUTHORED edge
            authored_cypher, authored_params = create_authored_edge(
                agent_id=discovery.agent_id,
                discovery_id=discovery.id,
                at=timestamp,
            )
            await db.graph_query(authored_cypher, authored_params, conn=conn)

            # Create RESPONDS_TO edge if response_to exists
            if discovery.response_to:
                responds_cypher, responds_params = create_responds_to_edge(
                    from_discovery_id=discovery.id,
                    to_discovery_id=discovery.response_to.discovery_id,
                )
                await db.graph_query(responds_cypher, responds_params, conn=conn)

            # Create RELATED_TO edges
            for related_id in discovery.related_to:
                related_cypher, related_params = create_related_to_edge(
                    from_discovery_id=discovery.id,
                    to_discovery_id=related_id,
                )
                await db.graph_query(related_cypher, related_params, conn=conn)

            # Create TAGGED edges
            for tag in discovery.tags:
                tagged_cypher, tagged_params = create_tagged_edge(
                    discovery_id=discovery.id,
                    tag_name=tag,
                )
                await db.graph_query(tagged_cypher, tagged_params, conn=conn)

        # Store embedding for semantic search (async, best-effort)
        if await self._pgvector_available():
            try:
                from src.embeddings import get_embeddings_service, embeddings_available
                if embeddings_available():
                    embeddings = await get_embeddings_service()
                    text = f"{discovery.summary}\n{discovery.details[:EMBED_DETAILS_WINDOW] if discovery.details else ''}"
                    emb = await embeddings.embed(text)
                    if emb is not None:
                        task = asyncio.create_task(self._store_embedding(discovery.id, emb))
                        task.add_done_callback(
                            functools.partial(_log_store_task_failure, discovery.id)
                        )
                    else:
                        logger.warning(
                            f"Embedding returned None for {discovery.id}; stored "
                            f"without a vector, so {_BACKFILL_MISSING_HINT}"
                        )
                else:
                    _log_embedding_skipped(discovery.id, "store")
            except Exception as e:
                logger.warning(
                    f"Failed to create embedding for {discovery.id}: {e}; "
                    f"{_BACKFILL_MISSING_HINT}"
                )

        logger.debug(f"Added discovery {discovery.id} to AGE graph")

    async def _create_age_graph_for_discovery(self, db, discovery: DiscoveryNode) -> None:
        """Create the AGE Discovery vertex and its edges for one discovery.

        Mirrors the graph half of add_discovery() — node, agent, AUTHORED,
        RESPONDS_TO, RELATED_TO and TAGGED — but skips the rate-limit check,
        the SQL row persist, and embedding storage. Used by the backfill to
        rebuild graph nodes for rows that already exist in knowledge.discoveries
        (the source of truth). Every builder uses MERGE, so this is idempotent.
        """
        from src.knowledge_graph import normalize_tags

        tags = normalize_tags(discovery.tags) if discovery.tags else []

        eisv_e = eisv_i = eisv_s = eisv_v = regime = coherence = None
        if discovery.type == "self_observation" and discovery.provenance:
            prov = discovery.provenance
            eisv_e = prov.get("E") or prov.get("eisv_e")
            eisv_i = prov.get("I") or prov.get("eisv_i")
            eisv_s = prov.get("S") or prov.get("eisv_s")
            eisv_v = prov.get("V") or prov.get("eisv_v")
            regime = prov.get("regime")
            coherence = prov.get("coherence")

        timestamp = self._parse_optional_datetime(discovery.timestamp) or datetime.now()
        resolved_at = self._parse_optional_datetime(discovery.resolved_at)
        metadata = self._build_discovery_metadata(discovery)

        node_cypher, node_params = create_discovery_node(
            discovery_id=discovery.id,
            agent_id=discovery.agent_id,
            discovery_type=discovery.type,
            summary=discovery.summary,
            details=discovery.details,
            severity=discovery.severity,
            status=discovery.status,
            timestamp=timestamp,
            resolved_at=resolved_at,
            eisv_e=eisv_e,
            eisv_i=eisv_i,
            eisv_s=eisv_s,
            eisv_v=eisv_v,
            regime=regime,
            coherence=coherence,
            tags=tags,
            metadata=metadata,
            closure_class=discovery.closure_class,
            closure_evidence=closure_evidence_to_json(discovery.closure_evidence),
        )

        async with db.transaction() as conn:
            await db.graph_query(node_cypher, node_params, conn=conn)

            agent_cypher, agent_params = create_agent_node(
                agent_id=discovery.agent_id,
                created_at=timestamp,
                updated_at=timestamp,
            )
            await db.graph_query(agent_cypher, agent_params, conn=conn)

            authored_cypher, authored_params = create_authored_edge(
                agent_id=discovery.agent_id,
                discovery_id=discovery.id,
                at=timestamp,
            )
            await db.graph_query(authored_cypher, authored_params, conn=conn)

            if discovery.response_to:
                responds_cypher, responds_params = create_responds_to_edge(
                    from_discovery_id=discovery.id,
                    to_discovery_id=discovery.response_to.discovery_id,
                )
                await db.graph_query(responds_cypher, responds_params, conn=conn)

            for related_id in discovery.related_to or []:
                related_cypher, related_params = create_related_to_edge(
                    from_discovery_id=discovery.id,
                    to_discovery_id=related_id,
                )
                await db.graph_query(related_cypher, related_params, conn=conn)

            for tag in tags:
                tagged_cypher, tagged_params = create_tagged_edge(
                    discovery_id=discovery.id,
                    tag_name=tag,
                )
                await db.graph_query(tagged_cypher, tagged_params, conn=conn)










    async def _sql_update_discovery(
        self, discovery_id: str, updates: Dict[str, Any], conn: Any = None
    ) -> bool:
        """SQL UPDATE fallback for SQL-only discoveries that have no AGE node.

        ``conn`` is a connection already inside a transaction. update_discovery
        passes its own when the Cypher MATCH finds no node, so the fallback
        runs in that transaction instead of taking a second pool connection
        while the first is held (with a one-connection pool, or a pool full of
        such updates, that waits forever). Without one, the fallback opens its
        own transaction.
        """
        from src.knowledge_graph import normalize_tags
        db = await self._get_db()
        updates = apply_closure_reopen_rule(updates)

        set_parts: List[str] = []
        params: List[Any] = []

        for key in ("status", "severity", "type", "summary", "details", "closure_class"):
            if key in updates:
                params.append(updates[key])
                set_parts.append(f"{key} = ${len(params)}")

        if "closure_evidence" in updates:
            params.append(closure_evidence_to_json(updates["closure_evidence"]))
            set_parts.append(f"closure_evidence = ${len(params)}")

        for key in ("resolved_at", "updated_at"):
            if key in updates:
                params.append(self._parse_optional_datetime(updates[key]))
                set_parts.append(f"{key} = ${len(params)}")

        if "tags" in updates:
            tag_list = updates["tags"]
            params.append(normalize_tags(tag_list) if isinstance(tag_list, list) else tag_list)
            set_parts.append(f"tags = ${len(params)}")

        if not set_parts:
            return True

        params.append(discovery_id)
        # Through the backend, not db._pool: since the ExecutorPool (#218) the
        # pool has no fetchval, so every update on this path raised
        # AttributeError (reported as a failed update, or as "Discovery not
        # found" when reached from a missing AGE node) while tests that faked
        # the pool passed. In a transaction, not on a bare connection: the
        # UPDATE and the tag sync commit together, so a failed tag sync cannot
        # leave the row half-updated.
        sql = f"UPDATE knowledge.discoveries SET {', '.join(set_parts)} WHERE id = ${len(params)} RETURNING id"

        async def _apply(tx_conn) -> bool:
            result = await tx_conn.fetchval(sql, *params)
            if result is not None and "tags" in updates:
                await self._sync_discovery_tags(tx_conn, discovery_id, updates.get("tags") or [])
            return result is not None

        if conn is not None:
            return await _apply(conn)
        async with db.transaction() as own_conn:
            return await _apply(own_conn)

    async def update_discovery(self, discovery_id: str, updates: Dict[str, Any]) -> bool:
        """Update discovery fields in AGE graph.

        Supports updating: status, resolved_at, updated_at, tags, severity, type,
        summary, details, closure_class and closure_evidence. An update that
        reopens the row (status open or disputed) clears the closure pair.
        Falls back to direct SQL UPDATE when the discovery has no AGE node.
        Retries once on AGE concurrent-update conflicts ("Entity failed to be
        updated"), which AGE raises instead of re-evaluating the tuple the way
        plain PostgreSQL UPDATE does under READ COMMITTED.
        """
        db = await self._get_db()
        updates = apply_closure_reopen_rule(updates)

        if not await db.graph_available():
            logger.warning("AGE graph not available for update; falling back to SQL")
            return await self._sql_update_discovery(discovery_id, updates)

        # Build SET clauses for Cypher
        set_parts = []
        params = {"discovery_id": discovery_id}

        if "tags" in updates:
            from src.knowledge_graph import normalize_tags
            updates = dict(updates)
            updates["tags"] = normalize_tags(updates["tags"])

        for key, value in updates.items():
            if key in ("status", "resolved_at", "updated_at", "severity", "type", "summary", "details"):
                param_name = f"val_{key}"
                set_parts.append(f"d.{key} = ${{{param_name}}}")
                params[param_name] = value
            elif key == "tags":
                # Tags stored as JSON array in AGE
                param_name = "val_tags"
                set_parts.append(f"d.tags = ${{{param_name}}}")
                params[param_name] = json.dumps(value if isinstance(value, list) else [value])
            elif key == "closure_class":
                # NULL removes the property, which reads back as None.
                set_parts.append("d.closure_class = ${val_closure_class}")
                params["val_closure_class"] = value
            elif key == "closure_evidence":
                # Stored as JSON text on the node, as tags and metadata are.
                set_parts.append("d.closure_evidence = ${val_closure_evidence}")
                params["val_closure_evidence"] = closure_evidence_to_json(value)

        if not set_parts:
            return True  # Nothing to update

        cypher = f"""
            MATCH (d:Discovery {{id: ${{discovery_id}}}})
            SET {', '.join(set_parts)}
            RETURN d.id
        """

        max_attempts = 2
        for attempt in range(1, max_attempts + 1):
            try:
                async with db.transaction() as conn:
                    result = await db.graph_query(cypher, params, conn=conn)
                    if not result:
                        # A genuinely empty result set: the Cypher MATCH found
                        # no Discovery vertex with this id. That is the only
                        # condition that means "no AGE node" — fall back to
                        # SQL for SQL-only orphans, in this transaction.
                        return await self._sql_update_discovery(
                            discovery_id, updates, conn=conn
                        )
                    if isinstance(result[0], dict) and "error" in result[0]:
                        # A row that reports an error is a failed query, not a
                        # missing node, so it is a failure here rather than a
                        # redirect to the SQL fallback, which would write SQL
                        # and leave the AGE node behind. Defensive: no current
                        # graph_query path returns such a row. An oversized
                        # parameter (over _MAX_PARAM_LENGTH, 128 KiB) raises in
                        # _sanitize_cypher_param, the transaction rolls back,
                        # and the except below returns False. This keeps the
                        # orphan fallback keyed on an empty result alone.
                        logger.error(
                            f"AGE update query for discovery {discovery_id} "
                            f"returned an error result (not a missing node): "
                            f"{result[0].get('error')}"
                        )
                        return False
                    await self._sync_updated_discovery_row(conn, discovery_id, updates)
                    if "tags" in updates:
                        await self._sync_age_tag_edges(
                            db,
                            conn,
                            discovery_id,
                            updates["tags"],
                        )
                        await self._delete_orphan_age_tags(db, conn)
                if "summary" in updates or "details" in updates:
                    self._schedule_embedding_refresh(discovery_id)
                return True
            except Exception as e:
                # AGE raises TM_Updated ("Entity failed to be updated: 3") on a
                # write-write race; the conflict is transient once the other
                # transaction commits, so one retry resolves it.
                if "Entity failed to be updated" in str(e) and attempt < max_attempts:
                    logger.warning(
                        f"Concurrent update conflict on discovery {discovery_id}; "
                        f"retrying (attempt {attempt + 1} of {max_attempts})"
                    )
                    await asyncio.sleep(0.05 * attempt)
                    continue
                logger.error(f"Failed to update discovery {discovery_id}: {e}")
                return False
        return False



    async def health_check(self) -> Dict[str, Any]:
        """
        Lightweight health check — counts only, no breakdowns.
        Use get_stats() for full by_agent/by_tag/by_type/by_status breakdowns.
        """
        try:
            db = await self._get_db()

            cypher = "MATCH (d:Discovery) RETURN count(d)"
            total = await db.graph_query(cypher, {})
            total_count = int(total[0]) if total and isinstance(total[0], (int, float)) else 0

            cypher = (
                "MATCH (d:Discovery)-[r:TAGGED]->(t:Tag) RETURN count(r)"
            )
            tags_result = await db.graph_query(cypher, {})
            age_tag_assignments = (
                int(tags_result[0])
                if tags_result and isinstance(tags_result[0], (int, float))
                else 0
            )

            cypher = "MATCH ()-[r]->() RETURN count(r)"
            edges_result = await db.graph_query(cypher, {})
            total_edges = int(edges_result[0]) if edges_result and isinstance(edges_result[0], (int, float)) else 0

            tag_stats = await db.kg_tag_stats(
                including_cold=True,
                epoch=None,
            )
            canonical_assignments = tag_stats["total_tag_assignments"]

            return {
                "total_discoveries": total_count,
                "total_tags": tag_stats["total_tags"],
                "total_tag_assignments": canonical_assignments,
                "total_edges": total_edges,
                "tag_projection": {
                    "status": (
                        "count_aligned"
                        if age_tag_assignments == canonical_assignments
                        else "drifted"
                    ),
                    # Equal totals can still hide one missing + one stale pair.
                    # The operator backfill performs the exact set comparison.
                    "comparison": "assignment_counts_only",
                    "age_tag_assignments": age_tag_assignments,
                    "canonical_tag_assignments": canonical_assignments,
                    "assignment_delta": (
                        canonical_assignments - age_tag_assignments
                    ),
                },
            }
        except Exception as e:
            logger.warning(f"Health check failed, returning minimal info: {e}")
            return {
                "status": "degraded",
                "error": str(e),
                "backend": "age",
            }

    async def _check_rate_limit(self, agent_id: str, conn=None) -> None:
        """
        Check if agent has exceeded rate limit (20 stores/hour).
        Raises ValueError if limit exceeded.

        Args:
            agent_id: Agent to check.
            conn: Optional DB connection to reuse (e.g. from a transaction).

        Delegates to the backend-independent budget in
        ``src.storage.kg_write_budget`` so every backend enforces the same
        limit; see that module for why the check does not live here.
        """
        from src.storage.kg_write_budget import check_store_budget

        await check_store_budget(
            agent_id,
            db=await self._get_db(),
            limit=self.rate_limit_stores_per_hour,
            conn=conn,
        )

    async def load(self) -> None:
        """
        Initialize AGE backend and rehydrate the graph from PostgreSQL if needed.
        """
        db = await self._get_db()
        if not await db.graph_available():
            logger.warning("AGE graph not available during KnowledgeGraphAGE.load()")
            return

        try:
            pg_count = await self._count_postgres_discoveries()
            if pg_count == 0:
                return

            graph_count = await self._count_age_discoveries()
            if graph_count == pg_count:
                return

            if graph_count == 0:
                logger.warning(
                    f"AGE graph '{self.graph_name}' is empty while PostgreSQL has {pg_count} discoveries; rehydrating"
                )
                restored = await self._rehydrate_from_postgres()
                logger.warning(
                    f"Rehydrated AGE graph '{self.graph_name}' from PostgreSQL: "
                    f"{restored['discoveries']} discoveries, {restored['related_edges']} related edges"
                )
                return

            if graph_count < pg_count:
                logger.warning(
                    f"AGE graph '{self.graph_name}' has {graph_count}/{pg_count} "
                    "PostgreSQL discoveries; rehydrating missing rows"
                )
                restored = await self._rehydrate_missing_from_postgres()
                logger.warning(
                    f"Rehydrated missing AGE rows for graph '{self.graph_name}': "
                    f"{restored['discoveries']} discoveries, {restored['related_edges']} related edges"
                )
                return

            logger.warning(
                f"AGE graph '{self.graph_name}' has {graph_count} discoveries but "
                f"PostgreSQL has {pg_count}; AGE-only rows require operator review"
            )
        except Exception as e:
            logger.error(f"AGE graph rehydration check failed: {e}")

    async def _count_postgres_discoveries(self) -> int:
        """Count durable discovery rows in PostgreSQL."""
        db = await self._get_db()
        async with db.acquire() as conn:
            count = await conn.fetchval("SELECT COUNT(*) FROM knowledge.discoveries")
        return int(count or 0)

    async def _count_age_discoveries(self) -> int:
        """Count discovery vertices currently present in AGE."""
        db = await self._get_db()
        result = await db.graph_query("MATCH (d:Discovery) RETURN count(d)", {})
        if result and isinstance(result[0], (int, float)):
            return int(result[0])
        return 0

    async def _list_age_discovery_ids(self) -> Set[str]:
        """Return discovery IDs currently present in AGE."""
        db = await self._get_db()
        result = await db.graph_query("MATCH (d:Discovery) RETURN d.id", {})
        return {str(item) for item in result if item is not None}

    async def _fetch_missing_postgres_discovery_rows(self, limit: Optional[int] = None) -> List[Any]:
        """Fetch durable discovery rows that have no AGE Discovery vertex."""
        db = await self._get_db()
        age_ids = list(await self._list_age_discovery_ids())
        limit_clause = ""
        if limit is not None:
            limit_clause = f"LIMIT {max(0, int(limit))}"

        async with db.acquire() as conn:
            if age_ids:
                rows = await conn.fetch(
                    f"""
                    SELECT *
                    FROM knowledge.discoveries
                    WHERE NOT (id = ANY($1::text[]))
                    ORDER BY created_at ASC, id ASC
                    {limit_clause}
                    """,
                    age_ids,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT *
                    FROM knowledge.discoveries
                    ORDER BY created_at ASC, id ASC
                    {limit_clause}
                    """
                )
        return list(rows)

    async def _rehydrate_missing_from_postgres(self, limit: Optional[int] = None) -> Dict[str, int]:
        """Restore durable PostgreSQL discoveries that are missing from AGE."""
        db = await self._get_db()
        discovery_rows = await self._fetch_missing_postgres_discovery_rows(limit=limit)
        if not discovery_rows:
            return {"discoveries": 0, "related_edges": 0}

        discovery_ids = [row["id"] for row in discovery_rows]
        async with db.acquire() as conn:
            related_rows = await conn.fetch(
                """
                SELECT src_id, dst_id, weight, metadata
                FROM knowledge.discovery_edges
                WHERE edge_type = 'related'
                  AND (src_id = ANY($1::text[]) OR dst_id = ANY($1::text[]))
                ORDER BY created_at ASC, src_id ASC, dst_id ASC
                """,
                discovery_ids,
            )
            # Rows already in AGE whose related_to arrays point at a missing row.
            linking_rows = await conn.fetch(
                """
                SELECT id, related_to
                FROM knowledge.discoveries
                WHERE related_to && $1::text[]
                  AND NOT (id = ANY($1::text[]))
                """,
                discovery_ids,
            )

        edges = self._related_edges_to_rebuild(
            related_rows, [*discovery_rows, *linking_rows], only_touching=set(discovery_ids)
        )
        async with db.transaction() as conn:
            for row in discovery_rows:
                await self._import_discovery_row(conn, row)
            for edge in edges:
                cypher, params = create_related_to_edge(**edge)
                await db.graph_query(cypher, params, conn=conn)

        return {
            "discoveries": len(discovery_rows),
            "related_edges": len(edges),
        }

    async def _rehydrate_from_postgres(self) -> Dict[str, int]:
        """Restore AGE vertices and edges from durable PostgreSQL knowledge tables."""
        db = await self._get_db()

        async with db.acquire() as conn:
            discovery_rows = await conn.fetch(
                """
                SELECT *
                FROM knowledge.discoveries
                ORDER BY created_at ASC, id ASC
                """
            )
            related_rows = await conn.fetch(
                """
                SELECT src_id, dst_id, weight, metadata
                FROM knowledge.discovery_edges
                WHERE edge_type = 'related'
                ORDER BY created_at ASC, src_id ASC, dst_id ASC
                """
            )

        edges = self._related_edges_to_rebuild(related_rows, discovery_rows)
        async with db.transaction() as conn:
            for row in discovery_rows:
                await self._import_discovery_row(conn, row)
            for edge in edges:
                cypher, params = create_related_to_edge(**edge)
                await db.graph_query(cypher, params, conn=conn)

        return {
            "discoveries": len(discovery_rows),
            "related_edges": len(edges),
        }

    @staticmethod
    def _related_edges_to_rebuild(
        edge_rows, discovery_rows, only_touching: Optional[Set[str]] = None
    ) -> List[Dict[str, Any]]:
        """RELATED_TO edges for a rebuild, as ``create_related_to_edge`` kwargs.

        Only this backend writes ``knowledge.discovery_edges``. The postgres
        backend keeps a finding's links in its ``related_to`` array alone, so an
        install that ran ``postgres`` and then switches to ``age`` has no edge
        rows for them; the arrays are the record. Edge rows come first (they
        carry strength and reason), then each array entry not already covered.
        ``only_touching`` keeps edges with at least one endpoint in that set. An
        endpoint with no Discovery vertex makes the edge's MATCH a no-op.
        """
        edges: List[Dict[str, Any]] = []
        seen: Set[tuple] = set()

        def add(src, dst, strength=None, reason=None):
            if not src or not dst or src == dst or (src, dst) in seen:
                return
            if only_touching is not None and src not in only_touching and dst not in only_touching:
                return
            seen.add((src, dst))
            edges.append({
                "from_discovery_id": src,
                "to_discovery_id": dst,
                "strength": strength,
                "reason": reason,
            })

        for row in edge_rows:
            metadata = row["metadata"]
            add(
                row["src_id"],
                row["dst_id"],
                strength=row["weight"],
                reason=metadata.get("reason") if isinstance(metadata, dict) else None,
            )
        for row in discovery_rows:
            for dst in row.get("related_to") or []:
                add(row["id"], dst)
        return edges

    async def _import_discovery_row(self, conn, row) -> None:
        """Import one durable PostgreSQL discovery row into AGE without rate limiting."""
        db = await self._get_db()

        timestamp = row.get("created_at")
        resolved_at = row.get("resolved_at")
        metadata: Dict[str, Any] = {}
        if row.get("related_to"):
            metadata["related_to"] = row["related_to"]
        if row.get("references_files"):
            metadata["references_files"] = row["references_files"]
        if row.get("confidence") is not None:
            metadata["confidence"] = row["confidence"]
        if row.get("provenance"):
            metadata["provenance"] = row["provenance"]
        if row.get("provenance_chain"):
            metadata["provenance_chain"] = row["provenance_chain"]
        if row.get("response_to_id"):
            metadata["response_to"] = {
                "discovery_id": row["response_to_id"],
                "response_type": row.get("response_type") or "extend",
            }

        cypher, params = create_discovery_node(
            discovery_id=row["id"],
            agent_id=row["agent_id"],
            discovery_type=row["type"],
            summary=row["summary"],
            details=row.get("details"),
            severity=row.get("severity"),
            status=row.get("status") or "open",
            timestamp=timestamp,
            resolved_at=resolved_at,
            tags=row.get("tags") or [],
            metadata=metadata or None,
            closure_class=row.get("closure_class"),
            closure_evidence=closure_evidence_to_json(
                closure_evidence_from_stored(row.get("closure_evidence"))
            ),
        )
        await db.graph_query(cypher, params, conn=conn)

        agent_cypher, agent_params = create_agent_node(
            agent_id=row["agent_id"],
            created_at=timestamp,
            updated_at=row.get("updated_at") or timestamp,
        )
        await db.graph_query(agent_cypher, agent_params, conn=conn)

        authored_cypher, authored_params = create_authored_edge(
            agent_id=row["agent_id"],
            discovery_id=row["id"],
            at=timestamp,
        )
        await db.graph_query(authored_cypher, authored_params, conn=conn)

        if row.get("response_to_id"):
            responds_cypher, responds_params = create_responds_to_edge(
                from_discovery_id=row["id"],
                to_discovery_id=row["response_to_id"],
            )
            await db.graph_query(responds_cypher, responds_params, conn=conn)

        for tag in row.get("tags") or []:
            tagged_cypher, tagged_params = create_tagged_edge(
                discovery_id=row["id"],
                tag_name=tag,
            )
            await db.graph_query(tagged_cypher, tagged_params, conn=conn)











    # Status multipliers for search ranking - resolved/archived/cold entries rank lower.
    # cold is long-term storage (queryable only with include_cold) and ranks below
    # archived so that, when explicitly surfaced, it never outranks live tiers.
    STATUS_MULTIPLIERS = {
        "open": 1.0,
        "resolved": 0.6,
        "archived": 0.3,
        "cold": 0.2,
        "disputed": 0.5,
    }







    # =========================================================================
    # LIFECYCLE MANAGEMENT
    # =========================================================================





"""
AGE-backed Knowledge Graph Implementation

Apache AGE implementation of the knowledge graph interface.
Uses PostgreSQL + AGE for native graph queries.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from src.logging_utils import get_logger
from src.knowledge_graph import DiscoveryNode, ResponseTo
from src.mcp_handlers.knowledge.limits import EMBED_DETAILS_WINDOW
import src.db as db_module
from src.db.age_queries import (
    create_discovery_node,
    create_agent_node,
    create_authored_edge,
    create_responds_to_edge,
    create_related_to_edge,
    create_tagged_edge,
    create_supersedes_edge,
)

logger = get_logger(__name__)


class KnowledgeGraphAGE:
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
                response_to_id, response_type, provenance, epoch
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8,
                $9, $10, $11, $12, $13,
                $14, $15, $16, $17
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
                epoch = EXCLUDED.epoch
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

        field_map = {
            "status": "status",
            "severity": "severity",
            "type": "type",
            "summary": "summary",
            "details": "details",
        }

        for key, column in field_map.items():
            if key in updates:
                params.append(updates[key])
                set_parts.append(f"{column} = ${len(params)}")

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
                        task.add_done_callback(lambda t: logger.debug(f"_store_embedding failed: {t.exception()}") if t.exception() else None)
                    else:
                        logger.debug(f"Embedding returned None for {discovery.id}, skipping storage")
            except Exception as e:
                logger.debug(f"Failed to create embedding for {discovery.id}: {e}")

        logger.debug(f"Added discovery {discovery.id} to AGE graph")

    async def get_discovery(self, discovery_id: str) -> Optional[DiscoveryNode]:
        """Get a discovery by ID."""
        db = await self._get_db()
        
        cypher = """
            MATCH (d:Discovery {id: ${discovery_id}})
            RETURN d
        """
        
        results = await db.graph_query(cypher, {"discovery_id": discovery_id})

        if not results:
            # No AGE node — fall back to the SQL source-of-truth so SQL-only
            # orphans (written while backend was postgres) are still readable.
            row = await db.kg_get_discovery(discovery_id)
            return self._dict_to_discovery(row)

        # Parse result (AGE returns agtype, need to convert)
        # graph_query returns parsed agtype directly
        result = results[0]
        if isinstance(result, dict) and "d" in result:
            node_data = self._parse_agtype_node(result["d"])
        else:
            node_data = self._parse_agtype_node(result)
        return self._node_to_discovery(node_data)

    async def get_response_chain(self, discovery_id: str, max_depth: int = 10) -> List[DiscoveryNode]:
        """
        Get a response chain for a discovery using AGE graph traversal.

        Uses AGE graph traversal where `RESPONDS_TO` edges represent
        replies pointing to their parent.

        Returns:
            Discoveries ordered by depth (root first, then replies).
        """
        db = await self._get_db()
        if not await db.graph_available():
            # Fallback to single-node chain
            root = await self.get_discovery(discovery_id)
            return [root] if root else []

        # Traverse from any node d to the root (discovery_id) via RESPONDS_TO edges.
        # Include depth 0 so the root itself is present in the chain.
        # Project a single map literal (not a bare multi-column `RETURN d, depth`):
        # graph_query declares one `result agtype` output column, so a map keeps
        # node + depth in one column. Ordering is done in Python below.
        cypher = f"""
            MATCH (root:Discovery {{id: ${{discovery_id}}}})
            MATCH p = (d:Discovery)-[:RESPONDS_TO*0..{max_depth}]->(root:Discovery)
            RETURN {{node: d, depth: length(p)}}
        """
        rows = await db.graph_query(cypher, {"discovery_id": discovery_id})

        # Deduplicate by id using smallest depth
        best: Dict[str, tuple[int, DiscoveryNode]] = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            node_data = self._parse_agtype_node(row.get("node", row))
            depth = int(row.get("depth", 0) or 0)
            d = self._node_to_discovery(node_data)
            if not d or not d.id:
                continue
            prev = best.get(d.id)
            if prev is None or depth < prev[0]:
                best[d.id] = (depth, d)

        ordered = sorted(best.values(), key=lambda x: x[0])
        return [d for _depth, d in ordered]

    async def query(
        self,
        agent_id: Optional[str] = None,
        type: Optional[str] = None,
        status: Optional[str] = None,
        severity: Optional[str] = None,
        tags: Optional[List[str]] = None,
        limit: int = 100,
        exclude_archived: bool = False,
    ) -> List[DiscoveryNode]:
        """
        Query discoveries with filters.

        Args:
            agent_id: Filter by agent
            type: Filter by discovery type
            status: Filter by status
            severity: Filter by severity
            tags: Filter by tags (any match)
            limit: Maximum results
        """
        db = await self._get_db()

        # Check if graph is available
        graph_ok = await db.graph_available()
        if not graph_ok:
            logger.warning("AGE graph not available for query")
            return []

        # Build query
        conditions = []
        params = {}
        
        if agent_id:
            conditions.append("d.agent_id = ${agent_id}")
            params["agent_id"] = agent_id
        
        if type:
            conditions.append("d.type = ${type}")
            params["type"] = type
        
        if status:
            conditions.append("d.status = ${status}")
            params["status"] = status

        if severity:
            conditions.append("d.severity = ${severity}")
            params["severity"] = severity

        # Exclude archived at the Cypher level so LIMIT applies to non-archived rows.
        # Without this, LIMIT N grabs the N most recent rows (mostly archived noise),
        # then post-hoc filtering removes them, returning far fewer than N results.
        if exclude_archived and not status:
            # Use IS NULL OR to include entries with missing status (AGE NULL semantics:
            # NULL <> 'archived' evaluates to NULL, not TRUE, so those rows get filtered out)
            conditions.append("(d.status IS NULL OR d.status <> 'archived')")

        where_clause = " AND ".join(conditions) if conditions else ""
        
        # Handle tags - AGE doesn't support EXISTS subqueries or re-matching
        # a variable with different labels. We need a single MATCH pattern.
        if tags:
            # Normalize search tags to match stored normalized form
            from src.knowledge_graph import normalize_tags
            params["tags"] = normalize_tags(tags)
            # Combined MATCH: Discovery with tag relationship
            base_match = "MATCH (d:Discovery)-[:TAGGED]->(t:Tag) WHERE t.name IN ${tags}"
            if where_clause:
                cypher = f"""
                    {base_match} AND {where_clause}
                    RETURN DISTINCT d
                    ORDER BY d.timestamp DESC
                    LIMIT ${{limit}}
                """
            else:
                cypher = f"""
                    {base_match}
                    RETURN DISTINCT d
                    ORDER BY d.timestamp DESC
                    LIMIT ${{limit}}
                """
        else:
            # No tag filter
            cypher = f"""
                MATCH (d:Discovery)
                {"WHERE " + where_clause if where_clause else ""}
                RETURN d
                ORDER BY d.timestamp DESC
                LIMIT ${{limit}}
            """
        
        params["limit"] = limit
        
        logger.debug(f"AGE query: {cypher[:200]}... params: {list(params.keys())}")
        results = await db.graph_query(cypher, params)
        logger.debug(f"AGE query returned {len(results)} results")

        discoveries = []
        seen_ids = set()
        for result in results:
            # graph_query returns parsed agtype directly, not {"d": node}
            # Handle both dict with "d" key and direct node data
            if isinstance(result, dict) and "d" in result:
                node_data = self._parse_agtype_node(result["d"])
            elif isinstance(result, dict) and "error" in result:
                logger.warning(f"AGE query error: {result.get('error')}")
                continue
            else:
                node_data = self._parse_agtype_node(result)
            discovery = self._node_to_discovery(node_data)
            if discovery and discovery.id not in seen_ids:
                seen_ids.add(discovery.id)
                discoveries.append(discovery)

        return discoveries

    async def get_agent_discoveries(
        self,
        agent_id: str,
        limit: Optional[int] = None,
    ) -> List[DiscoveryNode]:
        """Get all discoveries for an agent."""
        return await self.query(
            agent_id=agent_id,
            limit=limit or 100,
        )

    def _parse_agtype_node(self, agtype_value: Any) -> Dict[str, Any]:
        """
        Parse AGE agtype node to dictionary.

        AGE returns vertices as {id: internal_id, label: "...", properties: {...}}
        We extract the properties dict which contains our actual data.
        """
        if agtype_value is None:
            return {}

        parsed = None

        # If it's already a dict, use it directly
        if isinstance(agtype_value, dict):
            parsed = agtype_value

        # If it's a string (JSON), parse it
        elif isinstance(agtype_value, str):
            try:
                parsed = json.loads(agtype_value)
            except Exception:
                return {}

        if parsed is None:
            return {}

        # AGE vertex structure: {id: ..., label: ..., properties: {...}}
        # Extract properties if this is a vertex
        if "properties" in parsed and isinstance(parsed["properties"], dict):
            return parsed["properties"]

        return parsed

    def _dict_to_discovery(self, d: Optional[Dict[str, Any]]) -> Optional[DiscoveryNode]:
        """Convert a knowledge.discoveries SQL row to DiscoveryNode."""
        if not d:
            return None
        response_to = None
        if d.get("response_to_id") and d.get("response_type"):
            response_to = ResponseTo(
                discovery_id=d["response_to_id"],
                response_type=d["response_type"],
            )
        return DiscoveryNode(
            id=d["id"],
            agent_id=d["agent_id"],
            type=d["type"],
            summary=d["summary"],
            details=d.get("details", ""),
            tags=d.get("tags", []),
            severity=d.get("severity"),
            timestamp=d.get("timestamp", d.get("created_at", "")),
            status=d.get("status", "open"),
            related_to=d.get("related_to", []),
            response_to=response_to,
            references_files=d.get("references_files", []),
            resolved_at=d.get("resolved_at"),
            updated_at=d.get("updated_at"),
            provenance=d.get("provenance"),
        )

    def _node_to_discovery(self, node_data: Dict[str, Any]) -> Optional[DiscoveryNode]:
        """Convert AGE node data to DiscoveryNode."""
        if not node_data or "id" not in node_data:
            return None
        
        # Extract metadata if present
        metadata = node_data.get("metadata", {})
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}
        
        # Parse response_to if present
        response_to = None
        if "response_to" in metadata:
            resp_data = metadata["response_to"]
            if isinstance(resp_data, dict):
                response_to = ResponseTo(
                    discovery_id=resp_data.get("discovery_id", ""),
                    response_type=resp_data.get("response_type", "extend"),
                )

        # Parse tags (may be stored as JSON string in AGE)
        tags = node_data.get("tags", [])
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except Exception:
                tags = []

        return DiscoveryNode(
            id=node_data.get("id", ""),
            agent_id=node_data.get("agent_id", ""),
            type=node_data.get("type", "insight"),
            summary=node_data.get("summary", ""),
            details=node_data.get("details", ""),
            tags=tags,
            severity=node_data.get("severity"),
            timestamp=node_data.get("timestamp", datetime.now().isoformat()),
            status=node_data.get("status", "open"),
            related_to=metadata.get("related_to", []),
            response_to=response_to,
            references_files=metadata.get("references_files", []),
            resolved_at=node_data.get("resolved_at"),
            updated_at=node_data.get("updated_at"),
            confidence=metadata.get("confidence"),
            provenance=metadata.get("provenance"),
            provenance_chain=metadata.get("provenance_chain"),
        )

    async def _sql_update_discovery(self, discovery_id: str, updates: Dict[str, Any]) -> bool:
        """SQL UPDATE fallback for SQL-only discoveries that have no AGE node."""
        from src.knowledge_graph import normalize_tags
        db = await self._get_db()

        set_parts: List[str] = []
        params: List[Any] = []

        for key in ("status", "severity", "type", "summary", "details"):
            if key in updates:
                params.append(updates[key])
                set_parts.append(f"{key} = ${len(params)}")

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
        result = await db._pool.fetchval(
            f"UPDATE knowledge.discoveries SET {', '.join(set_parts)} WHERE id = ${len(params)} RETURNING id",
            *params,
        )
        if result is not None and "tags" in updates:
            async with db._pool.acquire() as conn:
                await self._sync_discovery_tags(conn, discovery_id, updates.get("tags") or [])
        return result is not None

    async def update_discovery(self, discovery_id: str, updates: Dict[str, Any]) -> bool:
        """Update discovery fields in AGE graph.

        Supports updating: status, resolved_at, updated_at, tags, severity, type,
        summary, and details.
        Falls back to direct SQL UPDATE when the discovery has no AGE node.
        Retries once on AGE concurrent-update conflicts ("Entity failed to be
        updated"), which AGE raises instead of re-evaluating the tuple the way
        plain PostgreSQL UPDATE does under READ COMMITTED.
        """
        db = await self._get_db()

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
                    if not result or (isinstance(result[0], dict) and "error" in result[0]):
                        # No AGE node — fall back to SQL for SQL-only orphans.
                        return await self._sql_update_discovery(discovery_id, updates)
                    await self._sync_updated_discovery_row(conn, discovery_id, updates)
                if "summary" in updates or "details" in updates:
                    await self._refresh_embedding(discovery_id)
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

    async def get_stats(
        self,
        epoch_scope: str = "current",
        including_cold: bool = False,
    ) -> Dict[str, Any]:
        """Get graph statistics.

        Note: AGE doesn't support GROUP BY or multi-column returns well,
        so we use single-column collect() queries and aggregate in Python.

        AGE vertices have no epoch property — epoch_scope is accepted for
        signature parity with the postgres backend but always behaves as
        'all'. including_cold=False filters cold rows from the by_status
        aggregation in Python after the collect() returns.
        """
        from collections import Counter

        db = await self._get_db()
        # AGE's epoch is intentionally unscoped — declare that in the response
        # so callers comparing across backends know which is which.
        effective_epoch_scope = "all"
        
        # Total discoveries
        cypher = "MATCH (d:Discovery) RETURN count(d)"
        total = await db.graph_query(cypher, {})
        total_count = int(total[0]) if total and isinstance(total[0], (int, float)) else 0
        
        # Collect agent_ids (single column - AGE handles this fine)
        cypher = "MATCH (d:Discovery) RETURN collect(d.agent_id)"
        result = await db.graph_query(cypher, {})
        agents = result[0] if result and isinstance(result[0], list) else []
        by_agent = dict(Counter(a for a in agents if a))
        
        # Collect types
        cypher = "MATCH (d:Discovery) RETURN collect(d.type)"
        result = await db.graph_query(cypher, {})
        types = result[0] if result and isinstance(result[0], list) else []
        by_type = dict(Counter(t for t in types if t))
        
        # Collect statuses
        cypher = "MATCH (d:Discovery) RETURN collect(d.status)"
        result = await db.graph_query(cypher, {})
        statuses = result[0] if result and isinstance(result[0], list) else []
        cold_count = sum(1 for s in statuses if s == "cold")
        if not including_cold:
            statuses = [s for s in statuses if s != "cold"]
            # Keep total_discoveries consistent with by_status — subtracting
            # cold here means a list response with including_cold=False reports
            # exactly the rows it bucketed.
            total_count = max(0, total_count - cold_count)
        by_status = dict(Counter(s for s in statuses if s))
        
        # Count edges
        cypher = "MATCH ()-[r]->() RETURN count(r)"
        edges_result = await db.graph_query(cypher, {})
        total_edges = int(edges_result[0]) if edges_result and isinstance(edges_result[0], (int, float)) else 0

        # Count tags (from Tag vertices)
        cypher = "MATCH (t:Tag) RETURN count(t) as tag_count"
        tags_result = await db.graph_query(cypher, {})
        # Handle different result formats: direct int, dict with count, or list
        total_tags = 0
        if tags_result:
            first_result = tags_result[0]
            # Check for error dict first
            if isinstance(first_result, dict) and "error" in first_result:
                logger.warning(f"Tag count query failed: {first_result.get('error')}")
                total_tags = 0
            elif isinstance(first_result, (int, float)):
                total_tags = int(first_result)
            elif isinstance(first_result, dict):
                # AGE might return {"tag_count": 1130} or {"count": 1130}
                total_tags = int(first_result.get("tag_count") or first_result.get("count") or 0)
            elif isinstance(first_result, list) and len(first_result) > 0:
                # Nested list case
                total_tags = int(first_result[0]) if isinstance(first_result[0], (int, float)) else 0
            else:
                logger.debug(f"Unexpected tag count result format: {type(first_result)}, value: {first_result}")

        # Collect tag names for by_tag breakdown
        cypher = "MATCH (t:Tag) RETURN collect(t.name)"
        result = await db.graph_query(cypher, {})
        tag_names = result[0] if result and isinstance(result[0], list) else []
        by_tag = dict(Counter(t for t in tag_names if t))

        return {
            "total_discoveries": total_count,
            "by_agent": by_agent,
            "by_type": by_type,
            "by_status": by_status,
            "by_tag": by_tag,
            "total_edges": total_edges,
            "total_agents": len(by_agent),
            "total_tags": total_tags,
            "scope": {
                "kind": "raw_status_aggregate",
                "epoch_scope": effective_epoch_scope,  # AGE: always "all"
                "including_cold": including_cold,
                "note": (
                    "AGE backend has no epoch property — counts span all "
                    "epochs. Compare with knowledge action=stats which uses "
                    "lifecycle buckets."
                ),
            },
        }

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

            cypher = "MATCH (t:Tag) RETURN count(t)"
            tags_result = await db.graph_query(cypher, {})
            total_tags = 0
            if tags_result:
                first_result = tags_result[0]
                if isinstance(first_result, (int, float)):
                    total_tags = int(first_result)
                elif isinstance(first_result, dict) and "error" not in first_result:
                    total_tags = int(first_result.get("tag_count") or first_result.get("count") or 0)

            cypher = "MATCH ()-[r]->() RETURN count(r)"
            edges_result = await db.graph_query(cypher, {})
            total_edges = int(edges_result[0]) if edges_result and isinstance(edges_result[0], (int, float)) else 0

            return {
                "total_discoveries": total_count,
                "total_tags": total_tags,
                "total_edges": total_edges,
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

        Uses Redis for fast rate limiting, falls back to PostgreSQL.
        """
        # Try Redis first (fast path)
        try:
            from src.cache import get_rate_limiter
            limiter = get_rate_limiter()
            window_seconds = 3600  # 1 hour
            
            # Check rate limit
            if not await limiter.check(
                agent_id,
                limit=self.rate_limit_stores_per_hour,
                window=window_seconds,
                operation="kg_store",
            ):
                # Get current count for error message
                count = await limiter.get_count(agent_id, window_seconds, operation="kg_store")
                raise ValueError(
                    f"Rate limit exceeded: Agent '{agent_id}' has stored {count} "
                    f"discoveries in the last hour (limit: {self.rate_limit_stores_per_hour}/hour). "
                    f"This prevents knowledge graph poisoning flood attacks. "
                    f"Please wait before storing more discoveries."
                )
            
            # Record this operation
            await limiter.record(agent_id, window_seconds, operation="kg_store")
            return  # Success - Redis handled it
        except ValueError:
            # Rate limit exceeded - re-raise
            raise
        except Exception as e:
            # Redis failed - fall back to PostgreSQL
            logger.debug(f"Redis rate limiting failed, falling back to PostgreSQL: {e}")
        
        # Fallback: Use PostgreSQL for persistent rate limit tracking
        # Uses atomic check-and-insert to prevent race conditions
        db = await self._get_db()

        async def _do_rate_limit_check(c):
            from datetime import datetime, timedelta
            one_hour_ago = datetime.now() - timedelta(hours=1)

            inserted = await c.fetchval(
                """
                INSERT INTO audit.rate_limits (agent_id, timestamp)
                SELECT $1, $2
                WHERE (
                    SELECT COUNT(*) FROM audit.rate_limits
                    WHERE agent_id = $1 AND timestamp > $3
                ) < $4
                RETURNING agent_id
                """,
                agent_id,
                datetime.now(),
                one_hour_ago,
                self.rate_limit_stores_per_hour,
            )

            if inserted is None:
                count = await c.fetchval(
                    "SELECT COUNT(*) FROM audit.rate_limits WHERE agent_id = $1 AND timestamp > $2",
                    agent_id, one_hour_ago,
                )
                raise ValueError(
                    f"Rate limit exceeded: Agent '{agent_id}' has stored {count or 0} "
                    f"discoveries in the last hour (limit: {self.rate_limit_stores_per_hour}/hour). "
                    f"This prevents knowledge graph poisoning flood attacks. "
                    f"Please wait before storing more discoveries."
                )

            await c.execute(
                "DELETE FROM audit.rate_limits WHERE timestamp < $1",
                one_hour_ago,
            )

        if conn is not None:
            await _do_rate_limit_check(conn)
        else:
            async with db.acquire() as pooled_conn:
                await _do_rate_limit_check(pooled_conn)

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

        async with db.transaction() as conn:
            for row in discovery_rows:
                await self._import_discovery_row(conn, row)
            for row in related_rows:
                cypher, params = create_related_to_edge(
                    from_discovery_id=row["src_id"],
                    to_discovery_id=row["dst_id"],
                    strength=row["weight"],
                    reason=(row["metadata"] or {}).get("reason") if isinstance(row["metadata"], dict) else None,
                )
                await db.graph_query(cypher, params, conn=conn)

        return {
            "discoveries": len(discovery_rows),
            "related_edges": len(related_rows),
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

        async with db.transaction() as conn:
            for row in discovery_rows:
                await self._import_discovery_row(conn, row)
            for row in related_rows:
                cypher, params = create_related_to_edge(
                    from_discovery_id=row["src_id"],
                    to_discovery_id=row["dst_id"],
                    strength=row["weight"],
                    reason=(row["metadata"] or {}).get("reason") if isinstance(row["metadata"], dict) else None,
                )
                await db.graph_query(cypher, params, conn=conn)

        return {
            "discoveries": len(discovery_rows),
            "related_edges": len(related_rows),
        }

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

    async def find_similar(
        self,
        discovery: DiscoveryNode,
        limit: int = 5,
    ) -> List[DiscoveryNode]:
        """
        Find similar discoveries by tag overlap.

        Args:
            discovery: Discovery to find similar ones for
            limit: Maximum number of results

        Returns:
            List of similar DiscoveryNodes
        """
        if not discovery.tags:
            return []

        # AGE doesn't support ORDER BY on WITH-clause aliases,
        # so we fetch all matches and rank by tag overlap in Python.
        db = await self._get_db()

        cypher = """
            MATCH (d:Discovery)-[:TAGGED]->(t:Tag)
            WHERE t.name IN ${tags}
              AND d.id <> ${exclude_id}
            WITH DISTINCT d
            RETURN d
        """

        params = {
            "tags": discovery.tags,
            "exclude_id": discovery.id,
        }

        results = await db.graph_query(cypher, params)

        similar = []
        for result in results:
            # Handle both dict with "d" key and direct node data
            if isinstance(result, dict) and "d" in result:
                node_data = self._parse_agtype_node(result["d"])
            else:
                node_data = self._parse_agtype_node(result)
            disc = self._node_to_discovery(node_data)
            if disc:
                similar.append(disc)

        # Rank by tag overlap count (descending)
        input_tags = set(discovery.tags)
        similar.sort(key=lambda d: len(set(d.tags or []) & input_tags), reverse=True)
        return similar[:limit]

    async def find_similar_by_tags(
        self,
        tags: List[str],
        exclude_id: Optional[str] = None,
        limit: int = 5,
    ) -> List[DiscoveryNode]:
        """
        Find discoveries with overlapping tags.

        Args:
            tags: List of tags to match
            exclude_id: Discovery ID to exclude from results
            limit: Maximum number of results

        Returns:
            List of similar DiscoveryNodes
        """
        if not tags:
            return []

        db = await self._get_db()

        exclude_clause = " AND d.id <> ${exclude_id}" if exclude_id else ""

        # AGE doesn't support ORDER BY on WITH-clause aliases,
        # so we fetch all matches and rank by tag overlap in Python.
        cypher = f"""
            MATCH (d:Discovery)-[:TAGGED]->(t:Tag)
            WHERE t.name IN ${{tags}}{exclude_clause}
            WITH DISTINCT d
            RETURN d
        """

        params = {
            "tags": tags,
        }
        if exclude_id:
            params["exclude_id"] = exclude_id

        results = await db.graph_query(cypher, params)

        similar = []
        for result in results:
            # Handle both dict with "d" key and direct node data
            if isinstance(result, dict) and "d" in result:
                node_data = self._parse_agtype_node(result["d"])
            else:
                node_data = self._parse_agtype_node(result)
            disc = self._node_to_discovery(node_data)
            if disc:
                similar.append(disc)

        # Rank by tag overlap count (descending)
        input_tags = set(tags)
        similar.sort(key=lambda d: len(set(d.tags or []) & input_tags), reverse=True)
        return similar[:limit]

    async def _pgvector_available(self) -> bool:
        """Check if pgvector extension and the active embeddings table exist."""
        db = await self._get_db()
        if not hasattr(db, '_pool') or db._pool is None:
            return False

        try:
            from src.embeddings import get_active_table_name
            # Table name is schema-qualified (e.g. "core.discovery_embeddings_bge_m3");
            # split for information_schema lookup.
            qualified = get_active_table_name()
            schema, _, table = qualified.partition(".")
            async with db.acquire() as conn:
                ext_exists = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
                )
                if not ext_exists:
                    return False

                table_exists = await conn.fetchval(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = $1 AND table_name = $2
                    )
                    """,
                    schema, table,
                )
                return table_exists
        except Exception as e:
            logger.debug(f"pgvector check failed: {e}")
            return False

    async def _pgvector_search(
        self,
        query_embedding: List[float],
        limit: int,
        min_similarity: float,
        agent_id: Optional[str] = None,
    ) -> List[tuple[str, float]]:
        """
        Search using pgvector's HNSW index.

        Returns list of (discovery_id, similarity_score) tuples.
        """
        from src.embeddings import get_active_table_name
        db = await self._get_db()
        table = get_active_table_name()

        # Convert list to pgvector string format: '[0.1, 0.2, ...]'
        embedding_str = '[' + ','.join(str(x) for x in query_embedding) + ']'

        async with db.acquire() as conn:
            if agent_id:
                # Hybrid query: pgvector for similarity, later filter by agent via AGE
                rows = await conn.fetch(
                    f"""
                    SELECT de.discovery_id, (1 - (de.embedding <=> $1::vector)) AS similarity
                    FROM {table} de
                    WHERE de.embedding IS NOT NULL
                      AND (1 - (de.embedding <=> $1::vector)) >= $2
                    ORDER BY de.embedding <=> $1::vector
                    LIMIT $3
                    """,
                    embedding_str, min_similarity, limit * 3,
                )
            else:
                rows = await conn.fetch(
                    f"""
                    SELECT discovery_id, (1 - (embedding <=> $1::vector)) AS similarity
                    FROM {table}
                    WHERE embedding IS NOT NULL
                      AND (1 - (embedding <=> $1::vector)) >= $2
                    ORDER BY embedding <=> $1::vector
                    LIMIT $3
                    """,
                    embedding_str, min_similarity, limit,
                )

            return [(row['discovery_id'], float(row['similarity'])) for row in rows]

    async def _store_embedding(self, discovery_id: str, embedding: List[float]) -> None:
        """Store embedding in the pgvector table for the active model."""
        from src.embeddings import get_active_table_name, get_embeddings_service
        db = await self._get_db()
        table = get_active_table_name()
        svc = await get_embeddings_service()
        model_name = svc.model_name

        # Convert list to pgvector string format: '[0.1, 0.2, ...]'
        embedding_str = '[' + ','.join(str(x) for x in embedding) + ']'

        try:
            async with db.acquire() as conn:
                await conn.execute(
                    f"""
                    INSERT INTO {table} (discovery_id, embedding, model_name)
                    VALUES ($1, $2::vector, $3)
                    ON CONFLICT (discovery_id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        model_name = EXCLUDED.model_name,
                        updated_at = now()
                    """,
                    discovery_id, embedding_str, model_name,
                )
        except Exception as e:
            logger.debug(f"Failed to store embedding for {discovery_id}: {e}")

    async def _refresh_embedding(self, discovery_id: str) -> None:
        """Regenerate the stored embedding after summary/details edits."""
        if not await self._pgvector_available():
            return

        try:
            from src.embeddings import get_embeddings_service, embeddings_available
            if not embeddings_available():
                return
            discovery = await self.get_discovery(discovery_id)
            if not discovery:
                return
            embeddings = await get_embeddings_service()
            text = f"{discovery.summary}\n{discovery.details[:EMBED_DETAILS_WINDOW] if discovery.details else ''}"
            emb = await embeddings.embed(text)
            if emb is None:
                return
            await self._store_embedding(discovery_id, emb)
        except Exception as e:
            logger.debug(f"Failed to refresh embedding for {discovery_id}: {e}")

    async def get_connectivity_score(self, discovery_id: str) -> float:
        """
        Get connectivity score for a discovery based on inbound edges.

        Higher score = more other discoveries reference this one.
        Used to rank well-connected knowledge above orphaned entries.

        Returns:
            Normalized score in [0, 1] range
        """
        db = await self._get_db()

        if not await db.graph_available():
            return 0.0

        # Count inbound edges (other discoveries pointing to this one)
        # Weight: RESPONDS_TO edges count more than RELATED_TO
        # Return single column as {related: N, responds: M} to work with graph_query
        cypher = """
            MATCH (d:Discovery {id: ${discovery_id}})
            OPTIONAL MATCH (other:Discovery)-[r:RELATED_TO]->(d)
            OPTIONAL MATCH (resp:Discovery)-[rt:RESPONDS_TO]->(d)
            RETURN {related: count(DISTINCT other), responds: count(DISTINCT resp)}
        """

        try:
            results = await db.graph_query(cypher, {"discovery_id": discovery_id})
            if not results:
                return 0.0

            result = results[0]
            # Result is either a dict or a nested structure
            if isinstance(result, dict) and "error" not in result:
                related_count = int(result.get("related", 0) or 0)
                responds_count = int(result.get("responds", 0) or 0)
            else:
                return 0.0

            # Weight responds_to higher (it's a stronger signal)
            raw_score = related_count + (responds_count * 2)

            # Normalize: log scale to prevent a few highly-linked nodes from dominating
            # score = log(1 + raw) / log(1 + max_expected)
            # Assume max ~100 inbound links as ceiling
            import math
            normalized = math.log1p(raw_score) / math.log1p(100)
            return min(1.0, normalized)
        except Exception as e:
            logger.debug(f"Failed to get connectivity score for {discovery_id}: {e}")
            return 0.0

    async def get_connectivity_scores_batch(self, discovery_ids: List[str]) -> Dict[str, float]:
        """
        Get connectivity scores for multiple discoveries in one query.

        More efficient than calling get_connectivity_score() repeatedly.
        """
        if not discovery_ids:
            return {}

        db = await self._get_db()

        if not await db.graph_available():
            return {d: 0.0 for d in discovery_ids}

        # Batch query for all discovery IDs - return single column per row
        # Need WITH clause for proper grouping before RETURN
        # Also count inbound SUPERSEDES edges to penalize superseded entries
        cypher = """
            UNWIND ${ids} as disc_id
            MATCH (d:Discovery {id: disc_id})
            OPTIONAL MATCH (other:Discovery)-[r:RELATED_TO]->(d)
            OPTIONAL MATCH (resp:Discovery)-[rt:RESPONDS_TO]->(d)
            OPTIONAL MATCH (newer:Discovery)-[s:SUPERSEDES]->(d)
            WITH d.id as id, count(DISTINCT other) as related, count(DISTINCT resp) as responds, count(DISTINCT newer) as superseded_by
            RETURN {id: id, related: related, responds: responds, superseded_by: superseded_by}
        """

        try:
            results = await db.graph_query(cypher, {"ids": discovery_ids})
            scores = {}

            import math
            for result in results:
                if not isinstance(result, dict) or "error" in result:
                    continue

                disc_id = result.get("id", "")
                if isinstance(disc_id, str):
                    disc_id = disc_id.strip('"')

                related_count = int(result.get("related", 0) or 0)
                responds_count = int(result.get("responds", 0) or 0)
                superseded_count = int(result.get("superseded_by", 0) or 0)

                raw_score = min(related_count + (responds_count * 2), 50)
                normalized = math.log1p(raw_score) / math.log1p(100)
                # Penalize superseded entries: halve score for each supersession
                if superseded_count > 0:
                    normalized *= 0.5 ** superseded_count
                scores[disc_id] = min(1.0, normalized)

            # Fill in zeros for any missing IDs
            for d in discovery_ids:
                if d not in scores:
                    scores[d] = 0.0

            return scores
        except Exception as e:
            logger.debug(f"Failed to get batch connectivity scores: {e}")
            return {d: 0.0 for d in discovery_ids}

    # Status multipliers for search ranking - resolved/archived entries rank lower
    STATUS_MULTIPLIERS = {
        "open": 1.0,
        "resolved": 0.6,
        "archived": 0.3,
        "disputed": 0.5,
    }

    async def _blend_with_connectivity(
        self,
        raw_results: List[tuple[DiscoveryNode, float]],
        connectivity_weight: float,
        exclude_orphans: bool,
        limit: int,
        temporal_decay: bool = True,
        half_life_days: float = 90.0,
        status_weight: bool = True,
    ) -> List[tuple[DiscoveryNode, float]]:
        """
        Blend similarity scores with connectivity scores, temporal decay, and status weighting.

        Args:
            raw_results: List of (discovery, similarity_score) tuples
            connectivity_weight: Weight for connectivity (0-1)
            exclude_orphans: If True, filter discoveries with 0 inbound links
            limit: Maximum results to return
            temporal_decay: If True, apply age-based decay (newer entries rank higher)
            half_life_days: Half-life for temporal decay in days (default 90)
            status_weight: If True, apply status-based multipliers (archived ranks lower)

        Returns:
            List of (discovery, blended_score) tuples, sorted by score descending
        """
        if not raw_results:
            return []

        # Fetch connectivity scores in batch
        discovery_ids = [d.id for d, _ in raw_results]
        connectivity_scores = await self.get_connectivity_scores_batch(discovery_ids)

        now = datetime.now()

        # Blend scores
        blended_results = []
        for discovery, similarity in raw_results:
            connectivity = connectivity_scores.get(discovery.id, 0.0)

            # Exclude orphans if requested
            if exclude_orphans and connectivity == 0.0:
                continue

            # Base blend: similarity * (1 - weight) + connectivity * weight
            score = (similarity * (1 - connectivity_weight)) + (connectivity * connectivity_weight)

            # Status multiplier: archived/resolved entries rank lower
            if status_weight:
                status_mult = self.STATUS_MULTIPLIERS.get(discovery.status, 1.0)
                score *= status_mult

            # Temporal decay: older entries rank lower
            if temporal_decay and half_life_days > 0:
                try:
                    ts = discovery.timestamp
                    if ts:
                        created = datetime.fromisoformat(ts.replace("Z", "+00:00").replace("+00:00", ""))
                        age_days = max(0, (now - created).total_seconds() / 86400)
                        decay = 1.0 / (1.0 + age_days / half_life_days)
                        score *= decay
                except (ValueError, TypeError):
                    pass  # Can't parse timestamp, skip decay

            blended_results.append((discovery, score))

        # Sort by blended score descending
        blended_results.sort(key=lambda x: x[1], reverse=True)

        # Apply limit
        return blended_results[:limit]

    async def full_text_search(
        self,
        query: str,
        limit: int = 20,
        operator: str = "AND",
    ) -> List[DiscoveryNode]:
        """Full-text search using PostgreSQL tsvector (ts_rank_cd ranking).

        AGE and PG backends share the same underlying `knowledge.discoveries`
        table, so we can reuse the DB mixin's kg_full_text_search. This keeps
        hybrid retrieval (RRF over semantic + FTS) identical across backends.

        Defaults to AND for multi-term queries (#165). Callers wanting recall
        pass operator="OR".
        """
        db = await self._get_db()
        rows = await db.kg_full_text_search(query, limit, operator=operator)
        # Hydrate via get_discovery so edge/response metadata is consistent
        # with what the rest of AGE returns. Row count is small (<= limit).
        results: List[DiscoveryNode] = []
        for row in rows:
            doc = await self.get_discovery(row["id"])
            if doc is not None:
                results.append(doc)
        return results

    async def semantic_search(
        self,
        query: str,
        limit: int = 10,
        min_similarity: float = 0.3,
        agent_id: Optional[str] = None,
        connectivity_weight: float = 0.3,
        exclude_orphans: bool = False,
        temporal_decay: bool = True,
        half_life_days: float = 90.0,
        status_weight: bool = True,
    ) -> List[tuple[DiscoveryNode, float]]:
        """
        Semantic search using sentence-transformer embeddings.

        Uses pgvector for fast similarity search when available,
        falls back to in-memory comparison otherwise.

        Blends semantic similarity with connectivity score to rank
        well-connected knowledge above orphaned entries. Applies
        temporal decay and status-based weighting to prevent old
        or archived entries from dominating results.

        Args:
            query: Search query text
            limit: Maximum number of results
            min_similarity: Minimum cosine similarity threshold (0-1)
            agent_id: Optional agent filter
            connectivity_weight: Weight for connectivity in final score (0-1)
                                 Final = similarity*(1-weight) + connectivity*weight
                                 Default 0.3 = 70% similarity, 30% connectivity
            exclude_orphans: If True, filter out discoveries with zero inbound links
            temporal_decay: If True, apply age-based decay (newer entries rank higher)
            half_life_days: Half-life for temporal decay in days (default 90)
            status_weight: If True, apply status multipliers (archived/resolved rank lower)

        Returns:
            List of (DiscoveryNode, final_score) tuples, sorted by score descending.
            If embeddings are unavailable, returns ([], error_info_dict) instead.
            Callers should check: if isinstance(result, tuple) and len(result) == 2
            and isinstance(result[1], dict), it's a degraded response.
        """
        try:
            from src.embeddings import get_embeddings_service, embeddings_available
        except ImportError:
            logger.warning("Embeddings module not available — semantic search degraded")
            return ([], {"error": "embeddings_import_failed", "message": "Embeddings module not available"})

        if not embeddings_available():
            logger.warning("sentence-transformers not installed — semantic search degraded")
            return ([], {"error": "embeddings_unavailable", "message": "sentence-transformers not installed, semantic search unavailable"})

        # Get embedding service and embed query
        try:
            embeddings = await get_embeddings_service()
            query_embedding = await embeddings.embed(query)
        except Exception as e:
            logger.warning(f"Embedding service failed — semantic search degraded: {e}")
            return ([], {"error": "embeddings_failed", "message": f"Embedding service error: {e}"})

        if query_embedding is None:
            logger.warning("Embedding service returned None — semantic search unavailable")
            return ([], {"error": "embeddings_failed", "message": "Embedding returned None"})

        # Try pgvector first (fast, indexed)
        use_pgvector = await self._pgvector_available()
        
        if use_pgvector:
            logger.debug("Using pgvector for semantic search")
            scored_ids = await self._pgvector_search(
                query_embedding=query_embedding,
                limit=limit,
                min_similarity=min_similarity,
                agent_id=agent_id,
            )
            
            if scored_ids:
                # Fetch full discovery nodes
                raw_results = []
                for discovery_id, similarity in scored_ids:
                    discovery = await self.get_discovery(discovery_id)
                    if discovery:
                        # Apply agent filter if needed (pgvector doesn't filter by agent)
                        if agent_id and discovery.agent_id != agent_id:
                            continue
                        raw_results.append((discovery, similarity))

                if raw_results:
                    # Blend with connectivity scores
                    return await self._blend_with_connectivity(
                        raw_results,
                        connectivity_weight=connectivity_weight,
                        exclude_orphans=exclude_orphans,
                        limit=limit,
                        temporal_decay=temporal_decay,
                        half_life_days=half_life_days,
                        status_weight=status_weight,
                    )
            
            # Fall through to in-memory if pgvector returned nothing
            logger.debug("pgvector returned no results, falling back to in-memory")
        
        # Fallback: In-memory semantic search
        logger.debug("Using in-memory semantic search")
        
        # Get candidate discoveries
        candidates = await self.query(
            agent_id=agent_id,
            limit=limit * 5,
        )
        
        if not candidates:
            return []
        
        # Embed candidates
        candidate_texts = [
            f"{d.summary}\n{d.details[:EMBED_DETAILS_WINDOW] if d.details else ''}"
            for d in candidates
        ]
        
        candidate_embeddings = await embeddings.embed_batch(candidate_texts)

        valid_candidates = [
            (discovery, emb)
            for discovery, emb in zip(candidates, candidate_embeddings)
            if emb is not None
        ]
        if not valid_candidates:
            return []

        # Store embeddings for future pgvector use (async, best-effort)
        if use_pgvector:
            for discovery, emb in valid_candidates:
                task = asyncio.create_task(self._store_embedding(discovery.id, emb))
                task.add_done_callback(lambda t: logger.debug(f"_store_embedding failed: {t.exception()}") if t.exception() else None)

        # Rank by similarity
        scored = await embeddings.rank_by_similarity(
            query_embedding=query_embedding,
            candidate_embeddings=list(zip(
                [d.id for d, _emb in valid_candidates],
                [emb for _d, emb in valid_candidates]
            )),
            top_k=limit * 2,
        )

        # Build raw results
        id_to_discovery = {d.id: d for d, _emb in valid_candidates}
        raw_results = []

        for discovery_id, similarity in scored:
            if similarity < min_similarity:
                continue
            if discovery_id in id_to_discovery:
                raw_results.append((id_to_discovery[discovery_id], similarity))

        if not raw_results:
            return []

        # Blend with connectivity scores
        return await self._blend_with_connectivity(
            raw_results,
            connectivity_weight=connectivity_weight,
            exclude_orphans=exclude_orphans,
            limit=limit,
            temporal_decay=temporal_decay,
            half_life_days=half_life_days,
            status_weight=status_weight,
        )

    async def link_discoveries(
        self,
        from_id: str,
        to_id: str,
        reason: Optional[str] = None,
        strength: Optional[float] = None,
        bidirectional: bool = False,
    ) -> Dict[str, Any]:
        """
        Create a RELATED_TO edge between two discoveries.

        This enables agents to organically build the knowledge graph by
        connecting related discoveries they encounter.

        Args:
            from_id: Source discovery ID
            to_id: Target discovery ID
            reason: Optional explanation for the relationship
            strength: Optional relationship strength (0.0-1.0)
            bidirectional: If True, create edges in both directions

        Returns:
            Dict with success status and edge details
        """
        db = await self._get_db()

        if not await db.graph_available():
            return {"success": False, "error": "Graph database not available"}

        # Validate both discoveries exist
        check_cypher = """
            MATCH (d:Discovery)
            WHERE d.id IN [${from_id}, ${to_id}]
            RETURN collect(d.id) as found_ids
        """
        try:
            results = await db.graph_query(check_cypher, {"from_id": from_id, "to_id": to_id})
            if not results:
                return {"success": False, "error": "Failed to validate discoveries"}

            # graph_query returns list of results; single-column returns the value directly
            found_ids_raw = results[0]
            if isinstance(found_ids_raw, dict):
                if "error" in found_ids_raw:
                    return {"success": False, "error": found_ids_raw["error"]}
                found_ids_raw = found_ids_raw.get("found_ids", [])

            # Handle list result (direct value from collect())
            if isinstance(found_ids_raw, list):
                found_ids = [str(x).strip('"') for x in found_ids_raw]
            else:
                found_ids = []

            if from_id not in found_ids:
                return {"success": False, "error": f"Discovery '{from_id}' not found"}
            if to_id not in found_ids:
                return {"success": False, "error": f"Discovery '{to_id}' not found"}
        except Exception as e:
            return {"success": False, "error": f"Validation failed: {e}"}

        # Build the edge creation query
        from src.db.age_queries import create_related_to_edge

        edges_created = []

        # Create forward edge
        cypher, params = create_related_to_edge(
            from_discovery_id=from_id,
            to_discovery_id=to_id,
            strength=strength,
            reason=reason,
        )

        try:
            await db.graph_query(cypher, params)
            edges_created.append({"from": from_id, "to": to_id})
        except Exception as e:
            return {"success": False, "error": f"Failed to create edge: {e}"}

        # Create reverse edge if bidirectional
        if bidirectional:
            cypher, params = create_related_to_edge(
                from_discovery_id=to_id,
                to_discovery_id=from_id,
                strength=strength,
                reason=reason,
            )
            try:
                await db.graph_query(cypher, params)
                edges_created.append({"from": to_id, "to": from_id})
            except Exception as e:
                logger.warning(f"Failed to create reverse edge: {e}")

        return {
            "success": True,
            "edges_created": edges_created,
            "from_id": from_id,
            "to_id": to_id,
            "reason": reason,
            "bidirectional": bidirectional,
            "message": f"Linked '{from_id[:30]}...' to '{to_id[:30]}...'" + (" (bidirectional)" if bidirectional else "")
        }

    async def supersede_discovery(
        self,
        new_id: str,
        old_id: str,
    ) -> Dict[str, Any]:
        """
        Mark a discovery as superseding another.

        Creates a SUPERSEDES edge from new_id to old_id. Superseded entries
        receive a connectivity penalty in search ranking.

        Args:
            new_id: The newer discovery that replaces the old one
            old_id: The older discovery being superseded

        Returns:
            Dict with success status
        """
        db = await self._get_db()

        if not await db.graph_available():
            return {"success": False, "error": "Graph database not available"}

        # Validate both exist
        for did, label in [(new_id, "new"), (old_id, "old")]:
            node = await self.get_discovery(did)
            if not node:
                return {"success": False, "error": f"{label.title()} discovery '{did}' not found"}

        cypher, params = create_supersedes_edge(new_id, old_id)
        try:
            await db.graph_query(cypher, params)
            return {
                "success": True,
                "new_id": new_id,
                "old_id": old_id,
                "message": f"'{new_id[:30]}...' now supersedes '{old_id[:30]}...'"
            }
        except Exception as e:
            return {"success": False, "error": f"Failed to create SUPERSEDES edge: {e}"}

    # =========================================================================
    # LIFECYCLE MANAGEMENT
    # =========================================================================

    async def get_orphan_discoveries(
        self,
        limit: int = 100,
        min_age_days: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Find discoveries with no inbound edges (orphans).

        Orphans are discoveries that no other discovery references.
        They may still have outbound edges (referencing others).

        Args:
            limit: Maximum discoveries to return
            min_age_days: Only return orphans older than this many days

        Returns:
            List of orphan discovery summaries with metadata
        """
        db = await self._get_db()

        if not await db.graph_available():
            return []

        # Calculate cutoff date
        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=min_age_days)).isoformat()

        cypher = """
            MATCH (d:Discovery)
            WHERE d.timestamp < ${cutoff}
            OPTIONAL MATCH (other:Discovery)-[:RELATED_TO]->(d)
            OPTIONAL MATCH (resp:Discovery)-[:RESPONDS_TO]->(d)
            WITH d, count(other) + count(resp) as inbound_count
            WHERE inbound_count = 0
            RETURN {
                id: d.id,
                summary: d.summary,
                type: d.type,
                status: d.status,
                agent_id: d.agent_id
            } as discovery
            ORDER BY d.id ASC
            LIMIT ${limit}
        """

        try:
            results = await db.graph_query(cypher, {"cutoff": cutoff, "limit": limit})
            orphans = []
            for result in results:
                if isinstance(result, dict) and "error" not in result:
                    orphans.append(result)
            return orphans
        except Exception as e:
            logger.warning(f"Failed to get orphan discoveries: {e}")
            return []

    async def get_stale_discoveries(
        self,
        older_than_days: int = 30,
        status: Optional[str] = "open",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Find stale discoveries that may need attention.

        Stale discoveries are old, unresolved items that might need
        archiving or resolution.

        Staleness is creation-time based (d.timestamp). Read-recency, if a
        future predicate wants it, is durably recorded in audit.events as
        knowledge_read events (via _broadcast_knowledge_read) — do not
        reintroduce a per-read vertex property for it; that caused AGE
        TM_Updated write-write races (removed 2026-06-11).

        Args:
            older_than_days: Find discoveries older than this
            status: Filter by status (None = any status)
            limit: Maximum to return

        Returns:
            List of stale discovery summaries
        """
        db = await self._get_db()

        if not await db.graph_available():
            return []

        from datetime import datetime, timedelta
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat()

        # Build status filter
        status_clause = "AND d.status = ${status}" if status else ""

        cypher = f"""
            MATCH (d:Discovery)
            WHERE d.timestamp < ${{cutoff}} {status_clause}
            RETURN {{
                id: d.id,
                summary: d.summary,
                type: d.type,
                status: d.status,
                agent_id: d.agent_id,
                severity: d.severity
            }} as discovery
            ORDER BY d.id ASC
            LIMIT ${{limit}}
        """

        params = {"cutoff": cutoff, "limit": limit}
        if status:
            params["status"] = status

        try:
            results = await db.graph_query(cypher, params)
            stale = []
            for result in results:
                if isinstance(result, dict) and "error" not in result:
                    stale.append(result)
            return stale
        except Exception as e:
            logger.warning(f"Failed to get stale discoveries: {e}")
            return []

    async def archive_discoveries_batch(
        self,
        discovery_ids: List[str],
        reason: str = "lifecycle_cleanup",
    ) -> Dict[str, Any]:
        """
        Archive multiple discoveries in a batch — DUAL-WRITE.

        Sets status to 'archived' on BOTH the AGE graph node and the canonical
        relational ``knowledge.discoveries`` row, in a single transaction, so the
        two stores stay consistent. The live search/dashboard reads the AGE
        nodes; the relational table is the canonical row store. A previous
        version updated only the AGE node, silently diverging the stores (an
        archived discovery still showed `status='open'` in relational and vice
        versa).

        Accounting note: AGE's ``UNWIND … MATCH … SET … RETURN`` yields no rows
        in the deployed AGE build even when the SET applies, so we must NOT
        derive the archived count from that RETURN (the old code did, and always
        reported `archived: 0 / all-errors` on success). We instead take the
        authoritative affected-id list from the relational UPDATE's RETURNING —
        valid because every discovery is dual-written, so the relational row is
        the membership source of truth.

        Args:
            discovery_ids: List of discovery IDs to archive
            reason: Reason for archiving

        Returns:
            Dict with success count and any errors
        """
        if not discovery_ids:
            return {"success": True, "archived": 0, "errors": []}

        db = await self._get_db()

        if not await db.graph_available():
            return {"success": False, "error": "Graph database not available"}

        from datetime import datetime
        archived_at = datetime.now().isoformat()

        # AGE node update. No RETURN — the count comes from the relational side
        # (AGE UNWIND-SET-RETURN is empty on this build even when SET succeeds).
        age_cypher = """
            UNWIND ${ids} AS disc_id
            MATCH (d:Discovery {id: disc_id})
            SET d.status = 'archived',
                d.archived_at = ${archived_at},
                d.archive_reason = ${reason}
        """

        try:
            async with db.transaction() as conn:
                # 1) AGE graph nodes (the store search/dashboard reads).
                await db.graph_query(
                    age_cypher,
                    {"ids": discovery_ids, "archived_at": archived_at, "reason": reason},
                    conn=conn,
                )
                # 2) Canonical relational rows — same transaction. RETURNING
                #    gives the authoritative set of rows actually archived.
                #    (relational has no archived_at/archive_reason columns; that
                #    metadata lives only on the AGE node.)
                rows = await conn.fetch(
                    """
                    UPDATE knowledge.discoveries
                    SET status = 'archived',
                        resolved_at = COALESCE(resolved_at, now()),
                        updated_at = now()
                    WHERE id = ANY($1::text[])
                    RETURNING id
                    """,
                    discovery_ids,
                )
            archived_ids = {r["id"] for r in rows}
            errors = [
                {"id": did, "error": "Not found or update failed"}
                for did in discovery_ids if did not in archived_ids
            ]
            return {
                "success": len(errors) == 0,
                "archived": len(archived_ids),
                "errors": errors,
                "reason": reason,
            }
        except Exception as e:
            return {
                "success": False,
                "archived": 0,
                "errors": [{"id": "batch", "error": str(e)}],
                "reason": reason,
            }

    async def cleanup_stale_discoveries(
        self,
        orphan_age_days: int = 30,
        open_age_days: int = 60,
        dry_run: bool = True,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """
        Clean up stale discoveries in the knowledge graph.

        This is the main lifecycle management entry point. It identifies
        and optionally archives:
        1. Orphan discoveries (no inbound edges) older than orphan_age_days
        2. Open discoveries that have been unresolved for open_age_days

        Args:
            orphan_age_days: Archive orphans older than this (default 30)
            open_age_days: Archive open items older than this (default 60)
            dry_run: If True, report what would be done without doing it
            limit: Max discoveries to process per category

        Returns:
            Dict with cleanup results and statistics
        """
        # Find candidates
        orphans = await self.get_orphan_discoveries(limit=limit, min_age_days=orphan_age_days)
        stale_open = await self.get_stale_discoveries(
            older_than_days=open_age_days,
            status="open",
            limit=limit,
        )

        # Deduplicate (an orphan might also be stale)
        orphan_ids = {o.get("id", o) if isinstance(o, dict) else o for o in orphans}
        stale_ids = {s.get("id", s) if isinstance(s, dict) else s for s in stale_open}
        all_candidates = orphan_ids | stale_ids

        result = {
            "dry_run": dry_run,
            "orphans_found": len(orphan_ids),
            "stale_open_found": len(stale_ids),
            "total_candidates": len(all_candidates),
            "orphan_threshold_days": orphan_age_days,
            "open_threshold_days": open_age_days,
        }

        if dry_run:
            # Just report what would be done
            result["would_archive"] = list(all_candidates)[:20]  # Sample
            result["message"] = f"Dry run: would archive {len(all_candidates)} discoveries"
            return result

        # Actually archive
        if all_candidates:
            archive_result = await self.archive_discoveries_batch(
                list(all_candidates),
                reason=f"lifecycle_cleanup:orphan>{orphan_age_days}d,open>{open_age_days}d",
            )
            result.update({
                "archived": archive_result.get("archived", 0),
                "errors": archive_result.get("errors", []),
                "message": f"Archived {archive_result.get('archived', 0)} discoveries",
            })
        else:
            result["archived"] = 0
            result["message"] = "No discoveries matched cleanup criteria"

        return result

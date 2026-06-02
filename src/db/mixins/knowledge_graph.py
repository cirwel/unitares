"""Knowledge graph operations mixin for PostgresBackend."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)


def _apply_operator(query: str, operator: str = "AND") -> str:
    """Join multi-term queries with the given operator for websearch_to_tsquery.

    Preserves quoted phrases, existing operators, and negations (-). Single-term
    queries are returned unchanged. AND is the default — earlier OR-default
    drowned paraphrased queries in common-token noise (issue #165 #1).

    Callers wanting recall over precision pass operator="OR", typically as a
    fallback after AND returns zero hits.
    """
    op = operator.upper()
    if op not in ("AND", "OR"):
        op = "AND"
    # If query already contains explicit operators, leave as-is
    if re.search(r'\b(OR|AND)\b', query):
        return query
    # Split into tokens, preserving quoted phrases
    tokens = [m.group() for m in re.finditer(r'"[^"]*"|\S+', query)]
    if len(tokens) <= 1:
        return query
    return f' {op} '.join(tokens)


# Backwards-compat shim. Older imports expect _or_default_query; route them
# through the new operator-aware helper with operator="OR" so behavior is
# identical to the pre-#165 implementation.
def _or_default_query(query: str) -> str:
    return _apply_operator(query, operator="OR")


class KnowledgeGraphMixin:
    """Knowledge graph (PostgreSQL FTS) discovery operations."""

    async def kg_add_discovery(self, discovery) -> None:
        """Add a discovery to the knowledge graph."""
        from datetime import datetime as dt
        from src.knowledge_graph import normalize_tags

        if hasattr(discovery, 'tags') and discovery.tags:
            discovery.tags = normalize_tags(discovery.tags)

        async with self.acquire() as conn:
            response_to_id = None
            response_type = None
            if hasattr(discovery, 'response_to') and discovery.response_to:
                response_to_id = discovery.response_to.discovery_id
                response_type = discovery.response_to.response_type

            created_at = None
            if hasattr(discovery, 'timestamp') and discovery.timestamp:
                ts = discovery.timestamp
                if isinstance(ts, str):
                    try:
                        created_at = dt.fromisoformat(ts.replace('Z', '+00:00'))
                    except ValueError:
                        created_at = dt.now()
                elif isinstance(ts, dt):
                    created_at = ts
                else:
                    created_at = dt.now()

            from config.governance_config import GovernanceConfig
            await conn.execute("""
                INSERT INTO knowledge.discoveries (
                    id, agent_id, type, summary, details, tags, severity, status,
                    references_files, related_to, response_to_id, response_type,
                    provenance, provenance_chain, created_at, epoch
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                ON CONFLICT (id) DO UPDATE SET
                    summary = EXCLUDED.summary,
                    details = EXCLUDED.details,
                    tags = EXCLUDED.tags,
                    status = EXCLUDED.status,
                    provenance_chain = EXCLUDED.provenance_chain,
                    updated_at = now()
            """,
                discovery.id,
                discovery.agent_id,
                discovery.type,
                discovery.summary,
                discovery.details or "",
                discovery.tags or [],
                discovery.severity or "low",
                discovery.status or "open",
                discovery.references_files or [],
                discovery.related_to or [],
                response_to_id,
                response_type,
                json.dumps(discovery.provenance) if discovery.provenance else None,
                json.dumps(discovery.provenance_chain) if discovery.provenance_chain else None,
                created_at,
                GovernanceConfig.CURRENT_EPOCH,
            )

    async def kg_query(
        self,
        agent_id: Optional[str] = None,
        tags: Optional[List[str]] = None,
        type: Optional[str] = None,
        severity: Optional[str] = None,
        status: Optional[str] = None,
        created_after: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Query discoveries with filters."""
        async with self.acquire() as conn:
            conditions = []
            params = []
            param_idx = 1

            if agent_id:
                conditions.append(f"agent_id = ${param_idx}")
                params.append(agent_id)
                param_idx += 1
            if type:
                conditions.append(f"type = ${param_idx}")
                params.append(type)
                param_idx += 1
            if severity:
                conditions.append(f"severity = ${param_idx}")
                params.append(severity)
                param_idx += 1
            if status:
                conditions.append(f"status = ${param_idx}")
                params.append(status)
                param_idx += 1
            if tags:
                from src.knowledge_graph import normalize_tags
                conditions.append(f"tags && ${param_idx}")
                params.append(normalize_tags(tags))
                param_idx += 1
            if created_after:
                conditions.append(f"created_at > ${param_idx}")
                params.append(created_after)
                param_idx += 1

            where_clause = " AND ".join(conditions) if conditions else "1=1"
            params.append(limit)

            rows = await conn.fetch(f"""
                SELECT * FROM knowledge.discoveries
                WHERE {where_clause}
                ORDER BY created_at DESC
                LIMIT ${param_idx}
            """, *params)

            results = [self._row_to_discovery_dict(row) for row in rows]

            if results:
                ids = [r["id"] for r in results]
                backlink_rows = await conn.fetch("""
                    SELECT response_to_id, id FROM knowledge.discoveries
                    WHERE response_to_id = ANY($1)
                    ORDER BY created_at
                """, ids)
                backlinks_map: Dict[str, List[str]] = {}
                for br in backlink_rows:
                    backlinks_map.setdefault(br["response_to_id"], []).append(br["id"])
                for r in results:
                    if r["id"] in backlinks_map:
                        r["responses_from"] = backlinks_map[r["id"]]

            return results

    async def kg_full_text_search(
        self,
        query: str,
        limit: int = 20,
        operator: str = "AND",
    ) -> List[Dict[str, Any]]:
        """Full-text search using PostgreSQL tsvector.

        Multi-term queries default to AND (all terms must match) — switched
        from OR-default in #165 because OR drowned paraphrased queries in
        common-token noise. Callers wanting recall over precision pass
        operator="OR", typically as an automatic fallback after AND returns
        zero hits (the high-level handler handles that loop). Quoted phrases
        and explicit AND/OR/NOT in the query are preserved as-is.
        """
        # Use ts_rank_cd (cover density) — considers term proximity and is
        # generally better than vanilla ts_rank on short structured docs.
        ts_query = _apply_operator(query, operator=operator)
        async with self.acquire() as conn:
            rows = await conn.fetch("""
                SELECT *, ts_rank_cd(search_vector, websearch_to_tsquery('english', $1)) as rank
                FROM knowledge.discoveries
                WHERE search_vector @@ websearch_to_tsquery('english', $1)
                ORDER BY rank DESC, created_at DESC
                LIMIT $2
            """, ts_query, limit)

            return [self._row_to_discovery_dict(row) for row in rows]

    async def kg_find_similar(
        self,
        discovery_id: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Find similar discoveries by tag overlap."""
        async with self.acquire() as conn:
            source_row = await conn.fetchrow(
                "SELECT tags FROM knowledge.discoveries WHERE id = $1",
                discovery_id
            )
            if not source_row or not source_row['tags']:
                return []

            source_tags = source_row['tags']

            rows = await conn.fetch("""
                SELECT d.*,
                       cardinality(ARRAY(SELECT unnest(d.tags) INTERSECT SELECT unnest($1::text[]))) as overlap
                FROM knowledge.discoveries d
                WHERE d.id != $2
                  AND d.tags && $1::text[]
                ORDER BY overlap DESC, created_at DESC
                LIMIT $3
            """, source_tags, discovery_id, limit)

            return [self._row_to_discovery_dict(row) for row in rows]

    async def kg_topic_candidates(
        self,
        min_members: int = 3,
        limit: int = 20,
        exclude_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Topics (normalized tags) dense enough to be worth rolling up.

        Powers the on-demand synthesis pass (Issue #1): returns each tag that
        appears on at least ``min_members`` non-archived discoveries, most active
        first. ``exclude_types`` drops rows whose ``type`` is in the list — the
        synthesis pass passes its own rollup type so rollups never become members
        of a future rollup (no feedback loop). Read-only aggregate; no writes.
        """
        excluded = exclude_types or []
        async with self.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT tag AS topic,
                       COUNT(*) AS member_count,
                       COUNT(*) FILTER (WHERE status = 'open') AS open_count
                FROM knowledge.discoveries d, unnest(d.tags) AS tag
                WHERE d.status <> 'archived'
                  AND ($3::text[] IS NULL OR d.type <> ALL($3::text[]))
                GROUP BY tag
                HAVING COUNT(*) >= $1
                ORDER BY member_count DESC, tag ASC
                LIMIT $2
                """,
                min_members,
                limit,
                excluded or None,
            )
            return [
                {
                    "topic": r["topic"],
                    "member_count": r["member_count"],
                    "open_count": r["open_count"],
                }
                for r in rows
            ]

    async def kg_get_discovery(self, discovery_id: str) -> Optional[Dict[str, Any]]:
        """Get a single discovery by ID, including backlinks."""
        async with self.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT * FROM knowledge.discoveries WHERE id = $1
            """, discovery_id)

            if not row:
                return None

            d = self._row_to_discovery_dict(row)

            backlinks = await conn.fetch("""
                SELECT id FROM knowledge.discoveries
                WHERE response_to_id = $1
                ORDER BY created_at
            """, discovery_id)
            if backlinks:
                d["responses_from"] = [r["id"] for r in backlinks]

            return d

    async def kg_update_status(
        self,
        discovery_id: str,
        status: str,
        resolved_at: Optional[str] = None,
    ) -> bool:
        """Update discovery status."""
        async with self.acquire() as conn:
            if resolved_at:
                result = await conn.execute("""
                    UPDATE knowledge.discoveries
                    SET status = $1, resolved_at = $2, updated_at = now()
                    WHERE id = $3
                """, status, resolved_at, discovery_id)
            else:
                result = await conn.execute("""
                    UPDATE knowledge.discoveries
                    SET status = $1, updated_at = now()
                    WHERE id = $2
                """, status, discovery_id)
            return "UPDATE 1" in result

    def _row_to_discovery_dict(self, row) -> Dict[str, Any]:
        """Convert a database row to discovery dict."""
        d = dict(row)
        for ts_field in ['created_at', 'updated_at', 'resolved_at']:
            if d.get(ts_field):
                d[ts_field] = d[ts_field].isoformat()
        if 'created_at' in d:
            d['timestamp'] = d['created_at']
        if d.get('provenance') and isinstance(d['provenance'], str):
            d['provenance'] = json.loads(d['provenance'])
        if d.get('provenance_chain') and isinstance(d['provenance_chain'], str):
            d['provenance_chain'] = json.loads(d['provenance_chain'])
        d.pop('search_vector', None)
        d.pop('rank', None)
        d.pop('overlap', None)
        return d

"""
Knowledge Graph - Data types and backend factory

Provides DiscoveryNode/ResponseTo data types used by all backends,
and get_knowledge_graph() factory for backend selection.

Backends:
- PostgreSQL FTS - canonical/default, configured via UNITARES_KNOWLEDGE_BACKEND=postgres
- AGE (PostgreSQL + Apache AGE) - optional graph backend, configured via UNITARES_KNOWLEDGE_BACKEND=age
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Literal, Any, get_args
from datetime import datetime, timezone
import os
import re
import asyncio

# Import structured logging
from src.logging_utils import get_logger
logger = get_logger(__name__)


def normalize_tags(tags) -> List[str]:
    """Normalize tags to canonical form for consistent search.

    Applies: lowercase, fold any run of separators/punctuation to a single
    hyphen, strip edge hyphens, apply the formatting-layer spelling-variant
    map (``postgresql`` → ``postgres``), deduplicate while preserving order.

    This mirrors the governance-plugin client normalizer (``tag_normalize.py``)
    so tags minted server-side, by non-plugin clients, or via the direct REST
    path all converge on the same canonical form. Tag fragmentation is mostly
    a formatting problem (``Postgres`` / ``postgres`` / ``PostgreSQL`` filed
    three ways); this collapses it at every write and tag-filtered search.

    Deliberately conservative: no plural stripping (``metrics`` → ``metric``
    is lossy) and no semantic synonym merging (``auth`` vs ``identity``). The
    semantic residue is handled by the lifecycle pass via
    ``src.knowledge_ontology.apply_semantic_synonyms``, never here.

    Handles string input (JSON arrays or comma-separated) gracefully since MCP
    unified tools may pass tags as strings instead of lists.

    Examples:
        ["EISV", "eisv-dynamics", "eisv_framework"] → ["eisv", "eisv-dynamics", "eisv-framework"]
        ["bug", "Bug Fix", "bug-fix", "bug_fix"] → ["bug", "bug-fix"]
        ["Postgres", "postgres", "PostgreSQL"] → ["postgres"]
        ["node.js", "C++"] → ["node-js", "c"]
        '["ux", "identity"]' → ["ux", "identity"]
        "ux, identity" → ["ux", "identity"]
    """
    if not tags:
        return []
    from src.knowledge_ontology import SPELLING_VARIANTS
    # Handle string input: try JSON parse, then comma-split
    if isinstance(tags, str):
        import json
        try:
            parsed = json.loads(tags)
            if isinstance(parsed, list):
                tags = parsed
            else:
                tags = [str(parsed)]
        except (json.JSONDecodeError, ValueError):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
    seen = set()
    result = []
    for tag in tags:
        # Lowercase and strip
        t = tag.strip().lower()
        if not t:
            continue
        # Fold any run of separators/punctuation (spaces, underscores, dots,
        # slashes, etc.) to a single hyphen. A run collapses in one pass, so
        # no separate multi-hyphen collapse is needed.
        t = re.sub(r'[^a-z0-9]+', '-', t)
        # Strip leading/trailing hyphens
        t = t.strip('-')
        if not t:
            continue
        # Formatting-layer spelling-variant map (postgresql → postgres).
        t = SPELLING_VARIANTS.get(t, t)
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


# Response-link and status vocabularies. Single source for the dataclass
# Literal, the handler validation sets (src/mcp_handlers/knowledge/handlers.py),
# and — via tests/test_knowledge_enum_sync.py — the SQL CHECK constraints in
# db/postgres/knowledge_schema.sql and migration 047.
ResponseType = Literal[
    "extend", "question", "disagree", "support", "answer",
    "follow_up", "correction", "elaboration", "supersedes",
]
VALID_RESPONSE_TYPES = frozenset(get_args(ResponseType))

VALID_DISCOVERY_STATUSES = frozenset({
    "open", "resolved", "archived", "disputed", "closed", "wont_fix", "superseded",
})

VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})


@dataclass
class ResponseTo:
    """Typed response link to another discovery"""
    discovery_id: str
    response_type: ResponseType

@dataclass
class DiscoveryNode:
    """Node in knowledge graph representing a single discovery"""
    id: str
    agent_id: str
    type: str  # "bug_found", "insight", "pattern", "improvement", "question", "answer"
    summary: str
    details: str = ""
    tags: List[str] = field(default_factory=list)
    severity: Optional[str] = None  # "low", "medium", "high", "critical"
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "open"  # "open", "resolved", "archived", "disputed", "superseded", "closed", "wont_fix"
    superseded_by: Optional[str] = None  # discovery_id of the entry that superseded this one (KG hygiene v1)
    related_to: List[str] = field(default_factory=list)  # IDs of related discoveries (backward compat)
    response_to: Optional[ResponseTo] = None  # Typed response to parent discovery
    responses_from: List[str] = field(default_factory=list)  # IDs of discoveries that respond to this one (backlinks)
    references_files: List[str] = field(default_factory=list)
    resolved_at: Optional[str] = None
    updated_at: Optional[str] = None
    confidence: Optional[float] = None
    # ENHANCED PROVENANCE: Agent state at time of creation (2025-12-15)
    provenance: Optional[Dict[str, Any]] = None
    # PROVENANCE CHAIN: Full lineage context for multi-agent collaboration
    provenance_chain: Optional[List[Dict[str, Any]]] = None

    def to_dict(self, include_details: bool = True) -> dict:
        """Convert to dictionary for JSON serialization"""
        result = {
            "id": self.id,
            "agent_id": self.agent_id,
            "type": self.type,
            "summary": self.summary,
            "tags": self.tags,
            "severity": self.severity,
            "timestamp": self.timestamp,
            "created_at": self.timestamp,  # Alias for timestamp (UX consistency)
            "status": self.status,
            "related_to": self.related_to,
            "references_files": self.references_files,
            "resolved_at": self.resolved_at,
            "updated_at": self.updated_at
        }

        # Add typed response_to if present
        if self.response_to:
            result["response_to"] = {
                "discovery_id": self.response_to.discovery_id,
                "response_type": self.response_to.response_type
            }

        # Add backlinks (responses_from)
        if self.responses_from:
            result["responses_from"] = self.responses_from

        if self.confidence is not None:
            result["confidence"] = self.confidence

        if self.superseded_by is not None:
            result["superseded_by"] = self.superseded_by

        # Include provenance if present (agent state at creation)
        if self.provenance:
            result["provenance"] = self.provenance

        # Include provenance chain if present (lineage context)
        if self.provenance_chain:
            result["provenance_chain"] = self.provenance_chain

        if include_details:
            result["details"] = self.details
        else:
            # Include preview + has_more hint so the agent can decide whether
            # to fetch full details via knowledge(action='get', id=...).
            if self.details:
                # Function-local import to avoid a cycle with mcp_handlers.
                from src.mcp_handlers.knowledge.limits import DETAILS_PREVIEW_CHARS
                result["has_details"] = True
                if len(self.details) > DETAILS_PREVIEW_CHARS:
                    result["details_preview"] = self.details[:DETAILS_PREVIEW_CHARS] + "..."
                    result["details_length"] = len(self.details)
                    result["has_more_details"] = True
                else:
                    result["details_preview"] = self.details
                    result["has_more_details"] = False
        return result

    @classmethod
    def from_dict(cls, data: dict) -> 'DiscoveryNode':
        """Create from dictionary"""
        # Parse response_to if present
        response_to = None
        if "response_to" in data and data["response_to"]:
            resp_data = data["response_to"]
            if isinstance(resp_data, dict):
                response_to = ResponseTo(
                    discovery_id=resp_data["discovery_id"],
                    response_type=resp_data["response_type"]
                )

        return cls(
            id=data["id"],
            agent_id=data["agent_id"],
            type=data["type"],
            summary=data["summary"],
            details=data.get("details", ""),
            tags=data.get("tags", []),
            severity=data.get("severity"),
            timestamp=data.get("timestamp", datetime.now(timezone.utc).isoformat()),
            status=data.get("status", "open"),
            related_to=data.get("related_to", []),
            response_to=response_to,
            responses_from=data.get("responses_from", []),
            references_files=data.get("references_files", []),
            resolved_at=data.get("resolved_at"),
            updated_at=data.get("updated_at"),
            confidence=data.get("confidence"),
            provenance=data.get("provenance"),
            provenance_chain=data.get("provenance_chain"),
            superseded_by=data.get("superseded_by"),
        )


# Global graph instance (initialized on first use)
_graph_instance: Optional[Any] = None
_graph_lock: Optional[asyncio.Lock] = None  # Created lazily to avoid binding to wrong event loop


async def get_knowledge_graph() -> Any:
    """
    Get global knowledge graph instance (singleton).

    Backend selection (priority order):
    1. UNITARES_KNOWLEDGE_BACKEND env var (explicit override)
       - age     -> AGE (PostgreSQL + Apache AGE) backend
       - postgres -> PostgreSQL FTS backend
       - auto    -> see below
    2. DB_BACKEND env var (implicit selection when UNITARES_KNOWLEDGE_BACKEND=auto)
       - postgres -> PostgreSQL FTS backend

    If PostgreSQL is unavailable, the server fails honestly rather than
    silently degrading to an in-memory store.
    """
    global _graph_instance, _graph_lock

    # Create lock lazily in the current event loop (fixes import-time binding issue)
    if _graph_lock is None:
        _graph_lock = asyncio.Lock()

    async with _graph_lock:
        if _graph_instance is not None:
            return _graph_instance

        backend = os.getenv("UNITARES_KNOWLEDGE_BACKEND", "auto").strip().lower()
        db_backend = os.getenv("DB_BACKEND", "postgres").strip().lower()

        # If auto and main database is PostgreSQL, use PostgreSQL for knowledge graph too
        if backend == "auto" and db_backend == "postgres":
            backend = "postgres"
            logger.info("Auto-selecting PostgreSQL knowledge backend (DB_BACKEND=postgres)")

        # AGE backend (PostgreSQL + Apache AGE)
        if backend == "age":
            from src.storage.knowledge_graph import KnowledgeGraphAGE
            _graph_instance = KnowledgeGraphAGE()
            await _graph_instance.load()
            logger.info("Using AGE (PostgreSQL + Apache AGE) knowledge graph backend")
            return _graph_instance

        # PostgreSQL FTS backend (unified with main database)
        if backend in ("postgres", "auto"):
            from src.storage.knowledge_graph import KnowledgeGraphPostgres
            _graph_instance = KnowledgeGraphPostgres()
            await _graph_instance.load()
            logger.info("Using PostgreSQL FTS knowledge graph backend")
            return _graph_instance

        raise RuntimeError(
            f"Unknown knowledge backend '{backend}'. "
            f"Set UNITARES_KNOWLEDGE_BACKEND to 'age' or 'postgres'."
        )


def tag_provenance_source(
    provenance: Optional[Dict[str, Any]],
    source: str,
) -> Dict[str, Any]:
    """Attach `source` to a discovery's provenance without clobbering keys.

    Implicit writers (self_recovery, dialectic_thesis, lifecycle_op) set
    provenance.source so list/stats can split by_agent into explicit vs
    implicit buckets — the previous "phantom write" symptom (#165) was a
    side effect of implicit writes being indistinguishable from caller-
    intentional ones in the by_agent count.

    Explicit writes (`explicit_store`, `explicit_answer`, `explicit_leave_note`)
    are tagged the same way for symmetry — that way an absent provenance.source
    is unambiguously a legacy row.
    """
    base: Dict[str, Any] = dict(provenance) if provenance else {}
    base.setdefault("source", source)
    return base


# Sentinel set for `source` values that count as caller-intentional writes
# (visible in by_agent_explicit). Anything else — including absent — counts as
# implicit / legacy / unknown and is bucketed separately.
EXPLICIT_PROVENANCE_SOURCES = frozenset({
    "explicit_store",
    "explicit_answer",
    "explicit_leave_note",
})


def is_explicit_source(provenance: Optional[Dict[str, Any]]) -> bool:
    """True when provenance.source declares a caller-intentional write."""
    if not provenance or not isinstance(provenance, dict):
        return False
    return provenance.get("source") in EXPLICIT_PROVENANCE_SOURCES


def selected_backend_name() -> str:
    """Resolve the active backend label without instantiating it.

    Mirrors the env-var precedence in ``get_knowledge_graph`` so health checks
    and capability probes can run inside anyio contexts where instantiating
    the backend would deadlock on asyncpg.
    """
    backend = os.getenv("UNITARES_KNOWLEDGE_BACKEND", "auto").strip().lower()
    db_backend = os.getenv("DB_BACKEND", "postgres").strip().lower()
    if backend == "auto" and db_backend == "postgres":
        return "postgres"
    if backend in ("age", "postgres"):
        return backend
    if backend == "auto":
        return "postgres"
    return backend


def backend_supports_semantic_search() -> bool:
    """True when the configured backend exposes ``semantic_search``.

    Class-level introspection — does not instantiate or touch the DB. Used by
    health checks to distinguish embedder availability (the model service is
    loadable) from semantic-search reachability (the active backend can use it).
    """
    name = selected_backend_name()
    if name == "age":
        try:
            from src.storage.knowledge_graph import KnowledgeGraphAGE
            return hasattr(KnowledgeGraphAGE, "semantic_search")
        except Exception:
            return False
    return False

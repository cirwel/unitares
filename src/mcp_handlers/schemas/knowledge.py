from typing import Optional, Union, Literal, Dict, Any, List, Sequence
from pydantic import Field, model_validator
from .mixins import AgentIdentityMixin

class StoreKnowledgeGraphParams(AgentIdentityMixin):
    """
    Store knowledge discovery/discoveries in graph
    """
    discovery_type: Optional[Literal[
        "architectural_decision", "learning", "pattern", "bug_fix", 
        "refactoring", "documentation", "experiment", "question", "note", "rule"
    ]] = Field(
        default=None,
        description="Type of discovery"
    )
    summary: Optional[str] = Field(
        default=None,
        description="Short description (max 200 chars)"
    )
    details: Optional[str] = Field(
        default=None,
        description="Full text/code of the discovery"
    )
    tags: Optional[List[str]] = Field(
        default_factory=list,
        description="Categorization tags"
    )
    related_to: Optional[List[str]] = Field(
        default_factory=list,
        description="IDs of related discoveries"
    )
    severity: Optional[Literal["low", "medium", "high", "critical"]] = Field(
        default="medium",
        description="Importance/impact"
    )
    discoveries: Optional[List[dict]] = Field(
        default=None,
        description="Array of discovery objects for batch storage"
    )


class SearchKnowledgeGraphParams(AgentIdentityMixin):
    """
    Search knowledge graph (indexed filters; optional FTS query).
    """
    query: Optional[str] = Field(
        default=None,
        description="Optional full-text search string"
    )
    tags: Optional[List[str]] = Field(
        default_factory=list,
        description="Filter by exact tags"
    )
    created_after: Optional[str] = Field(
        default=None,
        description="ISO 8601 date string"
    )
    created_before: Optional[str] = Field(
        default=None,
        description="ISO 8601 date string"
    )
    discovery_type: Optional[str] = Field(
        default=None,
        description="Filter by type"
    )
    agent_id_filter: Optional[str] = Field(
        default=None,
        description="Filter by author agent UUID"
    )
    status: Optional[str] = Field(
        default=None,
        description="Filter by status (open, resolved, archived, superseded)"
    )
    severity: Optional[str] = Field(
        default=None,
        description="Filter by severity"
    )
    sort_by: Literal["created_at", "relevance", "score", "related_count"] = Field(
        default="created_at",
        description="Sort field"
    )
    limit: Union[int, str, None] = Field(
        default=10,
        description="Max results (1-50)"
    )
    include_summary_only: Union[bool, str, None] = Field(
        default=False,
        description="If true, omits details field"
    )
    include_provenance: Union[bool, str, None] = Field(
        default=False,
        description="If true, includes provenance chain"
    )
    include_details: Union[bool, str, None] = Field(
        default=False,
        description="If true, includes full details for each discovery"
    )
    search_mode: Optional[Literal["auto", "fts", "semantic", "hybrid"]] = Field(
        default="auto",
        description=(
            "Force a specific retrieval mode. 'auto' picks the best available "
            "(hybrid > semantic > fts > substring). 'semantic' and 'hybrid' "
            "fail honestly when the backend has no semantic_search — the "
            "router does not silently fall back to FTS."
        ),
    )
    operator: Optional[Literal["AND", "OR"]] = Field(
        default=None,
        description=(
            "Boolean operator for multi-term FTS queries. Default (None) is "
            "AND with automatic OR fallback when AND returns zero results. "
            "Pass 'OR' explicitly for broad recall (skips the AND-first step)."
        ),
    )

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.limit, str):
            try:
                self.limit = int(self.limit)
            except ValueError:
                self.limit = 10
        if isinstance(self.include_summary_only, str):
            self.include_summary_only = self.include_summary_only.lower() in ('true', '1', 'yes')
        if isinstance(self.include_provenance, str):
            self.include_provenance = self.include_provenance.lower() in ('true', '1', 'yes')
        if isinstance(self.include_details, str):
            self.include_details = self.include_details.lower() in ('true', '1', 'yes')
        return self


class GetKnowledgeGraphParams(AgentIdentityMixin):
    """
    Get all knowledge for an agent - summaries only
    """
    tags: Optional[List[str]] = Field(
        default_factory=list,
        description="Filter by exact tags"
    )
    limit: Union[int, str, None] = Field(
        default=50,
        description="Max results to return"
    )
    
    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.limit, str):
            try:
                self.limit = int(self.limit)
            except ValueError:
                self.limit = 50
        return self


class ListKnowledgeGraphParams(AgentIdentityMixin):
    """
    List knowledge graph statistics (raw status aggregate).

    Numbers may differ from knowledge action=stats (which uses lifecycle
    buckets) — see #165. The response surfaces a `scope` block declaring
    the epoch_scope and including_cold settings so the difference is
    visible rather than hidden behind same-name fields.
    """
    epoch_scope: Optional[Literal["current", "all"]] = Field(
        default="current",
        description=(
            "'current' restricts to the active epoch (default); 'all' "
            "counts every epoch ever stored. AGE backend ignores this — "
            "it has no epoch property."
        ),
    )
    including_cold: Union[bool, str, None] = Field(
        default=False,
        description=(
            "Include cold-storage rows in totals and by_status. False "
            "(default) excludes them so 'active' KG counts aren't "
            "conflated with the deep-archive tier."
        ),
    )

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.including_cold, str):
            self.including_cold = self.including_cold.lower() in ('true', '1', 'yes')
        return self


class UpdateDiscoveryStatusGraphParams(AgentIdentityMixin):
    """
    Update discovery status - fast graph update
    """
    discovery_id: str = Field(
        ..., description="ID of discovery to update"
    )
    new_status: Literal["active", "resolved", "superseded", "archived"] = Field(
        ..., description="New status"
    )
    superseded_by: Optional[str] = Field(
        default=None,
        description="ID of discovery that supersedes this one"
    )
    resolution_notes: Optional[str] = Field(
        default=None,
        description="Notes on how/why this was resolved"
    )


class GetDiscoveryDetailsParams(AgentIdentityMixin):
    """
    Get full details for a specific discovery
    """
    discovery_id: str = Field(
        ..., description="ID of discovery"
    )
    offset: Union[int, str, None] = Field(
        default=0,
        description="Character offset for details pagination"
    )
    fetch_chain: Union[bool, str, None] = Field(
        default=False,
        description="If true, fetches the full discussion chain linked to this item"
    )
    include_provenance: Union[bool, str, None] = Field(
        default=False,
        description="If true, fetches lineage and temporal ordering metadata"
    )
    
    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.offset, str):
            try:
                self.offset = int(self.offset)
            except ValueError:
                self.offset = 0
        if isinstance(self.fetch_chain, str):
            self.fetch_chain = self.fetch_chain.lower() in ('true', '1', 'yes')
        if isinstance(self.include_provenance, str):
            self.include_provenance = self.include_provenance.lower() in ('true', '1', 'yes')
        return self


class AnswerQuestionParams(AgentIdentityMixin):
    """
    Answer a question in the knowledge graph
    """
    question: str = Field(
        ..., description="Text of question to answer"
    )
    answer: str = Field(
        ..., description="Your answer"
    )
    tags: Optional[List[str]] = Field(
        default_factory=list,
        description="Tags"
    )


class LeaveNoteParams(AgentIdentityMixin):
    """
    Leave a quick note in the knowledge graph
    """
    summary: str = Field(
        ..., description="Note text"
    )
    tags: Optional[List[str]] = Field(
        default_factory=list,
        description="Tags"
    )


class CleanupKnowledgeGraphParams(AgentIdentityMixin):
    """
    Run knowledge graph lifecycle cleanup
    """
    dry_run: Union[bool, str, None] = Field(
        default=True,
        description="If true, returns what WOULD be archived without modifying"
    )
    
    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.dry_run, str):
            self.dry_run = self.dry_run.lower() in ('true', '1', 'yes')
        return self


class KnowledgeParams(AgentIdentityMixin):
    """Parameters for knowledge"""
    action: Literal["store", "search", "get", "list", "update", "details", "note", "cleanup", "stats", "supersede", "audit"] = Field(..., description="Operation to perform")
    query: Optional[str] = Field(None, description="Search query (for action=search)")
    content: Optional[str] = Field(None, description="Extended content/details (for action=store or action=note)")
    details: Optional[str] = Field(None, description="Extended details for discovery (for action=store). Alias: content")
    summary: Optional[str] = Field(None, description="Discovery summary (for action=store)")
    discovery_type: Optional[str] = Field(None, description="Type: bug_found, insight, pattern, question, note, etc. (for action=store)")
    tags: Optional[List[str]] = Field(None, description="Tags for discovery (for action=store, search, note)")
    severity: Optional[str] = Field(None, description="Severity: low, medium, high, critical (for action=store)")
    discovery_id: Optional[str] = Field(None, description="Discovery ID (for action=details, update)")
    status: Optional[str] = Field(None, description="Status filter/update value (open, resolved, archived, superseded)")
    agent_id: Optional[str] = Field(None, description="Filter by agent (for action=get, search)")
    limit: Optional[int] = Field(None, description="Max results")
    include_details: Optional[bool] = Field(None, description="Include full details inline (for action=search/get)")
    dry_run: Union[bool, str, None] = Field(None, description="Dry run mode (for action=cleanup)")

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.dry_run, str):
            self.dry_run = self.dry_run.lower() in ('true', '1', 'yes')
        return self

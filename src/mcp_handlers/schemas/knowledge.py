from typing import Optional, Union, Literal, List
from pydantic import Field, model_validator
from .mixins import AgentIdentityMixin

DiscoveryType = Literal[
    "architectural_decision", "learning", "pattern", "bug_fix",
    "refactoring", "documentation", "experiment", "question", "note", "rule",
    "insight", "bug_found", "bug", "improvement", "exploration", "observation"
]

Severity = Literal["low", "medium", "high", "critical"]

class StoreKnowledgeGraphParams(AgentIdentityMixin):
    """
    Store knowledge discovery/discoveries in graph
    """
    discovery_type: Optional[DiscoveryType] = Field(
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
    severity: Optional[Severity] = Field(
        default="medium",
        description="Importance/impact"
    )
    discoveries: Optional[List[dict]] = Field(
        default=None,
        description="Array of discovery objects for batch storage"
    )
    # S22 provenance — agent-knowable subset only. Harness/server-knowable
    # fields (harness, harness_id, harness_type, transport, model,
    # model_provider, tool_surface, governance_mode, verification_source,
    # episode_id, invocation_id, process_instance_id, locus, affordance_state)
    # are filled server-side by build_s22_write_context from request signals;
    # exposing them to agents invited confabulation (KG 2026-05-09T13:03 by
    # 43a2cbf9). The four kept here are author-of-intent facts that only the
    # agent can honestly supply for H5 cross-harness comparison.
    comparison_key: Optional[str] = Field(None, description="S22 H5 provenance: stable key for comparing the same bounded task across harnesses")
    task_label: Optional[str] = Field(None, description="S22 H5 provenance: human-readable bounded task label")
    task_outcome: Optional[str] = Field(None, description="S22 H5 provenance: outcome label for the bounded task")
    memory_context: Optional[str] = Field(None, description="S22 provenance: memory/KG/transcript surfaces visible to the writer")


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
    # S22 provenance - agent-knowable subset only. Keep parity with
    # StoreKnowledgeGraphParams and KnowledgeParams so knowledge(action="note")
    # can preserve dogfood diagnostic provenance instead of silently dropping it.
    comparison_key: Optional[str] = Field(None, description="S22 H5 provenance: stable key for comparing the same bounded task across harnesses")
    task_label: Optional[str] = Field(None, description="S22 H5 provenance: human-readable bounded task label")
    task_outcome: Optional[str] = Field(None, description="S22 H5 provenance: outcome label for the bounded task")
    memory_context: Optional[str] = Field(None, description="S22 provenance: memory/KG/transcript surfaces visible to the writer")


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
    action: Literal["store", "search", "get", "list", "update", "details", "note", "cleanup", "synthesize", "stats", "supersede", "audit"] = Field(..., description="Operation to perform")
    query: Optional[str] = Field(None, description="Search query (for action=search)")
    content: Optional[str] = Field(None, description="Extended content/details (for action=store or action=note)")
    details: Optional[str] = Field(None, description="Extended details for discovery (for action=store). Alias: content")
    summary: Optional[str] = Field(None, description="Discovery summary (for action=store)")
    discovery_type: Optional[str] = Field(None, description="Type: bug_found, insight, pattern, question, note, etc. (for action=store)")
    response_to: Optional[dict] = Field(None, description="Typed response link {discovery_id, response_type} for threaded store/note writes")
    tags: Optional[List[str]] = Field(None, description="Tags for discovery (for action=store, search, note)")
    severity: Optional[str] = Field(None, description="Severity: low, medium, high, critical (for action=store)")
    confidence: Union[float, str, None] = Field(
        None,
        description=(
            "Writer confidence for action=store, clamped to 0-1 and "
            "cross-checked against agent coherence"
        ),
    )
    discovery_id: Optional[str] = Field(None, description="Discovery ID (for action=get/details, update; the NEW discovery for action=supersede)")
    status: Optional[str] = Field(None, description="Status filter/update value (open, resolved, archived, superseded)")
    resolution_notes: Optional[str] = Field(None, description="Rationale to append when closing or updating a discovery")
    # Supersession LINK params. Without these declared here the unified tool's
    # validator silently strips them, so the directed link is never recorded —
    # the 2026-06-21 finding (status='superseded' set, but 0 SUPERSEDES edges).
    supersedes: Optional[str] = Field(None, description="ID of an older discovery this new one replaces (for action=store)")
    superseded_by: Optional[str] = Field(None, description="ID of the discovery that supersedes this one (for action=update with status=superseded)")
    supersedes_id: Optional[str] = Field(None, description="ID of the older discovery being replaced (for action=supersede; discovery_id is the newer one)")
    agent_id: Optional[str] = Field(None, description="Filter by agent (for action=get, search; omit when using discovery_id readback)")
    limit: Optional[int] = Field(None, description="Max results")
    include_details: Optional[bool] = Field(None, description="Include full details inline (for action=search or agent-scoped action=get)")
    include_provenance: Union[bool, str, None] = Field(None, description="Include provenance and lineage chain fields in search/details results")
    search_mode: Optional[Literal["auto", "fts", "semantic", "hybrid"]] = Field(
        None,
        description="Force retrieval mode for action=search. 'semantic' and 'hybrid' fail honestly when unsupported by the active backend.",
    )
    semantic: Union[bool, str, None] = Field(None, description="Legacy action=search toggle to force or skip semantic retrieval when supported")
    min_similarity: Union[float, str, None] = Field(None, description="Minimum cosine similarity for semantic retrieval modes")
    operator: Optional[Literal["AND", "OR"]] = Field(None, description="Boolean operator for multi-term FTS queries")
    offset: Optional[int] = Field(None, description="Character offset for action=details pagination")
    length: Optional[int] = Field(None, description="Maximum details characters returned for action=details")
    include_response_chain: Union[bool, str, None] = Field(None, description="Include typed response chain for action=details")
    max_chain_depth: Optional[int] = Field(None, description="Maximum response-chain traversal depth for action=details")
    # Recall-recovery levers for action=search. Default search excludes archived
    # and cold-storage notes; pass these to reach them when an active-tier search
    # comes up empty. The handler already honors both — they were just never
    # exposed on the unified tool (see test_knowledge_param_coverage backlog).
    include_archived: Optional[bool] = Field(None, description="Include archived discoveries in search results (default: excluded)")
    include_cold: Optional[bool] = Field(None, description="Include cold-storage (long-term) discoveries in search results (default: excluded)")
    epoch_scope: Optional[Literal["current", "all"]] = Field(None, description="Stats/list scope: current epoch only or all epochs")
    including_cold: Union[bool, str, None] = Field(None, description="Include cold-storage discoveries in action=list raw status aggregates")
    scope: Optional[Literal["open", "all", "by_agent"]] = Field(None, description="KG audit scope for action=audit")
    top_n: Optional[int] = Field(None, description="Maximum stale entries returned by action=audit")
    dry_run: Union[bool, str, None] = Field(None, description="Dry run mode (for action=cleanup, synthesize)")
    # Synthesis (action=synthesize): roll discoveries up into topic summaries.
    topic: Optional[str] = Field(None, description="Synthesize just this one tag/topic (for action=synthesize). Omit to sweep the densest topics.")
    min_members: Optional[int] = Field(None, description="Minimum discoveries a topic needs before it is rolled up (for action=synthesize, default 3)")
    use_llm: Union[bool, str, None] = Field(None, description="Use the local LLM for the rollup narrative (for action=synthesize, default true; falls back to deterministic when unreachable)")
    # S22 provenance - agent-knowable subset only. See StoreKnowledgeGraphParams
    # above for the dropped-field rationale.
    comparison_key: Optional[str] = Field(None, description="S22 H5 provenance: stable key for comparing the same bounded task across harnesses")
    task_label: Optional[str] = Field(None, description="S22 H5 provenance: human-readable bounded task label")
    task_outcome: Optional[str] = Field(None, description="S22 H5 provenance: outcome label for the bounded task")
    memory_context: Optional[str] = Field(None, description="S22 provenance: memory/KG/transcript surfaces visible to the writer")

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.dry_run, str):
            self.dry_run = self.dry_run.lower() in ('true', '1', 'yes')
        if isinstance(self.use_llm, str):
            self.use_llm = self.use_llm.lower() in ('true', '1', 'yes')
        if isinstance(self.include_provenance, str):
            self.include_provenance = self.include_provenance.lower() in ('true', '1', 'yes')
        if isinstance(self.semantic, str):
            self.semantic = self.semantic.lower() in ('true', '1', 'yes')
        if isinstance(self.min_similarity, str):
            try:
                self.min_similarity = float(self.min_similarity)
            except ValueError:
                self.min_similarity = None
        if isinstance(self.confidence, str):
            try:
                self.confidence = float(self.confidence)
            except ValueError:
                self.confidence = None
        if isinstance(self.include_response_chain, str):
            self.include_response_chain = self.include_response_chain.lower() in ('true', '1', 'yes')
        if isinstance(self.including_cold, str):
            self.including_cold = self.including_cold.lower() in ('true', '1', 'yes')
        return self

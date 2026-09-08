from typing import Any, ClassVar, List, Literal, Mapping, Optional, Tuple, Union
from pydantic import Field, model_validator
from .mixins import AgentIdentityMixin

class ObserveAgentParams(AgentIdentityMixin):
    """
    Observe another agent's governance state with pattern analysis
    """
    target_agent_id: str = Field(..., description="UUID or label of agent to observe")

class CompareAgentsParams(AgentIdentityMixin):
    """
    Compare governance patterns across multiple agents
    """
    agent_ids: List[str] = Field(..., description="List of UUIDs or labels of agents to compare")

class CompareMeToSimilarParams(AgentIdentityMixin):
    """
    Compare yourself to similar agents automatically
    """
    agent_id: Optional[str] = Field(default=None, description="Your UUID or label (auto-detected if bound)")
    max_peers: Union[int, str, None] = Field(default=3, description="Maximum number of peers to compare against")
    focus: Literal["all", "ethics", "stability", "complexity", "knowledge"] = Field(
        default="all", 
        description="Focus area for comparison"
    )

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.max_peers, str):
            try:
                self.max_peers = int(self.max_peers)
            except ValueError:
                self.max_peers = 3
        return self

class DetectAnomaliesParams(AgentIdentityMixin):
    """
    Detect anomalies across agents
    """
    focus: Literal["all", "drift", "complexity", "void", "coherence"] = Field(
        default="all",
        description=(
            "Type of anomaly to focus on. coherence means provenance-qualified "
            "behavioral update consistency; legacy C(V) is excluded."
        ),
    )

class AggregateMetricsParams(AgentIdentityMixin):
    """
    Get fleet-level health overview
    """
    group_by: Literal["none", "label_prefix", "status"] = Field(
        default="none",
        description="How to group the metrics"
    )

class ObserveParams(AgentIdentityMixin):
    """Parameters for observe"""
    # Which of these flat parameters each action uses. The wire schema stays
    # flat (the MCP wrapper builds its argument model from top-level
    # properties), so this is the only machine-readable statement of the
    # per-action contract; describe_tool(action=...) serves it and
    # tests/test_router_action_fields.py holds it to the routing table.
    # Identity and session parameters are common to every action and are
    # not repeated here (schemas/router_actions.COMMON_ROUTER_FIELDS).
    ACTION_FIELDS: ClassVar[Mapping[str, Tuple[str, ...]]] = {
        "agent": (
                "target_agent_id", "include_history", "analyze_patterns",
        ),
        "compare": (
                "agent_ids", "compare_metrics",
        ),
        "similar": (
                "limit",
        ),
        # No paging parameter: anomalies returns every finding that passes the
        # filters, so nothing a caller asked to see is withheld from them.
        "anomalies": (),
        "aggregate": (),
        "telemetry": (
                "window_hours", "include_calibration",
        ),
        "audit_events": (
                "event_type", "event_types", "since", "until", "include_events",
                "include_test_fixtures",
        ),
        "outcome_evidence": (
                "outcome_type", "corroboration_grade", "diagnostic",
                "include_detail", "include_events", "low_weight_threshold",
                "min_completions",
        ),
        "bridge": (),
    }
    action: Literal["agent", "compare", "similar", "anomalies", "aggregate", "telemetry", "audit_events", "outcome_evidence", "bridge"] = Field(..., description="Operation to perform")
    target_agent_id: Optional[str] = Field(None, description="Agent to observe — UUID or label (for action=agent). Use list_agents to find.")
    agent_ids: Optional[List[Any]] = Field(None, description="Agent identifiers to compare (for action=compare, min 2)")
    include_history: bool = Field(True, description="Include recent history (for action=agent). Default true.")
    analyze_patterns: bool = Field(True, description="Perform pattern analysis (for action=agent). Default true.")
    compare_metrics: Optional[List[Any]] = Field(
        None,
        description=(
            "Metrics to compare (for action=compare). Default: risk_score, E, I, S, V. "
            "Raw coherence remains visible with provenance but is excluded from "
            "similarity and outlier calculations even when requested."
        ),
    )
    limit: Optional[int] = Field(None, description="Max results to return (for action=similar, audit_events, outcome_evidence, bridge). For audit_events this bounds the returned events only — total_emits/by_agent_id/first_ts/last_ts always describe the whole window. Not accepted by action=anomalies, which returns every anomaly passing its filters and is narrowed with anomaly_types/min_severity/agent_ids instead.")
    event_type: Optional[str] = Field(None, description="Audit event type to filter on (for action=audit_events)")
    event_types: Optional[List[str]] = Field(None, description="IN-list of audit event types (for action=audit_events, alternative to event_type)")
    since: Optional[str] = Field(None, description="Window start: '14d'/'24h'/'30m' shorthand or ISO 8601 (for action=audit_events/outcome_evidence/bridge). Default 7d for audit evidence, 24h for bridge.")
    until: Optional[str] = Field(None, description="Window end: ISO 8601 (for action=audit_events/bridge). Default now.")
    include_events: Optional[bool] = Field(None, description="Include event payloads in response. audit_events defaults false; outcome_evidence defaults true except agent_summary; bridge defaults true.")
    include_test_fixtures: bool = Field(True, description="Include agents matching Test_Agent_* fixture pattern (for action=audit_events). Default true.")
    diagnostic: Optional[Literal["claim_only_task_completed", "agent_summary", "field_verification", "events"]] = Field(None, description="Outcome evidence diagnostic mode (for action=outcome_evidence).")
    outcome_type: Optional[str] = Field(None, description="Outcome type filter (for action=outcome_evidence).")
    corroboration_grade: Optional[str] = Field(None, description="Corroboration grade filter (for action=outcome_evidence).")
    min_completions: Optional[int] = Field(None, description="Minimum task_completed rows before low-corroboration agent flagging.")
    low_weight_threshold: Optional[float] = Field(None, description="Average evidence_weight threshold for low-corroboration agent flagging.")
    include_detail: bool = Field(False, description="Include full outcome_event detail JSON in outcome_evidence events.")
    # action=telemetry delegates to handle_get_telemetry_metrics, which reads
    # these two. Until 2026-09-07 neither was declared here, so over the MCP
    # wire FastMCP dropped them before dispatch and observe(action='telemetry',
    # window_hours=48) silently used the 24h default; admin(action='telemetry')
    # declares both (AdminParams) and always honoured them.
    window_hours: Optional[float] = Field(None, description="Time window in hours for telemetry metrics (for action=telemetry; default 24).")
    include_calibration: bool = Field(False, description="Include full calibration metrics (for action=telemetry; default false, the data is system-wide and large).")

    @model_validator(mode='after')
    def default_telemetry_window(self):
        # Mirror the handler's default so omitting the parameter and passing
        # it explicitly agree (the validated dict carries every declared
        # field, None included, so the handler's own .get default never fires).
        if self.action == "telemetry" and self.window_hours is None:
            self.window_hours = 24.0
        return self

class OutcomeCorrelationParams(AgentIdentityMixin):
    """Run outcome correlation study: does EISV instability predict bad outcomes?"""
    since_hours: Union[float, str, None] = Field(
        default=168,
        description="Lookback window in hours (default: 168 = 1 week)"
    )

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.since_hours, str):
            try:
                self.since_hours = float(self.since_hours)
            except ValueError:
                self.since_hours = 168
        return self

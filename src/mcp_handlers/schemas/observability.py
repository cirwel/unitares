from typing import Optional, Union, Literal, Dict, Any, List, Sequence
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
        description="Type of anomaly to focus on"
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
    action: Literal["agent", "compare", "similar", "anomalies", "aggregate", "telemetry", "audit_events"] = Field(..., description="Operation to perform")
    target_agent_id: Optional[str] = Field(None, description="Agent to observe — UUID or label (for action=agent). Use list_agents to find.")
    agent_ids: Optional[List[Any]] = Field(None, description="Agent identifiers to compare (for action=compare, min 2)")
    include_history: bool = Field(True, description="Include recent history (for action=agent). Default true.")
    analyze_patterns: bool = Field(True, description="Perform pattern analysis (for action=agent). Default true.")
    compare_metrics: Optional[List[Any]] = Field(None, description="Metrics to compare (for action=compare). Default: risk_score, coherence, E, I, S, V")
    limit: Optional[int] = Field(None, description="Max results to return (for action=similar, anomalies, audit_events)")
    event_type: Optional[str] = Field(None, description="Audit event type to filter on (for action=audit_events)")
    event_types: Optional[List[str]] = Field(None, description="IN-list of audit event types (for action=audit_events, alternative to event_type)")
    since: Optional[str] = Field(None, description="Window start: '14d'/'24h'/'30m' shorthand or ISO 8601 (for action=audit_events). Default 24h.")
    until: Optional[str] = Field(None, description="Window end: ISO 8601 (for action=audit_events). Default now.")
    include_events: bool = Field(False, description="Include event payloads in response (for action=audit_events). Default false — counts only.")
    include_test_fixtures: bool = Field(True, description="Include agents matching Test_Agent_* fixture pattern (for action=audit_events). Default true.")

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

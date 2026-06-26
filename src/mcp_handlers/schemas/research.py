from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import Field, model_validator

from .mixins import AgentIdentityMixin


ResearchStatus = Literal["planned", "running", "completed", "aborted", "archived"]
ResearchAction = Literal["list", "query", "get", "stats", "record", "export"]
GroundingFilter = Literal["anchored", "linked", "missing"]


class ResearchRegistryParams(AgentIdentityMixin):
    """Register and query agent-network research runs."""

    action: ResearchAction = Field(
        default="list",
        description="Operation to perform",
    )
    run_id: Optional[str] = Field(
        default=None,
        description="Stable run id for get/export/update",
    )
    run: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Full run payload for action=record",
    )

    # Query filters
    query: Optional[str] = Field(default=None, description="Full-text substring query")
    status: Optional[ResearchStatus] = Field(default=None, description="Filter by run status")
    tag: Optional[str] = Field(default=None, description="Filter by exact tag")
    scenario_id: Optional[str] = Field(default=None, description="Filter by scenario.id")
    research_area: Optional[str] = Field(default=None, description="Filter by research area")
    grounding: Optional[GroundingFilter] = Field(default=None, description="Filter by grounding status")
    limit: Union[int, str, None] = Field(default=50, description="Max rows to return")
    include_details: Union[bool, str, None] = Field(
        default=False,
        description="Return full records instead of summaries for list/query",
    )

    # Direct record fields (alternative to nested run={...})
    title: Optional[str] = None
    scenario: Optional[Dict[str, Any]] = None
    topology: Optional[Dict[str, Any]] = None
    population: Optional[List[Any]] = None
    tools: Optional[List[Any]] = None
    memory: Optional[List[Any]] = None
    communication_channels: Optional[List[Any]] = None
    interventions: Optional[List[Any]] = None
    metrics: Optional[List[Any]] = None
    observations: Optional[List[Any]] = None
    outcomes: Optional[List[Any]] = None
    artifacts: Optional[List[Any]] = None
    linked_knowledge_ids: Optional[List[str]] = None
    linked_outcome_ids: Optional[List[str]] = None
    linked_finding_ids: Optional[List[str]] = None
    research_areas: Optional[List[str]] = None
    tags: Optional[List[str]] = None
    exogenous_anchor: Optional[Dict[str, Any]] = None
    hypothesis: Optional[str] = None
    operator_question: Optional[str] = None
    notes: Optional[str] = None

    @model_validator(mode="after")
    def coerce_flags(self):
        if isinstance(self.limit, str):
            try:
                self.limit = int(self.limit)
            except ValueError:
                self.limit = 50
        if isinstance(self.include_details, str):
            self.include_details = self.include_details.lower() in ("true", "1", "yes")
        return self

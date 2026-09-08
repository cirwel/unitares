from typing import ClassVar, List, Literal, Mapping, Optional, Tuple, Union
from pydantic import Field, model_validator
from .mixins import AgentIdentityMixin

class RequestDialecticReviewParams(AgentIdentityMixin):
    """
    Create a dialectic recovery session
    """
    issue_description: str = Field(
        ..., description="Description of the issue or current state"
    )

class GetDialecticSessionParams(AgentIdentityMixin):
    """
    View a dialectic session
    """
    session_id: Optional[str] = Field(
        default=None,
        description="ID of specific session to retrieve"
    )
    check_timeout: Union[bool, str, None] = Field(
        default=False,
        description="Check reviewer/session timeouts and auto-facilitation state"
    )

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.check_timeout, str):
            self.check_timeout = self.check_timeout.lower() in ('true', '1', 'yes')
        return self

class ListDialecticSessionsParams(AgentIdentityMixin):
    """
    List all dialectic sessions with optional filtering
    """
    agent_id: Optional[str] = Field(
        default=None,
        description="Filter by agent UUID/label (paused or reviewer)"
    )
    status: Optional[str] = Field(
        default=None,
        description="Filter by status (active, converged, failed, canceled)"
    )
    limit: Union[int, str, None] = Field(
        default=10,
        description="Max results"
    )
    include_transcript: Union[bool, str, None] = Field(
        default=False,
        description="Include full transcript"
    )

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.limit, str):
            try:
                self.limit = int(self.limit)
            except ValueError:
                self.limit = 10
        if isinstance(self.include_transcript, str):
            self.include_transcript = self.include_transcript.lower() in ('true', '1', 'yes')
        return self

class SubmitThesisParams(AgentIdentityMixin):
    """
    Paused agent submits thesis
    """
    session_id: str = Field(..., description="Dialectic session ID")
    root_cause: str = Field(..., description="Agent's understanding of root cause")
    proposed_conditions: List[str] = Field(..., description="List of conditions for resumption")
    reasoning: Optional[str] = Field(default=None, description="Natural language explanation")

class SubmitAntithesisParams(AgentIdentityMixin):
    """
    Reviewer agent submits antithesis
    """
    session_id: str = Field(..., description="Dialectic session ID")
    observed_metrics: dict = Field(..., description="Metrics observed about paused agent")
    concerns: List[str] = Field(..., description="List of concerns")
    reasoning: Optional[str] = Field(default=None, description="Natural language explanation")
    reviewer_provenance: Optional[dict] = Field(
        default=None,
        description=(
            "Optional reviewer/model provenance persisted with this verdict: "
            "reviewer_kind (agent_submitted|external_consult|orchestrated), "
            "backend, model_used, consult_source, degraded, etc. Use "
            "reviewer_kind='external_consult' to file a verdict obtained from a "
            "model consulted outside the server. Descriptive, not identity proof."
        ),
    )
    take_over_if_requested: Union[bool, str, None] = Field(
        default=False,
        description="If true, let a credentialed operator move reviewer ownership to the bound candidate before submitting"
    )
    takeover_reason: Optional[str] = Field(
        default=None,
        description="Why reviewer ownership is being taken over for this antithesis submission"
    )

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.take_over_if_requested, str):
            self.take_over_if_requested = self.take_over_if_requested.lower() in ('true', '1', 'yes')
        return self

class SubmitSynthesisParams(AgentIdentityMixin):
    """
    Either agent submits synthesis proposal
    """
    session_id: str = Field(..., description="Dialectic session ID")
    proposed_conditions: List[str] = Field(..., description="Proposed resumption conditions")
    reasoning: Optional[str] = Field(default=None, description="Natural language explanation")
    agrees: Union[bool, str, None] = Field(default=None, description="Whether this agent agrees with current proposal")
    reviewer_provenance: Optional[dict] = Field(
        default=None,
        description=(
            "Optional reviewer/model provenance persisted with this verdict "
            "(see antithesis); reviewer_kind='external_consult' files an "
            "outside-model consult as a governed record."
        ),
    )
    
    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.agrees, str):
            self.agrees = self.agrees.lower() in ('true', '1', 'yes')
        return self

class LlmAssistedDialecticParams(AgentIdentityMixin):
    """
    Run LLM-assisted dialectic recovery
    """
    root_cause: str = Field(..., description="Your understanding of what caused the issue")
    proposed_conditions: List[str] = Field(..., description="Your proposed conditions for resumption")
    reasoning: Optional[str] = Field(default=None, description="Your explanation/reasoning")

class DialecticParams(AgentIdentityMixin):
    """Parameters for dialectic"""
    # Which of these flat parameters each action uses. The wire schema stays
    # flat (the MCP wrapper builds its argument model from top-level
    # properties), so this is the only machine-readable statement of the
    # per-action contract; describe_tool(action=...) serves it and
    # tests/test_router_action_fields.py holds it to the routing table.
    # Identity and session parameters are common to every action and are
    # not repeated here (schemas/router_actions.COMMON_ROUTER_FIELDS).
    ACTION_FIELDS: ClassVar[Mapping[str, Tuple[str, ...]]] = {
        "get": (
                "session_id", "check_timeout",
        ),
        "list": (
                "status", "limit", "include_transcript",
        ),
        "quick": (
                "position", "decision", "reasoning",
        ),
        "request": (
                "issue_description", "reason", "use_brief_as_thesis",
        ),
        "thesis": (
                "session_id", "position", "reasoning", "root_cause",
                "proposed_conditions", "conditions",
        ),
        "antithesis": (
                "session_id", "concerns", "reasoning", "observed_metrics",
                "reviewer_provenance", "take_over_if_requested",
                "takeover_reason",
        ),
        "synthesis": (
                "session_id", "agrees", "reasoning", "proposed_conditions",
                "conditions",
        ),
        "reassign": (
                "session_id", "new_reviewer_id",
        ),
    }
    # default mirrors action_router's default_action="list" — the schema
    # validated BEFORE the router and a required field here made
    # dialectic({}) error despite the router's fallback (PR #611 council
    # live battery, probe 3g).
    action: Literal["get", "list", "quick", "request", "thesis", "antithesis", "synthesis", "reassign"] = Field("list", description="Operation: get, list, quick, request, thesis, antithesis, synthesis, reassign")
    session_id: Optional[str] = Field(None, description="Dialectic session ID")
    agent_id: Optional[str] = Field(None, description="Filter by agent (for action=get or list)")
    status: Optional[str] = Field(None, description="Filter by phase (for action=list)")
    limit: Optional[int] = Field(None, description="Max sessions to return (for action=list, default 50, min 1, values above 200 are capped)")
    include_transcript: Optional[bool] = Field(None, description="Include full transcript (for action=list, default false)")
    check_timeout: Optional[bool] = Field(None, description="Check reviewer/session timeouts for action=get")
    # Write action fields
    issue_description: Optional[str] = Field(None, description="Issue description (for action=request)")
    position: Optional[str] = Field(None, description="Current position or proposed decision (for action=quick)")
    decision: Optional[Literal["proceed", "defer", "escalate", "block", "unknown"]] = Field(None, description="Decision label (for action=quick)")
    root_cause: Optional[str] = Field(None, description="Root cause analysis (for action=thesis/synthesis)")
    proposed_conditions: Optional[List[str]] = Field(None, description="Conditions for resumption (for action=thesis/synthesis)")
    reasoning: Optional[str] = Field(None, description="Explanation/reasoning")
    use_brief_as_thesis: Optional[bool] = Field(
        None,
        description=(
            "For action=request or thesis, reuse the issue description or saved "
            "session brief as the thesis instead of repeating it. The friendly "
            "request_review alias defaults this to true when no explicit reasoning "
            "or root_cause is supplied; raw dialectic calls default to false."
        ),
    )
    observed_metrics: Optional[dict] = Field(None, description="Observed metrics (for action=antithesis)")
    reviewer_provenance: Optional[dict] = Field(
        None,
        description="Reviewer/model provenance for the verdict (for action=antithesis/synthesis); reviewer_kind='external_consult' files an outside-model consult as a governed record",
        json_schema_extra={
            "brief": (
                "Reviewer/model provenance (action=antithesis/synthesis); "
                "reviewer_kind='external_consult' files an outside model."
            )
        },
    )
    concerns: Optional[List[str]] = Field(None, description="Concerns (for action=antithesis)")
    take_over_if_requested: Optional[bool] = Field(None, description="Let a credentialed operator move reviewer ownership to the bound agent before antithesis")
    takeover_reason: Optional[str] = Field(None, description="Reason for reviewer takeover during antithesis")
    agrees: Union[bool, str, None] = Field(None, description="Agreement flag (for action=synthesis)")
    # `vote` was removed 2026-09-08: it documented "for action=vote" against a
    # router with no `vote` action, and no handler ever read it. `conditions`
    # is live — it is the accepted alias for `proposed_conditions`
    # (dialectic/handlers._read_proposed_conditions), so it carries that
    # parameter's actions and says so.
    conditions: Optional[List[str]] = Field(None, description="Resumption conditions; alias for proposed_conditions (for action=thesis/synthesis)")
    new_reviewer_id: Optional[str] = Field(None, description="New reviewer agent ID (for action=reassign)")
    reason: Optional[str] = Field(None, description="Reason (for action=request/reassign)")

class ReassignReviewerParams(AgentIdentityMixin):
    """Operator/current-reviewer handoff for an active dialectic session."""
    session_id: str = Field(..., description="Dialectic session ID")
    new_reviewer_id: Optional[str] = Field(
        default=None,
        description="Agent ID to assign as new reviewer (operator/current reviewer authorization required; auto-selected if omitted)"
    )
    reason: Optional[str] = Field(
        default="Reviewer unresponsive",
        description="Reason for reassignment"
    )

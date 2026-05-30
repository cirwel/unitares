from typing import Optional, Union, Literal, Dict, Any, List, Sequence
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
    take_over_if_requested: Union[bool, str, None] = Field(
        default=False,
        description="If true, let the bound reviewer candidate take over reviewer ownership before submitting"
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
    action: Literal["get", "list", "quick", "request", "thesis", "antithesis", "synthesis", "reassign"] = Field(..., description="Operation: get, list, quick, request, thesis, antithesis, synthesis, reassign")
    session_id: Optional[str] = Field(None, description="Dialectic session ID")
    agent_id: Optional[str] = Field(None, description="Filter by agent (for action=get or list)")
    status: Optional[str] = Field(None, description="Filter by phase (for action=list)")
    limit: Optional[int] = Field(None, description="Max sessions to return (for action=list, default 50)")
    include_transcript: Optional[bool] = Field(None, description="Include full transcript (for action=list, default false)")
    check_timeout: Optional[bool] = Field(None, description="Check reviewer/session timeouts for action=get")
    # Write action fields
    issue_description: Optional[str] = Field(None, description="Issue description (for action=request)")
    position: Optional[str] = Field(None, description="Current position or proposed decision (for action=quick)")
    decision: Optional[Literal["proceed", "defer", "escalate", "block", "unknown"]] = Field(None, description="Decision label (for action=quick)")
    root_cause: Optional[str] = Field(None, description="Root cause analysis (for action=thesis/synthesis)")
    proposed_conditions: Optional[List[str]] = Field(None, description="Conditions for resumption (for action=thesis/synthesis)")
    reasoning: Optional[str] = Field(None, description="Explanation/reasoning")
    observed_metrics: Optional[dict] = Field(None, description="Observed metrics (for action=antithesis)")
    concerns: Optional[List[str]] = Field(None, description="Concerns (for action=antithesis)")
    take_over_if_requested: Optional[bool] = Field(None, description="Let the current bound agent take reviewer ownership before antithesis")
    takeover_reason: Optional[str] = Field(None, description="Reason for reviewer takeover during antithesis")
    agrees: Union[bool, str, None] = Field(None, description="Agreement flag (for action=synthesis)")
    vote: Optional[str] = Field(None, description="Vote: resume, block, or cooldown (for action=vote)")
    conditions: Optional[List[str]] = Field(None, description="Conditions (for action=vote)")
    new_reviewer_id: Optional[str] = Field(None, description="New reviewer agent ID (for action=reassign)")
    reason: Optional[str] = Field(None, description="Reason (for action=request/reassign)")

class ReassignReviewerParams(AgentIdentityMixin):
    """Reassign the reviewer for an active dialectic session."""
    session_id: str = Field(..., description="Dialectic session ID")
    new_reviewer_id: Optional[str] = Field(
        default=None,
        description="Agent ID to assign as new reviewer (auto-selected if omitted)"
    )
    reason: Optional[str] = Field(
        default="Reviewer unresponsive",
        description="Reason for reassignment"
    )

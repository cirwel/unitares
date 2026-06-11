from typing import Optional, Union, Literal, Any, List
from pydantic import Field, model_validator
from .mixins import AgentIdentityMixin


class ListAgentsParams(AgentIdentityMixin):
    """
    List all agents currently being monitored with lifecycle metadata and health status
    """
    lite: Union[bool, str, None] = Field(
        default=True,
        description="LITE MODE: Use lite=true for minimal response (~1KB vs ~15KB)."
    )

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.lite, str):
            self.lite = self.lite.lower() in ('true', '1', 'yes')
        return self


class GetAgentMetadataParams(AgentIdentityMixin):
    """
    Get complete metadata for an agent including lifecycle events, current state, and computed fields.
    """
    target_agent: Optional[str] = Field(
        default=None,
        description="Optional UUID or label of agent to look up."
    )


class UpdateAgentMetadataParams(AgentIdentityMixin):
    """
    Update agent tags and notes.
    """
    action: Literal["add_tag", "remove_tag", "set_purpose", "add_note"] = Field(
        ..., description="The update action to perform."
    )
    tag: Optional[str] = Field(
        default=None,
        description="Tag name (for add_tag/remove_tag actions)."
    )
    purpose: Optional[str] = Field(
        default=None,
        description="Purpose string (for set_purpose action)."
    )
    note: Optional[str] = Field(
        default=None,
        description="Note string (for add_note action)."
    )


class ArchiveAgentParams(AgentIdentityMixin):
    """
    Archive an agent for long-term storage.
    """
    target_agent: str = Field(
        ..., description="UUID or label of agent to archive."
    )


class ResumeAgentParams(AgentIdentityMixin):
    """
    Resume a paused/stuck agent from the dashboard.
    """
    target_agent: str = Field(
        ..., description="UUID or label of agent to resume."
    )


class DeleteAgentParams(AgentIdentityMixin):
    """
    Delete agent and archive data.
    """
    target_agent: str = Field(
        ..., description="UUID or label of agent to delete."
    )
    confirm: bool = Field(
        ..., description="Must be true to confirm deletion."
    )


class ArchiveOldTestAgentsParams(AgentIdentityMixin):
    """
    Archive stale agents.
    """
    include_all: Union[bool, str, None] = Field(
        default=False,
        description="If true, archives all inactive agents regardless of naming pattern."
    )
    max_age_days: Union[int, str, None] = Field(
        default=3,
        description="Agents inactive for this many days will be archived (default: 3)."
    )

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.include_all, str):
            self.include_all = self.include_all.lower() in ('true', '1', 'yes')
        if isinstance(self.max_age_days, str):
            try:
                self.max_age_days = int(self.max_age_days)
            except ValueError:
                self.max_age_days = 3
        return self


class ArchiveOrphanAgentsParams(AgentIdentityMixin):
    """
    Aggressively archive orphan agents to prevent proliferation.
    """
    dry_run: Union[bool, str, None] = Field(
        default=True,
        description="If true (default), returns what WOULD be archived without taking action."
    )
    max_age_hours: Union[int, str, None] = Field(
        default=6,
        description="Agents inactive for this many hours will be evaluated (default: 6)."
    )
    max_updates: Union[int, str, None] = Field(
        default=3,
        description="Agents with this many updates or fewer will be evaluated (default: 3)."
    )

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.dry_run, str):
            self.dry_run = self.dry_run.lower() in ('true', '1', 'yes')
        
        if isinstance(self.max_age_hours, str):
            try:
                self.max_age_hours = int(self.max_age_hours)
            except ValueError:
                self.max_age_hours = 6

        if isinstance(self.max_updates, str):
            try:
                self.max_updates = int(self.max_updates)
            except ValueError:
                self.max_updates = 3
        return self


class MarkResponseCompleteParams(AgentIdentityMixin):
    """
    Mark agent as having completed response, waiting for input.
    """
    pass


class SelfRecoveryReviewParams(AgentIdentityMixin):
    """
    Self-reflection recovery - lightweight alternative to dialectic.
    """
    reflection: str = Field(
        ..., description="Agent's reflection on what went wrong and how to fix it."
    )
    proposed_conditions: Optional[List[str]] = Field(
        default_factory=list,
        description="Conditions for resuming (e.g., 'reduce complexity', 'take breaks')."
    )
    root_cause: Optional[str] = Field(
        default=None,
        description="Agent's understanding of root cause."
    )


class DetectStuckAgentsParams(AgentIdentityMixin):
    """
    Detect agents that are stuck.
    """
    timeout_minutes: Union[int, float, str, None] = Field(
        default=15,
        description="Consider agents stuck if inactive for this many minutes (default: 15)."
    )
    auto_resolve: Union[bool, str, None] = Field(
        default=False,
        description="If true, attempt to auto-resolve stuck agents (e.g., move to waiting_input)."
    )
    
    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.timeout_minutes, str):
            try:
                self.timeout_minutes = float(self.timeout_minutes)
            except ValueError:
                self.timeout_minutes = 15.0
        if isinstance(self.auto_resolve, str):
            self.auto_resolve = self.auto_resolve.lower() in ('true', '1', 'yes')
        return self


class PingAgentParams(AgentIdentityMixin):
    """
    Ping an agent to check if it's responsive/alive.
    """
    target_agent: str = Field(
        ..., description="UUID or label of agent to ping."
    )

class AgentParams(AgentIdentityMixin):
    """Parameters for agent"""
    action: Literal["list", "get", "update", "archive", "resume", "delete"] = Field(..., description="Operation to perform (alias: op)")
    op: Optional[Literal["list", "get", "update", "archive", "resume", "delete"]] = Field(None, description="Alias for action. Use action or op.")
    agent_id: Optional[str] = Field(None, description="Target agent ID (for get, update, archive, delete)")
    tags: Optional[List[Any]] = Field(None, description="Tags to set (for action=update)")
    notes: Optional[str] = Field(None, description="Notes to set (for action=update)")
    confirm: Optional[bool] = Field(None, description="Confirm deletion (for action=delete)")


class SelfRecoveryParams(AgentIdentityMixin):
    """Parameters for self_recovery"""
    action: Literal["check", "quick", "review"] = Field("check", description="Recovery action: check (diagnose), quick (fast resume), review (with reflection)")
    reflection: Optional[str] = Field(None, description="What went wrong and what you'll change (required for action=review)")
    conditions: Optional[List[Any]] = Field(None, description="Recovery conditions (optional for action=review)")
    reason: Optional[str] = Field(None, description="Brief reason (optional for action=quick)")


class OperatorResumeAgentParams(AgentIdentityMixin):
    """Parameters for operator_resume_agent"""
    target_agent_id: str = Field(..., description="UUID of the agent to resume (target, not caller)")
    reason: str = Field(..., description="Operator's reason for override")


class DirectResumeIfSafeParams(AgentIdentityMixin):
    """Parameters for direct_resume_if_safe"""



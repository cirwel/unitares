from typing import Optional, Union, Literal, Any, List
from pydantic import Field, model_validator
from .mixins import AgentIdentityMixin


def _coerce_optional_bool(value):
    if isinstance(value, str):
        return value.lower() in ('true', '1', 'yes')
    return value


def _coerce_optional_int(value):
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    return value


class ListAgentOptionsMixin:
    lite: Union[bool, str, None] = Field(
        default=True,
        description="LITE MODE: Use lite=true for minimal response (~1KB vs ~15KB)."
    )
    grouped: Union[bool, str] = Field(
        default=True,
        description="Group returned agents by lifecycle status in full mode."
    )
    include_metrics: Union[bool, str] = Field(
        default=True,
        description="Include governance metrics and health status in full mode."
    )
    loaded_only: Union[bool, str] = Field(
        default=False,
        description="Only include agents whose monitors are already loaded in memory."
    )
    summary_only: Union[bool, str] = Field(
        default=False,
        description="Return only summary counts instead of agent rows."
    )
    standardized: Union[bool, str] = Field(
        default=True,
        description="Include standardized response fields."
    )
    include_test_agents: Union[bool, str] = Field(
        default=False,
        description="Include test/pytest agents; filtered out by default."
    )
    named_only: Union[bool, str, None] = Field(
        default=None,
        description="If true, include only labeled agents. If omitted, compact mode hides ghosts."
    )
    status_filter: str = Field(
        default="active",
        description="Lifecycle status filter: active, waiting_input, paused, archived, deleted, unknown, or all."
    )
    min_updates: Union[int, str] = Field(
        default=0,
        description="Minimum total update/check-in count."
    )
    recent_days: Optional[Union[int, str]] = Field(
        default=None,
        description="Only include agents active in the last N days. Use 0 for all."
    )
    limit: Optional[Union[int, str]] = Field(
        default=None,
        description="Maximum number of agents to return."
    )
    offset: Union[int, str] = Field(
        default=0,
        description="Pagination offset for full-mode responses."
    )

    @model_validator(mode='after')
    def coerce_list_options(self):
        provided_fields = set(getattr(self, 'model_fields_set', set()))
        for field in (
            'lite',
            'grouped',
            'include_metrics',
            'loaded_only',
            'summary_only',
            'standardized',
            'include_test_agents',
            'named_only',
        ):
            setattr(self, field, _coerce_optional_bool(getattr(self, field)))
        for field in ('min_updates', 'recent_days', 'limit', 'offset'):
            setattr(self, field, _coerce_optional_int(getattr(self, field)))
        if 'lite' not in provided_fields:
            advanced_requested = (
                ('include_metrics' in provided_fields and self.include_metrics is True)
                or ('limit' in provided_fields and self.limit is not None)
                or ('offset' in provided_fields and self.offset is not None)
                or (
                    'status_filter' in provided_fields
                    and self.status_filter not in (None, 'active')
                )
                or (
                    'include_test_agents' in provided_fields
                    and self.include_test_agents is True
                )
                or ('summary_only' in provided_fields and self.summary_only is True)
                or ('grouped' in provided_fields and self.grouped is False)
            )
            if advanced_requested:
                self.lite = False
        return self


class ListAgentsParams(ListAgentOptionsMixin, AgentIdentityMixin):
    """
    List all agents currently being monitored with lifecycle metadata and health status
    """
    pass


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
    force: Union[bool, str, None] = Field(
        default=False,
        description="Archive even if the agent looks live (running process, recent activity, or declared causal lineage). Default false refuses such archives."
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
    dry_run: Union[bool, str, None] = Field(
        default=True,
        description="If true (default), returns what WOULD be archived without taking action. Pass false to execute."
    )

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.include_all, str):
            self.include_all = self.include_all.lower() in ('true', '1', 'yes')
        if isinstance(self.dry_run, str):
            self.dry_run = self.dry_run.lower() in ('true', '1', 'yes')
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

class AgentParams(ListAgentOptionsMixin, AgentIdentityMixin):
    """Parameters for agent"""
    action: Literal["list", "get", "update", "archive", "resume", "delete"] = Field(..., description="Operation to perform (alias: op)")
    op: Optional[Literal["list", "get", "update", "archive", "resume", "delete"]] = Field(None, description="Alias for action. Use action or op.")
    agent_id: Optional[str] = Field(None, description="Target agent ID (for get, update, archive, delete)")
    tags: Optional[List[Any]] = Field(None, description="Tags to set (for action=update)")
    notes: Optional[str] = Field(None, description="Notes to set (for action=update)")
    confirm: Optional[bool] = Field(None, description="Confirm deletion (for action=delete)")
    force: Optional[Union[bool, str]] = Field(None, description="For action=archive: archive even if the agent looks live (running process, recent activity, or declared causal lineage).")


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

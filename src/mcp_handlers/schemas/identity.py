from typing import Optional, Union
from pydantic import Field, model_validator
from .mixins import AgentIdentityMixin
from .core import BootstrapStateParams

class IdentityParams(AgentIdentityMixin):
    """
    Who am I? Auto-creates identity if first call.
    """
    agent_uuid: Optional[str] = Field(
        default=None,
        description="Resume a known identity by UUID directly. Skips session/name resolution. Returns error if not found."
    )
    name: Optional[str] = Field(
        default=None,
        description=(
            "Optional COSMETIC display name. Sets `display_name` only — it does "
            "NOT change your public structured handle (`agent_id`) or the registry "
            "key (`uuid`). For cross-tool threading, key on `uuid` (the identity "
            "key), not on this name."
        )
    )
    model_type: Optional[str] = Field(
        default=None,
        description="Optional model type for distinct identity"
    )
    resume: Union[bool, str, None] = Field(
        default=False,
        description="Explicitly resume existing identity"
    )
    force_new: Union[bool, str, None] = Field(
        default=False,
        description="Force new identity creation"
    )

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.resume, str):
            self.resume = self.resume.lower() in ('true', '1', 'yes')
        if isinstance(self.force_new, str):
            self.force_new = self.force_new.lower() in ('true', '1', 'yes')
        return self


class OnboardParams(AgentIdentityMixin):
    """Single entry point for new agents.

    The schema exposes many fields, but the "Strict Identity, Simple Contract"
    normal path uses only a few — the rest are adapter/advanced-only. So the
    surface a reader sees matches the contract the docs describe:

    - Normal path: `force_new` (mint fresh — the default posture for a new
      process), and on later calls `client_session_id` (the write binding,
      threaded by adapters). For a real handoff from a finished predecessor:
      `parent_agent_id` + `spawn_reason`.
    - Advanced / adapter-only: `name` (cosmetic), `model_type`, `client_hint`,
      `resume`, `orchestrated`, `thread_id`, `trajectory_signature`,
      `process_fingerprint`, `initial_state`, `response_mode`. Ordinary
      interactive callers should not need these.

    See docs/ontology/identity.md ("Strict Identity, Simple Contract" + the
    identifier reference table) for which identifier to pass when.
    """
    name: Optional[str] = Field(
        default=None,
        description=(
            "Optional COSMETIC display name. Sets `display_name` only — it does "
            "NOT set your public structured handle (`agent_id`); the cosmetic name "
            "and the public handle deliberately diverge. The canonical identifier "
            "for cross-tool reference is `uuid` (is_identity_key:true in the "
            "response). Thread that, not the display name."
        )
    )
    model_type: Optional[str] = Field(
        default=None,
        description="Optional model type"
    )
    client_hint: Optional[str] = Field(
        default=None,
        description="Client hint string"
    )
    orchestrated: Union[bool, str, None] = Field(
        default=False,
        description=(
            "Declare that a client_session_id is a thread-stable anchor "
            "provisioned by an orchestrator for a headless turn-child. Used "
            "only to allow first-bind creation under STRICT_IDENTITY_REQUIRED; "
            "ordinary interactive callers should leave this false."
        )
    )
    resume: Union[bool, str, None] = Field(
        default=True,
        description=(
            "Resume existing identity when a proof signal is present "
            "(continuity_token, agent_uuid, agent_id, client_session_id, "
            "or name). Per identity.md v2 ontology (S13), an arg-less "
            "onboard() with no proof signal mints fresh — the server "
            "gates `force_new=True` automatically when nothing is presented."
        )
    )
    force_new: Union[bool, str, None] = Field(
        default=False,
        description=(
            "Force new identity creation. Per identity.md v2 ontology "
            "(force_new=true is the default posture for fresh process-"
            "instances), declare lineage via parent_agent_id rather than "
            "resume via token when continuity matters across process boundaries."
        )
    )
    trajectory_signature: Optional[dict] = Field(
        default=None,
        description="Trajectory signature dict"
    )
    # Thread identity (honest forking)
    parent_agent_id: Optional[str] = Field(
        default=None,
        description="UUID of predecessor agent (for fork lineage)"
    )
    spawn_reason: Optional[str] = Field(
        default=None,
        description="Why this fork was created: compaction, subagent, new_session, explicit"
    )
    thread_id: Optional[str] = Field(
        default=None,
        description="Explicit thread ID to join (auto-derived from session if not provided)"
    )
    # Concurrent identity binding invariant (issue #123).
    # Client-reported execution context — used to detect same-UUID siphoning
    # when two processes on the same host claim the same UUID. Audit-only in
    # v1; see issue #123 for the detection rule and policy flags.
    process_fingerprint: Optional[dict] = Field(
        default=None,
        description=(
            "Optional client-reported execution context: "
            "{host_id, pid, pid_start_time, transport, ppid?, tty?, "
            "anchor_path_hash?}. Recorded server-side; used to detect "
            "concurrent identity bindings. Declaration-only — never used "
            "to resolve or recover identity."
        )
    )
    initial_state: Optional[BootstrapStateParams] = Field(
        default=None,
        description=(
            "Optional bootstrap check-in payload. When present, the server "
            "writes a synthetic state row tagged source='bootstrap' "
            "immediately after identity creation. Bootstrap rows seed "
            "trajectory genesis only and are excluded by default from "
            "calibration, outcome correlation, trust-tier observation "
            "counts, and real-check-in counts."
        ),
    )
    response_mode: Optional[str] = Field(
        default="minimal",
        description=(
            "Verbosity of the identity envelope. 'minimal' (default) returns a "
            "lean payload — uuid, agent_id, session id, a single "
            "identity_assurance block, the resolution verdict, lineage flags, "
            "and a next_step hint. 'full' returns the complete identity "
            "ontology (identity_context + nested registry/label/harness blocks "
            "and the descriptive session_resolution_source / "
            "continuity_token_supported / date_context fields)."
        ),
    )

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.resume, str):
            self.resume = self.resume.lower() in ('true', '1', 'yes')
        if isinstance(self.force_new, str):
            self.force_new = self.force_new.lower() in ('true', '1', 'yes')
        if isinstance(self.orchestrated, str):
            self.orchestrated = self.orchestrated.lower() in ('true', '1', 'yes', 'on')
        return self

class LinkIdentityTrajectoryParams(AgentIdentityMixin):
    """
    Link multiple identities via behavioral trajectory
    """
    target_uuid: str = Field(
        ..., description="UUID of identity to link to"
    )
    behavioral_signature: dict = Field(
        ..., description="Behavioral signature dict for verification"
    )

class GetAgentApiKeyParams(AgentIdentityMixin):
    """
    Alias/stub for identity()
    """
    pass

class SyncMemoryContextParams(AgentIdentityMixin):
    """
    Sync memory context
    """
    memory_summary: dict = Field(..., description="Summary of current memory bindings")

class GetTrajectoryStatusParams(AgentIdentityMixin):
    """Parameters for get_trajectory_status"""


class VerifyTrajectoryIdentityParams(AgentIdentityMixin):
    """Parameters for verify_trajectory_identity"""


class BindSessionParams(AgentIdentityMixin):
    """Bind current MCP session to an existing agent identity via client_session_id."""
    resume: Union[bool, str, None] = Field(
        default=False,
        description="Must be true to explicitly reattach to a prior identity (unless strict mode is used)."
    )
    strict: Union[bool, str, None] = Field(
        default=False,
        description="If true, require explicit agent_id and reject mismatched binding."
    )

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.resume, str):
            self.resume = self.resume.lower() in ('true', '1', 'yes')
        if isinstance(self.strict, str):
            self.strict = self.strict.lower() in ('true', '1', 'yes')
        return self

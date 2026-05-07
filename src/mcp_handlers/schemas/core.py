from datetime import datetime
from typing import Optional, Union, Literal, Dict, Any, List, Sequence
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .mixins import AgentIdentityMixin


# Single source of truth for the task_type Literal — used by
# ProcessAgentUpdateParams and BootstrapStateParams. If the canonical set
# changes, edit it here.
TaskType = Literal[
    "convergent", "divergent", "mixed", "refactoring", "bugfix", "testing",
    "documentation", "feature", "exploration", "research", "design", "debugging",
    "review", "deployment", "introspection"
]

ToolResultKind = Literal["command", "test", "lint", "build", "file_op", "tool_call"]


def _infer_tool_result_kind(tool: Any, summary: Any = "") -> str:
    text = f"{tool or ''} {summary or ''}".strip().lower()
    tool_text = str(tool or "").lower()

    test_markers = (
        "pytest", "unittest", "jest", "vitest", "go test", "cargo test",
        "npm test", "test-cache", "tests",
    )
    lint_markers = ("ruff", "flake8", "eslint", "mypy", "pylint", "lint")
    build_markers = ("build", "make", "cargo build", "npm run build")
    file_markers = ("apply_patch", "patch", "write_file", "edit_file")

    if any(marker in text for marker in test_markers) or tool_text == "test":
        return "test"
    if any(marker in text for marker in lint_markers):
        return "lint"
    if any(marker in text for marker in build_markers):
        return "build"
    if any(marker in text for marker in file_markers):
        return "file_op"
    if tool_text:
        return "command"
    return "tool_call"


class BootstrapStateParams(BaseModel):
    """Subset of process_agent_update fields accepted as a bootstrap check-in
    via onboard.initial_state. All fields optional; the server fills defaults
    when absent. Extras are rejected (model_config below) so this isn't a
    back-door for setting arbitrary internal state."""
    model_config = ConfigDict(extra="forbid")

    response_text: Optional[str] = Field(default=None)
    complexity: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    task_type: Optional[TaskType] = Field(default=None)
    ethical_drift: Optional[List[float]] = Field(
        default=None, min_length=3, max_length=3
    )


class GetGovernanceMetricsParams(AgentIdentityMixin):
    """
    Get current governance state and metrics for an agent without updating state.
    """
    include_state: Union[bool, str, None] = Field(
        default=False,
        description="Include nested state dict in response (can be large). Default false to reduce context bloat. Accepts boolean or string ('true'/'false')."
    )
    lite: Union[bool, str, None] = Field(
        default=True,
        description="If true (default), returns minimal essential metrics only. Set lite=false for full diagnostic data."
    )

    @model_validator(mode='after')
    def coerce_booleans(self):
        if isinstance(self.include_state, str):
            self.include_state = self.include_state.lower() in ('true', '1', 'yes')
        if isinstance(self.lite, str):
            self.lite = self.lite.lower() in ('true', '1', 'yes')
        return self


class SimulateUpdateParams(AgentIdentityMixin):
    """
    Dry-run governance cycle without persisting state.
    """
    parameters: List[float] = Field(
        default_factory=list,
        description="Agent parameters vector (optional)."
    )
    ethical_drift: List[float] = Field(
        default_factory=lambda: [0.0, 0.0, 0.0],
        description="Ethical drift signals (3 components)."
    )
    response_text: Optional[str] = Field(
        default="",
        description="Agent's response text (optional)."
    )
    complexity: Union[float, str, None] = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Estimated task complexity (0-1)."
    )
    confidence: Union[float, str, None] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Confidence level for this update (0-1)."
    )
    lite: Union[bool, str, None] = Field(
        default=False,
        description="If true, returns minimalist output."
    )

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.complexity, str):
            try:
                self.complexity = float(self.complexity)
            except ValueError:
                self.complexity = 0.5
        if isinstance(self.confidence, str):
            try:
                self.confidence = float(self.confidence)
            except ValueError:
                self.confidence = None
        if isinstance(self.lite, str):
            self.lite = self.lite.lower() in ('true', '1', 'yes')
        return self


class ToolResultEvidence(BaseModel):
    """Self-reported tool outcome evidence from a recent agent action.

    Self-report — the server treats this as
    `verification_source="agent_reported_tool_result"`. A future server-verified
    primitive will provide `server_observation` outcomes for the subset of
    work the server can independently verify. See spec §1.
    """
    model_config = ConfigDict(extra="forbid")

    kind: ToolResultKind = Field(
        default="tool_call",
        description=(
            "Tool-result category. If omitted, the server infers it from "
            "tool/summary and falls back to command or tool_call."
        ),
    )
    tool: str = Field(..., max_length=64)
    summary: str = Field(..., max_length=512)
    exit_code: Optional[int] = None
    is_bad: Optional[bool] = None
    prediction_id: Optional[str] = None
    observed_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def fill_omitted_kind(cls, data):
        if not isinstance(data, dict):
            return data
        if data.get("kind") in (None, ""):
            data = dict(data)
            data["kind"] = _infer_tool_result_kind(
                data.get("tool"),
                data.get("summary", ""),
            )
        return data


class ProcessAgentUpdateParams(AgentIdentityMixin):
    """
    Share your work and get supportive feedback. Your main tool for checking in.
    """
    parameters: List[float] = Field(
        default_factory=list,
        description="Agent parameters vector (optional, deprecated)."
    )
    ethical_drift: List[float] = Field(
        default_factory=lambda: [0.0, 0.0, 0.0],
        description="Ethical drift signals (3 components): [primary_drift, coherence_loss, complexity_contribution]"
    )
    response_text: Optional[str] = Field(
        default=None,
        description="Agent's response text (optional, for analysis)"
    )
    complexity: Union[float, str, None] = Field(
        default=0.5,
        ge=0.0, 
        le=1.0,
        description="Estimated task complexity (0-1, optional)."
    )
    confidence: Union[float, str, None] = Field(
        default=None,
        ge=0.0, 
        le=1.0,
        description="Confidence level for this update (0-1, optional)."
    )
    response_mode: Literal["minimal", "compact", "standard", "full", "mirror", "auto"] = Field(
        default="auto",
        description="Response verbosity mode. 'mirror' returns actionable self-awareness signals instead of raw EISV. 'compact' or 'minimal' returns a smaller payload."
    )
    lite: Union[bool, str, None] = Field(
        default=None,
        description="If true, returns minimal response. Alias for response_mode='minimal'."
    )
    auto_export_on_significance: bool = Field(
        default=False,
        description="If true, automatically export governance history when thermodynamically significant events occur."
    )
    require_strong_identity: Union[bool, str, None] = Field(
        default=False,
        description="If true, reject updates unless identity assurance tier is strong."
    )
    task_type: TaskType = Field(
        default="mixed",
        description="Task type context. Core types: convergent | divergent | mixed. Use 'introspection' for epistemic self-examination where low confidence is appropriate."
    )
    trajectory_signature: Optional[dict] = Field(
        default=None,
        description="Trajectory identity signature from anima-mcp."
    )
    agent_name: Optional[str] = Field(
        default=None,
        description="Your display name for identity reconnection."
    )
    recent_tool_results: Optional[List[ToolResultEvidence]] = Field(
        None,
        description=(
            "Self-reported tool outcomes from the agent's most recent actions. "
            "Server emits one outcome_event per item (gated by "
            "UNITARES_PHASE5_EVIDENCE_WRITE). See docs/proposals/refined-phase-5-evidence-contract.md."
        ),
    )
    harness: Optional[str] = Field(None, description="S22 provenance: harness type/body mediating the write")
    harness_id: Optional[str] = Field(None, description="S22 provenance: concrete harness/runtime instance")
    harness_type: Optional[str] = Field(None, description="S22 provenance: normalized harness family")
    transport: Optional[str] = Field(None, description="S22 provenance: channel/protocol used for the write")
    model_provider: Optional[str] = Field(None, description="S22 provenance: model provider")
    model: Optional[str] = Field(None, description="S22 provenance: model identifier")
    memory_context: Optional[str] = Field(None, description="S22 provenance: memory/KG/transcript surfaces visible to the writer")
    tool_surface: Optional[Union[List[str], str]] = Field(None, description="S22 provenance: available tool families")
    governance_mode: Optional[str] = Field(None, description="S22 provenance: explicit/ambient/gated/lifecycle/posthoc mode")
    verification_source: Optional[str] = Field(None, description="S22 provenance: evidence source vocabulary")
    comparison_key: Optional[str] = Field(None, description="S22 H5 provenance: stable key for comparing the same bounded task across harnesses")
    task_label: Optional[str] = Field(None, description="S22 H5 provenance: human-readable bounded task label")
    task_outcome: Optional[str] = Field(None, description="S22 H5 provenance: outcome label for the bounded task")
    episode_id: Optional[str] = Field(None, description="S22 provenance: local interaction span")
    invocation_id: Optional[str] = Field(None, description="S22 provenance: concrete command/run invocation")
    process_instance_id: Optional[str] = Field(None, description="S22 provenance: opaque process-instance fingerprint")
    locus: Optional[Dict[str, Any]] = Field(None, description="S22 provenance: situated transport context")
    affordance_state: Optional[Dict[str, Any]] = Field(None, description="S22 provenance: available reach/capability snapshot")

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.complexity, str):
            try:
                self.complexity = float(self.complexity)
            except ValueError:
                self.complexity = 0.5
        if isinstance(self.confidence, str):
            try:
                self.confidence = float(self.confidence)
            except ValueError:
                self.confidence = None
        if isinstance(self.lite, str):
            val = str(self.lite).lower() in ('true', '1', 'yes')
            if val and self.response_mode == "auto":
                self.response_mode = "minimal"
        if isinstance(self.require_strong_identity, str):
            self.require_strong_identity = self.require_strong_identity.lower() in ('true', '1', 'yes')
        return self


class OutcomeEventParams(AgentIdentityMixin):
    """Parameters for outcome_event"""
    outcome_type: Literal["drawing_completed", "drawing_abandoned", "test_passed", "test_failed", "tool_rejected", "task_completed", "task_failed", "trajectory_validated", "dialectic_resolved"] = Field(..., description="Type of outcome event")
    outcome_score: Optional[float] = Field(None, description="Quality score 0.0 (worst) to 1.0 (best). Inferred from type if omitted.")
    is_bad: Optional[bool] = Field(None, description="Whether this is a negative outcome. Inferred from type if omitted.")
    detail: Optional[Dict[str, Any]] = Field(None, description="Type-specific metadata (e.g., mark_count, test_name, error_message)")
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0, description="Agent confidence at outcome time (0-1). Looked up from last check-in if omitted.")
    prediction_id: Optional[str] = Field(None, description="Tactical prediction id from a prior process_agent_update response. When provided, the registered confidence for that id is used instead of the temporal proxy fallback.")
    agent_id: Optional[str] = Field(None, description="Agent ID. Falls back to session-bound agent_id if omitted.")
    decision_action: Optional[str] = Field(None, description="The decision the agent took (e.g. 'proceed', 'pause'). Used by sequential calibration tracking; for test_passed/test_failed defaults to 'proceed'.")
    session_id: Optional[str] = Field(None, description="Optional session id; falls back to client_session_id and then to context.")
    verification_source: Literal[
        "agent_reported_tool_result",
        "server_observation",
        "external_signal",
    ] = Field(
        "agent_reported_tool_result",
        description=(
            "Provenance of this outcome. v1 default is agent_reported_tool_result. "
            "server_observation reserved for v2 server-verified primitive (KG writes, "
            "dialectic verdicts, state transitions). external_signal for CI webhooks etc."
        ),
    )


class CirsProtocolParams(AgentIdentityMixin):
    """Parameters for cirs_protocol"""
    protocol: Literal["void_alert", "state_announce", "coherence_report", "boundary_contract", "governance_action"] = Field(..., description="Which CIRS protocol to use")
    action: Optional[str] = Field(None, description="Action within the protocol (emit/query/compute/set/get/initiate/respond)")
    target_agent_id: Optional[str] = Field(None, description="Target agent (for coherence_report)")
    severity: Optional[Literal["warning", "critical"]] = Field(None, description="Alert severity (for void_alert)")
    limit: Optional[int] = Field(None, description="Max results for queries")


class ValidateFilePathParams(AgentIdentityMixin):
    """Parameters for validate_file_path"""
    file_path: str = Field(..., description="File path to validate against project policies")


class GetWorkspaceHealthParams(AgentIdentityMixin):
    """Parameters for get_workspace_health"""
    pass


class CallModelParams(AgentIdentityMixin):
    """Parameters for call_model"""
    prompt: str = Field(..., description="The prompt/question to send to the model (required)")
    model: str = Field("auto", description="Model to use. For ollama: any model pulled locally (default UNITARES_LLM_MODEL or gemma4:latest). For hf: model IDs like 'deepseek-ai/DeepSeek-R1' or 'Qwen/Qwen2.5-72B-Instruct'. Default: auto")
    provider: Literal["auto", "hf", "ollama"] = Field("auto", description="Provider to use. Options: auto (ollama first, hf fallback), hf (Hugging Face Inference Providers), ollama (local). Default: auto")
    task_type: Literal["reasoning", "generation", "analysis"] = Field("reasoning", description="Type of task. Options: reasoning, generation, analysis. Default: reasoning")
    max_tokens: float = Field(500, description="Maximum tokens in response. Default: 500")
    temperature: float = Field(0.7, description="Temperature (creativity). Range: 0.0-1.0. Default: 0.7")
    privacy: Literal["local", "auto", "cloud"] = Field("local", description="Privacy mode. Options: local (Ollama, default), auto (system chooses), cloud (external providers)")

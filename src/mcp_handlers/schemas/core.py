from datetime import datetime
from typing import Optional, Union, Literal, Dict, Any, List
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

StateEpistemicClass = Literal[
    "agent_report",
    "substrate_observation",
    "substrate_interpretation",
    "prediction",
    "synthetic",
]
ProcessUpdateEpistemicClass = Literal[
    "agent_report",
    "substrate_observation",
    "substrate_interpretation",
    "prediction",
]


_COMPLEXITY_ALIAS_HINT = (
    " For 1-10 or other scales, call a check-in alias (checkin/log/update/"
    "sync_state) with complexity={'value': N, 'scale': M} or a named level "
    "like 'medium'."
)


def _coerce_unit_string_fields(data: Any, *field_names: str, alias_hint: str = "") -> Any:
    """Coerce numeric strings to float BEFORE field validation.

    ge/le on Union[float, str, None] raises a bare TypeError when the value
    is a string, which the dispatch middleware's generic except treats as
    "validation unavailable" — so '5' and 'abc' used to reach handlers
    unvalidated. Coercing here lets the field's own ge/le enforce the 0-1
    range; unparseable strings reject instead of silently degrading.
    Canonical check-in tools are strict; friendly aliases (checkin/log/
    update/sync_state) normalize richer vocabulary upstream."""
    if not isinstance(data, dict):
        return data
    coerced = None
    for field_name in field_names:
        value = data.get(field_name)
        if isinstance(value, str):
            try:
                numeric = float(value)
            except ValueError:
                hint = alias_hint if field_name == "complexity" else ""
                raise ValueError(
                    f"{field_name} must be a number between 0 and 1, got "
                    f"{value!r}.{hint}"
                )
            if coerced is None:
                coerced = dict(data)
            coerced[field_name] = numeric
    return coerced if coerced is not None else data


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

    @model_validator(mode='before')
    @classmethod
    def coerce_unit_strings(cls, data):
        return _coerce_unit_string_fields(
            data, "complexity", "confidence", alias_hint=_COMPLEXITY_ALIAS_HINT
        )

    @model_validator(mode='after')
    def coerce_types(self):
        if isinstance(self.lite, str):
            self.lite = self.lite.lower() in ('true', '1', 'yes')
        return self


class ToolResultEvidence(BaseModel):
    """Self-reported tool outcome evidence from a recent agent action.

    Self-report — the server treats this as
    `verification_source="agent_reported_tool_result"`. A future server-verified
    primitive will provide `server_observation` outcomes for the subset of
    work the server can independently verify. See spec §1.

    Example: {"tool": "pytest", "summary": "tests passed", "is_bad": false}
    """
    model_config = ConfigDict(extra="forbid")

    kind: ToolResultKind = Field(
        default="tool_call",
        description=(
            "Tool-result category. If omitted, the server infers it from "
            "tool/summary and falls back to command or tool_call."
        ),
    )
    tool: str = Field(..., max_length=64, description="Tool name, NOT 'name'.")
    summary: str = Field(..., max_length=512)
    exit_code: Optional[int] = None
    is_bad: Optional[bool] = Field(
        default=None,
        description=(
            "Whether the tool outcome was bad (true=failure). Note: this is "
            "the inverse of 'success'."
        ),
    )
    prediction_id: Optional[str] = None
    observed_at: Optional[datetime] = None

    @model_validator(mode="before")
    @classmethod
    def reject_success_alias_shape(cls, data):
        if not isinstance(data, dict):
            return data
        if "name" in data and "success" in data:
            raise ValueError(
                "ToolResultEvidence does not accept {name, success}. "
                "Use {tool, summary, is_bad}. "
                "Example: {\"tool\": \"pytest\", \"summary\": \"tests passed\", "
                "\"is_bad\": false}. "
                "The 'kind' field is optional and inferred from tool when omitted."
            )
        return data

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
        description=(
            "Estimated task complexity, strictly 0-1. Check-in aliases "
            "(checkin/log/update/sync_state) also accept named levels "
            "('trivial'|'low'|'medium'|'high'|'very_high') and explicit "
            "scale objects like {'value': 5, 'scale': 10}."
        )
    )
    confidence: Union[float, str, None] = Field(
        default=None,
        ge=0.0, 
        le=1.0,
        description="Confidence level for this update (0-1, optional)."
    )
    epistemic_class: ProcessUpdateEpistemicClass = Field(
        default="agent_report",
        description=(
            "Forward-only storage label for this state row. Defaults to "
            "agent_report. Use substrate_observation for raw measured facts, "
            "substrate_interpretation for hook/tool-derived heuristics, "
            "and prediction for explicit forward claims. Server-authored "
            "bootstrap rows are labeled synthetic internally."
        ),
    )
    response_mode: Literal["minimal", "compact", "standard", "full", "mirror", "auto"] = Field(
        default="auto",
        description=(
            "Response verbosity. 'auto' (default) adapts to health — mirror when "
            "healthy/disembodied, else minimal/standard/compact. 'mirror' returns "
            "actionable self-awareness signals instead of raw EISV. 'standard' is a "
            "human-readable interpretation. 'minimal' and 'compact' are both small "
            "payloads (minimal = action + EISV snapshot + margin; compact = brief "
            "metrics + decision). 'full' returns the complete payload unfiltered. "
            "The 'lite' boolean below is an alias for 'minimal'."
        )
    )
    lite: Union[bool, str, None] = Field(
        default=None,
        description="Boolean alias for response_mode='minimal'. Applies only when response_mode is left at 'auto' (an explicit response_mode always wins)."
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
 "UNITARES_PHASE5_EVIDENCE_WRITE). ."
        ),
    )
    provenance_context: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Optional S22 write-local provenance envelope. Use this visible "
            "object for situating metadata such as harness_type, model_provider, "
            "model, transport, tool_surface, governance_mode, verification_source, "
            "locus, and session-resolution fields. Do not put these fields in "
            "recent_tool_results. Descriptive only, not identity proof."
        ),
    )
    # S22 provenance — compact top-level subset retained for older callers and
    # H5 comparison keys. Richer situating metadata should use
    # provenance_context above so LLM-facing clients have one typed slot instead
    # of stuffing hidden/prose fields into recent_tool_results. r6_dogfood-style
    # internal callers can still pass known S22 fields via extra-field
    # preservation in src/mcp_handlers/middleware/params_step.py.
    comparison_key: Optional[str] = Field(None, description="S22 H5 provenance: stable key for comparing the same bounded task across harnesses")
    task_label: Optional[str] = Field(None, description="S22 H5 provenance: human-readable bounded task label")
    task_outcome: Optional[str] = Field(None, description="S22 H5 provenance: outcome label for the bounded task")
    memory_context: Optional[str] = Field(None, description="S22 provenance: memory/KG/transcript surfaces visible to the writer")

    @model_validator(mode='before')
    @classmethod
    def coerce_unit_strings(cls, data):
        return _coerce_unit_string_fields(
            data, "complexity", "confidence", alias_hint=_COMPLEXITY_ALIAS_HINT
        )

    @model_validator(mode='after')
    def coerce_types(self):
        # `lite` is a boolean alias for response_mode='minimal'. Accept a real
        # bool OR a truthy string — previously only the string form was honored,
        # so an actual JSON `lite: true` was silently ignored. Only applies when
        # response_mode is still 'auto', so an explicit response_mode always wins.
        if self.lite is not None:
            lite_on = self.lite if isinstance(self.lite, bool) else (
                str(self.lite).lower() in ('true', '1', 'yes')
            )
            if lite_on and self.response_mode == "auto":
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
    include_semantics: Union[bool, str, None] = Field(
        default=False,
        description=(
            "If true, the response's eisv_snapshot carries the full EISV ontology "
            "(state_semantics role table + hierarchy). Default false returns a lite "
            "confirmation-sized snapshot. Alias: response_mode='full'."
        ),
    )
    response_mode: Optional[Literal["lite", "full"]] = Field(
        default=None,
        description="'full' is an alias for include_semantics=true; 'lite' (default) returns the small snapshot.",
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

"""
UpdateContext — Shared state for process_agent_update phases.

Replaces the ~20+ local variables threaded through the original monolithic function.
Each phase reads/writes fields on this dataclass instead of relying on closure scope.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
@dataclass
class UpdateContext:
    """Carries state between extracted update phases and enrichments."""

    # ── Raw arguments ──────────────────────────────────────────────
    arguments: Dict[str, Any] = field(default_factory=dict)

    # ── Identity (Phase 1) ─────────────────────────────────────────
    agent_uuid: str = ""
    agent_id: str = ""           # Same as agent_uuid (UUID). Label/display in declared_agent_id.
    session_key: Optional[str] = None
    declared_agent_id: str = ""
    label: Optional[str] = None
    is_new_agent: bool = False
    meta: Optional[Any] = None   # AgentMetadata instance
    session_resolution_source: Optional[str] = None
    proof_origin: Optional[str] = None  # 'caller_asserted' | 'server_inferred'
    trajectory_confidence: Optional[float] = None
    identity_assurance: Dict[str, Any] = field(default_factory=dict)

    # ── Validated inputs (Phase 3) ─────────────────────────────────
    response_text: str = ""
    complexity: float = 0.5
    confidence: Optional[float] = None
    epistemic_class: str = "agent_report"
    ethical_drift: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    task_type: str = "mixed"
    calibration_correction_info: Optional[str] = None

    # ── Onboarding (Phase 2) ───────────────────────────────────────
    onboarding_guidance: Optional[Dict] = None
    dialectic_enforcement_warning: Optional[str] = None

    # ── Core result (Phase 4) ──────────────────────────────────────
    result: Dict[str, Any] = field(default_factory=dict)
    monitor: Optional[Any] = None   # UNITARESMonitor instance
    agent_state: Dict[str, Any] = field(default_factory=dict)

    # ── Side effects (Phase 5) ─────────────────────────────────────
    health_status: Optional[Any] = None
    health_message: str = ""
    metrics_dict: Dict[str, Any] = field(default_factory=dict)
    risk_score: Optional[float] = None
    coherence: Optional[float] = None
    cirs_alert: Optional[Dict] = None
    cirs_state_announce: Optional[Dict] = None
    outcome_event_id: Optional[str] = None

    # ── Response accumulator (Phase 6) ─────────────────────────────
    response_data: Dict[str, Any] = field(default_factory=dict)

    # ── Class-conditional grounding ────────────────────────────────
    agent_class: Optional[str] = None  # set by enrich_grounding via classify_agent

    # ── Cached computations ────────────────────────────────────────
    _cal_error: Optional[float] = None
    _cal_error_ready: bool = False

    # ── Phase-5 evidence supply (Task 4) ──────────────────────────
    recent_tool_results: List[Any] = field(default_factory=list)

    # ── Flags ──────────────────────────────────────────────────────
    key_was_generated: bool = False
    api_key_auto_retrieved: bool = False
    api_key: Optional[str] = None
    policy_warnings: List[str] = field(default_factory=list)
    loop_info: Optional[Dict] = None
    warnings: List[str] = field(default_factory=list)
    previous_void_active: bool = False

    # ── Runtime references (set by orchestrator) ─────────────────
    loop: Optional[Any] = None       # asyncio event loop
    mcp_server: Optional[Any] = None # mcp_server_std module (from core.py's patched ref)


def get_mean_calibration_error(ctx: 'UpdateContext') -> Optional[float]:
    """Return mean calibration error, computing once and caching on ctx."""
    if ctx._cal_error_ready:
        return ctx._cal_error
    try:
        from src.calibration import calibration_checker
        metrics = calibration_checker.compute_calibration_metrics()
        if metrics:
            errors = [b.calibration_error for b in metrics.values() if b.count >= 5]
            if errors:
                ctx._cal_error = sum(errors) / len(errors)
    except Exception:
        pass
    ctx._cal_error_ready = True
    return ctx._cal_error

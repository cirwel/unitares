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
    # Call-local observational sidecar. It never enters agent_state, the
    # monitor, the ODE, or policy; post-update persistence reads it directly.
    submitted_afferents: Optional[Dict[str, Any]] = None

    # ── Side effects (Phase 5) ─────────────────────────────────────
    health_status: Optional[Any] = None
    health_message: str = ""
    metrics_dict: Dict[str, Any] = field(default_factory=dict)
    risk_score: Optional[float] = None
    coherence: Optional[float] = None
    # Which form produced `coherence`: "legacy_tanh_v" or the grounded source
    # ("manifold"). Set by run_grounding_stage, persisted to state_json by
    # agent_storage. New rows are tagged even when neither grounding flag is set;
    # None is reserved for malformed/no-metrics paths and pre-tag stored rows.
    coherence_form: Optional[str] = None
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
    # Observational provenance for the deployed fleet statistic and the
    # agent-scoped candidate. Nothing in the live I/policy path reads this.
    _calibration_signal: Dict[str, Any] = field(default_factory=dict)

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
    """Return the deployed fleet error and cache its observational provenance.

    The returned value deliberately preserves the live policy input.  The
    agent-scoped estimate is collected beside it as a measurement-only
    candidate so consumers can evaluate coverage and freshness before any
    separately reviewed authority change.
    """
    if ctx._cal_error_ready:
        return ctx._cal_error
    try:
        from src.calibration import calibration_checker
        metrics = calibration_checker.compute_calibration_metrics()
        eligible = [b for b in metrics.values() if b.count >= 5]
        if metrics:
            errors = [b.calibration_error for b in eligible]
            if errors:
                ctx._cal_error = sum(errors) / len(errors)
        candidate = calibration_checker.compute_agent_calibration_candidate(
            ctx.agent_id
        )
        ctx._calibration_signal = {
            "schema": "eisv.calibration-signal.v1",
            "mode": "measurement_only",
            "policy_applied": False,
            "deployed": {
                "scope": "fleet",
                "estimator": "mean_absolute_strategic_bin_error",
                "evidence_channel": "strategic_mixed_proxy",
                "calibration_error": ctx._cal_error,
                "sample_count": sum(b.count for b in metrics.values()),
                "eligible_sample_count": sum(b.count for b in eligible),
                "eligible_bin_count": len(eligible),
                "minimum_samples_per_bin": 5,
                "sample_window": "lifetime",
                "freshness_status": "unknown",
                "freshness_reason": "legacy_fleet_bins_have_no_timestamps",
            },
            "agent_candidate": candidate,
        }
    except Exception:
        ctx._calibration_signal = {
            "schema": "eisv.calibration-signal.v1",
            "mode": "measurement_only",
            "policy_applied": False,
            "deployed": {
                "scope": "fleet",
                "calibration_error": ctx._cal_error,
                "freshness_status": "unknown",
                "evidence_status": "unavailable",
            },
            "agent_candidate": {
                "scope": "agent",
                "agent_id": ctx.agent_id or None,
                "evidence_status": "unavailable",
            },
        }
    ctx._cal_error_ready = True
    return ctx._cal_error

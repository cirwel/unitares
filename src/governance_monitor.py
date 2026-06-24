"""
UNITARES Governance Monitor v2.0 - Core Implementation
Complete thermodynamic governance framework with all decision points implemented.

Now uses governance_core module (canonical UNITARES Phase-3 implementation)
while maintaining backward-compatible MCP interface.

Version History:
- v1.0: Used unitaires_core directly
- v2.0: Migrated to governance_core (single source of truth for dynamics)
"""

import os
import numpy as np
from typing import Dict, Optional, Any
from collections import deque
from datetime import datetime
from pathlib import Path
import json

# How many model<->body divergence samples to retain per agent for trend reads.
SENSOR_DIVERGENCE_HISTORY_MAX = 200

from config.governance_config import config

# Import structured logging
from src.logging_utils import get_logger
logger = get_logger(__name__)

_VERDICT_SEVERITY = {"safe": 0, "caution": 1, "high-risk": 2}


def _more_severe_verdict(left: Optional[str], right: Optional[str]) -> Optional[str]:
    """Return the higher-severity governance verdict without downgrading unknowns."""
    if left not in _VERDICT_SEVERITY:
        return right if right in _VERDICT_SEVERITY else left
    if right not in _VERDICT_SEVERITY:
        return left
    return right if _VERDICT_SEVERITY[right] > _VERDICT_SEVERITY[left] else left

# Import audit logging and calibration for accountability and self-awareness
from src.audit_log import audit_logger
from src.calibration import calibration_checker

# Extracted monitor subsystems (Phase 6 decomposition)
from src.monitor_void import check_void_state as _check_void_state, calculate_void_frequency as _calculate_void_frequency
from src.monitor_risk import estimate_risk as _estimate_risk
from src.monitor_decision import make_decision as _make_decision
from src.monitor_regime import detect_regime as _detect_regime
from src.monitor_lambda import update_lambda1 as _update_lambda1

# Import dual-log architecture for grounded EISV inputs (Patent: Dual-Log Architecture)
from src.dual_log import ContinuityLayer, RestorativeBalanceMonitor


# Import UNITARES Phase-3 engine from governance_core (v2.0)
# Core dynamics are now in governance_core module
from src._imports import ensure_project_root

# Ensure project root is in path for imports
ensure_project_root()

# Import core dynamics from governance_core (canonical implementation)
from governance_core import (
    State, Theta,
    DEFAULT_STATE, DEFAULT_THETA, DEFAULT_PARAMS,
    step_state, coherence,
    eisv_divergence,
    phi_objective, verdict_from_phi,  # noqa: F401 — re-exported; consumers import them from this module
    )

# UNITARES params profile selection (optional v4.1 alignment)
from governance_core.parameters import get_active_params

# Import extracted modules
from src.governance_state import GovernanceState
from src.confidence import derive_confidence
from src.cirs import (
    OscillationDetector, ResonanceDamper, OscillationState,
    CIRS_DEFAULTS,
)
from src.hck_reflexive import (
    compute_update_coherence as _compute_update_coherence,
    compute_continuity_energy as _compute_continuity_energy,
    modulate_gains as _modulate_gains,
)
from src.monitor_metrics import (
    get_monitor_metrics as _get_monitor_metrics,
    get_eisv_labels as _get_eisv_labels,
    export_monitor_history as _export_monitor_history,
)
from src.behavioral_state import BehavioralEISV
from src.behavioral_assessment import assess_behavioral_state
from src.behavioral_sensor import compute_behavioral_sensor_eisv

# Extracted monitor subsystems (Phase 7 decomposition)
from src.monitor_drift import compute_drift_vector as _compute_drift_vector_impl
from src.monitor_phi import compute_phi_and_risk as _compute_phi_and_risk_impl
from src.monitor_cirs import run_cirs as _run_cirs_impl
from src.monitor_calibration import run_calibration_recording as _run_calibration_recording_impl
from src.monitor_result import build_result as _build_result_impl
from src.monitor_prediction import (
    register_tactical_prediction as _register_prediction,
    lookup_prediction as _lookup_prediction,
    consume_prediction as _consume_prediction,
    expire_old_predictions as _expire_predictions,
)


class UNITARESMonitor:
    """
    UNITARES v1.0 Governance Monitor

    Implements complete thermodynamic governance with:
    - 4D state evolution (E, I, S, V)
    - Risk estimation from agent behavior
    - Adaptive λ₁ via PI controller
    - Void detection with adaptive thresholds
    - Decision logic (approve/reflect/reject)
    - HCK v3.0: Update coherence ρ(t) and gain modulation
    - CIRS v0.1: Oscillation detection and resonance damping
    """

    # HCK v3.0: Delegating to src/hck_reflexive.py
    compute_update_coherence = staticmethod(_compute_update_coherence)
    compute_continuity_energy = staticmethod(_compute_continuity_energy)
    modulate_gains = staticmethod(_modulate_gains)

    def __init__(self, agent_id: str, load_state: bool = True):
        """
        Initialize monitor for a specific agent
        
        Args:
            agent_id: Unique identifier for the agent
            load_state: If True, attempt to load persisted state from disk
        """
        self.agent_id = agent_id
        self.state = GovernanceState()
        
        # Initialize prev_parameters (needed for coherence calculation)
        # This must be initialized regardless of whether state is loaded
        self.prev_parameters: Optional[np.ndarray] = None
        
        # Initialize last_update timestamp (needed for simulate_update)
        self.last_update = datetime.now()

        # created_at: same invariant — set unconditionally here so the persisted-state
        # branch never returns a monitor without it. load_persisted_state() overrides
        # with the file's created_at_iso when present; for older files this now() stands.
        # (Replaces the post-hoc `if not hasattr(self,'created_at')` band-aid that
        # patched this one attribute the same way #800 patched the divergence write.)
        self.created_at = datetime.now()

        # Model<->body sensor divergence ("compare, don't couple"). MUST be set
        # here, not only in _initialize_fresh_state(): the persisted-state branch
        # below (load_state=True + state file present) skips _initialize_fresh_state
        # entirely, so any established agent whose state file predates these fields
        # would otherwise reach update_dynamics without them and reject its check-in
        # with AttributeError (incident 2026-06-16, #800). load_persisted_state()
        # overrides these when the file carries a divergence history.
        self._last_sensor_divergence: Optional[dict] = None
        self._sensor_divergence_history: deque = deque(maxlen=SENSOR_DIVERGENCE_HISTORY_MAX)

        # Initialize dual-log architecture for grounded EISV inputs
        # ContinuityLayer compares operational (server-derived) vs reflective (agent-reported)
        # to produce grounded complexity, divergence metrics, and EISV inputs
        self.continuity_layer = ContinuityLayer(agent_id=agent_id, redis_client=None)
        self.restorative_monitor = RestorativeBalanceMonitor(agent_id=agent_id, redis_client=None)
        self._last_continuity_metrics = None
        self._last_restorative_status = None
        self._last_drift_vector = None  # Concrete ethical drift (Δη)
        # Last signed complexity gap (self − derived) that was marked novel
        # for the mirror's calibration line. Monitor-lifetime only,
        # deliberately not persisted: after a restart — or once per worker
        # under multi-process serving (each worker caches its own monitor)
        # — the line may fire anew, which is acceptable session-scoped
        # novelty. Written by monitor_result._complexity_divergence_novel;
        # simulate_update saves/restores it so simulations don't consume
        # the agent's first real surfacing.
        self._last_surfaced_complexity_gap: Optional[float] = None

        # HCK v3.0: Track previous EISV for update coherence ρ(t) and state velocity
        self._prev_E: Optional[float] = None
        self._prev_I: Optional[float] = None
        self._prev_S: Optional[float] = None
        self._prev_V: Optional[float] = None
        self._last_state_velocity: float = 0.0

        # Behavioral EISV: observation-first state (no ODE, no attractor)
        self._behavioral_state = BehavioralEISV()
        self._last_behavioral_verdict: Optional[str] = None  # safe/caution/high-risk
        self._cached_outcome_history: Optional[list] = None  # Populated by Phase 5, used by process_update

        # Continuous self-validation: track previous verdict for trajectory comparison
        self._prev_verdict_action: Optional[str] = None   # 'proceed', 'pause', etc.
        self._prev_drift_norm: Optional[float] = None
        self._prev_confidence: Optional[float] = None
        self._prev_checkin_time: Optional[float] = None  # monotonic time of last check-in

        # Gap-recovery: bumped to GAP_RECOVERY_CYCLES when wall-clock dt
        # saturates DT_MAX (e.g., MacBook clamshell sleep-wake). While > 0,
        # 'pause' decisions are downgraded to 'proceed' and logged separately
        # via audit_logger.log_attest_gap_suppressed. Decrements each cycle.
        self._gap_recovery_cycles_remaining: int = 0

        # Per-process update counter for the warmup STRUCTURAL grace. Counts
        # process_update calls in THIS process only — NOT persisted, NOT restored
        # by load/hydrate, so every (re)start begins at 0. Distinct from
        # state.update_count (lifetime, restored) and from behavioral update_count.
        # While this is small, the ODE state is a cold-start transient.
        self._process_local_updates: int = 0

        # Tactical prediction registry: open (confidence, id) pairs awaiting an
        # outcome. Minted at check-in time, consumed when an outcome references
        # the id. Enables exact filtration for the sequential calibration lane
        # when the agent echoes the id back on outcome_event. See
        # src/sequential_calibration.py module docstring for the null it serves.
        self._open_predictions: Dict[str, Dict[str, Any]] = {}
        self._last_prediction_id: Optional[str] = None
        self._prediction_ttl_seconds: float = 3600.0  # orphan cleanup threshold

        # CIRS: Initialize oscillation detector / adaptive governor
        from config.governance_config import GovernanceConfig as GovConfig
        if GovConfig.ADAPTIVE_GOVERNOR_ENABLED:
            from governance_core.adaptive_governor import AdaptiveGovernor
            self.adaptive_governor = AdaptiveGovernor()
            self.oscillation_detector = None
            self.resonance_damper = None
        else:
            # Legacy v0.1 path
            self.adaptive_governor = None
            self.oscillation_detector = OscillationDetector(
                window=CIRS_DEFAULTS['window'],
                ema_lambda=CIRS_DEFAULTS['ema_lambda'],
                oi_threshold=CIRS_DEFAULTS['oi_threshold'],
                flip_threshold=CIRS_DEFAULTS['flip_threshold']
            )
            self.resonance_damper = ResonanceDamper(
                kappa_r=CIRS_DEFAULTS['kappa_r'],
                delta_tau=CIRS_DEFAULTS['delta_tau'],
                tau_bounds=CIRS_DEFAULTS['tau_bounds'],
                beta_bounds=CIRS_DEFAULTS['beta_bounds']
            )
        self._last_oscillation_state: Optional[OscillationState] = None
        self._gains_modulated: bool = False  # Track if gains were modulated this cycle
        
        # Try to load persisted state if requested
        if load_state:
            persisted_state = self.load_persisted_state()
            if persisted_state is not None:
                self.state = persisted_state
                # Restore AdaptiveGovernor state if available
                gov_dict = getattr(persisted_state, '_governor_state_dict', None)
                if gov_dict and self.adaptive_governor is not None:
                    from governance_core.adaptive_governor import GovernorState
                    self.adaptive_governor.state = GovernorState.from_dict(gov_dict)
                    logger.debug(f"Restored governor state: tau={self.adaptive_governor.state.tau:.3f}, beta={self.adaptive_governor.state.beta:.3f}")
                # created_at is initialized unconditionally in the early block above
                # and overridden by load_persisted_state() when the file carries it,
                # so no post-hoc fallback is needed here.
                logger.info(f"Loaded persisted state for agent: {agent_id} ({len(self.state.V_history)} history entries)")
            else:
                # Initialize fresh state
                self._initialize_fresh_state()
                logger.info(f"Initialized new monitor for agent: {agent_id}")
        else:
            self._initialize_fresh_state()
            logger.info(f"Initialized monitor for agent: {agent_id} (no state loading)")

        logger.debug(f"λ₁ initial: {self.state.lambda1:.4f}")
        logger.debug(f"Void threshold: {config.VOID_THRESHOLD_INITIAL:.4f}")
    
    def _initialize_fresh_state(self):
        """Initialize fresh state with default values"""
        # Initialize UNITARES Phase-3 state and theta
        self.state.unitaires_state = State(**{
            'E': DEFAULT_STATE.E,
            'I': DEFAULT_STATE.I,
            'S': DEFAULT_STATE.S,
            'V': DEFAULT_STATE.V
        })
        self.state.unitaires_theta = Theta(**{
            'C1': DEFAULT_THETA.C1,
            'eta1': DEFAULT_THETA.eta1
        })

        # Previous state for drift calculation
        self.prev_parameters: Optional[np.ndarray] = None

        # Most recent model<->body divergence (compare, don't couple); None until
        # a check-in arrives carrying sensor_eisv (embodied agents like Lumen).
        # The bounded history makes the divergence legible as a TREND — the
        # evidence of whether the EISV mapping holds without coupling forcing it.
        self._last_sensor_divergence: Optional[dict] = None
        self._sensor_divergence_history: deque = deque(maxlen=SENSOR_DIVERGENCE_HISTORY_MAX)

        # Timestamps for agent lifecycle tracking
        self.created_at = datetime.now()
        self.last_update = datetime.now()
    
    def load_persisted_state(self) -> Optional[GovernanceState]:
        """Load persisted state from disk if it exists"""
        # Get project root
        from src._imports import ensure_project_root
        project_root = ensure_project_root()
        
        # Use organized structure: data/agents/
        new_path = Path(project_root) / "data" / "agents" / f"{self.agent_id}_state.json"
        old_path = Path(project_root) / "data" / f"{self.agent_id}_state.json"

        # Check new location first, then old location for backward compatibility
        if new_path.exists():
            state_file = new_path
        elif old_path.exists():
            state_file = old_path
        else:
            return None
        
        try:
            with open(state_file, 'r') as f:
                data = json.load(f)
                # Restore behavioral EISV if present (backward compatible)
                beh_data = data.pop('behavioral_eisv', None)
                if beh_data:
                    self._behavioral_state = BehavioralEISV.from_dict(beh_data)
                # Restore last divergence snapshot if present (transient; pop so
                # GovernanceState.from_dict does not see an unknown key).
                self._last_sensor_divergence = data.pop('sensor_divergence', None)
                # Restore bounded divergence trend history (pop for the same reason).
                div_hist = data.pop('sensor_divergence_history', None)
                if div_hist:
                    self._sensor_divergence_history = deque(
                        div_hist, maxlen=SENSOR_DIVERGENCE_HISTORY_MAX
                    )
                # Restore created_at if persisted; otherwise __init__ will fall
                # back to now() for older state files that predate this field.
                created_at_iso = data.pop('created_at_iso', None)
                if created_at_iso:
                    try:
                        self.created_at = datetime.fromisoformat(created_at_iso)
                    except (TypeError, ValueError) as e:
                        logger.warning(
                            f"Could not parse created_at_iso for {self.agent_id} "
                            f"({created_at_iso!r}): {e}; falling back to now()",
                        )
                # Restore last_update if persisted; otherwise leave the in-memory
                # value (set in __init__) intact. last_update lives on the monitor,
                # not on GovernanceState, so it's handled here as a side effect —
                # mirroring the behavioral_eisv pattern above.
                last_update_iso = data.pop('last_update_iso', None)
                if last_update_iso:
                    try:
                        self.last_update = datetime.fromisoformat(last_update_iso)
                    except (TypeError, ValueError) as e:
                        logger.warning(
                            f"Could not parse last_update_iso for {self.agent_id} "
                            f"({last_update_iso!r}): {e}; falling back to now()",
                        )
                return GovernanceState.from_dict(data)
        except Exception as e:
            logger.warning(f"Could not load persisted state for {self.agent_id}: {e}", exc_info=True)
            return None
    
    def save_persisted_state(self) -> None:
        """Save current state to disk"""
        # Use organized structure: data/agents/
        from src._imports import ensure_project_root
        project_root = ensure_project_root()
        state_file = Path(project_root) / "data" / "agents" / f"{self.agent_id}_state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            import tempfile
            state_data = self.state.to_dict_with_history()
            # Include behavioral EISV state for persistence
            state_data['behavioral_eisv'] = self._behavioral_state.to_dict_with_history()
            # Persist the most recent model<->body divergence snapshot (transient
            # signal; recomputed each check-in, kept for observability/debugging).
            last_div = getattr(self, '_last_sensor_divergence', None)
            if last_div is not None:
                state_data['sensor_divergence'] = last_div
            # Persist the bounded divergence trend so it survives restarts.
            div_hist = getattr(self, '_sensor_divergence_history', None)
            if div_hist:
                state_data['sensor_divergence_history'] = list(div_hist)
            # Persist created_at so agent age/maturity survives process restarts.
            created_at = getattr(self, 'created_at', None)
            if created_at is not None:
                state_data['created_at_iso'] = created_at.isoformat()
            # Persist last_update so cross-restart gaps integrate against the real
            # prior check-in time, not the lazy-init wall-clock.
            state_data['last_update_iso'] = self.last_update.isoformat()
            # Atomic write: write to temp file, then rename to prevent corruption
            tmp_fd, tmp_path = tempfile.mkstemp(dir=state_file.parent, suffix='.tmp')
            try:
                with os.fdopen(tmp_fd, 'w') as f:
                    json.dump(state_data, f, indent=2)
                os.replace(tmp_path, state_file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.warning(f"Could not save state for {self.agent_id}: {e}", exc_info=True)
    
    def _trim_histories(self) -> None:
        """Trim all history arrays to HISTORY_WINDOW."""
        window = config.HISTORY_WINDOW
        for attr in (
            'E_history', 'I_history', 'S_history', 'V_history',
            'coherence_history', 'timestamp_history', 'lambda1_history',
            'regime_history', 'rho_history', 'CE_history', 'oi_history',
        ):
            history = getattr(self.state, attr, None)
            if history is not None and len(history) > window:
                setattr(self.state, attr, history[-window:])

    def coherence_function(self, V: float) -> float:
        """
        Bounded coherence function C(V) using governance_core coherence function.

        Delegates to canonical governance_core.coherence() function.
        """
        return coherence(V, self.state.unitaires_theta, DEFAULT_PARAMS)
    
    def compute_ethical_drift(self,
                             current_params: np.ndarray,
                             prev_params: Optional[np.ndarray]) -> float:
        """
        Computes ethical drift ||Δη||² from parameter changes.

        If no previous parameters, returns 0 (no drift yet).
        Otherwise: ||Δη||² = ||θ_t - θ_{t-1}||² / dim
        """
        if prev_params is None or len(current_params) != len(prev_params):
            return 0.0

        # Guard against empty parameter arrays (division by zero)
        if len(current_params) == 0:
            return 0.0

        # Check for NaN or inf in inputs
        if np.any(np.isnan(current_params)) or np.any(np.isinf(current_params)):
            return 0.0
        if np.any(np.isnan(prev_params)) or np.any(np.isinf(prev_params)):
            return 0.0

        delta = np.asarray(current_params - prev_params, dtype=np.float64)
        with np.errstate(over="ignore"):
            drift_squared = np.sum(delta ** 2) / len(delta)

        # Check for NaN/inf in result
        if np.isnan(drift_squared) or np.isinf(drift_squared):
            return 0.0

        return float(drift_squared)

    def detect_regime(self) -> str:
        """Detect current operational regime based on state and history."""
        return _detect_regime(self.state, behavioral=self._behavioral_state)
    
    def _record_sensor_divergence(self, sensor_eisv) -> None:
        """Record model<->body EISV divergence as an observability signal.

        FAIL-OPEN by contract. This is optional telemetry on the *mandatory*
        check-in path: update_dynamics() has already evolved the state and the
        governance verdict does not depend on anything written here. So any
        error must degrade to "no divergence recorded this cycle" and never
        propagate — an exception here would reject the whole check-in.

        Incident 2026-06-16 (#800/#803): an unguarded write on this exact path
        (`self._sensor_divergence_history.append(...)` plus a missing-attr load
        invariant) rejected check-ins fleet-wide. The init invariant is fixed
        in __init__; this guard closes the broader class — a *future* fault in
        eisv_divergence() or the record shape must not take check-ins down.
        Mirrors the calibration-penalty fail-safe in update_dynamics().
        """
        self._last_sensor_divergence = None
        if sensor_eisv is None:
            return
        try:
            div = eisv_divergence(sensor_eisv, self.state.unitaires_state)
            # Stamp + retain for trend reads (bounded by SENSOR_DIVERGENCE_HISTORY_MAX).
            div["at"] = datetime.now().isoformat()
            # Self-heal: a monitor restored bypassing __init__ — a pickle/cache
            # instance from before this attribute existed, or the Pi plugin's
            # older build — can lack the deque. Initialize it lazily (the reads
            # at runtime_queries already guard with getattr; this is the write).
            if not hasattr(self, "_sensor_divergence_history"):
                self._sensor_divergence_history = deque(maxlen=SENSOR_DIVERGENCE_HISTORY_MAX)
            self._sensor_divergence_history.append(div)
            self._last_sensor_divergence = div
            logger.debug(
                "sensor_divergence agent=%s magnitude=%.4f "
                "dE=%+.3f dI=%+.3f dS=%+.3f dV=%+.3f",
                self.agent_id,
                div["magnitude"], div["dE"], div["dI"], div["dS"], div["dV"],
            )
        except Exception:
            # Telemetry only — log and move on; the check-in must still succeed.
            logger.warning(
                "sensor_divergence recording failed for agent=%s; skipping this "
                "cycle (check-in unaffected)", self.agent_id, exc_info=True,
            )
            self._last_sensor_divergence = None

    def update_dynamics(self,
                       agent_state: Dict,
                       dt: float = None) -> None:
        """
        Updates UNITARES dynamics for one timestep using governance_core engine.

        This now uses the canonical governance_core.step_state() implementation.

        Agent state should contain:
        - parameters: array-like, agent parameters
        - ethical_drift: array-like, ethical signals (delta_eta)
        - (optional) response_text: str for risk estimation
        - (optional) complexity: float
        """
        if dt is None:
            dt = config.DT

        # Extract agent information
        parameters = np.array(agent_state.get('parameters', []))
        ethical_signals = np.array(agent_state.get('ethical_drift', [0.0, 0.0, 0.0, 0.0]))

        # Validate and normalize ethical_drift (delta_eta) to list
        # Accept any length — drift_norm() handles variable-length vectors.
        # Governance computes 4 components; agents may send 3. Both are valid.
        if len(ethical_signals) == 0:
            delta_eta = [0.0, 0.0, 0.0]
        else:
            delta_eta = ethical_signals.tolist()

        # Replace NaN/inf with zeros
        delta_eta = [0.0 if (np.isnan(x) or np.isinf(x)) else float(x) for x in delta_eta]

        # Extract complexity (default to 0.5 if not provided)
        complexity = agent_state.get('complexity', 0.5)
        if complexity is None or np.isnan(complexity) or np.isinf(complexity):
            complexity = 0.5
        complexity = float(np.clip(complexity, 0.0, 1.0))  # Ensure in valid range

        # Extract sensor EISV for spring coupling (agents that publish sensor_eisv)
        sensor_eisv = None
        raw_sensor_eisv = agent_state.get('sensor_eisv')
        if raw_sensor_eisv and isinstance(raw_sensor_eisv, dict):
            try:
                sensor_eisv = State(
                    E=float(np.clip(raw_sensor_eisv.get('E', 0.5), 0.0, 1.0)),
                    I=float(np.clip(raw_sensor_eisv.get('I', 0.5), 0.0, 1.0)),
                    S=float(np.clip(raw_sensor_eisv.get('S', 0.2), 0.001, 1.0)),
                    V=float(np.clip(raw_sensor_eisv.get('V', 0.0), -1.0, 1.0)),
                )
            except (TypeError, ValueError, KeyError):
                sensor_eisv = None

        # Per-source coupling policy: decide whether THIS sensor source may spring
        # into the ODE. `sensor_eisv` (the full submitted reading) is always kept
        # for divergence; only `coupling_sensor` (possibly None) reaches the ODE.
        # This is how the Lumen-only cut works: UNITARES_SENSOR_COUPLING=behavioral_only
        # nulls physical coupling while leaving the behavioral fleet anchored.
        from governance_core.parameters import sensor_coupling_allows
        sensor_source = agent_state.get('sensor_eisv_source')
        coupling_sensor = sensor_eisv if (
            sensor_eisv is not None and sensor_coupling_allows(sensor_source)
        ) else None

        # Store parameters for potential future use (deprecated - not used in coherence)
        # Note: param_coherence removed in favor of pure thermodynamic signal
        self.prev_parameters = parameters.copy() if len(parameters) > 0 else None

        # Use governance_core step_state() to evolve state (CANONICAL DYNAMICS)
        # Params are profile-selectable (default vs v4.1 paper-aligned) via:
        # - UNITARES_PARAMS_PROFILE=default|v41
        # - UNITARES_PARAMS_JSON='{"beta_I": 0.05, ...}'
        active_params = get_active_params()

        # Apply per-agent delta from adaptive governor
        if self.adaptive_governor is not None:
            from dataclasses import replace as dataclass_replace
            active_params = dataclass_replace(
                active_params, delta=self.adaptive_governor.state.delta
            )

        # Calibration feedback: overconfidence raises entropy S
        # When agents claim high confidence but achieve low trajectory health,
        # the calibration error becomes a thermodynamic price on S.
        calibration_penalty = 0.0
        try:
            metrics = calibration_checker.compute_calibration_metrics()
            if metrics:
                # Find max overconfidence across bins with sufficient data
                for bin_metrics in metrics.values():
                    if bin_metrics.count >= 2:
                        # Positive = expected > actual = overconfident
                        overconfidence = bin_metrics.expected_accuracy - bin_metrics.accuracy
                        if overconfidence > 0:
                            # Dampen penalty for low-sample bins (full weight at 5+ samples)
                            sample_weight = min(1.0, bin_metrics.count / 5.0)
                            calibration_penalty = max(calibration_penalty, 0.2 * overconfidence * sample_weight)
        except Exception:
            pass  # Fail-safe: no penalty if calibration unavailable

        # Stage A (EISV S-attractor calibration): per-class S rest target so the
        # ODE equilibrium lands on measured-healthy S instead of ~0.09. Off by
        # default (UNITARES_S_SETPOINT) — when off, s_setpoint stays 0.0 and the
        # dynamics are unchanged. Kept zero-cost on the off path (no class
        # resolution unless the flag is enabled).
        from config.governance_config import get_s_setpoint, s_setpoint_enabled
        s_setpoint = 0.0
        if s_setpoint_enabled():
            if not hasattr(self, "_resolved_agent_class"):
                self._resolved_agent_class = self._resolve_agent_class()
            s_setpoint = get_s_setpoint(self._resolved_agent_class)

        self.state.unitaires_state = step_state(
            state=self.state.unitaires_state,
            theta=self.state.unitaires_theta,
            delta_eta=delta_eta,
            dt=dt,
            noise_S=calibration_penalty,  # Overconfidence raises entropy
            params=active_params,
            complexity=complexity,  # Complexity now affects S dynamics
            sensor_eisv=coupling_sensor,  # per-source gated; None => no spring (still compared below)
            s_setpoint=s_setpoint,  # 0.0 unless UNITARES_S_SETPOINT enabled
        )

        # Compare, don't couple: record model<->body divergence as a signal.
        # The ODE above evolved as an independent predictor; the sensor EISV is
        # the measured body. Their disagreement (cf. allostatic load) is recorded
        # for observability rather than sprung away. Coupling is opt-in and
        # flag-gated inside step_state (see sensor_coupling_enabled()). This is
        # OPTIONAL telemetry on the mandatory check-in path, so it is fail-open
        # (see _record_sensor_divergence) — the verdict is already decided above.
        self._record_sensor_divergence(sensor_eisv)

        # Epistemic humility safeguard: Enforce entropy floor (S >= 0.001) always
        # Perfect equilibrium (S=0.0) is dangerous and brittle - maintain epistemic humility at all times
        if self.state.unitaires_state.S < 0.001:
            # Maintain epistemic humility: "I could be wrong about something I can't see"
            self.state.unitaires_state.S = 0.001

        # V bounds: soft barrier in _derivatives() handles this now; clip is safety net only
        # (kept in ODE integrators as defense-in-depth)

        # Update coherence from governance_core coherence function (pure thermodynamic)
        # Removed param_coherence blend - using pure C(V) signal for honest calibration
        C_V = coherence(self.state.V, self.state.unitaires_theta, active_params)
        self.state.coherence = C_V
        self.state.coherence = np.clip(self.state.coherence, 0.0, 1.0)

        # Update history
        # Record full state history (E, I, S, V, coherence)
        self.state.E_history.append(float(self.state.E))
        self.state.I_history.append(float(self.state.I))
        self.state.S_history.append(float(self.state.S))
        self.state.V_history.append(float(self.state.V))
        self.state.coherence_history.append(float(self.state.coherence))
        self.state.timestamp_history.append(datetime.now().isoformat())  # Track timestamp
        
        # Track current lambda1 value (even if not updated this cycle)
        # Records the value active during THIS ODE step; the PI controller
        # in process_update() may change it afterward for the NEXT step.
        self.state.lambda1_history.append(float(self.state.lambda1))
        
        # Detect and track regime (operational state)
        
        previous_regime = self.state.regime
        new_regime = self.detect_regime()
        self.state.regime = new_regime
        self.state.regime_history.append(new_regime)
        
        # Log regime transitions
        if new_regime != previous_regime:
            logger.info(
                f"Regime transition for {self.agent_id}: {previous_regime} → {new_regime} "
                f"(I={self.state.I:.3f}, S={self.state.S:.3f}, V={self.state.V:.3f})"
            )
        
        # Log STABLE state events (when first reached)
        if new_regime == "STABLE" and previous_regime != "STABLE":
            logger.info(
                f"Reached STABLE state for {self.agent_id} "
                f"(I={self.state.I:.3f}, S={self.state.S:.3f}) - "
                f"system will naturally transition when state changes"
            )

        # =================================================================
        # HCK v3.0: Compute and store update coherence ρ(t) and CE
        # Also compute 4D state velocity for ethical drift signal injection
        # =================================================================
        import math as _math
        if all(v is not None for v in [self._prev_E, self._prev_I, self._prev_S, self._prev_V]):
            delta_E = float(self.state.E) - self._prev_E
            delta_I = float(self.state.I) - self._prev_I
            delta_S = float(self.state.S) - self._prev_S
            delta_V = float(self.state.V) - self._prev_V
            rho = self.compute_update_coherence(delta_E, delta_I)
            self._last_state_velocity = _math.sqrt(
                delta_E**2 + delta_I**2 + delta_S**2 + delta_V**2
            )
        else:
            rho = 0.0  # First update, no previous state
            self._last_state_velocity = 0.0

        # Update prev values for next iteration
        self._prev_E = float(self.state.E)
        self._prev_I = float(self.state.I)
        self._prev_S = float(self.state.S)
        self._prev_V = float(self.state.V)

        # Store ρ(t) in state
        self.state.current_rho = rho
        self.state.rho_history.append(rho)

        # Compute Continuity Energy CE from state history snapshots
        # Build state snapshot list for CE computation
        state_snapshots = []
        history_len = min(10, len(self.state.E_history))
        for i in range(-history_len, 0):
            try:
                snapshot = {
                    'E': self.state.E_history[i],
                    'I': self.state.I_history[i],
                    'S': self.state.S_history[i],
                    'V': self.state.V_history[i],
                    'decision': self.state.decision_history[i] if abs(i) <= len(self.state.decision_history) else None
                }
                state_snapshots.append(snapshot)
            except IndexError:
                pass

        CE = self.compute_continuity_energy(state_snapshots)
        self.state.CE_history.append(CE)

        # Trim all history arrays to window
        self._trim_histories()

        # Validate state after update (STRICT MODE - Issue #1 fix)
        is_valid, errors = self.state.validate()
        if not is_valid:
            # Categorize errors as critical (NaN, Inf) vs minor (bounds violations)
            critical_errors = []
            minor_errors = []

            for error in errors:
                if "NaN" in error or "Inf" in error:
                    critical_errors.append(error)
                else:
                    minor_errors.append(error)

            # CRITICAL ERRORS: Raise exception (don't auto-fix)
            # These indicate corrupt state that should not propagate
            if critical_errors:
                error_msg = (
                    f"CRITICAL: State validation failed for {self.agent_id} - "
                    f"corrupt state detected (NaN/Inf values). "
                    f"Errors: {', '.join(critical_errors)}"
                )
                logger.error(error_msg)
                raise ValueError(error_msg)

            # MINOR ERRORS: Log warning and auto-fix
            # These are bounds violations that can be safely clipped
            if minor_errors:
                logger.warning(f"State validation warnings for {self.agent_id}: {', '.join(minor_errors)}")
                # Auto-fix bounds violations by clipping
                if not (0.0 <= self.state.E <= 1.0):
                    self.state.unitaires_state.E = np.clip(self.state.E, 0.0, 1.0)
                    logger.info(f"Auto-fixed E to {self.state.E}")
                if not (0.0 <= self.state.I <= 1.0):
                    self.state.unitaires_state.I = np.clip(self.state.I, 0.0, 1.0)
                    logger.info(f"Auto-fixed I to {self.state.I}")
                if not (0.0 <= self.state.S <= 1.0):
                    self.state.unitaires_state.S = np.clip(self.state.S, 0.0, 1.0)
                    logger.info(f"Auto-fixed S to {self.state.S}")
                if not (-1.0 <= self.state.V <= 1.0):
                    self.state.unitaires_state.V = np.clip(self.state.V, -1.0, 1.0)
                    logger.info(f"Auto-fixed V to {self.state.V}")
                if not (0.0 <= self.state.coherence <= 1.0):
                    self.state.coherence = np.clip(self.state.coherence, 0.0, 1.0)
                    logger.info(f"Auto-fixed coherence to {self.state.coherence}")

        # Update time
        self.state.time += dt
        self.state.update_count += 1
    
    def check_void_state(self) -> bool:
        """Checks if system is in void state: |V| > threshold.

        RFC §7.13.6 PR 3 (interim safety net): resolves agent class once per
        monitor instance and passes it through so resident-class agents get
        the wider VOID_THRESHOLD_BY_CLASS override. Lookup is cached on the
        instance after first call. Failure to resolve is non-fatal — falls
        back to the default adaptive threshold.

        This wiring is scheduled for sunset at PR 8 once all residents have
        ported to lease-plane substrate_state and no resident remains in
        the monitor_decision pipeline.
        """
        if not hasattr(self, "_resolved_agent_class"):
            self._resolved_agent_class = self._resolve_agent_class()
        return _check_void_state(self.state, agent_class=self._resolved_agent_class)

    def _resolve_agent_class(self):
        """Best-effort lookup of this agent's class for void-threshold override.

        Returns None on any failure so the void-threshold falls back to
        adaptive default. Lookup is cached by `check_void_state` after first
        call to amortize the cost across check cycles.

        Resolution order:
          1. `state.agent_class` if pre-populated by an upstream loader.
          2. `agent_id` literal match against KNOWN_RESIDENT_LABELS — catches
             monitors constructed with a label string as agent_id (test pattern).
          3. **agent_metadata cache lookup** — production residents have
             agent_id = UUID, with `label` and `tags` set on the AgentMetadata
             entry. Pass that meta to `classify_agent` to resolve the class
             via the same logic the rest of the system uses. This was the
             missing path in the v0.11.3 PR 3 — without it, the canary tripped
             void_pause anyway because the UUID-form agent_id never matched
             the label set, so no override was applied. Caught 2026-05-04
             on Steward unpause smoke test (V=0.081, threshold=0.10 adaptive
             floor → void_active=true → pause).
          4. Synthetic meta from `state.label`/`state.tags` (legacy fallback).
        """
        explicit = getattr(self.state, "agent_class", None)
        if explicit:
            return explicit

        try:
            from src.grounding.class_indicator import (
                KNOWN_RESIDENT_LABELS,
                classify_agent,
            )
        except Exception:
            return None

        if self.agent_id in KNOWN_RESIDENT_LABELS:
            return self.agent_id

        # Production path: look up agent's metadata to find label/tags.
        # The agent_metadata cache is populated at gov-mcp startup via
        # `background_metadata_load` and refreshed lazily — it carries the
        # label and tags fields we need to classify.
        try:
            from src.agent_state import agent_metadata as _agent_metadata
            meta = _agent_metadata.get(self.agent_id)
        except Exception:
            meta = None

        if meta is not None:
            try:
                cls = classify_agent(meta)
                if cls != "default":
                    return cls
            except Exception:
                pass

        # Final fallback: synthesize meta from state-side fields if upstream
        # loaders populated them. Inert in production (state.label is rarely
        # set), but kept for tests and future upstream wiring.
        synthetic_meta = type("StateMeta", (), {
            "label": getattr(self.state, "label", None),
            "tags": getattr(self.state, "tags", None) or [],
        })()
        try:
            cls = classify_agent(synthetic_meta)
            return cls if cls != "default" else None
        except Exception:
            return None

    def _calculate_void_frequency(self) -> float:
        """Calculate void frequency from V history."""
        return _calculate_void_frequency(self.state)
    
    def update_lambda1(self) -> float:
        """Updates lambda1 using PI controller based on void frequency and coherence targets."""
        return _update_lambda1(self.state)
    
    def estimate_risk(self, agent_state: Dict, score_result: Dict = None) -> float:
        """Estimate risk score using governance_core phi_objective and verdict_from_phi."""
        return _estimate_risk(self.state, agent_state, score_result)
    
    def make_decision(self, risk_score: float, unitares_verdict: str = None,
                      response_tier: str = None, oscillation_state: 'OscillationState' = None) -> Dict:
        """Makes autonomous governance decision using UNITARES verdict and CIRS response tier."""
        return _make_decision(self.state, risk_score, unitares_verdict, response_tier, oscillation_state)
    
    def simulate_update(self, agent_state: Dict, confidence: Optional[float] = None) -> Dict:
        """
        Dry-run governance cycle: Returns decision without persisting state.
        
        Useful for testing decisions before committing. Does NOT modify state.
        
        Optimized: Uses shallow copy + selective deep copy instead of full deepcopy.
        Only deep copies mutable collections (history lists) and nested dataclasses.
        This is 10-100x faster for agents with long histories.
        
        Args:
            agent_state: Agent state dict with parameters, ethical_drift, response_text, complexity
            confidence: Confidence level [0, 1] for this update. If None (default),
                        confidence is derived from observed outcomes + EISV uncertainty.
        
        Returns:
            Same format as process_update, but state is NOT modified
        """
        import copy
        
        # Save current state (shallow copy is sufficient for reference)
        saved_state = self.state
        saved_prev_params = self.prev_parameters
        saved_last_update = self.last_update
        saved_prev_verdict = self._prev_verdict_action
        saved_prev_norm = self._prev_drift_norm
        saved_prev_conf = self._prev_confidence
        # The novelty gate for the mirror's complexity line mutates during
        # result building (monitor_result._complexity_divergence_novel); a
        # simulation must not consume the agent's first real surfacing.
        saved_last_gap = self._last_surfaced_complexity_gap
        
        try:
            # OPTIMIZED: Shallow copy + selective deep copy
            # Shallow copy the state object (fast)
            temp_state = copy.copy(self.state)
            
            # Deep copy only mutable collections (history lists) - these get appended to
            temp_state.E_history = copy.deepcopy(self.state.E_history)
            temp_state.I_history = copy.deepcopy(self.state.I_history)
            temp_state.S_history = copy.deepcopy(self.state.S_history)
            temp_state.V_history = copy.deepcopy(self.state.V_history)
            temp_state.coherence_history = copy.deepcopy(self.state.coherence_history)
            temp_state.risk_history = copy.deepcopy(self.state.risk_history)
            temp_state.decision_history = copy.deepcopy(self.state.decision_history)
            temp_state.timestamp_history = copy.deepcopy(self.state.timestamp_history)
            temp_state.lambda1_history = copy.deepcopy(self.state.lambda1_history)
            
            # Deep copy nested dataclasses (they get modified during update_dynamics)
            temp_state.unitaires_state = copy.deepcopy(self.state.unitaires_state)
            temp_state.unitaires_theta = copy.deepcopy(self.state.unitaires_theta)
            
            # Shallow copy prev_parameters (it's a simple dict or None)
            temp_prev_params = copy.deepcopy(self.prev_parameters) if self.prev_parameters is not None else None
            
            # Swap to temporary state
            self.state = temp_state
            self.prev_parameters = temp_prev_params
            
            # Run full governance cycle (modifies temp_state) with confidence
            result = self.process_update(agent_state, confidence=confidence)
            
            # Mark as simulation
            result['simulation'] = True
            result['note'] = 'This was a simulation - state was not modified'
            
            return result
        finally:
            # Always restore original state, even if error occurred
            self.state = saved_state
            self.prev_parameters = saved_prev_params
            self.last_update = saved_last_update
            self._prev_verdict_action = saved_prev_verdict
            self._prev_drift_norm = saved_prev_norm
            self._prev_confidence = saved_prev_conf
            self._last_surfaced_complexity_gap = saved_last_gap
    
    def process_update(self, agent_state: Dict, confidence: Optional[float] = None, task_type: str = "mixed") -> Dict:
        """
        Complete governance cycle: Update → Adapt → Decide

        Args:
            agent_state: Agent state dict with parameters, ethical_drift, response_text, complexity
            confidence: Confidence level [0, 1] for this update. If None (default),
                        confidence is derived from thermodynamic state (I, S, C, V).
                        Pass explicit value to override derivation.
            task_type: Task type context ("convergent", "divergent", "mixed").
                      Affects S=0 interpretation: convergent S=0 is healthy (standardization),
                      divergent S=0 may indicate lack of divergence.

        This is the main API method called by the MCP server.

        Confidence derivation (when not explicitly provided):
            - High Integrity (I) → high confidence
            - Low Entropy (S) → high confidence
            - High Coherence (C) → high confidence
            - Low |Void| (V) → high confidence

        Returns:
        {
            'status': 'healthy' | 'moderate' | 'critical',
            'decision': {...},
            'metrics': {...},
        }
        """
        # Per-process cycle count for the warmup STRUCTURAL grace (see
        # _maybe_warmup_structural_suppress). Reset to 0 on every process start.
        self._process_local_updates += 1

        # Compute elapsed time for gap-aware decay scaling
        now = datetime.now()
        # Guard against NTP step-back / clock skew: dt must never be negative.
        elapsed_seconds = max(0.0, (now - self.last_update).total_seconds())
        self.last_update = now

        # Scale dt proportionally to elapsed time vs expected cadence.
        # Floor at DT (rapid updates stay at base), cap at DT_MAX (Euler stability).
        scaled_dt = elapsed_seconds * (config.DT / config.DT_EXPECTED_INTERVAL)
        effective_dt = max(config.DT, min(scaled_dt, config.DT_MAX))

        # Saturation event: gap exceeds the cadence band that the linear
        # scaling can represent. Above this threshold a 17h gap and a 30h
        # gap integrate identically — operator-visible signal that decay
        # is no longer gap-proportional. See task #7 for semantics decision.
        #
        # We also arm the gap-recovery window here. The next N attestations
        # may run on stale/transient state (e.g., MacBook clamshell sleep-wake
        # produced false high-risk verdicts on Lumen/Sentinel/Watcher 2026-05-08
        # to 2026-05-12); during the window we downgrade 'pause' decisions
        # so the circuit breaker doesn't trip on a sleep-wake artifact.
        if scaled_dt > config.DT_MAX:
            self._gap_recovery_cycles_remaining = config.GAP_RECOVERY_CYCLES
            logger.info(
                f"[DT_MAX saturation] {self.agent_id}: elapsed={elapsed_seconds:.1f}s "
                f"clipped to dt={config.DT_MAX} (linear scaling would give {scaled_dt:.2f}); "
                f"arming gap-recovery for {config.GAP_RECOVERY_CYCLES} cycles"
            )

        # === DUAL-LOG GROUNDING (Patent: Dual-Log Architecture) ===
        # Process through continuity layer to get grounded metrics.
        # This compares operational (server-derived) vs reflective (agent-reported)
        # to produce grounded complexity that feeds into EISV dynamics.
        response_text = agent_state.get('response_text', '')
        self_complexity = agent_state.get('complexity')
        self_confidence = confidence  # May be None at this point
        client_session_id = agent_state.get('client_session_id', '')
        
        # Extract tool usage stats for complexity grounding
        tu_stats = None
        try:
            from src.tool_usage_tracker import get_tool_usage_tracker
            raw_stats = get_tool_usage_tracker().get_usage_stats(
                agent_id=self.agent_id, window_hours=1,
            )
            tu_total = raw_stats.get("total_calls", 0)
            if tu_total > 0:
                tu_failed = sum(
                    t.get("error_count", 0)
                    for t in raw_stats.get("tools", {}).values()
                )
                tu_stats = {
                    "unique_tools": raw_stats.get("unique_tools", 0),
                    "total_calls": tu_total,
                    "error_rate": tu_failed / tu_total,
                    "files_modified": agent_state.get("files_modified", 0),
                }
        except Exception:
            pass  # Fail-safe

        continuity_metrics = self.continuity_layer.process_update(
            response_text=response_text,
            self_complexity=self_complexity,
            self_confidence=self_confidence,
            client_session_id=client_session_id,
            task_type=task_type,
            tool_usage_stats=tu_stats,
        )
        
        # Store for response and downstream use
        self._last_continuity_metrics = continuity_metrics
        
        # Check restorative balance (detect overload)
        self.restorative_monitor.record(continuity_metrics)
        restorative_status = self.restorative_monitor.check()
        self._last_restorative_status = restorative_status
        
        # Use GROUNDED complexity instead of self-reported
        grounded_agent_state = agent_state.copy()
        grounded_agent_state['complexity'] = continuity_metrics.derived_complexity

        # Log grounding if significant divergence
        if continuity_metrics.complexity_divergence > 0.2:
            logger.info(
                f"Dual-log grounding for {self.agent_id}: "
                f"self={self_complexity}, derived={continuity_metrics.derived_complexity:.3f}, "
                f"divergence={continuity_metrics.complexity_divergence:.3f}"
            )

        # === CONCRETE ETHICAL DRIFT (Patent: De-abstracted Δη) ===
        drift_vector, agent_drift_norm = self._compute_drift_vector(
            grounded_agent_state=grounded_agent_state,
            agent_state=agent_state,
            confidence=self_confidence,
            task_type=task_type,
            continuity_metrics=continuity_metrics,
        )

        # === BEHAVIORAL EISV (observation-first, no ODE) ===
        # Extract observations from existing signals for behavioral state
        sensor_eisv = agent_state.get('sensor_eisv')
        if sensor_eisv:
            # Use externally supplied sensor EISV directly when available
            beh_E_obs = float(sensor_eisv.get('E', 0.5))
            beh_I_obs = float(sensor_eisv.get('I', 0.5))
            beh_S_obs = float(sensor_eisv.get('S', 0.2))
        else:
            # Non-embodied agents: compute from behavioral_sensor
            beh_sensor = compute_behavioral_sensor_eisv(
                decision_history=self.state.decision_history,
                coherence_history=self.state.coherence_history,
                regime_history=self.state.regime_history,
                E_history=self.state.E_history,
                I_history=self.state.I_history,
                S_history=self.state.S_history,
                V_history=self.state.V_history,
                calibration_error=getattr(drift_vector, 'calibration_deviation', None),
                drift_norm=getattr(drift_vector, 'norm', None),
                complexity_divergence=continuity_metrics.complexity_divergence,
                continuity_E_input=continuity_metrics.E_input,
                continuity_I_input=continuity_metrics.I_input,
                continuity_S_input=continuity_metrics.S_input,
                outcome_history=self._cached_outcome_history,
                tool_error_rate=tu_stats.get('error_rate') if tu_stats else None,
            )
            if beh_sensor:
                beh_E_obs = beh_sensor['E']
                beh_I_obs = beh_sensor['I']
                beh_S_obs = beh_sensor['S']
            else:
                # Insufficient history — use continuity layer inputs as fallback
                beh_E_obs = continuity_metrics.E_input if continuity_metrics.E_input is not None else 0.5
                beh_I_obs = continuity_metrics.I_input if continuity_metrics.I_input is not None else 0.5
                beh_S_obs = continuity_metrics.S_input if continuity_metrics.S_input is not None else 0.2

        self._behavioral_state.update(beh_E_obs, beh_I_obs, beh_S_obs)

        # Assess behavioral state with auxiliary signals
        behavioral_assessment = assess_behavioral_state(
            state=self._behavioral_state,
            rho=getattr(self.state, 'current_rho', 0.0),
            continuity_energy=self.state.CE_history[-1] if self.state.CE_history else 0.0,
            agent_context={'task_type': task_type},
        )
        self._last_behavioral_verdict = behavioral_assessment.verdict

        # ── ODE Dynamics (Diagnostic) ──
        # The ODE engine runs in parallel but does NOT drive verdicts when
        # BEHAVIORAL_VERDICT_ENABLED is True (default). Primary verdicts
        # come from behavioral assessment (EMA + z-score deviations).
        # ODE provides: phi objective, regime detection, historical continuity.
        self.update_dynamics(grounded_agent_state, dt=effective_dt)

        # Step 1b: Confidence handling
        # When agent reports confidence, use it as-is — capping created calibration
        # circularity (derived from EISV, compared against EISV-derived health).
        # When no confidence reported, derive from observed tool outcomes as fallback.
        if confidence is None:
            confidence, confidence_metadata = derive_confidence(
                self.state,
                agent_id=self.agent_id
            )
        else:
            confidence = float(confidence)
            # Agent-reported confidence is taken at face value, but it has NOT
            # been validated against any calibration history. Claiming
            # reliability "high" here contradicted calibration_samples: 0 in the
            # surfaced response (dogfood 2026-06-13): with zero backing samples
            # the reliability of the *estimate* is unknown, not high.
            confidence_metadata = {
                'source': 'external',
                'reliability': 'unknown',
                'calibration_applied': False,
                'calibration_samples': 0,
                'external_provided': True,
                'value': confidence,
                'honesty_note': (
                    'Agent-reported confidence taken as-is; not validated '
                    'against calibration history (0 samples).'
                ),
            }

        # Store confidence and metadata for audit logging and transparency
        self.current_confidence = confidence
        self.confidence_metadata = confidence_metadata
        
        # Step 2: Check void state
        void_active = self.check_void_state()
        
        # Step 3: Update λ₁ (every N updates) - WITH CONFIDENCE GATING
        # Updated to every 5 cycles for faster adaptation (was 10)
        lambda1_skipped = False
        if self.state.update_count % 5 == 0:  # Update λ₁ every 5 cycles
            # Gate lambda1 updates based on confidence
            # Relax threshold proportionally when coherence drops significantly
            # below target — prevents feedback loop where low confidence blocks
            # the controller that would fix declining coherence
            effective_conf_threshold = config.CONTROLLER_CONFIDENCE_THRESHOLD
            if len(self.state.coherence_history) >= 3:
                coherence_deficit = config.TARGET_COHERENCE - self.state.coherence
                if coherence_deficit > 0.05:  # Only relax for meaningful drops
                    # Scale relaxation: 0.05 deficit → small relax, 0.15+ → max relax
                    relax_factor = min(1.0, (coherence_deficit - 0.05) / 0.10)
                    effective_conf_threshold = config.CONTROLLER_CONFIDENCE_THRESHOLD - 0.15 * relax_factor

            if confidence >= effective_conf_threshold:
                self.update_lambda1()
            else:
                # Skip lambda1 update due to low confidence
                lambda1_skipped = True
                self.state.lambda1_update_skips += 1
                
                # Log skip via audit logger
                audit_logger.log_lambda1_skip(
                    agent_id=self.agent_id,
                    confidence=confidence,
                    threshold=effective_conf_threshold,
                    update_count=self.state.update_count,
                    reason=f"confidence {confidence:.3f} < threshold {effective_conf_threshold}"
                )

                logger.debug(
                    f"Skipping λ₁ update for {self.agent_id}: "
                    f"confidence {confidence:.3f} < threshold {effective_conf_threshold}"
                )
        
        # Step 4: Estimate risk (also gets UNITARES verdict)
        phi, unitares_verdict, risk_score, task_type_adjustment, original_risk_score = (
            self._compute_phi_and_risk(grounded_agent_state, agent_state, task_type)
        )

        # ── Behavioral Verdict Override ──
        # Behavioral assessment can add independent evidence, but it must not
        # erase a worse self-attested phi/drift signal. Otherwise a maturing
        # behavioral EMA can invert risk during monotonically worsening
        # complexity/confidence/drift check-ins.
        from config.governance_config import GovernanceConfig as GovConfig
        if GovConfig.BEHAVIORAL_VERDICT_ENABLED and self._behavioral_state.confidence >= 0.3:
            beh_verdict_map = {"safe": "safe", "caution": "caution", "high-risk": "high-risk"}
            behavioral_verdict = beh_verdict_map.get(behavioral_assessment.verdict)
            unitares_verdict = _more_severe_verdict(unitares_verdict, behavioral_verdict)
            risk_score = max(float(risk_score), float(behavioral_assessment.risk))

        oscillation_state, response_tier, cirs_result, damping_result = self._run_cirs(
            risk_score=risk_score,
            unitares_verdict=unitares_verdict,
        )

        # Step 5: Make decision (using UNITARES verdict + CIRS oscillation state)
        decision = self.make_decision(
            risk_score,
            unitares_verdict=unitares_verdict,
            response_tier=response_tier,
            oscillation_state=oscillation_state
        )

        # Pre-flag gap-suppression so calibration, audit, and history can record
        # the *original* verdict truthfully — the actual decision mutation
        # happens after those recording paths so calibration doesn't learn from
        # a synthetic 'proceed' that the operator never intended.
        gap_suppression_pending = (
            self._gap_recovery_cycles_remaining > 0
            and decision.get('action') == 'pause'
        )

        trajectory_validation = self._run_calibration_recording(
            confidence=confidence,
            decision=decision,
            drift_vector=drift_vector,
        )

        # Log decision via audit logger (for accountability and transparency).
        # Records the un-suppressed decision so operators can later analyze
        # whether suppression masked real drift; gap_suppressed flag in details.
        audit_logger.log_auto_attest(
            agent_id=self.agent_id,
            confidence=confidence,
            ci_passed=False,  # CI status not available in governance_monitor
            risk_score=risk_score,
            decision=decision['action'],
            details={
                'reason': decision.get('reason', ''),
                'coherence': float(self.state.coherence),
                'void_active': void_active,
                'unitares_verdict': unitares_verdict,
                'gap_suppressed': gap_suppression_pending,
                'beh_obs': [round(beh_E_obs, 4), round(beh_I_obs, 4), round(beh_S_obs, 4)],
                'drift': {
                    'emotional': round(drift_vector.calibration_deviation, 4),
                    'epistemic': round(drift_vector.coherence_deviation, 4),
                    'behavioral': round(drift_vector.stability_deviation, 4),
                    'norm': round(drift_vector.norm, 4),
                },
                'continuity': {
                    'derived_cx': round(continuity_metrics.derived_complexity, 4),
                    'self_cx': round(continuity_metrics.self_complexity, 4) if continuity_metrics.self_complexity is not None else None,
                    'divergence': round(continuity_metrics.complexity_divergence, 4),
                    'E_input': round(continuity_metrics.E_input, 4),
                    'I_input': round(continuity_metrics.I_input, 4),
                    'S_input': round(continuity_metrics.S_input, 4),
                    'overconf': continuity_metrics.overconfidence_signal,
                    'underconf': continuity_metrics.underconfidence_signal,
                },
                'behavioral': {
                    'verdict': behavioral_assessment.verdict,
                    'health': behavioral_assessment.health,
                    'risk': behavioral_assessment.risk,
                    'components': behavioral_assessment.components,
                    'baselined': self._behavioral_state.is_baselined,
                },
            }
        )

        # Track decision history for governance auditing
        self.state.decision_history.append(decision.get('sub_action', decision['action']))
        if len(self.state.decision_history) > config.HISTORY_WINDOW:
            self.state.decision_history = self.state.decision_history[-config.HISTORY_WINDOW:]

        # Track verdict tier history (safe/caution/high-risk) — separate vocabulary
        # from decision_history's actions; both surface in observe summary so users
        # see governance verdicts even on agents whose only persisted history is
        # core.agent_state (DB-hydrated post PR #200).
        if isinstance(unitares_verdict, str) and unitares_verdict:
            self.state.verdict_history.append(unitares_verdict)
            if len(self.state.verdict_history) > config.HISTORY_WINDOW:
                self.state.verdict_history = self.state.verdict_history[-config.HISTORY_WINDOW:]

        # Gap-recovery suppression: applied last so recording paths above saw
        # the original 'pause'. This mutates decision['action'] to 'proceed'
        # for downstream enforcement (circuit breaker in agent_loop_detection),
        # records a dedicated audit event, and decrements the counter.
        decision = self._maybe_gap_suppress(
            decision, elapsed_seconds, risk_score, confidence
        )

        # Warmup structural grace: on the first few process-LOCAL cycles after a
        # restart, the cold ODE state can trip the void/coherence/basin floors
        # even for a healthy agent. Suppress those STRUCTURAL pauses only when the
        # restored behavioral baseline is established and says 'safe'. Applied
        # after gap-suppress and after the recording paths above (which saw the
        # original decision). See _maybe_warmup_structural_suppress.
        decision = self._maybe_warmup_structural_suppress(decision)

        # Determine overall status using health thresholds (aligned with health_checker)
        # Use same thresholds as health_checker for consistency: risk_healthy_max=0.35, risk_moderate_max=0.60
        from src.health_thresholds import HealthThresholds
        health_checker = HealthThresholds()
        
        if void_active or self.state.coherence < config.COHERENCE_CRITICAL_THRESHOLD:
            status = 'critical'
        elif risk_score >= health_checker.risk_moderate_max:  # >= 0.60: critical
            status = 'critical'
        elif risk_score >= health_checker.risk_healthy_max:  # 0.35-0.60: moderate
            status = 'moderate'
        else:  # < 0.35: healthy
            status = 'healthy'
        
        # Build metrics dict
        # Primary EISV: behavioral (per-agent EMA observations) when confident,
        # ODE fallback for new agents. ODE values preserved in 'ode' sub-field.
        pE, pI, pS, pV = self.get_primary_eisv()
        metrics = {
            'E': pE,
            'I': pI,
            'S': pS,
            'V': pV,
            'coherence': float(self.state.coherence),
            'lambda1': float(self.state.lambda1),
            'risk_score': float(risk_score),  # Governance/operational risk (70% phi-based + 30% traditional)
            'phi': float(phi),  # Primary physics signal: Φ objective function
            'verdict': unitares_verdict,  # Primary governance signal: safe/caution/high-risk
            'void_active': bool(void_active),
            'regime': str(getattr(self.state, 'regime', 'divergence')),  # Operational regime: DIVERGENCE | TRANSITION | CONVERGENCE | STABLE (with fallback)
            'time': float(self.state.time),
            'updates': int(self.state.update_count),
            'confidence': float(confidence),
            'lambda1_skipped': lambda1_skipped
        }
        
        metrics['lambda1_update_skips'] = int(self.state.lambda1_update_skips)
        metrics['ode'] = {
            'E': float(self.state.E),
            'I': float(self.state.I),
            'S': float(self.state.S),
            'V': float(self.state.V),
        }

        # Expire stale predictions each cycle to prevent unbounded growth
        self.expire_old_predictions()

        return self._build_result(
            status=status,
            decision=decision,
            metrics=metrics,
            confidence=confidence,
            confidence_metadata=confidence_metadata,
            task_type_adjustment=task_type_adjustment,
            trajectory_validation=trajectory_validation,
            oscillation_state=oscillation_state,
            response_tier=response_tier,
            cirs_result=cirs_result,
            damping_result=damping_result,
            behavioral_assessment=behavioral_assessment,
        )
    
    def _compute_drift_vector(self, grounded_agent_state, agent_state, confidence, task_type, continuity_metrics):
        """Compute concrete ethical drift vector from measurable signals."""
        return _compute_drift_vector_impl(self, grounded_agent_state, agent_state, confidence, task_type, continuity_metrics)

    def _maybe_gap_suppress(self, decision: Dict, elapsed_seconds: float,
                             risk_score: float,
                             confidence: Optional[float] = None) -> Dict:
        """Suppress 'pause' decisions in the post-gap recovery window.

        Triggered by DT_MAX-saturating wall-clock gaps (e.g., MacBook clamshell
        sleep-wake). When the counter is non-zero and the decision is 'pause',
        downgrade to 'proceed', annotate the decision dict, and emit a
        dedicated audit event. Decrements the counter on every call while the
        window is open. Returns the (possibly modified) decision dict.
        """
        if self._gap_recovery_cycles_remaining <= 0:
            return decision
        if decision.get('action') == 'pause':
            original_reason = decision.get('reason', 'pause')
            decision['original_action'] = 'pause'
            decision['gap_suppressed'] = True
            decision['action'] = 'proceed'
            decision['reason'] = (
                f"gap-suppressed (was: {original_reason}); "
                f"cycles_remaining={self._gap_recovery_cycles_remaining - 1}"
            )
            audit_logger.log_attest_gap_suppressed(
                agent_id=self.agent_id,
                elapsed_seconds=elapsed_seconds,
                risk_score=risk_score,
                confidence=confidence if confidence is not None else getattr(self, 'current_confidence', None),
                original_reason=original_reason,
                cycles_remaining=self._gap_recovery_cycles_remaining - 1,
            )
        self._gap_recovery_cycles_remaining -= 1
        return decision

    # Structural pauses that are cold-ODE transients on restart and may be
    # suppressed during warmup. NOT included: 'risk_pause' (high-risk verdict)
    # and any oscillation-edged block — those reflect real signal.
    _WARMUP_SUPPRESSIBLE_SUBACTIONS = frozenset(
        {'void_pause', 'coherence_pause', 'basin_pause', 'cirs_block'}
    )

    def _maybe_warmup_structural_suppress(self, decision: Dict) -> Dict:
        """Suppress a STRUCTURAL pause during the post-restart warmup window.

        Rationale: on the first WARMUP_STRUCTURAL_GRACE_CYCLES process-LOCAL
        cycles, the ODE integrators are cold and state.coherence/V can briefly
        cross the void/coherence/basin floors even for a healthy agent (verified
        2026-06-03: a restored-but-healthy Lumen still tripped void_pause on the
        first post-restart check-in, with behavioral risk 0.00). We trust the
        restored behavioral baseline over the cold structural metric — but ONLY
        when that baseline is established AND says 'safe'. A genuinely-degraded
        agent has high behavioral risk or a non-safe verdict and is never
        suppressed; CIRS resonance (oscillation) and the high-risk verdict path
        are never suppressed. Fail-safe: any uncertainty leaves the pause intact.

        Composes with the DB behavioral-restore (#575), which is what makes the
        baseline available to trust here. Counter is per-process (not persisted).

        Operator-transparency note (same semantics as _maybe_gap_suppress): this
        runs AFTER the recording paths (calibration, log_auto_attest,
        decision_history.append), so those record the ORIGINAL structural pause —
        the `observe` decision_distribution will show e.g. a `void_pause` for a
        cycle the agent actually proceeded through. That's intentional (audit sees
        the true pre-suppress decision); the dedicated warmup_structural_suppressed
        audit event is the reconciling record.
        """
        if not config.WARMUP_STRUCTURAL_GRACE_ENABLED:
            return decision
        if self._process_local_updates > config.WARMUP_STRUCTURAL_GRACE_CYCLES:
            return decision
        if decision.get('action') != 'pause':
            return decision
        if decision.get('sub_action') not in self._WARMUP_SUPPRESSIBLE_SUBACTIONS:
            return decision
        # CIRS resonance is accumulated real signal, never a cold-start artifact.
        if decision.get('nearest_edge') == 'oscillation':
            return decision
        # Only trust the behavioral signal when its baseline is established AND
        # it independently judges the agent safe. Fresh/unbaselined behavioral
        # (the universal-threshold regime) is exactly what NOT to trust here.
        bs = self._behavioral_state
        if not bs.is_baselined:
            return decision
        if self._last_behavioral_verdict != 'safe':
            return decision

        original_reason = decision.get('reason', 'pause')
        original_sub = decision.get('sub_action')
        decision['original_action'] = 'pause'
        decision['warmup_structural_suppressed'] = True
        decision['action'] = 'proceed'
        decision['reason'] = (
            f"warmup-structural-suppressed (was: {original_reason}); "
            f"behavioral baselined+safe overrides cold structural metric; "
            f"process_cycle={self._process_local_updates}/"
            f"{config.WARMUP_STRUCTURAL_GRACE_CYCLES}"
        )
        try:
            audit_logger.log_warmup_structural_suppressed(
                agent_id=self.agent_id,
                sub_action=original_sub,
                original_reason=original_reason,
                process_cycle=self._process_local_updates,
                coherence=self.state.coherence,
                void=self.state.V,
            )
        except Exception:
            logger.debug("warmup_structural_suppressed audit log skipped", exc_info=True)
        return decision

    def _compute_phi_and_risk(self, grounded_agent_state, agent_state, task_type):
        """Compute phi objective, UNITARES verdict, risk score with task-type adjustments."""
        return _compute_phi_and_risk_impl(self, grounded_agent_state, agent_state, task_type)

    def _run_cirs(self, risk_score, unitares_verdict):
        """Run CIRS oscillation detection and resonance damping."""
        return _run_cirs_impl(self, risk_score, unitares_verdict)

    def _run_calibration_recording(self, confidence, decision, drift_vector):
        """Retrospective trajectory validation + strategic/tactical calibration."""
        return _run_calibration_recording_impl(self, confidence, decision, drift_vector)

    # ------------------------------------------------------------------
    # Tactical prediction registry
    #
    # Mints per-check-in ids so outcome_event can reference a specific
    # (confidence, timestamp) pair exactly instead of relying on the
    # _prev_confidence temporal proxy. The registry is in-memory only;
    # orphaned entries are expired by TTL. See sequential_calibration.py
    # module docstring for how this feeds the anytime-valid e-process.
    # ------------------------------------------------------------------

    def register_tactical_prediction(self, confidence: float, *, decision_action: Optional[str] = None) -> str:
        """Mint a prediction id for this (agent, confidence) pair and register it."""
        pid = _register_prediction(
            self._open_predictions, confidence,
            decision_action=decision_action,
            prediction_ttl_seconds=self._prediction_ttl_seconds,
        )
        self._last_prediction_id = pid
        return pid

    def lookup_prediction(self, prediction_id: str) -> Optional[Dict[str, Any]]:
        """Return the registered record for prediction_id, or None if unknown."""
        return _lookup_prediction(self._open_predictions, prediction_id)

    def consume_prediction(self, prediction_id: str) -> Optional[Dict[str, Any]]:
        """Mark a prediction as consumed and return its record.

        Forwards the live TTL config (self._prediction_ttl_seconds) so any
        per-agent override is honored. Without this forwarding the v2
        per-agent-class TTL would silently fall back to the module default.
        """
        return _consume_prediction(
            self._open_predictions,
            prediction_id,
            ttl_seconds=self._prediction_ttl_seconds,
        )

    def expire_old_predictions(self, ttl_seconds: Optional[float] = None) -> int:
        """Drop prediction records older than ttl_seconds. Returns count removed."""
        ttl = float(ttl_seconds if ttl_seconds is not None else self._prediction_ttl_seconds)
        removed = _expire_predictions(self._open_predictions, ttl)
        if self._last_prediction_id and self._last_prediction_id not in self._open_predictions:
            self._last_prediction_id = None
        return removed

    def _build_result(self, status, decision, metrics, confidence, confidence_metadata,
                       task_type_adjustment, trajectory_validation, oscillation_state,
                       response_tier, cirs_result, damping_result, behavioral_assessment=None):
        """Assemble the final result dict returned by process_update()."""
        return _build_result_impl(
            self, status, decision, metrics, confidence, confidence_metadata,
            task_type_adjustment, trajectory_validation, oscillation_state,
            response_tier, cirs_result, damping_result, behavioral_assessment,
        )

    def get_primary_eisv(self) -> tuple:
        """Primary EISV: behavioral when confident, ODE fallback.

        Returns (E, I, S, V) from the behavioral state if its confidence
        is >= 0.3, otherwise from the ODE state. This centralizes the
        behavioral-first policy so all consumers use the same source.
        """
        if self._behavioral_state.confidence >= 0.3:
            b = self._behavioral_state
            return float(b.E), float(b.I), float(b.S), float(b.V)
        return float(self.state.E), float(self.state.I), float(self.state.S), float(self.state.V)

    # Metrics and export: delegating to src/monitor_metrics.py
    def get_metrics(self, include_state: bool = True) -> Dict:
        """Returns current governance metrics."""
        return _get_monitor_metrics(self, include_state)

    get_eisv_labels = staticmethod(_get_eisv_labels)

    def export_history(self, format: str = 'json') -> str:
        """Exports complete history for analysis."""
        return _export_monitor_history(self, format)


# Example usage
if __name__ == "__main__":
    # Create monitor for test agent
    monitor = UNITARESMonitor(agent_id="test_agent")
    
    # Simulate some updates
    for i in range(100):
        agent_state = {
            'parameters': np.random.randn(128) * 0.01,  # Small random changes
            'ethical_drift': np.random.rand(3) * 0.1,
            'response_text': "This is a test response." * (i % 10),
            'complexity': 0.3 + 0.1 * (i % 5)
        }
        
        result = monitor.process_update(agent_state)
        
        if i % 20 == 0:
            logger.debug(f"\n[Update {i}]")
            logger.debug(f"  Status: {result['status']}")
            logger.debug(f"  Decision: {result['decision']['action']}")
            logger.debug(f"  Metrics: E={result['metrics']['E']:.3f}, "
                          f"I={result['metrics']['I']:.3f}, S={result['metrics']['S']:.3f}, "
                          f"V={result['metrics']['V']:.3f}, coherence={result['metrics']['coherence']:.3f}, "
                          f"λ₁={result['metrics']['lambda1']:.3f}")
    
    # Get final metrics
    logger.info("\n" + "="*60)
    logger.info("Final Metrics:")
    logger.info(json.dumps(monitor.get_metrics(), indent=2))

"""
Governance State Module

Wrapper around UNITARES Phase-3 State with additional tracking and history.
"""

import numpy as np
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from governance_core import (
    State, Theta,
    DEFAULT_STATE, DEFAULT_THETA,
    lambda1 as lambda1_from_theta
)
from governance_core.parameters import get_active_params


@dataclass
class GovernanceState:
    """Wrapper around UNITARES Phase-3 State with additional tracking"""
    
    # UNITARES Phase-3 state (internal engine)
    unitaires_state: State = field(default_factory=lambda: State(
        E=DEFAULT_STATE.E,
        I=DEFAULT_STATE.I,
        S=DEFAULT_STATE.S,
        V=DEFAULT_STATE.V
    ))
    unitaires_theta: Theta = field(default_factory=lambda: Theta(
        C1=DEFAULT_THETA.C1,
        eta1=DEFAULT_THETA.eta1
    ))
    
    # Derived metrics (computed from UNITARES state)
    coherence: float = 0.5      # Computed from UNITARES coherence function (midpoint default)
    void_active: bool = False     # Whether E-I imbalance exceeds threshold (|V| > threshold)
    
    # History tracking
    time: float = 0.0
    update_count: int = 0
    
    # Regime tracking (operational state detection)
    regime: str = "divergence"  # DIVERGENCE | TRANSITION | CONVERGENCE | STABLE
    regime_history: List[str] = field(default_factory=list)  # Track regime over time
    locked_persistence_count: int = 0  # Count consecutive steps at STABLE threshold
    
    # Rolling statistics for adaptive thresholds
    E_history: List[float] = field(default_factory=list)  # Energy history
    I_history: List[float] = field(default_factory=list)  # Information integrity history
    S_history: List[float] = field(default_factory=list)  # Entropy history
    V_history: List[float] = field(default_factory=list)  # E-I imbalance integral history
    coherence_history: List[float] = field(default_factory=list)
    risk_history: List[float] = field(default_factory=list)
    decision_history: List[str] = field(default_factory=list)  # Track approve/reflect/reject decisions
    verdict_history: List[str] = field(default_factory=list)  # Track safe/caution/high-risk EISV verdict tier
    timestamp_history: List[str] = field(default_factory=list)  # Track timestamps for each update
    lambda1_history: List[float] = field(default_factory=list)  # Track lambda1 adaptation over time
    
    # PI controller state
    pi_integral: float = 0.0  # Integral term state for PI controller (anti-windup protected)

    # HCK v3.0: Update coherence and continuity energy tracking
    rho_history: List[float] = field(default_factory=list)  # Update coherence ρ(t) history
    CE_history: List[float] = field(default_factory=list)   # Continuity Energy history
    current_rho: float = 0.0  # Current update coherence value

    # CIRS v0.1: Oscillation tracking
    oi_history: List[float] = field(default_factory=list)   # Oscillation Index history
    resonance_events: int = 0  # Count of resonance detections
    damping_applied_count: int = 0  # Count of damping applications

    # Lambda1 controller: skip tracking
    lambda1_update_skips: int = 0  # Count of lambda1 updates skipped due to low confidence
    
    # Compatibility: expose E, I, S, V as properties for backward compatibility
    @property
    def E(self) -> float:
        return self.unitaires_state.E
    
    @property
    def I(self) -> float:
        return self.unitaires_state.I
    
    @property
    def S(self) -> float:
        return self.unitaires_state.S
    
    @property
    def V(self) -> float:
        return self.unitaires_state.V
    
    @property
    def lambda1(self) -> float:
        """Get lambda1 from UNITARES theta using governance_core (adaptive via eta1)"""
        # Pass lambda1 bounds from config to enable adaptive control
        from config.governance_config import config
        active_params = get_active_params()
        return lambda1_from_theta(
            self.unitaires_theta, 
            active_params,
            lambda1_min=config.LAMBDA1_MIN,
            lambda1_max=config.LAMBDA1_MAX
        )
    
    def to_dict(self) -> Dict:
        """Export state as dictionary"""
        return {
            'E': float(self.E),
            'I': float(self.I),
            'S': float(self.S),
            'V': float(self.V),
            'coherence': float(self.coherence),
            'lambda1': float(self.lambda1),
            'void_active': bool(self.void_active),
            'regime': str(self.regime),  # Include current regime
            'time': float(self.time),
            'update_count': int(self.update_count)
        }
    
    def to_dict_with_history(self, max_history: int = 100) -> Dict:
        """
        Export state with history for persistence.

        Args:
            max_history: Maximum number of history entries to keep (default: 100)
                         This prevents unbounded state file growth.
        """
        # SECURITY: Cap history arrays to prevent disk exhaustion
        # Keep only the most recent max_history entries
        def cap_history(history_list, max_len=max_history):
            """Return last max_len entries from history"""
            if len(history_list) <= max_len:
                return history_list
            return history_list[-max_len:]

        return {
            # Current state values
            'E': float(self.E),
            'I': float(self.I),
            'S': float(self.S),
            'V': float(self.V),
            'coherence': float(self.coherence),
            'lambda1': float(self.lambda1),
            'void_active': bool(self.void_active),
            'time': float(self.time),
            'update_count': int(self.update_count),
            # UNITARES internal state
            'unitaires_state': {
                'E': float(self.unitaires_state.E),
                'I': float(self.unitaires_state.I),
                'S': float(self.unitaires_state.S),
                'V': float(self.unitaires_state.V)
            },
            'unitaires_theta': {
                'C1': float(self.unitaires_theta.C1),
                'eta1': float(self.unitaires_theta.eta1)
            },
            # History arrays (capped to last max_history entries)
            'regime': str(self.regime),
            'regime_history': [str(r) for r in cap_history(self.regime_history)],
            'locked_persistence_count': int(self.locked_persistence_count),
            'E_history': [float(e) for e in cap_history(self.E_history)],
            'I_history': [float(i) for i in cap_history(self.I_history)],
            'S_history': [float(s) for s in cap_history(self.S_history)],
            'V_history': [float(v) for v in cap_history(self.V_history)],
            'coherence_history': [float(c) for c in cap_history(self.coherence_history)],
            'risk_history': [float(r) for r in cap_history(self.risk_history)],
            'lambda1_history': [float(l) for l in cap_history(getattr(self, 'lambda1_history', []))],  # Lambda1 adaptation history
            'decision_history': list(cap_history(self.decision_history)),
            'verdict_history': list(cap_history(self.verdict_history)),
            'timestamp_history': list(cap_history(self.timestamp_history)),  # Timestamps for each update
            'pi_integral': float(getattr(self, 'pi_integral', 0.0)),  # PI controller integral state
            # HCK v3.0 metrics
            'rho_history': [float(r) for r in cap_history(getattr(self, 'rho_history', []))],
            'CE_history': [float(c) for c in cap_history(getattr(self, 'CE_history', []))],
            'current_rho': float(getattr(self, 'current_rho', 0.0)),
            # CIRS v0.1 metrics
            'oi_history': [float(o) for o in cap_history(getattr(self, 'oi_history', []))],
            'resonance_events': int(getattr(self, 'resonance_events', 0)),
            'damping_applied_count': int(getattr(self, 'damping_applied_count', 0)),
            'lambda1_update_skips': int(self.lambda1_update_skips),
            # CIRS v2: Adaptive Governor state (persisted across restarts)
            'governor_state': getattr(self, '_governor_state_dict', None),
        }
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'GovernanceState':
        """Create GovernanceState from dictionary (for loading persisted state)"""
        from governance_core import State, Theta
        
        # Create state with loaded values
        state = cls()
        
        # Load UNITARES internal state
        if 'unitaires_state' in data:
            us = data['unitaires_state']
            state.unitaires_state = State(
                E=float(us.get('E', DEFAULT_STATE.E)),
                I=float(us.get('I', DEFAULT_STATE.I)),
                S=float(us.get('S', DEFAULT_STATE.S)),
                V=float(us.get('V', DEFAULT_STATE.V))
            )
        else:
            # Fallback: use current state values
            state.unitaires_state = State(
                E=float(data.get('E', DEFAULT_STATE.E)),
                I=float(data.get('I', DEFAULT_STATE.I)),
                S=float(data.get('S', DEFAULT_STATE.S)),
                V=float(data.get('V', DEFAULT_STATE.V))
            )
        
        # Load UNITARES theta
        # C1 is a system-wide parameter (coherence steepness), always use current default.
        # eta1 is per-agent adaptive state, load from stored data.
        if 'unitaires_theta' in data:
            ut = data['unitaires_theta']
            state.unitaires_theta = Theta(
                C1=DEFAULT_THETA.C1,
                eta1=float(ut.get('eta1', DEFAULT_THETA.eta1))
            )
        
        # Load derived metrics
        # CRITICAL FIX: Recalculate coherence from current V to avoid discontinuity
        # Old state files may have blended coherence (0.64), but we now use pure C(V)
        # Recalculate immediately to prevent discontinuity on first update
        from governance_core.coherence import coherence as coherence_func
        from governance_core.parameters import get_active_params
        # Recalculate from current V to ensure consistency (ignore persisted value)
        recalculated_coherence = coherence_func(state.V, state.unitaires_theta, get_active_params())
        state.coherence = float(np.clip(recalculated_coherence, 0.0, 1.0))
        state.void_active = bool(data.get('void_active', False))
        state.time = float(data.get('time', 0.0))
        state.update_count = int(data.get('update_count', 0))
        
        # Load regime tracking (backward compatible - default to "divergence")
        state.regime = str(data.get('regime', 'divergence'))
        state.regime_history = [str(r) for r in data.get('regime_history', [])]
        state.locked_persistence_count = int(data.get('locked_persistence_count', 0))
        
        # Load history arrays
        state.E_history = [float(e) for e in data.get('E_history', [])]
        state.I_history = [float(i) for i in data.get('I_history', [])]
        state.S_history = [float(s) for s in data.get('S_history', [])]
        state.V_history = [float(v) for v in data.get('V_history', [])]
        state.coherence_history = [float(c) for c in data.get('coherence_history', [])]
        state.risk_history = [float(r) for r in data.get('risk_history', [])]
        state.decision_history = list(data.get('decision_history', []))
        state.verdict_history = list(data.get('verdict_history', []))
        state.timestamp_history = list(data.get('timestamp_history', []))  # Load timestamps
        state.lambda1_history = [float(l) for l in data.get('lambda1_history', [])]  # Load lambda1 history
        
        # Load PI controller integral state (backward compatible)
        state.pi_integral = float(data.get('pi_integral', 0.0))

        # Load HCK v3.0 metrics (backward compatible)
        state.rho_history = [float(r) for r in data.get('rho_history', [])]
        state.CE_history = [float(c) for c in data.get('CE_history', [])]
        state.current_rho = float(data.get('current_rho', 0.0))

        # Load CIRS v0.1 metrics (backward compatible)
        state.oi_history = [float(o) for o in data.get('oi_history', [])]
        state.resonance_events = int(data.get('resonance_events', 0))
        state.damping_applied_count = int(data.get('damping_applied_count', 0))

        # Lambda1 controller skip count (backward compatible)
        state.lambda1_update_skips = int(data.get('lambda1_update_skips', 0))

        # CIRS v2: Adaptive Governor state (backward compatible)
        state._governor_state_dict = data.get('governor_state', None)

        return state
    
    def validate(self) -> tuple[bool, list[str]]:
        """
        Validate state invariants and bounds.
        
        Returns:
            (is_valid, list_of_errors)
        """
        errors = []
        
        # Check bounds
        if not (0.0 <= self.E <= 1.0):
            errors.append(f"E out of bounds: {self.E} (expected [0, 1])")
        if not (0.0 <= self.I <= 1.0):
            errors.append(f"I out of bounds: {self.I} (expected [0, 1])")
        if not (0.0 <= self.S <= 1.0):
            errors.append(f"S out of bounds: {self.S} (expected [0, 1])")
        if not (-1.0 <= self.V <= 1.0):
            errors.append(f"V out of bounds: {self.V} (expected [-1, 1])")
        if not (0.0 <= self.coherence <= 1.0):
            errors.append(f"Coherence out of bounds: {self.coherence} (expected [0, 1])")
        
        # Check for NaN/inf
        if np.isnan(self.E) or np.isinf(self.E):
            errors.append(f"E is NaN or Inf: {self.E}")
        if np.isnan(self.I) or np.isinf(self.I):
            errors.append(f"I is NaN or Inf: {self.I}")
        if np.isnan(self.S) or np.isinf(self.S):
            errors.append(f"S is NaN or Inf: {self.S}")
        if np.isnan(self.V) or np.isinf(self.V):
            errors.append(f"V is NaN or Inf: {self.V}")
        if np.isnan(self.coherence) or np.isinf(self.coherence):
            errors.append(f"Coherence is NaN or Inf: {self.coherence}")
        
        # Check lambda1 bounds
        lambda1_val = self.lambda1
        if np.isnan(lambda1_val) or np.isinf(lambda1_val):
            errors.append(f"lambda1 is NaN or Inf: {lambda1_val}")
        elif not (0.0 <= lambda1_val <= 1.0):
            errors.append(f"lambda1 out of bounds: {lambda1_val} (expected [0, 1])")
        
        # Check history consistency
        history_lengths = [
            len(self.E_history),
            len(self.I_history),
            len(self.S_history),
            len(self.V_history),
            len(self.coherence_history),
            len(self.risk_history)
        ]
        if len(set(history_lengths)) > 1:
            # Allow some variance (decision_history can be shorter)
            max_len = max(history_lengths)
            min_len = min(history_lengths)
            if max_len - min_len > 1:  # More than 1 entry difference
                errors.append(f"History length mismatch: E={len(self.E_history)}, I={len(self.I_history)}, S={len(self.S_history)}, V={len(self.V_history)}, coherence={len(self.coherence_history)}, risk={len(self.risk_history)}")
        
        return len(errors) == 0, errors



    # =========================================================================
    # INTERPRETATION LAYER (v2 API)
    # =========================================================================
    # Maps raw EISV metrics to human-readable semantic state
    # Goal: One glance tells you what's happening, not a wall of numbers
    
    def interpret_state(
        self, 
        risk_score: float = None,
        prev_mode: str = None,
        task_type: str = "mixed"
    ) -> dict:
        """
        Generate human-readable interpretation of governance state.
        
        Returns a structured block with:
        - health: one word (healthy, moderate, at_risk, critical)
        - basin: which attractor (high, low, transitional)
        - mode: operational pattern (building_alone, collaborating, etc.)
        - trajectory: what's changing (stable, improving, declining, stuck)
        - guidance: actionable suggestion or None
        - borderline: dict of any metrics near threshold (for hysteresis awareness)
        
        Args:
            risk_score: Current risk score (from estimate_risk), optional
            prev_mode: Previous mode for hysteresis (optional)
            task_type: Current task type for context-aware interpretation
        """
        E, I, S, V = self.E, self.I, self.S, self.V
        coherence = self.coherence
        
        # Calculate risk if not provided
        if risk_score is None:
            risk_score = self._estimate_risk_simple()
        
        # --- HEALTH ---
        health = self._interpret_health(coherence, risk_score)
        
        # --- BASIN ---
        basin = self._interpret_basin(E, I, risk_score=risk_score)
        
        # --- MODE ---
        mode, borderline = self._interpret_mode(E, I, S, prev_mode)
        
        # --- TRAJECTORY ---
        trajectory = self._interpret_trajectory()
        
        # --- GUIDANCE ---
        guidance = self._generate_guidance(
            health=health,
            basin=basin,
            mode=mode,
            trajectory=trajectory,
            task_type=task_type,
            borderline=borderline
        )
        
        return {
            "health": health,
            "basin": basin,
            "mode": mode,
            "trajectory": trajectory,
            "guidance": guidance,
            "borderline": borderline if borderline else None
        }
    
    def _estimate_risk_simple(self) -> float:
        """Simple risk estimate when full risk_score not available."""
        # Risk increases with entropy, decreases with coherence
        S = self.S
        coh = self.coherence
        V_abs = abs(self.V)
        
        # Base risk from entropy and void
        risk = 0.3 * S + 0.3 * V_abs
        # Coherence reduces risk
        risk += 0.4 * (1.0 - coh)
        return min(1.0, max(0.0, risk))
    
    def _interpret_health(self, coherence: float, risk_score: float) -> str:
        """Map coherence and risk to health status."""
        if risk_score > 0.7:
            return "critical"
        if risk_score > 0.5:
            return "at_risk"
        if coherence < 0.3:
            return "unstable"
        if coherence > 0.6 and risk_score < 0.3:
            return "healthy"
        return "moderate"
    
    def _interpret_basin(self, E: float, I: float,
                         risk_score: float = None) -> str:
        """Classify which attractor basin the state occupies.

        Uses multi-dimensional bounds (E, I, S, V, coherence, risk)
        defined in ``governance_config.classify_basin``.

        Args:
            E: Energy (kept for backward-compatible call sites).
            I: Information integrity (kept for backward-compatible call sites).
            risk_score: Optional risk score.  When ``None`` a simple
                proxy is derived so the basin can still be computed
                without a full risk pipeline.
        """
        from config.governance_config import classify_basin

        if risk_score is None:
            risk_score = self._estimate_risk_simple()

        return classify_basin(
            E=E, I=I, S=self.S, V=self.V,
            coherence=self.coherence, risk_score=risk_score,
        )
    
    def _interpret_mode(
        self, 
        E: float, 
        I: float, 
        S: float, 
        prev_mode: str = None
    ) -> Tuple[str, dict]:
        """
        Determine operational mode from E, I, S.
        
        Returns (mode, borderline_dict) where borderline_dict contains
        any metrics near threshold for hysteresis awareness.
        """
        # Thresholds with hysteresis
        E_thresh = 0.5
        I_thresh = 0.5
        S_thresh = 0.3
        hysteresis_margin = 0.05
        
        borderline = {}
        
        # Apply hysteresis if we have previous mode
        def is_high(val, thresh, dim_name, prev_was_high=None):
            """Check if value is high with hysteresis."""
            if prev_was_high is not None:
                # Use asymmetric threshold based on previous state
                if prev_was_high:
                    effective_thresh = thresh - hysteresis_margin
                else:
                    effective_thresh = thresh + hysteresis_margin
            else:
                effective_thresh = thresh
            
            result = val > effective_thresh
            
            # Track borderline values
            if abs(val - thresh) < hysteresis_margin * 2:
                borderline[dim_name] = {
                    "value": round(val, 3),
                    "threshold": thresh,
                    "status": "high" if result else "low",
                    "note": f"Near threshold ({thresh}±{hysteresis_margin*2:.2f})"
                }
            
            return result
        
        # Determine previous state for hysteresis
        prev_high_E = prev_mode and "exploring" in prev_mode if prev_mode else None
        prev_high_I = prev_mode and "building" in prev_mode if prev_mode else None
        prev_high_S = prev_mode and ("together" in prev_mode or "collaborating" in prev_mode) if prev_mode else None
        
        high_E = is_high(E, E_thresh, "E", prev_high_E)
        high_I = is_high(I, I_thresh, "I", prev_high_I)
        high_S = is_high(S, S_thresh, "S", prev_high_S)
        
        # Mode mapping (8 patterns)
        patterns = {
            (True, True, True):   "collaborating",      # high E, high I, high S
            (True, True, False):  "building_alone",     # high E, high I, low S
            (True, False, True):  "exploring_together", # high E, low I, high S
            (True, False, False): "exploring_alone",    # high E, low I, low S
            (False, True, True):  "executing_together", # low E, high I, high S
            (False, True, False): "executing_alone",    # low E, high I, low S
            (False, False, True): "drifting_together",  # low E, low I, high S
            (False, False, False): "stalled",           # low everything
        }
        
        mode = patterns[(high_E, high_I, high_S)]
        return mode, borderline
    
    def _interpret_trajectory(self) -> str:
        """Determine trajectory from recent history."""
        V = self.V
        
        # Check V trend
        if V > 0.1:
            return "improving"
        if V < -0.1:
            return "declining"
        
        # Check for stuck pattern (multiple pauses in recent decisions)
        recent = self.decision_history[-5:] if self.decision_history else []
        pause_count = sum(1 for d in recent if d in ["pause", "reflect"])
        if pause_count >= 3:
            return "stuck"
        
        return "stable"
    
    def _generate_guidance(
        self,
        health: str,
        basin: str,
        mode: str,
        trajectory: str,
        task_type: str,
        borderline: dict
    ) -> Optional[str]:
        """Generate actionable guidance based on interpreted state."""
        
        # Priority 1: Critical warnings
        if health == "critical":
            return "Circuit breaker imminent. Pause and reassess. Consider dialectic review."
        
        if trajectory == "declining":
            return "Value trajectory negative. Simplify approach or seek input."
        
        if trajectory == "stuck":
            return "Multiple pauses detected. Try a different approach or request dialectic."
        
        # Priority 2: Mode-specific suggestions
        if mode == "stalled":
            return "Low activity across dimensions. New task or external input needed."
        
        if mode == "exploring_alone" and trajectory == "stable":
            return "High exploration, low integration. Consider consolidating findings."
        
        if mode == "drifting_together":
            return "Low energy and integrity but high social. Focus on productive collaboration."
        
        # Priority 3: Borderline warnings
        if borderline:
            borderline_items = list(borderline.items())
            if borderline_items:
                dim, info = borderline_items[0]
                return f"{dim}={info['value']:.2f} (borderline). Pattern may be shifting."
        
        # Priority 4: Basin-specific
        if basin == "boundary":
            return "Near basin boundary - state may flip. Maintain consistency."
        
        if basin == "low" and health != "critical":
            return "In low basin. Increase energy (E) to transition to high equilibrium."
        
        # Priority 5: Healthy productive states need no guidance
        if health == "healthy" and mode in ["building_alone", "collaborating"]:
            return None  # Healthy state, no action needed
        
        # Low-priority suggestions for moderate states
        if mode in ["building_alone", "executing_alone"] and trajectory == "stable":
            return "Working independently. Consider dialectic to sanity-check approach."
        
        return None

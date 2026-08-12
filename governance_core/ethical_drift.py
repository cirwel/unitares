"""
UNITARES Governance Core — Behavioral–Epistemic Drift Vector

Concrete, measurable implementation of the drift vector Δη described in the
UNITARES v6 paper (§Behavioral–Epistemic Drift Vector).

Overview
--------
Earlier drafts of the framework described "drift" abstractly. This module
defines Δη as a four-component vector computed from observable signals:

    Δη = (
        calibration_deviation,    # |predicted_correct - actual_correct|
        complexity_divergence,    # |derived_complexity - self_complexity|
        coherence_deviation,      # |current_coherence - baseline_coherence|
        stability_deviation       # decision-pattern instability
    )

Each component is derived from signals the system already measures. The
L2 norm ‖Δη‖ feeds into the EISV dynamics (dS/dt, dE/dt). The historical
``ethical`` and pillar labels are ontology, not validated construct equivalence:
in particular ``coherence_deviation`` currently tracks movement in the
configured legacy ODE control-feedback scalar (and may be floored by state
velocity), not loss of reasoning quality or robustness.

Mapping to AI-ethics pillar vocabulary
--------------------------------------
Earlier design notes associated the four-component decomposition with common
AI-ethics vocabulary:

- Fairness        → calibration_deviation  (confidence vs. outcome)
- Explainability  → complexity_divergence  (derived vs. self-reported)
- Robustness      → coherence_deviation    (historical aspiration; not established)
- Consistency     → stability_deviation    (decision patterns over time)

Do not read those labels as a measurement validation claim. The module computes
transparent operational proxies; external evidence is required before treating
any component as a measure of the named ethical property.
"""

from __future__ import annotations
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Any, Deque
import math
import os


# Per-agent decision-history window. Bounded by deque(maxlen=...) on
# AgentBaseline.recent_decisions so updates can't grow the buffer.
_RECENT_DECISIONS_MAXLEN = 20

# In-memory baseline cache size cap. Beyond this, least-recently-used
# entries are evicted; the next access for an evicted agent_id triggers
# a reload from PostgreSQL via src/mcp_handlers/updates/phases.py:667
# (load_agent_baseline), so eviction is loss-free as long as write-back
# at phases.py:1042 (save_agent_baseline) keeps the DB current.
# Override via UNITARES_BASELINE_CACHE_MAXLEN env var.
_BASELINE_CACHE_MAXLEN = int(os.environ.get("UNITARES_BASELINE_CACHE_MAXLEN", "1000"))


@dataclass
class EthicalDriftVector:
    """
    Concrete ethical drift vector with measurable components.

    Each component is in [0, 1] and represents an operational deviation.
    Lower values mean less measured deviation, not necessarily better outcomes.

    Attributes:
        calibration_deviation: Confidence-outcome mismatch (from calibration system)
        complexity_divergence: Gap between derived and self-reported complexity
        coherence_deviation: Movement in the configured compatibility signal;
            not a health or robustness score
        stability_deviation: Decision pattern instability over time
        timestamp: When this vector was computed
        agent_id: Which agent this applies to
    """
    calibration_deviation: float = 0.0
    complexity_divergence: float = 0.0
    coherence_deviation: float = 0.0
    stability_deviation: float = 0.0
    timestamp: Optional[datetime] = None
    agent_id: Optional[str] = None

    def __post_init__(self):
        """Validate and clip components to [0, 1]."""
        self.calibration_deviation = max(0.0, min(1.0, self.calibration_deviation))
        self.complexity_divergence = max(0.0, min(1.0, self.complexity_divergence))
        self.coherence_deviation = max(0.0, min(1.0, self.coherence_deviation))
        self.stability_deviation = max(0.0, min(1.0, self.stability_deviation))
        if self.timestamp is None:
            self.timestamp = datetime.now()

    @property
    def norm(self) -> float:
        """
        L2 norm of the drift vector.

        This is ||Δη|| that feeds into EISV dynamics.
        Range: [0, 2] (max when all components are 1.0)
        """
        return math.sqrt(
            self.calibration_deviation ** 2 +
            self.complexity_divergence ** 2 +
            self.coherence_deviation ** 2 +
            self.stability_deviation ** 2
        )

    @property
    def norm_squared(self) -> float:
        """||Δη||² - Used directly in dS/dt and dE/dt equations."""
        return (
            self.calibration_deviation ** 2 +
            self.complexity_divergence ** 2 +
            self.coherence_deviation ** 2 +
            self.stability_deviation ** 2
        )

    def to_list(self) -> List[float]:
        """
        Convert to list format for compatibility with existing dynamics code.

        Returns 4-element list [calibration, complexity, coherence, stability].
        """
        return [
            self.calibration_deviation,
            self.complexity_divergence,
            self.coherence_deviation,
            self.stability_deviation,
        ]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for storage and API responses."""
        return {
            'calibration_deviation': self.calibration_deviation,
            'complexity_divergence': self.complexity_divergence,
            'coherence_deviation': self.coherence_deviation,
            'stability_deviation': self.stability_deviation,
            'norm': self.norm,
            'norm_squared': self.norm_squared,
            'timestamp': self.timestamp.isoformat() if self.timestamp else None,
            'agent_id': self.agent_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'EthicalDriftVector':
        """Deserialize from storage."""
        return cls(
            calibration_deviation=data.get('calibration_deviation', 0.0),
            complexity_divergence=data.get('complexity_divergence', 0.0),
            coherence_deviation=data.get('coherence_deviation', 0.0),
            stability_deviation=data.get('stability_deviation', 0.0),
            timestamp=datetime.fromisoformat(data['timestamp']) if data.get('timestamp') else None,
            agent_id=data.get('agent_id'),
        )

    @classmethod
    def zero(cls, agent_id: Optional[str] = None) -> 'EthicalDriftVector':
        """Create a vector with zero measured deviation (not proven alignment)."""
        return cls(
            calibration_deviation=0.0,
            complexity_divergence=0.0,
            coherence_deviation=0.0,
            stability_deviation=0.0,
            agent_id=agent_id,
        )


@dataclass
class AgentBaseline:
    """
    Baseline measurements for an agent, used to compute deviations.

    Updated incrementally with exponential moving average.
    """
    agent_id: str

    # Baseline values (exponential moving averages)
    baseline_coherence: float = 0.5
    baseline_confidence: float = 0.6
    baseline_complexity: float = 0.4

    # Decision pattern tracking — bounded by deque(maxlen=...) so the
    # buffer can't grow past _RECENT_DECISIONS_MAXLEN regardless of how
    # many updates land.
    recent_decisions: Deque[str] = field(
        default_factory=lambda: deque(maxlen=_RECENT_DECISIONS_MAXLEN)
    )
    decision_consistency: float = 0.8  # How consistent are decisions

    # Update count for weighting
    update_count: int = 0

    # Previous raw observations for rate-of-change calculation
    prev_coherence: Optional[float] = None
    prev_confidence: Optional[float] = None
    prev_complexity: Optional[float] = None

    # EMA smoothing factor (higher = more responsive, lower = more stable)
    # Paper specifies 0.1. Phase detection handles legitimate phase transitions,
    # so the baseline should track drift within phases responsively.
    alpha: float = 0.1

    # Last update timestamp
    last_updated: Optional[datetime] = None

    def update(
        self,
        coherence: Optional[float] = None,
        confidence: Optional[float] = None,
        complexity: Optional[float] = None,
        decision: Optional[str] = None,
    ):
        """
        Update baselines with new observations using EMA.

        EMA formula: baseline = alpha * new_value + (1 - alpha) * old_baseline
        """
        if coherence is not None:
            self.baseline_coherence = (
                self.alpha * coherence + (1 - self.alpha) * self.baseline_coherence
            )
            self.prev_coherence = coherence  # Raw observation for rate-of-change

        if confidence is not None:
            self.baseline_confidence = (
                self.alpha * confidence + (1 - self.alpha) * self.baseline_confidence
            )
            self.prev_confidence = confidence  # Raw observation for rate-of-change

        if complexity is not None:
            self.baseline_complexity = (
                self.alpha * complexity + (1 - self.alpha) * self.baseline_complexity
            )
            self.prev_complexity = complexity  # Raw observation for rate-of-change

        if decision is not None:
            # deque(maxlen=_RECENT_DECISIONS_MAXLEN) auto-evicts the oldest
            # entry on append once full, so no manual trim is needed.
            self.recent_decisions.append(decision)
            self._update_decision_consistency()

        self.update_count += 1
        self.last_updated = datetime.now()

    def _update_decision_consistency(self):
        """Compute decision consistency from recent decisions."""
        if len(self.recent_decisions) < 2:
            self.decision_consistency = 0.8  # Default
            return

        # Count transitions (decision changes)
        transitions = sum(
            1 for i in range(1, len(self.recent_decisions))
            if self.recent_decisions[i] != self.recent_decisions[i-1]
        )

        # Stability = 1 - (transitions / possible_transitions)
        max_transitions = len(self.recent_decisions) - 1
        if max_transitions > 0:
            stability = 1.0 - (transitions / max_transitions)
            # Smooth with current value
            self.decision_consistency = (
                0.3 * stability + 0.7 * self.decision_consistency
            )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for storage."""
        return {
            'agent_id': self.agent_id,
            'baseline_coherence': self.baseline_coherence,
            'baseline_confidence': self.baseline_confidence,
            'baseline_complexity': self.baseline_complexity,
            'prev_coherence': self.prev_coherence,
            'prev_confidence': self.prev_confidence,
            'prev_complexity': self.prev_complexity,
            'recent_decisions': list(self.recent_decisions),
            'decision_consistency': self.decision_consistency,
            'update_count': self.update_count,
            'alpha': self.alpha,
            'last_updated': self.last_updated.isoformat() if self.last_updated else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AgentBaseline':
        """Deserialize from storage."""
        baseline = cls(agent_id=data['agent_id'])
        baseline.baseline_coherence = data.get('baseline_coherence', 0.5)
        baseline.baseline_confidence = data.get('baseline_confidence', 0.6)
        baseline.baseline_complexity = data.get('baseline_complexity', 0.4)
        baseline.prev_coherence = data.get('prev_coherence')
        baseline.prev_confidence = data.get('prev_confidence')
        baseline.prev_complexity = data.get('prev_complexity')
        baseline.recent_decisions = deque(
            data.get('recent_decisions', []), maxlen=_RECENT_DECISIONS_MAXLEN
        )
        baseline.decision_consistency = data.get('decision_consistency', 0.8)
        baseline.update_count = data.get('update_count', 0)
        baseline.alpha = data.get('alpha', 0.1)
        if data.get('last_updated'):
            baseline.last_updated = datetime.fromisoformat(data['last_updated'])
        return baseline


def compute_ethical_drift(
    agent_id: str,
    baseline: AgentBaseline,
    current_coherence: float,
    current_confidence: float,
    complexity_divergence: float,
    calibration_error: Optional[float] = None,
    decision: Optional[str] = None,
    state_velocity: Optional[float] = None,
    task_context: str = "mixed",
) -> EthicalDriftVector:
    """
    Compute concrete ethical drift vector from measurable signals.

    This is THE function that makes Δη concrete. It takes observable
    measurements and converts them into the drift vector.

    Args:
        agent_id: Agent identifier
        baseline: Agent's baseline measurements (for deviation calculation)
        current_coherence: Current compatibility value; deployed callers pass
            legacy C(V, Θ) directional control feedback
        current_confidence: Current confidence level [0, 1]
        complexity_divergence: |derived_complexity - self_complexity|
        calibration_error: Calibration deviation (if available)
        decision: Current decision outcome (for stability tracking)

    Returns:
        EthicalDriftVector with all components computed

    MEASUREMENT DEFINITIONS:

    1. calibration_deviation: How well confidence predicts outcomes
       - Uses calibration_error if provided
       - Otherwise uses |confidence - baseline_confidence|
       - Range: [0, 1]

    2. complexity_divergence: Gap between derived and reported complexity
       - Directly passed from dual-log continuity layer
       - Range: [0, 1]

    3. coherence_deviation: Current vs baseline compatibility signal
       - |current_coherence - baseline_coherence|
       - deployed source is legacy C(V_ODE) plus an optional state-velocity floor
       - movement is information, not proof of lost robustness or health
       - Range: [0, 1]

    4. stability_deviation: Decision pattern instability
       - Inverse of decision_consistency
       - Range: [0, 1]
    """

    # 1. Calibration deviation
    if calibration_error is not None:
        calibration_deviation = min(1.0, abs(calibration_error))
    else:
        # Fallback: use max of deviation-from-mean and rate-of-change
        deviation_from_mean = abs(current_confidence - baseline.baseline_confidence)
        rate_of_change = abs(current_confidence - baseline.prev_confidence) if baseline.prev_confidence is not None else 0.0
        calibration_deviation = max(deviation_from_mean, rate_of_change)

    # 2. Complexity divergence (passed directly)
    # Already computed by dual-log continuity layer
    complexity_dev = min(1.0, abs(complexity_divergence))

    # 3. Coherence deviation: max of deviation-from-mean and rate-of-change
    deviation_from_mean = abs(current_coherence - baseline.baseline_coherence)
    rate_of_change = abs(current_coherence - baseline.prev_coherence) if baseline.prev_coherence is not None else 0.0
    coherence_dev = max(deviation_from_mean, rate_of_change)

    # 4. Stability deviation (inverse of consistency)
    stability_dev = 1.0 - baseline.decision_consistency

    # Warmup dampening: deviations from uninitialized baselines are meaningless.
    # For the first few updates, the baseline is just defaults (0.6, 0.5, 0.8 etc.)
    # so large "deviations" are artifacts, not real drift signals.
    # Ramp from 0→1 over 5 updates (paper value — 2 was too few for meaningful baselines).
    warmup_updates = 5
    if baseline.update_count < warmup_updates:
        warmup_factor = baseline.update_count / warmup_updates
        calibration_deviation *= warmup_factor
        # complexity_divergence is measured directly, not from baseline — no dampening needed
        coherence_dev *= warmup_factor
        stability_dev *= warmup_factor

    # Epistemic context: exploration/introspection tasks expect low confidence.
    # Honest uncertainty is not drift — attenuate deviation signals so the dynamics
    # engine doesn't penalize appropriate epistemic humility.
    if task_context in ("exploration", "introspection"):
        calibration_deviation *= 0.3
        complexity_dev *= 0.5

    # State velocity floor: EISV state changes inject signal even when EMA baselines
    # track tightly. This prevents signal starvation for non-Lumen agents whose
    # drift vectors would otherwise flatline at [0,0,0,0].
    if state_velocity is not None and state_velocity > 0.01:
        velocity_signal = min(0.5, state_velocity)
        coherence_dev = max(coherence_dev, velocity_signal * 0.5)
        calibration_deviation = max(calibration_deviation, velocity_signal * 0.3)

    # Update baseline with current observations
    baseline.update(
        coherence=current_coherence,
        confidence=current_confidence,
        complexity=1.0 - complexity_divergence,  # Low divergence = high complexity accuracy
        decision=decision,
    )

    return EthicalDriftVector(
        calibration_deviation=calibration_deviation,
        complexity_divergence=complexity_dev,
        coherence_deviation=coherence_dev,
        stability_deviation=stability_dev,
        agent_id=agent_id,
    )


# Baseline storage — bounded LRU. Eviction is loss-free as long as the
# write-back at src/mcp_handlers/updates/phases.py:1042 keeps the DB
# current. Cap is _BASELINE_CACHE_MAXLEN (env-overridable).
_baseline_cache: "OrderedDict[str, AgentBaseline]" = OrderedDict()


def _touch_lru(agent_id: str) -> None:
    """Mark agent_id as most-recently-used in the cache."""
    _baseline_cache.move_to_end(agent_id)


def _evict_if_over_cap() -> None:
    """Drop the least-recently-used entry if cache exceeds the cap."""
    while len(_baseline_cache) > _BASELINE_CACHE_MAXLEN:
        _baseline_cache.popitem(last=False)


def get_agent_baseline(agent_id: str) -> AgentBaseline:
    """Get or create baseline for an agent (LRU-touched on access)."""
    if agent_id in _baseline_cache:
        _touch_lru(agent_id)
        return _baseline_cache[agent_id]
    baseline = AgentBaseline(agent_id=agent_id)
    _baseline_cache[agent_id] = baseline
    _evict_if_over_cap()
    return baseline


def clear_baseline(agent_id: str):
    """Clear baseline for an agent (for testing or reset)."""
    _baseline_cache.pop(agent_id, None)


def get_all_baselines() -> Dict[str, AgentBaseline]:
    """Get all baselines (for debugging/inspection). Snapshot copy."""
    return dict(_baseline_cache)


def set_agent_baseline(agent_id: str, baseline: AgentBaseline) -> None:
    """Preload a baseline (e.g., from persistent storage). LRU-touched."""
    _baseline_cache[agent_id] = baseline
    _touch_lru(agent_id)
    _evict_if_over_cap()


def get_baseline_or_none(agent_id: str) -> Optional[AgentBaseline]:
    """Get baseline if cached, without creating a default. LRU-touched on hit."""
    if agent_id in _baseline_cache:
        _touch_lru(agent_id)
        return _baseline_cache[agent_id]
    return None

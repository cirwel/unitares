"""Decision logic for governance monitor.

State-machine transitions are driven by **basin membership** — named
regions in EISV + coherence + risk state space (see ``governance_config``).

Basin → default transition:
  HIGH      → proceed (approve or guide per verdict)
  BOUNDARY  → proceed with guide, margin: tight
  LOW       → pause (suggest dialectic if sustained)

CIRS hard_block and high-risk verdict still override basin logic.
"""

from typing import Dict, Optional, TYPE_CHECKING

from config.governance_config import config, classify_basin
from src.logging_utils import get_logger

if TYPE_CHECKING:
    from src.cirs import OscillationState

logger = get_logger(__name__)


def get_effective_threshold(name: str, default: float) -> float:
    """Get effective threshold, allowing runtime overrides."""
    try:
        from src.runtime_config import get_effective_threshold as _get
        return _get(name, default=default)
    except ImportError:
        return default


def make_decision(
    state,
    risk_score: float,
    unitares_verdict: Optional[str] = None,
    response_tier: Optional[str] = None,
    oscillation_state: Optional['OscillationState'] = None,
) -> Dict:
    """
    Make autonomous governance decision using basin membership, UNITARES
    verdict, and CIRS response tier.

    Decision priority (highest first):
      1. CIRS hard_block → pause (oscillation override)
      2. void_active → pause (runtime adaptive threshold — safety gate)
      3. coherence < critical → pause (safety gate)
      4. high-risk verdict → pause (phi says ethical risk — specific signal)
      5. LOW basin → pause (state space says we're structurally degraded)
      6. BOUNDARY basin → proceed with guide, margin: tight
      7. HIGH basin + verdict logic → proceed (approve or guide)

    Args:
        state: GovernanceState instance.
        risk_score: Risk score [0, 1].
        unitares_verdict: "safe", "caution", or "high-risk".
        response_tier: CIRS tier — "hard_block", "soft_dampen", or "proceed".
        oscillation_state: CIRS oscillation state (for hard_block details).

    Returns:
        Decision dict with action, reason, guidance, critical, basin,
        margin, nearest_edge.
    """
    margin_info = config.compute_proprioceptive_margin(
        risk_score=risk_score,
        coherence=state.coherence,
        void_active=state.void_active,
        void_value=state.V,
        coherence_history=state.coherence_history,
    )

    basin = classify_basin(
        E=state.E, I=state.I, S=state.S, V=state.V,
        coherence=state.coherence, risk_score=risk_score,
    )

    # --- Priority 1: CIRS hard_block override ---
    # classify_response() can fire hard_block from three independent conditions:
    # coherence < tau_low, risk > beta_high, or resonant + bad state. Attribute
    # the reason to the actual trigger instead of blanket-labeling as resonance.
    if response_tier == 'hard_block':
        from src.cirs import CIRS_DEFAULTS
        resonant = bool(oscillation_state and oscillation_state.resonant)
        if resonant:
            oi = oscillation_state.oi
            flips = oscillation_state.flips
            reason = (
                f'CIRS resonance detected (OI={oi:.2f}, flips={flips}) — decision oscillating'
            )
            guidance = 'Governance is flip-flopping. Reduce complexity or wait for state to settle.'
            nearest_edge = 'oscillation'
        elif risk_score > CIRS_DEFAULTS['beta_high']:
            reason = f'CIRS risk ceiling breached (risk={risk_score:.2f} > {CIRS_DEFAULTS["beta_high"]})'
            guidance = 'Risk score exceeded the hard-block ceiling. Pause to investigate the input driving the spike.'
            nearest_edge = 'risk'
        elif state.coherence < CIRS_DEFAULTS['tau_low']:
            reason = f'CIRS coherence floor breached (coherence={state.coherence:.2f} < {CIRS_DEFAULTS["tau_low"]})'
            guidance = 'Coherence fell below the hard-block floor. Pause to let state stabilize.'
            nearest_edge = 'coherence'
        else:
            # hard_block reached us but none of the documented conditions hold —
            # surface that fact rather than mislabeling as resonance.
            oi = oscillation_state.oi if oscillation_state else 0.0
            flips = oscillation_state.flips if oscillation_state else 0
            reason = (
                f'CIRS hard_block (cause unclassified; OI={oi:.2f}, flips={flips}, '
                f'risk={risk_score:.2f}, coherence={state.coherence:.2f})'
            )
            guidance = 'CIRS forced a hard block but the trigger condition is ambiguous; inspect monitor inputs.'
            nearest_edge = 'oscillation'
        return {
            'action': 'pause',
            'sub_action': 'cirs_block',
            'reason': reason,
            'guidance': guidance,
            'critical': False,
            'basin': basin,
            'margin': 'critical',
            'nearest_edge': nearest_edge,
        }

    # --- Priority 2: void_active → pause (runtime adaptive threshold) ---
    if state.void_active:
        return {
            'action': 'pause',
            'sub_action': 'void_pause',
            'reason': 'Energy-integrity imbalance detected — time to recalibrate',
            'guidance': 'System needs a moment to stabilize. Take a break or shift focus.',
            'critical': False,
            'basin': basin,
            'margin': 'critical',
            'nearest_edge': 'void',
        }

    # --- Priority 3: coherence below critical → pause (safety gate) ---
    effective_coherence_threshold = get_effective_threshold(
        "coherence_critical_threshold", default=config.COHERENCE_CRITICAL_THRESHOLD)
    if state.coherence < effective_coherence_threshold:
        return {
            'action': 'pause',
            'sub_action': 'coherence_pause',
            'reason': f'Coherence needs attention ({state.coherence:.2f}) — moment to regroup',
            'guidance': 'Things are getting fragmented. Simplify, refocus, or take a breather.',
            'critical': True,
            'basin': basin,
            'margin': 'critical',
            'nearest_edge': 'coherence',
        }

    # CIRS soft_dampen: upgrade safe to caution
    if response_tier == 'soft_dampen' and unitares_verdict == 'safe':
        unitares_verdict = 'caution'

    # --- Priority 4: high-risk verdict → pause ---
    if unitares_verdict == "high-risk":
        try:
            reject_threshold = config.RISK_REJECT_THRESHOLD
        except AttributeError:
            reject_threshold = config.RISK_REVISE_THRESHOLD + 0.20
        effective_reject = get_effective_threshold("risk_reject_threshold", default=reject_threshold)
        is_critical = risk_score >= effective_reject
        return {
            'action': 'pause',
            'sub_action': 'risk_pause',
            'reason': f'UNITARES high-risk verdict (risk_score={risk_score:.2f}) - safety pause suggested',
            'guidance': 'This is a safety check, not a failure. The system detected high ethical risk and is protecting you from potential issues. Consider simplifying your approach.',
            'critical': is_critical,
            'basin': basin,
            'margin': 'critical',
            'nearest_edge': 'risk',
        }

    # --- Priority 5: LOW basin → pause (structural degradation) ---
    if basin == "low":
        try:
            reject_threshold = config.RISK_REJECT_THRESHOLD
        except AttributeError:
            reject_threshold = config.RISK_REVISE_THRESHOLD + 0.20
        effective_reject = get_effective_threshold("risk_reject_threshold", default=reject_threshold)
        is_critical = risk_score >= effective_reject or state.coherence < config.COHERENCE_CRITICAL_THRESHOLD

        return {
            'action': 'pause',
            'sub_action': 'basin_pause',
            'reason': f'Low basin (I={state.I:.2f}, coherence={state.coherence:.2f}, risk={risk_score:.2f})',
            'guidance': 'State has entered the low basin. Simplify approach or take a break.',
            'critical': is_critical,
            'basin': basin,
            'margin': margin_info['margin'],
            'nearest_edge': margin_info['nearest_edge'],
        }

    # --- Priority 6: BOUNDARY basin → proceed with guide, tight margin ---
    if basin == "boundary":
        # In the boundary region, always guide regardless of verdict
        return {
            'action': 'proceed',
            'sub_action': 'guide',
            'reason': f'Boundary basin — near state-space edge (risk={risk_score:.2f}, I={state.I:.2f})',
            'guidance': 'Operating near basin boundary. Maintain current approach; avoid increasing complexity.',
            'critical': False,
            'basin': basin,
            'margin': 'tight',
            'nearest_edge': margin_info.get('nearest_edge'),
        }

    # --- Priority 7: HIGH basin → standard verdict-driven logic ---
    if unitares_verdict == "caution":
        return {
            'action': 'proceed',
            'sub_action': 'guide',
            'reason': f'Proceeding mindfully (risk: {risk_score:.2f})',
            'guidance': 'Navigating complexity. Worth a moment of reflection.',
            'critical': False,
            'verdict_context': 'aware',
            'basin': basin,
            'margin': margin_info['margin'],
            'nearest_edge': margin_info['nearest_edge'],
        }

    # HIGH basin + safe/no verdict → approve via standard config decision
    decision = config.make_decision(
        risk_score=risk_score,
        coherence=state.coherence,
        void_active=state.void_active,
        void_value=state.V,
        coherence_history=state.coherence_history,
    )
    decision['basin'] = basin
    return decision

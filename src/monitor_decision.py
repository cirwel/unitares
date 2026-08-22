"""Decision logic for governance monitor.

State-machine transitions are driven by **basin membership** — named
regions in EISV + coherence + risk state space (see ``governance_config``).

Basin → default transition:
  HIGH      → proceed (approve or guide per verdict)
  BOUNDARY  → proceed with guide, margin: tight
  LOW       → pause (suggest dialectic if sustained)

CIRS hard_block and high-risk verdict still override basin logic.
"""

from collections.abc import Mapping
import math
from numbers import Real
from typing import Any, Dict, Optional, TYPE_CHECKING

from config.governance_config import (
    BASIN_LOW_COHERENCE_CEIL,
    BASIN_LOW_I_CEIL,
    BASIN_LOW_RISK_FLOOR,
    BASIN_LOW_V_ABS_FLOOR,
    classify_basin,
    config,
)
from src.logging_utils import get_logger

if TYPE_CHECKING:
    from src.cirs import OscillationState

logger = get_logger(__name__)

HARD_STOP_PROVENANCE_SCHEMA = "eisv.hard-stop-provenance.v1"
_CIRS_HARD_STOP_PROVENANCE_SCHEMA = "cirs.hard-stop-provenance.v1"


def get_effective_threshold(name: str, default: float) -> float:
    """Get effective threshold, allowing runtime overrides."""
    try:
        from src.runtime_config import get_effective_threshold as _get
        return _get(name, default=default)
    except ImportError:
        return default


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _build_hard_stop_provenance(
    *,
    state: Any,
    risk_score: float,
    unitares_verdict: Optional[str],
    response_tier: Optional[str],
    oscillation_state: Optional['OscillationState'],
    cirs_result: Mapping[str, Any] | None,
    basin: str,
    effective_coherence_threshold: float,
) -> dict[str, Any]:
    """Record every satisfied hard-stop condition before priority selection.

    ``nearest_edge`` is a lossy display label: the decision stack returns on the
    first matching branch, so that label cannot prove that no other safety gate
    was simultaneously active.  This record evaluates the full hard-stop set
    and includes a risk-neutral basin counterfactual.  Consumers may treat a
    pause as fallback-risk-only only when this versioned record is complete and
    has no independent hard stop.
    """
    from src.cirs import CIRS_DEFAULTS

    E = _finite_number(getattr(state, "E", None))
    I = _finite_number(getattr(state, "I", None))
    S = _finite_number(getattr(state, "S", None))
    V = _finite_number(getattr(state, "V", None))
    coherence = _finite_number(getattr(state, "coherence", None))
    risk = _finite_number(risk_score)
    coherence_threshold = _finite_number(effective_coherence_threshold)
    void_observed = getattr(state, "void_active", None)
    void_known = isinstance(void_observed, bool)
    inputs_complete = all(
        value is not None
        for value in (E, I, S, V, coherence, risk, coherence_threshold)
    ) and void_known

    basin_without_risk = None
    if inputs_complete:
        basin_without_risk = classify_basin(
            E=E,
            I=I,
            S=S,
            V=V,
            coherence=coherence,
            risk_score=0.0,
        )

    hard_block = response_tier == "hard_block"
    resonance_known = response_tier is None or oscillation_state is not None
    resonant = bool(oscillation_state and oscillation_state.resonant)
    cirs_mode = "not_supplied"
    cirs_complete = True
    cirs_coherence_floor = False
    cirs_risk_ceiling = False
    cirs_thresholds: dict[str, float | None] = {
        "coherence_floor": None,
        "risk_ceiling": None,
        "oscillation_index": None,
        "flips": None,
    }
    cirs_observed: dict[str, float | int | None] = {
        "coherence": coherence,
        "risk_score": risk,
        "oscillation_index": None,
        "flips": None,
    }

    adaptive_provenance = (
        cirs_result.get("hard_stop_provenance")
        if isinstance(cirs_result, Mapping)
        else None
    )
    if isinstance(cirs_result, Mapping):
        cirs_mode = "adaptive_v2"
        adaptive_provenance = (
            adaptive_provenance
            if isinstance(adaptive_provenance, Mapping)
            else {}
        )
        thresholds = adaptive_provenance.get("thresholds")
        thresholds = thresholds if isinstance(thresholds, Mapping) else {}
        conditions = adaptive_provenance.get("conditions")
        conditions = conditions if isinstance(conditions, Mapping) else {}
        observed = adaptive_provenance.get("observed")
        observed = observed if isinstance(observed, Mapping) else {}
        floor = _finite_number(thresholds.get("coherence_floor"))
        ceiling = _finite_number(thresholds.get("risk_ceiling"))
        oi_threshold = _finite_number(thresholds.get("oscillation_index"))
        flip_threshold = thresholds.get("flips")
        observed_coherence = _finite_number(observed.get("coherence"))
        observed_risk = _finite_number(observed.get("risk_score"))
        observed_oi = _finite_number(observed.get("oscillation_index"))
        observed_flips = observed.get("flips")
        floor_condition = conditions.get("coherence_floor")
        ceiling_condition = conditions.get("risk_ceiling")
        resonance_condition = conditions.get("resonance")
        result_resonant = cirs_result.get("resonant")
        tier_known = response_tier in {
            "hard_block",
            "safe",
            "caution",
            "high-risk",
        }
        cirs_complete = (
            adaptive_provenance.get("schema")
            == _CIRS_HARD_STOP_PROVENANCE_SCHEMA
            and adaptive_provenance.get("complete") is True
            and adaptive_provenance.get("mode") == "adaptive_v2"
            and floor is not None
            and ceiling is not None
            and oi_threshold is not None
            and isinstance(flip_threshold, int)
            and not isinstance(flip_threshold, bool)
            and observed_coherence == coherence
            and observed_risk == risk
            and observed_oi is not None
            and isinstance(observed_flips, int)
            and not isinstance(observed_flips, bool)
            and isinstance(floor_condition, bool)
            and isinstance(ceiling_condition, bool)
            and isinstance(resonance_condition, bool)
            and isinstance(result_resonant, bool)
            and tier_known
            and cirs_result.get("verdict") == response_tier
            and resonance_known
            and bool(oscillation_state.resonant) == result_resonant
            and _finite_number(oscillation_state.oi) == observed_oi
            and oscillation_state.flips == observed_flips
            and coherence is not None
            and risk is not None
            and floor_condition == (coherence < floor)
            and ceiling_condition == (risk > ceiling)
            and resonance_condition
            == (
                abs(observed_oi) >= oi_threshold
                or observed_flips >= flip_threshold
            )
            and resonance_condition == result_resonant
        )
        cirs_thresholds = {
            "coherence_floor": floor,
            "risk_ceiling": ceiling,
            "oscillation_index": oi_threshold,
            "flips": flip_threshold,
        }
        cirs_observed = {
            "coherence": observed_coherence,
            "risk_score": observed_risk,
            "oscillation_index": observed_oi,
            "flips": observed_flips,
        }
        if cirs_complete:
            cirs_coherence_floor = floor_condition
            cirs_risk_ceiling = ceiling_condition
    elif response_tier is not None:
        # The legacy monitor path has fixed absolute thresholds and returns no
        # CIRS result envelope.  Those exact values are therefore reproducible
        # here.  Adaptive v2 always supplies ``cirs_result`` on the live path.
        cirs_mode = "legacy_v0_1"
        floor = _finite_number(CIRS_DEFAULTS["tau_low"])
        ceiling = _finite_number(CIRS_DEFAULTS["beta_high"])
        cirs_thresholds = {
            "coherence_floor": floor,
            "risk_ceiling": ceiling,
            "oscillation_index": _finite_number(CIRS_DEFAULTS["oi_threshold"]),
            "flips": CIRS_DEFAULTS["flip_threshold"],
        }
        observed_oi = (
            _finite_number(oscillation_state.oi)
            if oscillation_state is not None
            else None
        )
        observed_flips = (
            oscillation_state.flips if oscillation_state is not None else None
        )
        cirs_observed = {
            "coherence": coherence,
            "risk_score": risk,
            "oscillation_index": observed_oi,
            "flips": observed_flips,
        }
        cirs_complete = (
            coherence is not None
            and risk is not None
            and floor is not None
            and ceiling is not None
            and response_tier in {"hard_block", "soft_dampen", "proceed"}
            and (
                response_tier is None
                or (
                    observed_oi is not None
                    and isinstance(observed_flips, int)
                    and not isinstance(observed_flips, bool)
                    and resonant
                    == (
                        abs(observed_oi) >= CIRS_DEFAULTS["oi_threshold"]
                        or observed_flips >= CIRS_DEFAULTS["flip_threshold"]
                    )
                )
            )
        )
        if cirs_complete:
            cirs_coherence_floor = coherence < floor
            cirs_risk_ceiling = risk > ceiling

    # A hard block without a reproducible absolute trigger or an observed
    # resonance is explicitly unclassified.  Inconsistency between a supplied
    # CIRS tier and its trigger record is also unclassified.
    cirs_trigger_present = (
        cirs_coherence_floor or cirs_risk_ceiling or resonant
    )
    cirs_tier_inconsistent = (
        (hard_block and not cirs_trigger_present)
        or (not hard_block and (cirs_coherence_floor or cirs_risk_ceiling))
    )
    cirs_unclassified = (
        (hard_block and not cirs_complete)
        or cirs_tier_inconsistent
    )
    cirs_complete = cirs_complete and resonance_known
    if hard_block and not resonance_known:
        cirs_unclassified = True

    void_active = void_observed is True
    policy_coherence_floor = bool(
        coherence is not None
        and coherence_threshold is not None
        and coherence < coherence_threshold
    )
    high_risk_verdict = unitares_verdict == "high-risk"
    basin_risk_floor = bool(
        risk is not None and risk >= BASIN_LOW_RISK_FLOOR
    )
    basin_low_integrity = bool(I is not None and I < BASIN_LOW_I_CEIL)
    basin_low_coherence = bool(
        coherence is not None and coherence < BASIN_LOW_COHERENCE_CEIL
    )
    basin_high_abs_valence = bool(
        V is not None and abs(V) > BASIN_LOW_V_ABS_FLOOR
    )
    low_basin = basin == "low"
    independent_low_basin = low_basin and basin_without_risk == "low"

    risk_hard_stops = [
        name
        for name, active in (
            ("cirs_risk_ceiling", cirs_risk_ceiling),
            ("high_risk_verdict", high_risk_verdict),
            ("basin_risk_floor", basin_risk_floor),
        )
        if active
    ]
    independent_hard_stops = [
        name
        for name, active in (
            ("cirs_resonance", resonant),
            ("cirs_coherence_floor", cirs_coherence_floor),
            ("cirs_unclassified_hard_block", cirs_unclassified),
            ("void_active", void_active),
            ("policy_coherence_floor", policy_coherence_floor),
            ("independent_low_basin", independent_low_basin),
        )
        if active
    ]
    complete = inputs_complete and cirs_complete and not cirs_tier_inconsistent

    return {
        "schema": HARD_STOP_PROVENANCE_SCHEMA,
        "complete": complete,
        "risk_only": (
            complete
            and bool(risk_hard_stops)
            and not independent_hard_stops
        ),
        "risk_hard_stops": risk_hard_stops,
        "independent_hard_stops": independent_hard_stops,
        "cirs": {
            "mode": cirs_mode,
            "response_tier": response_tier,
            "provenance_complete": cirs_complete,
            "observed": cirs_observed,
            "thresholds": cirs_thresholds,
            "conditions": {
                "coherence_floor": cirs_coherence_floor,
                "risk_ceiling": cirs_risk_ceiling,
                "resonance": resonant,
                "unclassified_hard_block": cirs_unclassified,
            },
        },
        "policy": {
            "observed": {
                "E": E,
                "I": I,
                "S": S,
                "V": V,
                "coherence": coherence,
                "risk_score": risk,
                "void_active": void_observed,
                "verdict": unitares_verdict,
                "basin": basin,
            },
            "thresholds": {
                "coherence_critical": coherence_threshold,
                "basin_low_I": BASIN_LOW_I_CEIL,
                "basin_low_coherence": BASIN_LOW_COHERENCE_CEIL,
                "basin_low_abs_V": BASIN_LOW_V_ABS_FLOOR,
                "basin_low_risk": BASIN_LOW_RISK_FLOOR,
            },
            "conditions": {
                "void_active": void_active,
                "coherence_floor": policy_coherence_floor,
                "high_risk_verdict": high_risk_verdict,
                "low_basin": low_basin,
                "basin_low_integrity": basin_low_integrity,
                "basin_low_coherence": basin_low_coherence,
                "basin_high_abs_valence": basin_high_abs_valence,
                "basin_risk_floor": basin_risk_floor,
                "independent_low_basin": independent_low_basin,
            },
            "risk_neutral_counterfactual": {
                "risk_score": 0.0,
                "basin": basin_without_risk,
            },
        },
    }


def _with_hard_stop_provenance(
    decision: Dict[str, Any],
    provenance: Mapping[str, Any],
) -> Dict[str, Any]:
    """Attach the selected policy route without discarding the full trigger set."""
    result = dict(decision)
    selected = {
        "action": result.get("action"),
        "sub_action": result.get("sub_action"),
        "nearest_edge": result.get("nearest_edge"),
    }
    result["hard_stop_provenance"] = {
        **dict(provenance),
        "selected_decision": selected,
    }
    return result


def make_decision(
    state,
    risk_score: float,
    unitares_verdict: Optional[str] = None,
    response_tier: Optional[str] = None,
    oscillation_state: Optional['OscillationState'] = None,
    cirs_result: Mapping[str, Any] | None = None,
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
        cirs_result: CIRS v2 result carrying exact hard-stop thresholds.

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
        # Provenance decides whether the coherence edge is measurable at all.
        # Absent on the state today (the producer emits it alongside the metric),
        # so this reads None and the edge reports as unmeasurable -- which is the
        # correct answer while coherence_role is 'ode_control_feedback'
        # fleet-wide, and self-corrects when the producer wires it through.
        coherence_role=getattr(state, 'coherence_role', None),
        # Same-provenance assertion for the history window. Untagged today, so
        # this reads None and the adaptive band stays closed -- correct while the
        # producer has not yet reset history on a role change.
        coherence_history_role=getattr(state, 'coherence_history_role', None),
        # The threshold check_void_state actually decided void_active against.
        void_threshold=getattr(state, 'void_threshold_effective', None),
    )

    basin = classify_basin(
        E=state.E, I=state.I, S=state.S, V=state.V,
        coherence=state.coherence, risk_score=risk_score,
    )
    effective_coherence_threshold = get_effective_threshold(
        "coherence_critical_threshold",
        default=config.COHERENCE_CRITICAL_THRESHOLD,
    )
    hard_stop_provenance = _build_hard_stop_provenance(
        state=state,
        risk_score=risk_score,
        unitares_verdict=unitares_verdict,
        response_tier=response_tier,
        oscillation_state=oscillation_state,
        cirs_result=cirs_result,
        basin=basin,
        effective_coherence_threshold=effective_coherence_threshold,
    )

    # --- Priority 1: CIRS hard_block override ---
    # Attribute the display reason from the same complete trigger record that
    # downstream authority guards consume.  The record still retains every
    # simultaneous trigger even though the reason can name only one.
    if response_tier == 'hard_block':
        cirs_provenance = hard_stop_provenance["cirs"]
        cirs_conditions = cirs_provenance["conditions"]
        cirs_thresholds = cirs_provenance["thresholds"]
        resonant = bool(oscillation_state and oscillation_state.resonant)
        if resonant:
            oi = oscillation_state.oi
            flips = oscillation_state.flips
            reason = (
                f'CIRS resonance detected (OI={oi:.2f}, flips={flips}) — decision oscillating'
            )
            guidance = 'Governance is flip-flopping. Reduce complexity or wait for state to settle.'
            nearest_edge = 'oscillation'
        elif cirs_conditions["coherence_floor"]:
            floor = cirs_thresholds["coherence_floor"]
            reason = (
                'CIRS legacy control-feedback floor crossed '
                f'(value={state.coherence:.2f} < {floor:.2f})'
            )
            guidance = (
                'A configured compatibility backstop fired. Inspect coherence '
                'producer provenance, EISV, and risk attribution before drawing '
                'a health conclusion.'
            )
            nearest_edge = 'coherence'
        elif cirs_conditions["risk_ceiling"]:
            ceiling = cirs_thresholds["risk_ceiling"]
            reason = (
                'CIRS risk ceiling breached '
                f'(risk={risk_score:.2f} > {ceiling:.2f})'
            )
            guidance = 'Risk score exceeded the hard-block ceiling. Pause to investigate the input driving the spike.'
            nearest_edge = 'risk'
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
        return _with_hard_stop_provenance({
            'action': 'pause',
            'sub_action': 'cirs_block',
            'reason': reason,
            'guidance': guidance,
            'critical': False,
            'basin': basin,
            'margin': 'critical',
            'nearest_edge': nearest_edge,
        }, hard_stop_provenance)

    # --- Priority 2: void_active → pause (runtime adaptive threshold) ---
    if state.void_active:
        return _with_hard_stop_provenance({
            'action': 'pause',
            'sub_action': 'void_pause',
            'reason': 'Energy-integrity imbalance detected — time to recalibrate',
            'guidance': 'System needs a moment to stabilize. Take a break or shift focus.',
            'critical': False,
            'basin': basin,
            'margin': 'critical',
            'nearest_edge': 'void',
        }, hard_stop_provenance)

    # --- Priority 3: legacy control feedback below configured floor → pause ---
    if state.coherence < effective_coherence_threshold:
        return _with_hard_stop_provenance({
            'action': 'pause',
            'sub_action': 'coherence_pause',
            'reason': (
                'Configured legacy control-feedback floor crossed '
                f'({state.coherence:.2f} < {effective_coherence_threshold:.2f})'
            ),
            'guidance': (
                'This compatibility safety backstop is not a health diagnosis. '
                'Inspect coherence_source, EISV, and risk_attribution before acting.'
            ),
            'critical': True,
            'basin': basin,
            'margin': 'critical',
            'nearest_edge': 'coherence',
        }, hard_stop_provenance)

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
        return _with_hard_stop_provenance({
            'action': 'pause',
            'sub_action': 'risk_pause',
            'reason': f'UNITARES high-risk verdict (risk_score={risk_score:.2f}) - safety pause suggested',
            # Honest provenance, regime-aware: which signal drove this verdict
            # depends on warmup. Post-warmup, with Φ telemetry (the default), it
            # is the independent behavioral assessment; pre-warmup it is the Φ
            # cold-start prior (mostly server-derived). risk_attribution carries
            # the exact primary_driver, so the guidance points there rather than
            # asserting a single (now-stale) "self-attested" provenance
            # (dogfood 2026-06-13 P0; driver-accuracy correction 2026-06-28).
            'guidance': (
                'This is a safety check, not a failure. This check-in scored '
                'high-risk. See risk_attribution for what drove it: once your '
                'behavioral baseline is warm the verdict is an independent '
                'assessment of your trajectory; before then it leans on the '
                'signals you reported (ethical_drift, complexity, confidence). '
                'Consider simplifying your approach or requesting a dialectic '
                'review.'
            ),
            'critical': is_critical,
            'basin': basin,
            'margin': 'critical',
            'nearest_edge': 'risk',
        }, hard_stop_provenance)

    # --- Priority 5: LOW basin → pause (structural degradation) ---
    if basin == "low":
        try:
            reject_threshold = config.RISK_REJECT_THRESHOLD
        except AttributeError:
            reject_threshold = config.RISK_REVISE_THRESHOLD + 0.20
        effective_reject = get_effective_threshold("risk_reject_threshold", default=reject_threshold)
        is_critical = risk_score >= effective_reject or state.coherence < config.COHERENCE_CRITICAL_THRESHOLD

        return _with_hard_stop_provenance({
            'action': 'pause',
            'sub_action': 'basin_pause',
            'reason': (
                f'Low policy basin (I={state.I:.2f}, '
                f'legacy_control_feedback={state.coherence:.2f}, risk={risk_score:.2f})'
            ),
            'guidance': (
                'A configured basin boundary was crossed. Inspect the individual '
                'inputs and their provenance before interpreting the cause.'
            ),
            'critical': is_critical,
            'basin': basin,
            'margin': margin_info['margin'],
            'nearest_edge': margin_info['nearest_edge'],
            'unmeasurable_edges': margin_info.get('unmeasurable_edges', []),
            'margin_scope': margin_info.get('margin_scope', 'all_edges'),
        }, hard_stop_provenance)

    # --- Priority 6: BOUNDARY basin → proceed with guide ---
    if basin == "boundary":
        # In the boundary region, always guide regardless of verdict.
        #
        # `basin` and `margin` are two DIFFERENT notions of "edge" and this
        # branch used to collapse them: it hardcoded margin='tight' because the
        # EISV state sits near a BASIN boundary, then reported nearest_edge from
        # margin_info, which is None whenever the threshold margin is
        # comfortable. The result said "you are near an edge" while being unable
        # to name one -- and it violated this module's own documented contract,
        # that only tight/warning/critical carry a non-null nearest_edge
        # (see _ACTIONABLE_MARGINS in response_formatter). Because "tight" is in
        # that actionable set, a comfortable agent was surfaced as needing
        # attention on an unnameable edge.
        #
        # The boundary condition is not lost by reporting the real margin: it is
        # already carried by `basin`, by sub_action='guide', and by the reason
        # and guidance strings. Margin goes back to meaning one thing --
        # distance to a decision threshold.
        return _with_hard_stop_provenance({
            'action': 'proceed',
            'sub_action': 'guide',
            'reason': f'Boundary basin — near state-space edge (risk={risk_score:.2f}, I={state.I:.2f})',
            'guidance': 'Operating near basin boundary. Maintain current approach; avoid increasing complexity.',
            'critical': False,
            'basin': basin,
            'margin': margin_info['margin'],
            'nearest_edge': margin_info['nearest_edge'],
            'unmeasurable_edges': margin_info.get('unmeasurable_edges', []),
            'margin_scope': margin_info.get('margin_scope', 'all_edges'),
        }, hard_stop_provenance)

    # --- Priority 7: HIGH basin → standard verdict-driven logic ---
    if unitares_verdict == "caution":
        return _with_hard_stop_provenance({
            'action': 'proceed',
            'sub_action': 'guide',
            'reason': f'Proceeding mindfully (risk: {risk_score:.2f})',
            'guidance': 'Navigating complexity. Worth a moment of reflection.',
            'critical': False,
            'verdict_context': 'aware',
            'basin': basin,
            'margin': margin_info['margin'],
            'nearest_edge': margin_info['nearest_edge'],
            'unmeasurable_edges': margin_info.get('unmeasurable_edges', []),
            'margin_scope': margin_info.get('margin_scope', 'all_edges'),
        }, hard_stop_provenance)

    # HIGH basin + safe/no verdict → approve via standard config decision
    decision = config.make_decision(
        risk_score=risk_score,
        coherence=state.coherence,
        void_active=state.void_active,
        void_value=state.V,
        coherence_history=state.coherence_history,
    )
    decision['basin'] = basin

    # F2 fast-trip: the gate above runs on the (possibly task-adjusted) risk_score.
    # The latest *raw* risk observation can spike past the pause threshold while
    # the adjusted/smoothed value still clears — a real spike then silently
    # approves. Surface at least a guide so the latent spike is not invisible.
    decision = _maybe_latest_risk_fast_trip(state, decision, risk_score)
    return _with_hard_stop_provenance(decision, hard_stop_provenance)


def _maybe_latest_risk_fast_trip(state, decision: Dict, gated_risk: float) -> Dict:
    """Upgrade a clean 'approve' to 'guide' when the latest raw risk observation
    crossed the pause (revise) threshold even though the gated risk cleared it.

    Never weakens a decision: applies only when the action is already an
    unqualified ``proceed``/``approve``. Pauses and existing guides are left
    intact. This is the F2 fast-trip — it guarantees that a single check-in whose
    latest risk reaches PAUSE raises at least a guide, regardless of whether the
    gated value was task-adjusted or smoothed below threshold.
    """
    if decision.get('action') != 'proceed' or decision.get('sub_action') != 'approve':
        return decision
    if not state.risk_history:
        return decision
    latest_risk = float(state.risk_history[-1])
    pause_threshold = get_effective_threshold(
        "risk_revise_threshold", default=config.RISK_REVISE_THRESHOLD)
    if latest_risk >= pause_threshold and latest_risk > gated_risk:
        decision['sub_action'] = 'guide'
        decision['reason'] = (
            f'Latest risk spiked (risk_latest={latest_risk:.2f} >= '
            f'{pause_threshold:.2f}) though gated risk cleared (risk={gated_risk:.2f})'
        )
        decision['guidance'] = (
            'A recent observation crossed the pause threshold even though the '
            'adjusted/smoothed risk did not. Reflect on whether the latest step '
            'introduced real risk before continuing.'
        )
        decision['latest_risk_fast_trip'] = {
            'latest_risk': round(latest_risk, 4),
            'gated_risk': round(gated_risk, 4),
            'threshold': round(pause_threshold, 4),
        }
    return decision

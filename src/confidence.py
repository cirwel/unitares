"""
Confidence Derivation Module (compatibility fallback path)

Used ONLY when agents report no confidence. When agents provide confidence,
it passes through uncapped (see governance_monitor.py process_update).

Derives confidence from observed tool outcomes and EISV state dynamics.

Known compatibility debt: the deployed formula still assigns 55% of its base
to ``state.coherence``, whose producer is legacy ``C(V_ODE)`` directional
control feedback. That input is not independent confidence or health evidence.
The dependency is surfaced in returned metadata so a prospective deconfounding
experiment can measure the distributional/calibration shift before changing the
live formula. Do not silently reweight this path: its output feeds calibration
history and can later affect the entropy penalty.

Second un-validated term: the per-agent offset (``stable_agent_offset``).
Making it reproducible across restarts fixed a defect in HOW the term is
computed. It established nothing about WHETHER agent-ID-derived jitter belongs
in a confidence estimate at all. A stable +/-0.01 nudge keyed on an identifier
is not outcome-grounded signal, and nothing here shows it improves anything; it
exists to stop identical-EISV agents reporting one identical number. Treat it as
a prospective ablation candidate alongside the coherence dependency above, and
do not cite its determinism as evidence for it.
"""

import hashlib
import math
from typing import Dict, Any, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from src.governance_state import GovernanceState

from config.governance_config import config


LEGACY_COHERENCE_CONFIDENCE_ABLATION_SCHEMA = (
    "legacy_coherence_confidence_ablation.v1"
)
LEGACY_COHERENCE_CONFIDENCE_NEUTRAL_VALUE = 0.5

# Per-agent confidence offset: bucket count and half-width of the injected
# spread. 1000 buckets over +/-0.01 -> a 2e-5 quantum per bucket.
AGENT_OFFSET_BUCKETS = 1000
AGENT_OFFSET_HALF_WIDTH = 0.01

# Machine-readable marker for WHICH offset formula produced a stored value.
# Every agent's offset moves exactly once, at the rollout of this change. Without
# a marker in the telemetry, a later calibration analysis sees a one-time step in
# every agent at the same instant and cannot tell that formula migration from a
# real change in agent state. Bump this string on any future change to the
# scheme; do not reuse a version across two formulas.
AGENT_OFFSET_SCHEME = "blake2b-v1"


def stable_agent_offset(agent_id: str) -> float:
    """Deterministic per-agent confidence offset in [-0.01, +0.01).

    Uses blake2b rather than the builtin ``hash()``. Python salts string
    hashing per interpreter process (PYTHONHASHSEED), so ``hash(agent_id)``
    gave the same agent a *different* offset after every server restart — the
    offset was constant only within one process, which is not what a
    per-agent identifier is for: it injected a fresh +/-0.01 step into that
    agent's calibration history at each boot, and made any test asserting on
    the offset a per-run lottery.

    Bucketing is intrinsically lossy: two distinct agent_ids collide with
    probability ~1/1000. That is a property of the quantization, not a
    defect. Callers must not treat "different agents get different offsets"
    as a guarantee; the guarantee is that one agent gets one offset, always.
    """
    digest = hashlib.blake2b(agent_id.encode("utf-8"), digest_size=8).digest()
    bucket = int.from_bytes(digest, "big") % AGENT_OFFSET_BUCKETS
    quantum = 2 * AGENT_OFFSET_HALF_WIDTH / AGENT_OFFSET_BUCKETS
    return (bucket - AGENT_OFFSET_BUCKETS / 2) * quantum



def _compute_deviation_signal(state: 'GovernanceState', agent_id: str = None) -> float:
    """
    Compute deviation penalty from EISV history.

    Compares current EISV values against rolling baseline (last 20 updates).
    When state suddenly shifts, penalty rises. When stable, penalty ≈ 0.

    Returns:
        Deviation penalty in [0, 0.25]. Returns 0 when insufficient history.
    """
    # Need history attributes on state
    histories = []
    current_vals = []
    for attr_hist, attr_val in [
        ('E_history', 'E'), ('I_history', 'I'),
        ('S_history', 'S'), ('V_history', 'V'),
    ]:
        hist = getattr(state, attr_hist, None)
        val = getattr(state, attr_val, None)
        if hist is None or val is None:
            return 0.0
        histories.append(list(hist))
        current_vals.append(float(val))

    # Need at least 5 entries for meaningful statistics
    min_len = min(len(h) for h in histories)
    if min_len < 5:
        return 0.0

    # Rolling window: last 20 entries
    window = 20
    z_scores = []
    for hist, current in zip(histories, current_vals):
        recent = hist[-window:]
        n = len(recent)
        mean = sum(recent) / n
        variance = sum((x - mean) ** 2 for x in recent) / n
        std = math.sqrt(variance) if variance > 0 else 0.0

        if std < 1e-8:
            # Near-zero std: no variability to measure deviation against
            z_scores.append(0.0)
        else:
            z_scores.append(abs(current - mean) / std)

    # L2 norm of z-scores
    z_norm = math.sqrt(sum(z ** 2 for z in z_scores))

    # Sigmoid map to [0, 0.25]: penalty = 0.25 * sigmoid(z_norm - 2)
    # At z_norm=0: penalty ≈ 0.03, at z_norm=2: penalty ≈ 0.125, at z_norm=4: penalty ≈ 0.22
    sigmoid = 1.0 / (1.0 + math.exp(-(z_norm - 2.0)))
    penalty = 0.25 * sigmoid

    return penalty


def derive_confidence(
    state: 'GovernanceState', 
    agent_id: str = None,
    apply_calibration: bool = True,
    response_text: str = None,
    reported_complexity: float = 0.5
) -> Tuple[float, Dict[str, Any]]:
    """
    Derive confidence by OBSERVING what already happened - not asking for reports.
    
    The system already tracks tool outcomes. It just needs to look instead of
    pretending it can't see. This version pulls from existing trackers.
    """
    from src.tool_usage_tracker import get_tool_usage_tracker
    
    metadata = {
        'source': 'observed',
        'reliability': 'medium'
    }
    
    # === OBSERVE TOOL OUTCOMES (system already knows this) ===
    tool_confidence = 0.5  # Default neutral
    
    if agent_id:
        try:
            tracker = get_tool_usage_tracker()
            # Look at recent tool calls for this agent (last hour)
            stats = tracker.get_usage_stats(window_hours=1, agent_id=agent_id)
            
            if stats.get('total_calls', 0) > 0:
                # Calculate success rate from what we already tracked
                tools = stats.get('tools', {})
                total_success = sum(t.get('success_count', 0) for t in tools.values())
                total_calls = sum(t.get('total_calls', 0) for t in tools.values())
                
                if total_calls > 0:
                    tool_confidence = total_success / total_calls
                    metadata['tool_stats'] = {
                        'total_calls': total_calls,
                        'success_rate': tool_confidence,
                        'window': '1h'
                    }
                    metadata['reliability'] = 'high' if total_calls >= 3 else 'medium'
        except Exception as e:
            # If tracker unavailable, continue with EISV only
            metadata['tracker_error'] = str(e)
    
    # === MIXED-PROVENANCE COMPATIBILITY ESTIMATE ===
    #
    # IMPORTANT: Avoid confidence saturation.
    # We explicitly incorporate entropy (S) and void magnitude (|V|) as *uncertainty penalties*.
    # High entropy and large |V| should reduce confidence. The legacy
    # ``coherence`` term below is retained to avoid an unshadowed production
    # distribution shift; it is directional controller feedback, not
    # independent evidence that confidence should rise.
    eisv_confidence = 0.5
    coherence_val = 0.5
    integrity_val = 0.5
    entropy_val = 0.5
    void_val = 0.0
    neutralized_eisv_confidence = None
    
    if state is not None:
        coherence_val = state.coherence if hasattr(state, 'coherence') else 0.5
        integrity_val = state.I if hasattr(state, 'I') else 0.5
        entropy_val = state.S if hasattr(state, 'S') else 0.5
        void_val = state.V if hasattr(state, 'V') else 0.0

        # Normalize void magnitude relative to threshold band (heuristic, monotonic).
        v_thresh = getattr(config, "VOID_THRESHOLD_INITIAL", 0.15) or 0.15
        void_norm = min(1.0, abs(float(void_val)) / float(v_thresh * 4.0)) if v_thresh > 0 else min(1.0, abs(float(void_val)))
        void_penalty = 0.25 * void_norm

        # Entropy penalty: higher S means more uncertainty.
        entropy_penalty = 0.20 * float(entropy_val)

        # Compatibility formula. Preserve its live weights until a prospective
        # candidate is outcome-calibrated; make the causal debt machine-readable.
        eisv_confidence = (
            (float(coherence_val) * 0.55) +
            (float(integrity_val) * 0.35) +
            ((1.0 - float(entropy_val)) * 0.10)
        ) - void_penalty - entropy_penalty

        # Prospective measurement-only candidate.  Replacing the directional
        # legacy controller with its transfer midpoint removes its variation
        # while preserving the deployed 55% weight and intercept.  This lets a
        # later trusted-outcome read ask whether that variation added anything
        # without changing today's confidence, calibration history, or policy.
        neutralized_eisv_confidence = (
            (LEGACY_COHERENCE_CONFIDENCE_NEUTRAL_VALUE * 0.55) +
            (float(integrity_val) * 0.35) +
            ((1.0 - float(entropy_val)) * 0.10)
        ) - void_penalty - entropy_penalty

        # Deviation-based penalty: breaks constant confidence when EISV shifts
        deviation_penalty = _compute_deviation_signal(state, agent_id)
        eisv_confidence -= deviation_penalty
        neutralized_eisv_confidence -= deviation_penalty

        eisv_confidence = float(max(0.0, min(1.0, eisv_confidence)))
        neutralized_eisv_confidence = float(
            max(0.0, min(1.0, neutralized_eisv_confidence))
        )

        metadata['eisv'] = {
            'coherence': float(coherence_val),
            'coherence_source': 'legacy_tanh_v',
            'coherence_role': 'ode_control_feedback',
            'coherence_weight': 0.55,
            'coherence_is_health_evidence': False,
            'integrity': float(integrity_val),
            'entropy': float(entropy_val),
            'void': float(void_val),
            'void_norm': float(void_norm),
            'void_penalty': float(void_penalty),
            'entropy_penalty': float(entropy_penalty),
            'deviation_penalty': float(deviation_penalty),
        }
        metadata['known_limitations'] = [
            'base confidence retains a 55% legacy ODE control-feedback term pending prospective deconfounding'
        ]
    
    # === COMBINE: EISV-based confidence with tool-gap deviation ===
    #
    # NOTE (Dec 2025): Previously blended tool_confidence with eisv_confidence.
    # This caused calibration inversion: high tool success → high confidence,
    # but trajectory_health (from phi/EISV) remained low → bins showed
    # high confidence with low health.
    #
    # FIX: Use EISV-only confidence as base. Tool confidence is NOT blended in
    # (preserving the calibration-inversion fix), but the GAP between tool and
    # EISV confidence is used as a deviation signal: disagreement between
    # tool outcomes and EISV state IS genuine uncertainty.
    #
    final_confidence = eisv_confidence
    # Compatibility source label retained for existing consumers; inspect the
    # nested coherence provenance before interpreting the estimate.
    metadata['source'] = 'eisv_with_variance'

    # Tool-EISV gap penalty: disagreement = uncertainty
    tool_eisv_gap = abs(tool_confidence - eisv_confidence)
    gap_penalty = 0.0
    if tool_eisv_gap > 0.1:
        gap_penalty = min(0.08, (tool_eisv_gap - 0.1) * 0.2)
        final_confidence -= gap_penalty
    metadata['tool_eisv_gap'] = float(tool_eisv_gap)
    metadata['gap_penalty'] = float(gap_penalty)

    # Deterministic per-agent offset to break identical-EISV convergence
    agent_offset = 0.0
    if agent_id:
        agent_offset = stable_agent_offset(agent_id)
        final_confidence += agent_offset
    metadata['agent_offset'] = float(agent_offset)
    # Emitted unconditionally, including for the agent_id-absent 0.0 case, so a
    # consumer can always tell which formula produced the value it is reading
    # rather than having to infer it from the record's timestamp.
    metadata['agent_offset_scheme'] = AGENT_OFFSET_SCHEME

    # Tool stats still recorded for transparency
    if metadata.get('tool_stats'):
        metadata['tool_confidence_excluded'] = tool_confidence
        metadata['exclusion_reason'] = 'Tool confidence used only for gap penalty, not blended (calibration consistency)'
    
    # Bound confidence to avoid pathological extremes.
    # NOTE: We intentionally avoid 1.0 in derived mode; perfect certainty is not available here.
    final_confidence = max(0.05, min(0.95, float(final_confidence)))
    metadata['confidence'] = final_confidence

    if neutralized_eisv_confidence is not None:
        candidate_gap = abs(tool_confidence - neutralized_eisv_confidence)
        candidate_gap_penalty = 0.0
        if candidate_gap > 0.1:
            candidate_gap_penalty = min(0.08, (candidate_gap - 0.1) * 0.2)
        candidate_confidence = neutralized_eisv_confidence - candidate_gap_penalty
        candidate_confidence += agent_offset
        candidate_confidence = max(0.05, min(0.95, float(candidate_confidence)))
        metadata['shadow_ablations'] = {
            'legacy_coherence_neutralized': {
                'schema': LEGACY_COHERENCE_CONFIDENCE_ABLATION_SCHEMA,
                'mode': 'measurement_only',
                'policy_applied': False,
                'eligible': True,
                'eligibility_reason': None,
                'applies_to': 'omitted_confidence_fallback_only',
                'intervention': {
                    'field': 'state.coherence',
                    'source': 'legacy_tanh_v',
                    'role': 'ode_control_feedback',
                    'operation': 'replace_with_transfer_midpoint',
                    'replacement_value': LEGACY_COHERENCE_CONFIDENCE_NEUTRAL_VALUE,
                    'preserved_weight': 0.55,
                },
                'deployed': {
                    'base': float(eisv_confidence),
                    'final': float(final_confidence),
                    'tool_gap_penalty': float(gap_penalty),
                },
                'candidate': {
                    'base': float(neutralized_eisv_confidence),
                    'final': float(candidate_confidence),
                    'tool_gap_penalty': float(candidate_gap_penalty),
                },
                'candidate_minus_deployed': {
                    'base': float(neutralized_eisv_confidence - eisv_confidence),
                    'final': float(candidate_confidence - final_confidence),
                },
                'not_modeled': [
                    'calibration_history_replay',
                    'entropy_feedback_replay',
                    'policy_effect',
                    'future_outcomes',
                ],
            }
        }
    
    return (final_confidence, metadata)

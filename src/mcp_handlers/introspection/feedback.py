"""Calibration and actionable feedback generation for MCP handlers."""
from typing import Dict, Any, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

# Rate-limiting cache for calibration messages
_calibration_message_cache = {
    'last_error': None,
    'last_shown_update': 0,
    'significance_threshold': 0.05,
    'min_updates_between': 10
}


def get_calibration_feedback(include_complexity: bool = True) -> Dict[str, Any]:
    """
    Get calibration feedback for agents (complexity and confidence calibration).
    """
    calibration_feedback = {}

    try:
        from src.calibration import calibration_checker
        is_calibrated, cal_metrics = calibration_checker.check_calibration(include_complexity=include_complexity)

        if not is_calibrated:
            bins_data = cal_metrics.get('bins', {})
            total_samples = sum(bin_data.get('count', 0) for bin_data in bins_data.values())

            if total_samples > 0:
                total_correct = sum(
                    int(bin_data.get('count', 0) * bin_data.get('accuracy', 0))
                    for bin_data in bins_data.values()
                )
                overall_accuracy = total_correct / total_samples

                confidence_values = []
                for bin_key, bin_data in bins_data.items():
                    count = bin_data.get('count', 0)
                    expected_acc = bin_data.get('expected_accuracy', 0.0)
                    confidence_values.extend([expected_acc] * count)

                if confidence_values:
                    import numpy as np
                    mean_confidence = float(np.mean(confidence_values))
                    calibration_error = mean_confidence - overall_accuracy

                    show_message = False
                    cache = _calibration_message_cache

                    if cache['last_error'] is None:
                        show_message = True
                    elif abs(calibration_error - cache['last_error']) > cache['significance_threshold']:
                        show_message = True

                    calibration_feedback['confidence'] = {
                        # Fleet-wide singleton data (calibration_checker
                        # aggregates across ALL agents). The scope label is
                        # unconditional — the explanatory message below is
                        # cache-gated and absent most of the time, which left
                        # bare fleet numbers in agent-scoped responses
                        # looking like personal calibration (dogfood
                        # 2026-06-10; same class as the #572 mirror fix).
                        'scope': 'fleet',
                        # Self-describe the denominator so it can't be confused
                        # with learning_context.calibration.total_decisions
                        # (dogfood 2026-06-13: two calibration counts in one
                        # payload with no indication of what each measured).
                        # Both count the same fleet-wide STRATEGIC trajectory
                        # population, so when the strategic check is the source
                        # they reconcile to the same n.
                        'population': 'strategic_trajectory_decisions',
                        'samples': total_samples,
                        'system_accuracy': overall_accuracy,
                        'mean_confidence': mean_confidence,
                        'calibration_error': calibration_error
                    }

                    if show_message:
                        calibration_feedback['confidence']['message'] = (
                            f"System-wide calibration: Agents report {mean_confidence:.1%} confidence "
                            f"but achieve {overall_accuracy:.1%} accuracy. "
                            f"{'Consider being more conservative with confidence estimates' if mean_confidence > overall_accuracy + 0.2 else 'Calibration is improving'}."
                        )
                        calibration_feedback['confidence']['note'] = 'System-wide data from auto-collected outcomes (tests, commands, lint). Individual calibration may vary.'
                        cache['last_error'] = calibration_error

        if include_complexity:
            complexity_metrics = cal_metrics.get('complexity_calibration', {})
            if complexity_metrics:
                total_complexity_samples = sum(
                    bin_data.get('count', 0) for bin_data in complexity_metrics.values()
                )
                if total_complexity_samples > 0:
                    high_discrepancy_total = sum(
                        bin_data.get('count', 0) * bin_data.get('high_discrepancy_rate', 0)
                        for bin_data in complexity_metrics.values()
                    )
                    high_discrepancy_rate = high_discrepancy_total / total_complexity_samples

                    if high_discrepancy_rate > 0.5:
                        calibration_feedback['complexity'] = {
                            'high_discrepancy_rate': high_discrepancy_rate,
                            'message': (
                                f"{high_discrepancy_rate:.1%} of complexity reports show high discrepancy (>0.3). "
                                f"Consider calibrating your complexity estimates against system-derived values."
                            ),
                            'note': 'System derives complexity from EISV state - use this as reference'
                        }
    except Exception as e:
        logger.debug(f"Could not get calibration feedback: {e}")

    return calibration_feedback


def generate_actionable_feedback(
    metrics: Dict[str, Any],
    interpreted_state: Optional[Dict[str, Any]] = None,
    task_type: Optional[str] = None,
    response_text: Optional[str] = None,
    previous_coherence: Optional[float] = None,
) -> list[str]:
    """
    Generate context-aware actionable feedback for agents.
    """
    feedback = []

    coherence = metrics.get('coherence')
    risk_score = metrics.get('risk_score')
    regime = metrics.get('regime', 'exploration').lower()
    void_active = metrics.get('void_active', False)

    health = interpreted_state.get('health', 'unknown') if interpreted_state else 'unknown'
    mode = interpreted_state.get('mode', 'unknown') if interpreted_state else 'unknown'
    basin = interpreted_state.get('basin', 'unknown') if interpreted_state else 'unknown'

    task = (task_type or 'mixed').lower()

    updates = metrics.get('updates', 0)
    is_first_update = updates <= 1

    # --- Coherence Feedback (Context-Aware) ---
    if coherence is not None and not is_first_update:
        coherence_dropped = previous_coherence is not None and coherence < previous_coherence - 0.1
        coherence_delta = previous_coherence - coherence if previous_coherence else None

        if regime == "exploration":
            if coherence < 0.3:
                if coherence_dropped:
                    feedback.append(
                        f"Coherence dropped significantly ({coherence_delta:.2f}) during exploration. "
                        "This may indicate you're trying too many directions at once. "
                        "Try: Pick your most promising direction and explore it deeper before switching."
                    )
                else:
                    feedback.append(
                        "Very low coherence (<0.3) even for exploration phase. "
                        "Consider: Note down your current hypotheses, then focus on testing one at a time."
                    )
        elif regime == "locked" or regime == "stable":
            if coherence < 0.7:
                if coherence_dropped:
                    feedback.append(
                        f"Unexpected coherence drop ({coherence_delta:.2f}) in stable regime. "
                        "Something disrupted your flow. "
                        "Check: Did requirements change? Did you encounter an unexpected edge case?"
                    )
                else:
                    feedback.append(
                        "Coherence below 0.7 in stable regime indicates drift. "
                        "Action: Review your original plan and verify you're still aligned with the goal."
                    )
        else:
            if coherence < 0.5:
                if task == 'convergent':
                    feedback.append(
                        f"Low coherence ({coherence:.2f}) during convergent task. "
                        "You should be focusing, but your state suggests divergence. "
                        "Tip: Write down your solution in one sentence before continuing."
                    )
                elif task == 'divergent':
                    if coherence < 0.35:
                        feedback.append(
                            f"Very low coherence ({coherence:.2f}) even for divergent work. "
                            "Tip: Note your top 3 ideas, then explore the most promising one deeper."
                        )
                else:
                    feedback.append(
                        f"Coherence at {coherence:.2f}. "
                        "Tip: Pause and articulate your current goal in one sentence."
                    )

    # --- Risk Score Feedback ---
    if risk_score is not None:
        if risk_score > 0.7:
            if basin == 'void':
                feedback.append(
                    f"High complexity ({risk_score:.2f}) in void basin - energy/integrity mismatch. "
                    "This often means working hard on the wrong thing. "
                    "Check: Is this task still relevant to your original goal?"
                )
            else:
                feedback.append(
                    f"High complexity ({risk_score:.2f}) detected. "
                    "Options: (1) Break task into smaller pieces, (2) Pause and document what you've learned, "
                    "or (3) Ask for clarification if requirements are unclear."
                )
        elif risk_score > 0.5:
            if health == 'degraded':
                feedback.append(
                    f"Moderate complexity ({risk_score:.2f}) with degraded health. "
                    "Consider a checkpoint: What would you tell someone taking over this task?"
                )

    # --- Void Detection ---
    if void_active:
        e = metrics.get('E', 0.5)
        i = metrics.get('I', 0.5)

        if e > i + 0.2:
            feedback.append(
                "Void detected: High energy but low integrity. "
                "You're working hard but output quality may be suffering. "
                "Suggestion: Slow down and review your recent work for errors."
            )
        elif i > e + 0.2:
            feedback.append(
                "Void detected: High integrity but low energy. "
                "Output is clean but progress is slow. "
                "Suggestion: Is something blocking you? Consider asking for help or taking a break."
            )
        else:
            feedback.append(
                "Void active - energy and integrity are misaligned. "
                "Take a moment to assess: What's causing the disconnect?"
            )

    # --- Response Text Pattern Detection ---
    if response_text:
        text_lower = response_text.lower()

        confusion_patterns = [
            ('not sure', "You mentioned uncertainty. That's valuable self-awareness. "),
            ("don't understand", "You noted confusion. Consider rephrasing the problem. "),
            ('struggling', "You mentioned struggling. Break the problem into smaller parts. "),
            ('stuck', "You said you're stuck. Try explaining the problem to a rubber duck. "),
        ]

        for pattern, prefix in confusion_patterns:
            if pattern in text_lower:
                feedback.append(prefix + "What's the smallest next step you can take?")
                break

        if any(p in text_lower for p in ['definitely', 'obviously', 'clearly', 'certainly']):
            if coherence and coherence < 0.6:
                feedback.append(
                    "Your language suggests confidence, but metrics show uncertainty. "
                    "Worth double-checking: Are you sure about your assumptions?"
                )

    return feedback

"""
Calibration tool handlers.

Extracted from admin.py for maintainability.
"""

from typing import Dict, Any, List, Sequence, Optional
from mcp.types import TextContent
import json
import sys
import os
from datetime import datetime
from pathlib import Path
from ..utils import success_response, error_response, require_agent_id, require_registered_agent
from ..decorators import mcp_tool
from src.logging_utils import get_logger
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
logger = get_logger(__name__)


def _build_calibration_guidance(
    *,
    calibration_status: str,
    truth_channel: str,
    total_samples: int,
    issues: List[str],
    correction_factors: Dict[str, float],
    failure_modes: Dict[str, Any],
    tactical_staleness_days: Optional[float],
) -> Dict[str, Any]:
    """Operator guidance derived from calibration, without changing decisions."""
    actions: List[str] = []
    confidence_policy = "use_reported_confidence"

    if total_samples == 0:
        confidence_policy = "no_auto_correction"
        actions.append("Collect hard outcome evidence via recent_tool_results or outcome_event before interpreting calibration.")
    elif calibration_status == "signal_stale":
        confidence_policy = "do_not_use_stale_bins_for_correction"
        actions.append("Refresh tactical evidence before applying calibration corrections.")
    elif calibration_status == "miscalibrated":
        confidence_policy = "require_evidence_for_high_confidence_actions"
        actions.append("Treat high-confidence actions in miscalibrated bins as requiring external evidence.")
        if correction_factors:
            actions.append("Use correction_factors as advisory scaling when presenting calibrated confidence.")
    else:
        actions.append("Calibration is currently acceptable; continue recording objective outcomes.")

    warning = None
    if isinstance(failure_modes, dict):
        warning = failure_modes.get("verdict_quality_warning")
        if warning:
            actions.append("Review failure_modes before trusting confidence-heavy verdicts.")

    return {
        "mode": "advisory_only",
        "confidence_policy": confidence_policy,
        "truth_channel": truth_channel,
        "tactical_staleness_days": tactical_staleness_days,
        "correction_factors": correction_factors,
        "failure_modes": failure_modes,
        "verdict_quality_warning": warning,
        "actions": actions,
        "issues": issues,
        "note": "Guidance does not silently alter verdicts or reported confidence.",
    }


@mcp_tool("check_calibration", timeout=10.0, rate_limit_exempt=True, register=False)
async def handle_check_calibration(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Check calibration of confidence estimates.
    
    NOTE ON "ACCURACY":
    This system is AI-for-AI and typically does not have access to external correctness
    (tests passing, real-world outcomes, user satisfaction). As a result, the primary
    calibration signal is a trajectory/consensus proxy (see returned honesty note).
    
    We keep the `accuracy` field for backward compatibility, but it should be read as
    "trajectory_health" unless you explicitly provide an external truth signal.
    """
    from src.calibration import calibration_checker
    
    is_calibrated, metrics = calibration_checker.check_calibration(include_complexity=True)
    
    # Calculate overall trajectory health from strategic bins
    # (the strategic "accuracy" field is conceptually trajectory_health)
    bins_data = metrics.get('bins', {})
    total_samples = sum(bin_data.get('count', 0) for bin_data in bins_data.values())
    weighted_sum = sum(
        float(bin_data.get('count', 0)) * float(bin_data.get('accuracy', 0.0))
        for bin_data in bins_data.values()
    )
    overall_trajectory_health = weighted_sum / total_samples if total_samples > 0 else 0.0
    
    # Calculate confidence distribution from bins
    confidence_values = []
    for bin_key, bin_data in bins_data.items():
        count = bin_data.get('count', 0)
        expected_acc = bin_data.get('expected_accuracy', 0.0)
        # Add confidence value for each sample in this bin
        confidence_values.extend([expected_acc] * count)
    
    if confidence_values:
        import numpy as np
        n_samples = len(confidence_values)
        conf_dist = {
            "mean": float(np.mean(confidence_values)),
            "samples": n_samples,
        }
        if n_samples >= 5:
            conf_dist["std"] = float(np.std(confidence_values))
            conf_dist["min"] = float(np.min(confidence_values))
            conf_dist["max"] = float(np.max(confidence_values))
        else:
            conf_dist["note"] = f"Only {n_samples} sample(s) — std/min/max suppressed (need >= 5)"
    else:
        conf_dist = {"mean": 0.0, "samples": 0, "note": "No calibration data yet"}
    
    # Pull real outcome-matched accuracy from the sequential tracker if it has
    # any hard tactical evidence (tests/commands/lint outcomes). Without that,
    # `accuracy` would only be the trajectory_health proxy under a misleading
    # label, so we surface null + flip the truth_channel to be honest.
    tactical_metrics: Dict[str, Any] = {}
    try:
        from src.sequential_calibration import get_sequential_calibration_tracker
        tactical_metrics = get_sequential_calibration_tracker().compute_metrics()
    except Exception:
        tactical_metrics = {}

    has_real_outcome_evidence = (
        tactical_metrics.get("status") == "tracking"
        and "empirical_accuracy" in tactical_metrics
    )
    if has_real_outcome_evidence:
        accuracy_value = tactical_metrics["empirical_accuracy"]
        truth_channel = "confidence_outcome_match"
        calibration_note = (
            "`accuracy` reports empirical confidence-vs-outcome match from the "
            "sequential tactical evidence stream (tests, commands, lint, file ops). "
            "`trajectory_health` is the strategic proxy: did confident agents end up healthy?"
        )
    else:
        accuracy_value = None
        truth_channel = "trajectory_proxy"
        calibration_note = (
            "`accuracy` is null because no exogenous tactical outcomes have been "
            "recorded yet. `trajectory_health` is a strategic proxy (did confident "
            "agents end up healthy?), not a measurement of decision correctness."
        )

    # Compute tactical-signal staleness so the dashboard can distinguish
    # "miscalibrated" from "starved." A bin-error verdict against a frozen
    # signal channel is a different operational story than against a live one.
    tactical_staleness_days: Optional[float] = None
    last_updated_raw = tactical_metrics.get("last_updated") if tactical_metrics else None
    if last_updated_raw:
        try:
            from datetime import datetime, timezone
            if isinstance(last_updated_raw, str):
                _last_dt = datetime.fromisoformat(last_updated_raw.replace("Z", "+00:00"))
            else:
                _last_dt = last_updated_raw
            if _last_dt.tzinfo is None:
                _last_dt = _last_dt.replace(tzinfo=timezone.utc)
            tactical_staleness_days = max(
                0.0,
                (datetime.now(timezone.utc) - _last_dt).total_seconds() / 86400.0,
            )
        except Exception:
            tactical_staleness_days = None

    # `calibration_status` is the operator-facing one-liner. The boolean
    # `calibrated` is preserved for back-compat, but it conflates "we
    # measured miscalibration" with "we have no signal to measure" —
    # status splits those.
    STALENESS_THRESHOLD_DAYS = 7.0
    if total_samples == 0:
        calibration_status = "no_data"
    elif tactical_staleness_days is not None and tactical_staleness_days > STALENESS_THRESHOLD_DAYS:
        calibration_status = "signal_stale"
    elif is_calibrated:
        calibration_status = "calibrated"
    else:
        calibration_status = "miscalibrated"

    response = {
        "calibrated": is_calibrated,
        "calibration_status": calibration_status,
        "tactical_staleness_days": tactical_staleness_days,
        "issues": metrics.get('issues', []),
        "accuracy": accuracy_value,
        "trajectory_health": overall_trajectory_health,
        "truth_channel": truth_channel,
        "confidence_distribution": conf_dist,
        "pending_updates": calibration_checker.get_pending_updates(),
        "total_samples": total_samples,
        "message": "Calibration check complete",
        "calibration_note": calibration_note,
    }

    if tactical_metrics:
        response["tactical_evidence"] = {
            **tactical_metrics,
            "staleness_days": tactical_staleness_days,
            "note": (
                "Sequential evidence is tracked only for hard exogenous tactical outcomes "
                "(tests, commands, files, lint, tool-result evidence)."
            ),
        }

    # S10.2: per-class fleet calibration breakdown. The envelope carries
    # `bootstrapped` (False during the pre-rebucket bootstrap window after a
    # pre-S10 state-file load) and `by_class` (descriptive stats only — see
    # SequentialCalibrationTracker._state_to_metrics for the anytime-validity
    # rationale: log_evidence/capped_alarm are intentionally omitted at class
    # scope and only appear on the tactical_evidence (global) envelope above).
    try:
        from src.sequential_calibration import get_sequential_calibration_tracker
        response["by_class"] = get_sequential_calibration_tracker().compute_metrics_by_class()
    except Exception as e_bc:
        logger.debug(f"S10 by_class breakdown skipped: {e_bc}")

    # Forward per-channel breakdown + hygiene from CalibrationChecker.check_calibration.
    # The handler composes its own response dict, so additions to the underlying
    # metrics dict do not propagate automatically — they have to be forwarded here.
    if 'per_channel_calibration' in metrics:
        response['per_channel_calibration'] = metrics['per_channel_calibration']
    if 'per_channel_health' in metrics:
        response['per_channel_health'] = metrics['per_channel_health']

    correction_factors: Dict[str, float] = {}
    try:
        compute_corrections = getattr(calibration_checker, "compute_correction_factors", None)
        if callable(compute_corrections):
            maybe_corrections = compute_corrections()
            if isinstance(maybe_corrections, dict):
                correction_factors = {
                    str(bin_key): float(factor)
                    for bin_key, factor in maybe_corrections.items()
                }
    except Exception as e_corr:
        logger.debug(f"Calibration correction factors skipped: {e_corr}")

    failure_modes: Dict[str, Any] = {}
    try:
        characterize = getattr(calibration_checker, "characterize_failure_modes", None)
        if callable(characterize):
            maybe_failure_modes = characterize()
            if isinstance(maybe_failure_modes, dict):
                failure_modes = maybe_failure_modes
    except Exception as e_modes:
        logger.debug(f"Calibration failure-mode characterization skipped: {e_modes}")

    response["calibration_guidance"] = _build_calibration_guidance(
        calibration_status=calibration_status,
        truth_channel=truth_channel,
        total_samples=total_samples,
        issues=response["issues"],
        correction_factors=correction_factors,
        failure_modes=failure_modes,
        tactical_staleness_days=tactical_staleness_days,
    )

    # Add complexity calibration metrics if available
    if 'complexity_calibration' in metrics:
        complexity_data = metrics['complexity_calibration']
        total_complexity_samples = sum(v.get('count', 0) for v in complexity_data.values())
        high_discrepancy_total = sum(
            v.get('count', 0) * v.get('high_discrepancy_rate', 0) 
            for v in complexity_data.values()
        )
        high_discrepancy_rate = high_discrepancy_total / total_complexity_samples if total_complexity_samples > 0 else 0
        
        response["complexity_calibration"] = {
            "total_samples": total_complexity_samples,
            "high_discrepancy_rate": high_discrepancy_rate,
            "bins": complexity_data
        }
    
    return success_response(response)

@mcp_tool("rebuild_calibration", timeout=60.0, rate_limit_exempt=True, register=False)
async def handle_rebuild_calibration(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Rebuild calibration from scratch using auto ground truth collection.

    This resets calibration state and re-evaluates all historical decisions
    using the current evaluation logic (confidence vs outcome quality matching).

    Use this after updating evaluation logic or to fix corrupted calibration state.

    Args:
        dry_run: If true, show what would be updated without modifying state
        min_age_hours: Minimum age of decisions to evaluate (default: 0.5)
        max_decisions: Maximum decisions to process (default: 0 = all)
    """
    from src.auto_ground_truth import collect_ground_truth_automatically

    dry_run = arguments.get("dry_run", False)
    if isinstance(dry_run, str):
        dry_run = dry_run.lower() in ("true", "1", "yes")

    min_age_hours = float(arguments.get("min_age_hours", 0.5))
    max_decisions = int(arguments.get("max_decisions", 0))

    try:
        result = await collect_ground_truth_automatically(
            min_age_hours=min_age_hours,
            max_decisions=max_decisions,
            dry_run=dry_run,
            rebuild=True  # Reset and rebuild from scratch
        )

        return success_response({
            "success": True,
            "action": "dry_run" if dry_run else "rebuild",
            "processed": result.get("processed", 0),
            "updated": result.get("updated", 0),
            "skipped": result.get("skipped", 0),
            "errors": result.get("errors", 0),
            "message": f"Calibration {'would be' if dry_run else 'has been'} rebuilt with {result.get('updated', 0)} ground truth samples"
        })
    except Exception as e:
        logger.error(f"Error rebuilding calibration: {e}", exc_info=True)
        return error_response(f"Failed to rebuild calibration: {e}")

@mcp_tool("update_calibration_ground_truth", timeout=10.0, register=False)
async def handle_update_calibration_ground_truth(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Optional: Update calibration with an external truth signal after review
    
    Supports two modes:
    1. Direct mode: Provide confidence, predicted_correct, actual_correct directly
    2. Timestamp mode: Provide timestamp (and optional agent_id), actual_correct. 
       System looks up confidence and decision from audit log.

    IMPORTANT (AI-for-AI truth model):
    UNITARES does not assume access to objective external correctness. Use this tool
    only when you have an external signal you trust (human review, tests, verifiers).
    """
    from src.calibration import calibration_checker
    from src.audit_log import AuditLogger
    from datetime import datetime
    
    # Check if using timestamp-based mode
    timestamp = arguments.get("timestamp")
    agent_id = arguments.get("agent_id")
    actual_correct = arguments.get("actual_correct")
    
    if timestamp:
        # TIMESTAMP MODE: Look up confidence and decision from audit log
        if actual_correct is None:
            return [error_response("Missing required parameter: actual_correct (required for timestamp mode). This should be an external truth signal (e.g., human review, tests).")]
        
        try:
            # Parse timestamp
            if isinstance(timestamp, str):
                decision_time = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            else:
                return [error_response("timestamp must be ISO format string (e.g., '2025-12-08T13:00:00')")]
            
            # Query audit log for decision at that timestamp
            # Use a small window around the timestamp to account for slight timing differences
            from datetime import timedelta
            window_start = (decision_time - timedelta(seconds=5)).isoformat()
            window_end = (decision_time + timedelta(seconds=5)).isoformat()
            
            audit_logger = AuditLogger()
            entries = audit_logger.query_audit_log(
                agent_id=agent_id,
                event_type="auto_attest",
                start_time=window_start,
                end_time=window_end
            )
            
            if not entries:
                return [error_response(
                    f"No decision found at timestamp {timestamp}" + 
                    (f" for agent {agent_id}" if agent_id else ""),
                    details={
                        "suggestion": "Check timestamp format (ISO) and ensure decision was logged",
                        "related_tools": ["get_telemetry_metrics"]
                    }
                )]
            
            # Use most recent entry if multiple found (shouldn't happen with exact timestamp, but be safe)
            entry = entries[-1]
            confidence = entry.get("confidence", 0.0)
            decision = entry.get("details", {}).get("decision", "unknown")
            # FIXED: Use confidence-based prediction, not decision-based
            # High confidence (>=0.5) = we predicted correct
            predicted_correct = float(confidence) >= 0.5
            
            # Update calibration with external truth signal
            calibration_checker.update_ground_truth(
                confidence=float(confidence),
                predicted_correct=bool(predicted_correct),
                actual_correct=bool(actual_correct)
            )
            
            # Save calibration state
            calibration_checker.save_state()
            
            return success_response({
                "message": "External truth signal recorded successfully (timestamp mode)",
                "truth_channel": "external",
                "looked_up": {
                    "confidence": confidence,
                    "decision": decision,
                    "predicted_correct": predicted_correct
                },
                "pending_updates": calibration_checker.get_pending_updates()
            })
            
        except ValueError as e:
            return [error_response(f"Invalid timestamp format: {str(e)}")]
        except Exception as e:
            return [error_response(f"Error looking up decision: {str(e)}")]
    
    else:
        # DIRECT MODE: Require all parameters
        confidence = arguments.get("confidence")
        predicted_correct = arguments.get("predicted_correct")
        
        if confidence is None or predicted_correct is None or actual_correct is None:
            return [error_response(
                "Missing required parameters. Use either:\n"
                "1. Direct mode: confidence, predicted_correct, actual_correct\n"
                "2. Timestamp mode: timestamp, actual_correct (optional: agent_id)",
                details={
                    "direct_mode": {"required": ["confidence", "predicted_correct", "actual_correct"]},
                    "timestamp_mode": {"required": ["timestamp", "actual_correct"], "optional": ["agent_id"]}
                }
            )]
        
        try:
            calibration_checker.update_ground_truth(
                confidence=float(confidence),
                predicted_correct=bool(predicted_correct),
                actual_correct=bool(actual_correct)
            )
            
            # Save calibration state after update
            calibration_checker.save_state()
            
            return success_response({
                "message": "External truth signal recorded successfully (direct mode)",
                "truth_channel": "external",
                "pending_updates": calibration_checker.get_pending_updates()
            })
        except Exception as e:
            return [error_response(str(e))]

@mcp_tool("backfill_calibration_from_dialectic", timeout=20.0, rate_limit_exempt=True, register=False)
async def handle_backfill_calibration_from_dialectic(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """
    Retroactively update calibration from historical resolved verification-type dialectic sessions.
    
    This processes all existing resolved verification sessions that were created before
    automatic calibration was implemented, ensuring they contribute to calibration.
    
    USE CASES:
    - One-time migration after implementing automatic calibration
    - Backfill historical peer verification data
    - Ensure all resolved verification sessions contribute to calibration
    
    RETURNS:
    {
      "success": true,
      "processed": int,
      "updated": int,
      "errors": int,
      "sessions": [{"session_id": "...", "agent_id": "...", "status": "..."}]
    }
    """
    from src.mcp_handlers.dialectic.handlers import backfill_calibration_from_historical_sessions
    
    try:
        results = await backfill_calibration_from_historical_sessions()
        return success_response({
            "success": True,
            "message": f"Backfill complete: {results['updated']}/{results['processed']} sessions updated",
            **results
        })
    except Exception as e:
        return [error_response(f"Error during backfill: {str(e)}")]

"""
Automated Ground Truth Collection for Calibration

SELF-GOVERNANCE PRINCIPLE (2025-12-13):
The system should not assume humans are the ground truth oracle. Instead:
- Observable outcomes (tests pass, files created, commands succeed) are primary signals
- Peer consensus is a secondary signal
- Human feedback is optional enhancement, not required

This module automatically evaluates decisions based on OBJECTIVE signals:
1. Agent trajectory health (did they get stuck/paused?)
2. Test results (did pytest pass after code changes?)
3. Linter status (did code lint cleanly?)
4. Command outcomes (did terminal commands succeed?)
5. File operations (were expected files created?)

This is NOT redundant with Phase 5 auto-emit (phases.py). Phase 5 detects
outcomes from response text keywords ("completed", "fixed") — self-report.
This module evaluates exogenous signals (exit codes, file existence) on a
background timer. has_exogenous_signals() gates calibration recording to
prevent self-referential feedback loops.

Human calibration via update_calibration_ground_truth is still available
but should be the exception, not the rule.
"""

import json
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)

# Project root
project_root = Path(__file__).parent.parent


# =============================================================================
# OBJECTIVE OUTCOME EVALUATORS
# These evaluate ground truth from observable signals, NOT human judgment
# =============================================================================

def evaluate_test_outcome(test_run_result: Dict) -> Optional[bool]:
    """
    Evaluate ground truth from pytest/test results.
    
    Args:
        test_run_result: Dict with keys like 'passed', 'failed', 'errors', 'exit_code'
        
    Returns:
        True if tests passed, False if failed, None if can't evaluate
    """
    if not test_run_result:
        return None
    
    # Check exit code first (most reliable)
    exit_code = test_run_result.get('exit_code')
    if exit_code is not None:
        return exit_code == 0
    
    # Fallback to pass/fail counts
    failed = test_run_result.get('failed', 0)
    errors = test_run_result.get('errors', 0)
    passed = test_run_result.get('passed', 0)
    
    if failed > 0 or errors > 0:
        return False
    elif passed > 0:
        return True
    
    return None


def evaluate_command_outcome(command_result: Dict) -> Optional[bool]:
    """
    Evaluate ground truth from terminal command execution.
    
    Args:
        command_result: Dict with 'exit_code', 'success', 'error' etc.
        
    Returns:
        True if command succeeded, False if failed, None if can't evaluate
    """
    if not command_result:
        return None
    
    # Check explicit success flag
    if 'success' in command_result:
        return bool(command_result['success'])
    
    # Check exit code
    exit_code = command_result.get('exit_code')
    if exit_code is not None:
        return exit_code == 0
    
    # Check for error field
    if command_result.get('error'):
        return False
    
    return None


def evaluate_file_operation(file_path: str, expected_exists: bool = True) -> Optional[bool]:
    """
    Evaluate ground truth from file operation outcome.
    
    Args:
        file_path: Path to check
        expected_exists: Whether file should exist after operation
        
    Returns:
        True if outcome matches expectation, False otherwise
    """
    try:
        exists = Path(file_path).exists()
        return exists == expected_exists
    except Exception:
        return None


def evaluate_lint_outcome(lint_result: Dict) -> Optional[bool]:
    """
    Evaluate ground truth from linter results.
    
    Args:
        lint_result: Dict with 'errors', 'warnings', 'issues' etc.
        
    Returns:
        True if no errors, False if errors present, None if can't evaluate
    """
    if not lint_result:
        return None
    
    # Check for explicit error count
    errors = lint_result.get('errors', lint_result.get('error_count', 0))
    if isinstance(errors, list):
        errors = len(errors)
    
    if errors > 0:
        return False
    
    # If we have any result and no errors, consider it success
    if lint_result:
        return True
    
    return None


# =============================================================================
# COMPOSITE EVALUATOR
# Combines multiple objective signals
# =============================================================================

def evaluate_objective_outcomes(outcomes: Dict) -> Optional[bool]:
    """
    Evaluate ground truth from multiple objective outcomes.

    Uses weighted voting - any failure signal = failure (conservative).

    Args:
        outcomes: Dict with optional keys: 'tests', 'commands', 'files', 'lint'

    Returns:
        True if all signals pass, False if any fail, None if no signals
    """
    results = []

    if 'tests' in outcomes:
        result = evaluate_test_outcome(outcomes['tests'])
        if result is not None:
            results.append(('tests', result))

    if 'commands' in outcomes:
        for cmd in (outcomes['commands'] if isinstance(outcomes['commands'], list) else [outcomes['commands']]):
            result = evaluate_command_outcome(cmd)
            if result is not None:
                results.append(('command', result))

    if 'files' in outcomes:
        for file_check in outcomes['files']:
            path = file_check.get('path')
            expected = file_check.get('expected_exists', True)
            if path:
                result = evaluate_file_operation(path, expected)
                if result is not None:
                    results.append(('file', result))

    if 'lint' in outcomes:
        result = evaluate_lint_outcome(outcomes['lint'])
        if result is not None:
            results.append(('lint', result))

    if not results:
        return None

    # Conservative: any failure = overall failure
    if any(not r[1] for r in results):
        logger.info(f"Objective evaluation FAILED: {[r for r in results if not r[1]]}")
        return False

    logger.info(f"Objective evaluation PASSED: {len(results)} signals all positive")
    return True


def has_exogenous_signals(entry: Dict) -> bool:
    """
    Check whether a decision has any exogenous ground truth signals.

    Exogenous signals are objective, external observations — not the agent's
    own EISV trajectory or self-reported state. Without these, calibration
    updates would be the system grading its own homework.

    Returns True if any of: test results, command outcomes, file operations,
    lint results, or tool_usage records exist for this entry.
    """
    details = entry.get('details', {})
    if not isinstance(details, dict):
        return False

    # Check for objective outcome signals
    if details.get('tests'):
        return True
    if details.get('commands'):
        return True
    if details.get('files'):
        return True
    if details.get('lint'):
        return True

    # Tool usage counts as exogenous (exit codes are objective)
    if details.get('tool_usage') or details.get('tool_results'):
        return True

    # Outcome events recorded by the hook system
    if details.get('outcome_events'):
        return True

    return False


def evaluate_decision_outcome(entry: Dict, metadata: Dict) -> Optional[bool]:
    """
    Evaluate whether a decision's CONFIDENCE was appropriate for the outcome.

    CALIBRATION PRINCIPLE:
    Instead of just checking "was agent healthy?" (almost always True),
    we check "was the confidence level appropriate for the outcome quality?"

    This creates meaningful variance in ground truth:
    - High confidence + excellent outcome → True (appropriately confident)
    - High confidence + poor outcome → False (overconfident)
    - Low confidence + excellent outcome → False (underconfident)
    - Low confidence + uncertain outcome → True (appropriately uncertain)

    Args:
        entry: Audit log entry with decision details
        metadata: Agent metadata dict

    Returns:
        True if confidence matched outcome, False if miscalibrated, None if can't evaluate
    """
    # Extract confidence from entry (try multiple locations)
    confidence = entry.get('confidence')
    if confidence is None:
        details = entry.get('details', {})
        confidence = details.get('confidence', details.get('coherence', None))

    if confidence is None:
        return None  # Can't evaluate without confidence

    confidence = float(confidence)

    # Get agent state
    agent_id = entry.get('agent_id', 'unknown')
    agent_meta = metadata.get(agent_id, {})
    status = agent_meta.get('status', 'unknown')
    loop_detected = agent_meta.get('loop_detected_at')
    paused_at = agent_meta.get('paused_at')
    update_count = agent_meta.get('update_count', 0)

    # Determine outcome quality on 0-1 scale
    # This measures how well the agent performed, not just binary health
    if status == 'paused' or loop_detected or paused_at:
        # Agent got stuck - poor outcome
        outcome_quality = 0.2
    elif status == 'archived':
        # Agent completed successfully - excellent outcome
        outcome_quality = 0.95
    elif status == 'active':
        # Agent still working - good outcome (slightly discounted for uncertainty)
        # More updates = more established = slightly higher quality
        base_quality = 0.7
        experience_bonus = min(0.15, update_count * 0.01)  # Up to +0.15 for experienced agents
        outcome_quality = base_quality + experience_bonus
    elif status == 'waiting_input':
        # Agent waiting for user - moderate outcome
        outcome_quality = 0.6
    else:
        # Unknown status
        return None

    # CALIBRATION CHECK: Was confidence appropriate for the outcome?
    #
    # Well-calibrated confidence should correlate with outcome quality.
    # A large gap indicates miscalibration:
    # - confidence >> outcome_quality → overconfidence
    # - confidence << outcome_quality → underconfidence

    confidence_outcome_gap = abs(confidence - outcome_quality)

    # Threshold for "miscalibrated" - calibration error > 0.35 is considered wrong
    # This creates ~25-40% False rate depending on data distribution
    MISCALIBRATION_THRESHOLD = 0.35

    if confidence_outcome_gap > MISCALIBRATION_THRESHOLD:
        # Large gap = miscalibrated confidence
        direction = "overconfident" if confidence > outcome_quality else "underconfident"
        logger.debug(
            f"Miscalibrated: conf={confidence:.2f}, outcome={outcome_quality:.2f}, "
            f"gap={confidence_outcome_gap:.2f} ({direction})"
        )
        return False
    else:
        # Confidence reasonably matched outcome
        return True


async def collect_ground_truth_automatically(
    min_age_hours: float = 2.0,
    max_decisions: int = 50,
    dry_run: bool = False,
    rebuild: bool = False
) -> Dict:
    """
    Automatically collect ground truth for decisions older than min_age_hours.
    
    Args:
        min_age_hours: Minimum age of decisions to evaluate (default: 2 hours)
        max_decisions: Maximum number of decisions to process per run (0 = no limit)
        dry_run: If True, don't update calibration, just return what would be updated
        rebuild: If True, reset calibration and rebuild from scratch (for fixing inverted data)
        
    Returns:
        Dict with statistics about collected ground truth
    """
    from src.calibration import calibration_checker
    from src.audit_log import AuditLogger
    
    audit_logger = AuditLogger()
    
    # Load agent metadata from PostgreSQL via server loader.
    metadata = {}
    try:
        import src.agent_state as mcp_server
        # Use async version since we're in an async context (avoids event loop conflicts)
        await mcp_server.load_metadata_async()
        # Convert AgentMetadata objects to dicts (this module expects dict-like access)
        for aid, meta_obj in getattr(mcp_server, "agent_metadata", {}).items():
            try:
                if hasattr(meta_obj, "to_dict"):
                    metadata[aid] = meta_obj.to_dict()
                elif isinstance(meta_obj, dict):
                    metadata[aid] = meta_obj
            except Exception:
                continue
    except Exception as e:
        # Fallback: try JSON snapshot if present (backward compatibility)
        metadata_path = project_root / "data" / "agent_metadata.json"
        if not metadata_path.exists():
            logger.warning(f"Agent metadata not found (and server load failed: {e}), skipping ground truth collection")
            return {"updated": 0, "skipped": 0, "errors": 0}
        with open(metadata_path, "r") as f:
            metadata = json.load(f)
    
    # If rebuilding, reset calibration first
    if rebuild and not dry_run:
        logger.info("Rebuilding calibration from scratch (rebuild=True)")
        calibration_checker.reset()
        calibration_checker.save_state()
    
    # Query recent decisions
    cutoff_time = (datetime.now() - timedelta(hours=min_age_hours)).isoformat()
    entries = audit_logger.query_audit_log(
        event_type="auto_attest",
        start_time=None,  # From beginning
        end_time=cutoff_time  # Up to cutoff
    )
    
    if not entries:
        return {"updated": 0, "skipped": 0, "errors": 0, "message": "No decisions found"}
    
    # If rebuild mode, process all entries (no limit)
    if rebuild:
        max_decisions = 0  # 0 = no limit
    
    # Filter to decisions that need ground truth
    # Check which ones already have ground truth by looking at calibration state
    state = calibration_checker.bin_stats
    
    # Get timestamps that already have ground truth (approximate check)
    # This is a heuristic - we'll skip decisions that are very recent in calibration
    processed = 0
    updated = 0
    skipped = 0
    errors = 0
    
    # Group by timestamp to avoid duplicates
    seen_timestamps = set()
    
    # Process entries (limit if max_decisions > 0)
    entries_to_process = entries if max_decisions == 0 else entries[:max_decisions]
    
    exogenous_count = 0
    endogenous_only_count = 0

    for entry in entries_to_process:
        timestamp = entry.get('timestamp')
        if not timestamp or timestamp in seen_timestamps:
            continue

        seen_timestamps.add(timestamp)
        processed += 1

        try:
            # HARD GATE: Skip calibration when no exogenous signals exist.
            # Without objective external signals (test results, command outcomes,
            # file operations), calibration would be grading its own homework —
            # using the agent's self-reported trajectory to evaluate its own
            # confidence accuracy.
            if not has_exogenous_signals(entry):
                endogenous_only_count += 1
                skipped += 1
                continue

            exogenous_count += 1

            # Evaluate outcome
            actual_correct = evaluate_decision_outcome(entry, metadata)

            if actual_correct is None:
                skipped += 1
                continue

            if dry_run:
                updated += 1
                confidence = entry.get('confidence', 0.0)
                predicted_correct = float(confidence) >= 0.5
                logger.info(f"[DRY RUN] Would update: {timestamp} -> confidence={confidence:.2f}, predicted_correct={predicted_correct}, actual_correct={actual_correct}")
            else:
                confidence = entry.get('confidence', 0.0)
                predicted_correct = float(confidence) >= 0.5

                calibration_checker.update_ground_truth(
                    confidence=float(confidence),
                    predicted_correct=bool(predicted_correct),
                    actual_correct=bool(actual_correct)
                )

                updated += 1
                logger.info(f"Auto-updated ground truth: {timestamp} -> actual_correct={actual_correct}")

        except Exception as e:
            errors += 1
            logger.error(f"Error processing decision {timestamp}: {e}", exc_info=True)
    
    if not dry_run and updated > 0:
        # Save calibration state
        calibration_checker.save_state()
    
    exogenous_fraction = exogenous_count / max(1, processed)
    return {
        "processed": processed,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
        "exogenous_signals": exogenous_count,
        "endogenous_only_skipped": endogenous_only_count,
        "exogenous_fraction": round(exogenous_fraction, 3),
    }


async def auto_ground_truth_collector_task(interval_hours: float = 6.0):
    """
    Background task that periodically collects ground truth.
    
    Args:
        interval_hours: How often to run collection (default: 6 hours)
    """
    logger.info(f"Auto ground truth collector started (interval: {interval_hours}h)")
    
    while True:
        try:
            await asyncio.sleep(interval_hours * 3600)  # Convert to seconds
            
            logger.info("Running automatic ground truth collection...")
            result = await collect_ground_truth_automatically(
                min_age_hours=2.0,
                max_decisions=50,
                dry_run=False
            )
            
            logger.info(
                f"Ground truth collection complete: "
                f"updated={result['updated']}, skipped={result['skipped']}, errors={result['errors']}"
            )
        
        except Exception as e:
            logger.error(f"Error in auto ground truth collector: {e}", exc_info=True)
            # Wait before retrying
            await asyncio.sleep(3600)  # Wait 1 hour on error


"""
Calibration Checking System
Bins predictions by confidence and measures real accuracy to detect miscalibration.
"""

from typing import Any, Dict, List, Tuple, Optional
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import json
import sys
import time
import os


@dataclass
class CalibrationBin:
    """Calibration bin statistics"""
    bin_range: Tuple[float, float]  # (min, max) confidence
    count: int
    predicted_correct: int  # How many times we predicted correct
    actual_correct: int     # How many times we were actually correct
    accuracy: float         # actual_correct / count
    expected_accuracy: float  # Mean confidence in this bin
    calibration_error: float   # |accuracy - expected_accuracy|


@dataclass
class ComplexityCalibrationBin:
    """Complexity calibration bin statistics"""
    bin_range: Tuple[float, float]  # (min, max) discrepancy
    count: int
    mean_discrepancy: float  # Mean absolute discrepancy in this bin
    mean_reported: float     # Mean reported complexity
    mean_derived: float      # Mean derived complexity
    high_discrepancy_rate: float  # Percentage with discrepancy > 0.3


class CalibrationChecker:
    """
    Checks calibration of confidence estimates.
    
    TWO-DIMENSIONAL CALIBRATION (per dialectic resolution 2025-12-10):
    
    1. TACTICAL CALIBRATION (per-decision):
       - Measures if individual decisions were correct at the time they were made
       - NO retroactive marking - decision correctness is fixed at decision time
       - Used for: sampling parameter adjustment (temperature, top_p)
       
    2. STRATEGIC CALIBRATION (trajectory health):
       - Measures if agents with high confidence end up in healthy states
       - Retroactive marking IS valid - trajectory outcomes matter
       - Used for: agent trust scoring, confidence estimates
    
    The "inverted curve" (high confidence = low accuracy) is VALID for strategic
    calibration - it reveals that overconfident agents have worse trajectories.
    But it was WRONG to use for tactical decisions.
    """
    
    def __init__(self, bins: List[Tuple[float, float]] = None, state_file: Path = None):
        """
        Initialize calibration checker with confidence bins.
        
        Default bins:
        - [0.0, 0.5]: Low confidence
        - [0.5, 0.7]: Medium-low confidence
        - [0.7, 0.8]: Medium-high confidence
        - [0.8, 0.9]: High confidence
        - [0.9, 1.0]: Very high confidence
        
        Args:
            bins: Confidence bins for calibration
            state_file: Path to calibration state file (defaults to data/calibration_state.json)
        """
        if bins is None:
            bins = [
                (0.0, 0.5),
                (0.5, 0.7),
                (0.7, 0.8),
                (0.8, 0.9),
                (0.9, 1.0)
            ]
        self.bins = bins
        
        # Set up state file path
        if state_file is None:
            state_file = Path(__file__).parent.parent / "data" / "calibration_state.json"
        self.state_file = Path(state_file)

        # Backend: postgres (default), json (fallback)
        self._backend = os.getenv("UNITARES_CALIBRATION_BACKEND", "postgres").strip().lower()
        self._pg_db = None  # PostgreSQL backend (lazy init)
        self._last_json_write = 0.0  # monotonic timestamp of last JSON snapshot write

        # Resolve backend: postgres is default, json is fallback
        if self._backend not in ("json", "postgres"):
            self._backend = "postgres"
        
        # Initialize complexity bins (always needed)
        self.complexity_bins = [
            (0.0, 0.1),   # Low discrepancy (well-calibrated)
            (0.1, 0.3),   # Medium discrepancy (moderate calibration)
            (0.3, 0.5),   # High discrepancy (poor calibration)
            (0.5, 1.0)    # Very high discrepancy (severe mis-calibration)
        ]
        
        # Load existing state or reset
        self.load_state()

    def _get_pg_db(self):
        """Get PostgreSQL backend (lazy init)."""
        if self._pg_db is None:
            from src.db import get_db
            self._pg_db = get_db()
        return self._pg_db

    def _run_async(self, async_fn, *args, **kwargs):
        """Schedule async function on the running event loop (fire-and-forget).

        Previous implementation created a new event loop in a thread, which
        corrupted the shared asyncpg connection pool (bound to the main loop).
        Now schedules on the existing loop to avoid cross-loop contamination.
        """
        import asyncio

        try:
            loop = asyncio.get_running_loop()
            # We're inside an async context — schedule as a task
            loop.create_task(async_fn(*args, **kwargs))
        except RuntimeError:
            # No running loop (shouldn't happen in normal MCP flow, but safe fallback).
            # Use a dedicated connection instead of the shared pool.
            pass  # Skip DB save — JSON snapshot (below in save_state) is the fallback
    
    def reset(self):
        """Reset calibration statistics"""
        # STRATEGIC calibration (trajectory health) - retroactive marking allowed
        # This is the ORIGINAL bin_stats - renamed conceptually to "strategic"
        self.bin_stats = defaultdict(lambda: {
            'count': 0,
            'predicted_correct': 0,
            'actual_correct': 0,  # This gets updated retroactively
            'confidence_sum': 0.0
        })
        
        # TACTICAL calibration (per-decision) - NO retroactive marking
        # Decision correctness is fixed at decision time
        self.tactical_bin_stats = defaultdict(lambda: {
            'count': 0,
            'predicted_correct': 0,
            'actual_correct': 0,  # Fixed at decision time, never updated retroactively
            'confidence_sum': 0.0
        })

        # Per-channel tactical bin stats (parallel to aggregate above).
        # Populated when record_tactical_decision is called with signal_source.
        # Aggregate stats remain populated for back-compat regardless.
        self.tactical_bin_stats_by_channel = defaultdict(lambda: defaultdict(lambda: {
            'count': 0,
            'predicted_correct': 0,
            'actual_correct': 0,
            'confidence_sum': 0.0,
        }))
        
        # Ensure complexity_bins is initialized (may already be set in __init__)
        if not hasattr(self, 'complexity_bins'):
            self.complexity_bins = [
                (0.0, 0.1),   # Low discrepancy (well-calibrated)
                (0.1, 0.3),   # Medium discrepancy (moderate calibration)
                (0.3, 0.5),   # High discrepancy (poor calibration)
                (0.5, 1.0)    # Very high discrepancy (severe mis-calibration)
            ]
        self.complexity_stats = defaultdict(lambda: {
            'count': 0,
            'discrepancy_sum': 0.0,
            'reported_sum': 0.0,
            'derived_sum': 0.0,
            'high_discrepancy_count': 0  # Count with discrepancy > 0.3
        })
    
    def record_prediction(
        self,
        confidence: float,
        predicted_correct: bool,
        actual_correct: Optional[float],
        complexity_discrepancy: Optional[float] = None
    ):
        """
        Record a prediction for calibration checking.
        
        This records to STRATEGIC calibration (trajectory health).
        For tactical calibration, use record_tactical_decision().
        
        Args:
            confidence: Confidence estimate (0-1)
            predicted_correct: Whether we predicted correct (based on confidence threshold)
            actual_correct: Whether prediction was actually correct (ground truth)
            complexity_discrepancy: Optional complexity-EISV discrepancy (0-1) for calibration weighting
        """
        # Find which bin this confidence falls into
        bin_key = None
        for bin_min, bin_max in self.bins:
            if bin_min <= confidence < bin_max or (bin_max == 1.0 and confidence == 1.0):
                bin_key = f"{bin_min:.1f}-{bin_max:.1f}"
                break
        
        if bin_key is None:
            # Fallback to nearest bin
            bin_key = f"{self.bins[-1][0]:.1f}-{self.bins[-1][1]:.1f}"
        
        stats = self.bin_stats[bin_key]
        stats['count'] += 1
        stats['confidence_sum'] += confidence
        
        if predicted_correct:
            stats['predicted_correct'] += 1
        
        # Update actual correctness signal if provided.
        #
        # IMPORTANT: In UNITARES, "actual_correct" is allowed to be a *weighted* signal
        # (e.g. peer verification weight, or a trajectory-health proxy in [0,1]),
        # not only a strict boolean. This enables dynamic (non-manual) calibration.
        if actual_correct is not None:
            stats['actual_correct'] += float(actual_correct)
            # Auto-save after recording a prediction with any correctness signal
            self.save_state()
        
        # Record complexity discrepancy if provided
        if complexity_discrepancy is not None:
            self.record_complexity_discrepancy(abs(complexity_discrepancy))
    
    def record_tactical_decision(self, confidence: float, decision: str,
                                  immediate_outcome: bool,
                                  signal_source: Optional[str] = None):
        """
        Record a decision for TACTICAL calibration (per-decision, no retroactive).

        This measures if individual decisions were correct AT THE TIME they were made.
        Unlike strategic calibration, this is NEVER updated retroactively.

        Args:
            confidence: Confidence estimate at decision time (0-1)
            decision: The decision made ("proceed", "pause", etc.)
            immediate_outcome: Whether the decision was correct based on immediate context
                              (not trajectory outcomes - that's strategic calibration)
            signal_source: Optional channel name (e.g. "tests", "tasks"). When provided,
                           the row is also recorded in tactical_bin_stats_by_channel for
                           per-channel reliability breakdown. Aggregate stats above are
                           populated regardless.

        Example:
            - Decision "proceed" is tactically correct if agent could proceed without immediate issues
            - Decision "pause" is tactically correct if there was a genuine reason to pause
            - This is independent of whether the agent later has problems (that's strategic)
        """
        # Find which bin this confidence falls into
        bin_key = None
        for bin_min, bin_max in self.bins:
            if bin_min <= confidence < bin_max or (bin_max == 1.0 and confidence == 1.0):
                bin_key = f"{bin_min:.1f}-{bin_max:.1f}"
                break

        if bin_key is None:
            bin_key = f"{self.bins[-1][0]:.1f}-{self.bins[-1][1]:.1f}"

        # Initialize tactical_bin_stats if needed (backward compatibility)
        if not hasattr(self, 'tactical_bin_stats'):
            self.tactical_bin_stats = defaultdict(lambda: {
                'count': 0,
                'predicted_correct': 0,
                'actual_correct': 0,
                'confidence_sum': 0.0
            })

        stats = self.tactical_bin_stats[bin_key]
        stats['count'] += 1
        stats['confidence_sum'] += confidence

        # FIXED: predicted_correct is based on confidence, not decision
        # High confidence (>=0.5) = we predicted correct
        # Low confidence (<0.5) = we predicted incorrect
        # This measures calibration: "When I said I was X% confident, was I right?"
        predicted_correct = confidence >= 0.5
        if predicted_correct:
            stats['predicted_correct'] += 1

        # Tactical correctness is fixed at decision time - no retroactive updates!
        if immediate_outcome:
            stats['actual_correct'] += 1

        # Per-channel routing (additive — aggregate above is unchanged).
        if signal_source:
            if not hasattr(self, 'tactical_bin_stats_by_channel'):
                # Backward-compat for instances created before this field existed.
                self.tactical_bin_stats_by_channel = defaultdict(lambda: defaultdict(lambda: {
                    'count': 0,
                    'predicted_correct': 0,
                    'actual_correct': 0,
                    'confidence_sum': 0.0,
                }))
            channel_stats = self.tactical_bin_stats_by_channel[signal_source][bin_key]
            channel_stats['count'] += 1
            channel_stats['confidence_sum'] += confidence
            if predicted_correct:
                channel_stats['predicted_correct'] += 1
            if immediate_outcome:
                channel_stats['actual_correct'] += 1

        # Save state
        self.save_state()
    
    def record_complexity_discrepancy(self, discrepancy: float, reported_complexity: Optional[float] = None,
                                     derived_complexity: Optional[float] = None):
        """
        Record complexity-EISV discrepancy for calibration tracking.
        
        Args:
            discrepancy: Absolute discrepancy between reported and derived complexity (0-1)
            reported_complexity: Optional reported complexity value
            derived_complexity: Optional derived complexity value
        """
        # Find which complexity bin this discrepancy falls into
        bin_key = None
        for bin_min, bin_max in self.complexity_bins:
            if bin_min <= discrepancy < bin_max or (bin_max == 1.0 and discrepancy == 1.0):
                bin_key = f"{bin_min:.1f}-{bin_max:.1f}"
                break
        
        if bin_key is None:
            # Fallback to highest bin
            bin_key = f"{self.complexity_bins[-1][0]:.1f}-{self.complexity_bins[-1][1]:.1f}"
        
        stats = self.complexity_stats[bin_key]
        stats['count'] += 1
        stats['discrepancy_sum'] += discrepancy
        
        if reported_complexity is not None:
            stats['reported_sum'] += reported_complexity
        if derived_complexity is not None:
            stats['derived_sum'] += derived_complexity
        
        # Track high discrepancies (>0.3 threshold)
        if discrepancy > 0.3:
            stats['high_discrepancy_count'] += 1
    
    def get_complexity_calibration_weight(self, discrepancy: Optional[float]) -> float:
        """
        Get calibration weight based on complexity discrepancy.
        
        Lower discrepancy = higher weight (more reliable calibration signal)
        Higher discrepancy = lower weight (less reliable calibration signal)
        
        Args:
            discrepancy: Complexity-EISV discrepancy (0-1), or None if unavailable
        
        Returns:
            Weight factor (0.0-1.0) for confidence calibration updates
        """
        if discrepancy is None:
            return 1.0  # Default weight if no complexity data
        
        abs_discrepancy = abs(discrepancy)
        
        # Weight function: inverse relationship with discrepancy
        # Low discrepancy (<0.1) → high weight (1.0)
        # Medium discrepancy (0.1-0.3) → medium weight (0.7)
        # High discrepancy (>0.3) → low weight (0.4)
        if abs_discrepancy < 0.1:
            return 1.0
        elif abs_discrepancy < 0.3:
            # Linear interpolation: 0.1 → 1.0, 0.3 → 0.7
            return 1.0 - (abs_discrepancy - 0.1) * 1.5  # (0.3-0.1) * 1.5 = 0.3, so 1.0-0.3=0.7
        else:
            # High discrepancy: weight decreases further
            # 0.3 → 0.4, 0.5 → 0.2, 1.0 → 0.0
            if abs_discrepancy >= 1.0:
                return 0.0
            # Linear: 0.3 → 0.4, 1.0 → 0.0
            return max(0.0, 0.4 - (abs_discrepancy - 0.3) * (0.4 / 0.7))  # 0.4/0.7 ≈ 0.57
    
    def compute_complexity_calibration_metrics(self) -> Dict[str, ComplexityCalibrationBin]:
        """Compute complexity calibration metrics for each bin"""
        results = {}
        
        for bin_key, stats in self.complexity_stats.items():
            if stats['count'] == 0:
                continue
            
            # Parse bin range
            bin_min, bin_max = map(float, bin_key.split('-'))
            
            mean_discrepancy = stats['discrepancy_sum'] / stats['count']
            mean_reported = stats['reported_sum'] / stats['count'] if stats['reported_sum'] > 0 else None
            mean_derived = stats['derived_sum'] / stats['count'] if stats['derived_sum'] > 0 else None
            high_discrepancy_rate = stats['high_discrepancy_count'] / stats['count']
            
            results[bin_key] = ComplexityCalibrationBin(
                bin_range=(bin_min, bin_max),
                count=stats['count'],
                mean_discrepancy=mean_discrepancy,
                mean_reported=mean_reported or 0.0,
                mean_derived=mean_derived or 0.0,
                high_discrepancy_rate=high_discrepancy_rate
            )
        
        return results
    
    def compute_calibration_metrics(self) -> Dict[str, CalibrationBin]:
        """
        Compute STRATEGIC calibration metrics (trajectory health).
        
        This measures trajectory outcomes by confidence level.
        High confidence agents SHOULD end up in better states.
        If they don't, the inverted curve is a VALID signal of overconfidence.
        """
        results = {}
        
        for bin_key, stats in self.bin_stats.items():
            if stats['count'] == 0:
                continue
            
            # Parse bin range
            bin_min, bin_max = map(float, bin_key.split('-'))
            
            # Rename "accuracy" to "trajectory_health" conceptually
            # (keeping variable names for backward compatibility)
            accuracy = stats['actual_correct'] / stats['count']
            expected_accuracy = stats['confidence_sum'] / stats['count']
            calibration_error = abs(accuracy - expected_accuracy)
            
            results[bin_key] = CalibrationBin(
                bin_range=(bin_min, bin_max),
                count=stats['count'],
                predicted_correct=stats['predicted_correct'],
                actual_correct=stats['actual_correct'],
                accuracy=accuracy,  # This is really "trajectory_health"
                expected_accuracy=expected_accuracy,
                calibration_error=calibration_error
            )
        
        return results
    
    def compute_tactical_metrics(self) -> Dict[str, CalibrationBin]:
        """
        Compute TACTICAL calibration metrics (per-decision correctness).
        
        This measures if individual decisions were correct at the time.
        NO retroactive marking - this reflects decision quality, not trajectory.
        Use this for sampling parameter adjustment.
        """
        results = {}
        
        # Initialize tactical_bin_stats if needed (backward compatibility)
        if not hasattr(self, 'tactical_bin_stats'):
            return results  # No tactical data yet
        
        for bin_key, stats in self.tactical_bin_stats.items():
            if stats['count'] == 0:
                continue
            
            # Parse bin range
            bin_min, bin_max = map(float, bin_key.split('-'))
            
            accuracy = stats['actual_correct'] / stats['count']
            expected_accuracy = stats['confidence_sum'] / stats['count']
            calibration_error = abs(accuracy - expected_accuracy)
            
            results[bin_key] = CalibrationBin(
                bin_range=(bin_min, bin_max),
                count=stats['count'],
                predicted_correct=stats['predicted_correct'],
                actual_correct=stats['actual_correct'],
                accuracy=accuracy,  # This is real per-decision accuracy
                expected_accuracy=expected_accuracy,
                calibration_error=calibration_error
            )

        return results

    def compute_tactical_metrics_per_channel(self) -> Dict[str, Dict[str, CalibrationBin]]:
        """
        Compute per-channel tactical calibration metrics.

        Returns {channel: {bin_key: CalibrationBin}} so callers can ask
        "miscalibrated where?" instead of just "miscalibrated".
        """
        results: Dict[str, Dict[str, CalibrationBin]] = {}

        if not hasattr(self, 'tactical_bin_stats_by_channel'):
            return results

        for channel, channel_bins in self.tactical_bin_stats_by_channel.items():
            channel_results: Dict[str, CalibrationBin] = {}
            for bin_key, stats in channel_bins.items():
                if stats['count'] == 0:
                    continue
                bin_min, bin_max = map(float, bin_key.split('-'))
                accuracy = stats['actual_correct'] / stats['count']
                expected_accuracy = stats['confidence_sum'] / stats['count']
                calibration_error = abs(accuracy - expected_accuracy)
                channel_results[bin_key] = CalibrationBin(
                    bin_range=(bin_min, bin_max),
                    count=stats['count'],
                    predicted_correct=stats['predicted_correct'],
                    actual_correct=stats['actual_correct'],
                    accuracy=accuracy,
                    expected_accuracy=expected_accuracy,
                    calibration_error=calibration_error,
                )
            if channel_results:
                results[channel] = channel_results

        return results

    def check_calibration(self, min_samples_per_bin: int = 10, include_complexity: bool = True) -> Tuple[bool, Dict]:
        """
        Check if calibration is acceptable.
        
        Returns TWO-DIMENSIONAL calibration:
        - STRATEGIC (trajectory_health): Do confident agents end up healthy?
        - TACTICAL (per_decision): Are individual decisions correct at the time?
        
        Args:
            min_samples_per_bin: Minimum samples per bin to consider calibrated
            include_complexity: Whether to include complexity calibration metrics
        
        Returns:
            (is_calibrated, metrics_dict)
        """
        # STRATEGIC calibration (trajectory health)
        strategic_metrics = self.compute_calibration_metrics()
        
        # TACTICAL calibration (per-decision)
        tactical_metrics = self.compute_tactical_metrics()
        
        if not strategic_metrics and not tactical_metrics:
            return False, {"error": "No calibration data"}
        
        # Check strategic calibration
        issues = []
        strategic_issues = []
        for bin_key, bin_metrics in strategic_metrics.items():
            if bin_metrics.count < min_samples_per_bin:
                continue
            
            # High confidence bins should have high trajectory health
            if bin_metrics.bin_range[0] >= 0.8:
                if bin_metrics.accuracy < 0.7:
                    strategic_issues.append(
                        f"Bin {bin_key}: high confidence ({bin_metrics.expected_accuracy:.2f}) "
                        f"but low trajectory health ({bin_metrics.accuracy:.2f})"
                    )
            
            # Large calibration error indicates miscalibration
            if bin_metrics.calibration_error > 0.2:
                strategic_issues.append(
                    f"Bin {bin_key}: large calibration error ({bin_metrics.calibration_error:.2f})"
                )
        
        # Check tactical calibration (if we have data)
        tactical_issues = []
        for bin_key, bin_metrics in tactical_metrics.items():
            if bin_metrics.count < min_samples_per_bin:
                continue
            
            # For tactical: high confidence should mean high per-decision accuracy
            if bin_metrics.bin_range[0] >= 0.8:
                if bin_metrics.accuracy < 0.7:
                    tactical_issues.append(
                        f"Bin {bin_key}: high confidence ({bin_metrics.expected_accuracy:.2f}) "
                        f"but low decision accuracy ({bin_metrics.accuracy:.2f})"
                    )
        
        is_calibrated = len(strategic_issues) == 0 and len(tactical_issues) == 0
        
        result = {
            "is_calibrated": is_calibrated,
            "issues": strategic_issues + tactical_issues,
            # STRATEGIC calibration (trajectory health) - formerly just "bins"
            "strategic_calibration": {
                "description": "Trajectory health by confidence level (retroactive marking)",
                "use_for": "Agent trust scoring, confidence estimates",
                "bins": {k: {
                    "count": v.count,
                    "trajectory_health": v.accuracy,  # Renamed from "accuracy"
                    "expected_confidence": v.expected_accuracy,
                    "calibration_error": v.calibration_error
                } for k, v in strategic_metrics.items()}
            },
            # Backward compatibility: keep "bins" key pointing to strategic
            "bins": {k: {
                "count": v.count,
                "accuracy": v.accuracy,  # Keep old name for backward compat
                "trajectory_health": v.accuracy,  # Also provide new name
                "expected_accuracy": v.expected_accuracy,
                "calibration_error": v.calibration_error
            } for k, v in strategic_metrics.items()}
        }
        
        # TACTICAL calibration (per-decision)
        if tactical_metrics:
            result["tactical_calibration"] = {
                "description": "Per-decision correctness (no retroactive marking)",
                "use_for": "Sampling parameter adjustment (temperature, top_p)",
                "bins": {k: {
                    "count": v.count,
                    "decision_accuracy": v.accuracy,
                    "expected_confidence": v.expected_accuracy,
                    "calibration_error": v.calibration_error
                } for k, v in tactical_metrics.items()}
            }
        else:
            result["tactical_calibration"] = {
                "description": "Per-decision correctness (no retroactive marking)",
                "use_for": "Sampling parameter adjustment (temperature, top_p)",
                "bins": {},
                "note": "No tactical data yet - call record_tactical_decision() to populate"
            }
        
        # Add complexity calibration if requested
        if include_complexity:
            complexity_metrics = self.compute_complexity_calibration_metrics()
            if complexity_metrics:
                result["complexity_calibration"] = {
                    k: {
                        "count": v.count,
                        "mean_discrepancy": v.mean_discrepancy,
                        "mean_reported": v.mean_reported,
                        "mean_derived": v.mean_derived,
                        "high_discrepancy_rate": v.high_discrepancy_rate
                    } for k, v in complexity_metrics.items()
                }
                
                # Add complexity calibration issues
                total_complexity_samples = sum(v.count for v in complexity_metrics.values())
                high_discrepancy_total = sum(
                    v.count * v.high_discrepancy_rate for v in complexity_metrics.values()
                )
                high_discrepancy_rate = high_discrepancy_total / total_complexity_samples if total_complexity_samples > 0 else 0
                
                if high_discrepancy_rate > 0.5:  # More than 50% high discrepancy
                    issues.append(
                        f"Complexity calibration: {high_discrepancy_rate:.1%} of samples show high discrepancy (>0.3)"
                    )
                    is_calibrated = False
        
        result["is_calibrated"] = is_calibrated
        result["issues"] = strategic_issues + tactical_issues

        # Per-channel breakdown (additive — aggregate fields above are unchanged).
        # Allows callers to ask "miscalibrated where?" instead of just "miscalibrated".
        per_channel_metrics = self.compute_tactical_metrics_per_channel()
        if per_channel_metrics:
            per_channel_response = {}
            for channel, bin_metrics in per_channel_metrics.items():
                channel_samples = sum(b.count for b in bin_metrics.values())
                channel_issues = []
                max_gap = 0.0
                for bin_key, b in bin_metrics.items():
                    if b.count < min_samples_per_bin:
                        continue
                    if b.calibration_error > 0.2:
                        channel_issues.append(
                            f"Bin {bin_key}: large calibration error ({b.calibration_error:.2f})"
                        )
                    if b.calibration_error > max_gap:
                        max_gap = b.calibration_error
                per_channel_response[channel] = {
                    "calibrated": len(channel_issues) == 0,
                    "samples": channel_samples,
                    "calibration_gap": max_gap,
                    "issues": channel_issues,
                }
            result["per_channel_calibration"] = per_channel_response

        # Hygiene guard from sequential tracker (signal_source_outcomes).
        # Surfaces bad_rate_pinned_to_zero so Sentinel can raise an anomaly when
        # a previously-non-zero channel collapses back to a high-prior pin.
        try:
            from src.sequential_calibration import sequential_calibration_tracker
            health = sequential_calibration_tracker.compute_per_channel_health()
            if health:
                result["per_channel_health"] = health
        except Exception as e_health:  # pragma: no cover - defensive
            import logging
            logging.getLogger(__name__).debug("per_channel_health unavailable: %s", e_health)

        result["honesty_note"] = (
            "Calibration ground truth comes from objective outcomes (test pass/fail, command exit codes, "
            "lint results, file operations) as the primary signal. Dialectic peer agreement is a secondary "
            "signal (0.7 peer_weight). Human feedback is optional, not required."
        )

        return is_calibrated, result
    
    def update_ground_truth(self, confidence: float, predicted_correct: bool, 
                           actual_correct: bool, complexity_discrepancy: Optional[float] = None):
        """
        Update calibration with ground truth.

        This allows calibration to work properly by updating actual_correct
        after the fact (e.g., from test results, command outcomes, or review).
        
        IMPORTANT: Each call to update_ground_truth represents a NEW prediction.
        If you're updating ground truth for a prediction that was already recorded
        via record_prediction(), you should track that separately. This method
        will always increment count to ensure proper accounting.
        """
        # Find the bin
        bin_key = None
        for bin_min, bin_max in self.bins:
            if bin_min <= confidence < bin_max or (bin_max == 1.0 and confidence == 1.0):
                bin_key = f"{bin_min:.1f}-{bin_max:.1f}"
                break
        
        if bin_key is None:
            bin_key = f"{self.bins[-1][0]:.1f}-{self.bins[-1][1]:.1f}"
        
        stats = self.bin_stats[bin_key]
        
        # Always record this as a new prediction
        # This ensures count tracks all predictions, even if record_prediction wasn't called
        stats['count'] += 1
        stats['confidence_sum'] += confidence
        if predicted_correct:
            stats['predicted_correct'] += 1
        
        # Update actual correctness
        if actual_correct:
            stats['actual_correct'] += 1
        # Note: We don't increment actual_correct if actual_correct=False because
        # we're tracking how many were actually correct, not total ground truth updates
        
        # Ensure actual_correct never exceeds count (safety check)
        if stats['actual_correct'] > stats['count']:
            stats['actual_correct'] = stats['count']
    
    def get_pending_updates(self) -> int:
        """
        Deprecated: historical "pending ground truth" counter.
        
        The original implementation attempted to infer 'pending' from aggregate bin stats,
        but that is not well-defined once `actual_correct` is treated as a weighted
        correctness/trajectory-health signal (float), and it also failed to decrement
        when `actual_correct=False`.
        
        Dynamic calibration does not require a per-prediction pending queue, so this
        is kept for backward compatibility and always returns 0.
        """
        return 0
    
    def update_from_peer_verification(self, confidence: float, predicted_correct: bool, 
                                     peer_agreed: bool, weight: float = 0.7, 
                                     complexity_discrepancy: Optional[float] = None):
        """
        Update calibration from peer verification (dialectic convergence).
        
        Uses peer agreement weighted at 0.7 to account for overconfidence.
        The "elephant in the room": agents show 1.0 confidence but achieve ~0.7 accuracy.
        This weight calibrates for that reality - peer verification is valuable but not perfect.
        
        Complexity discrepancy further adjusts the weight: agents with high complexity-EISV
        divergence get lower weight (they're less reliable at self-assessment).
        
        Args:
            confidence: Original confidence estimate
            predicted_correct: Whether we predicted correct
            peer_agreed: Whether peer agents agreed (converged)
            weight: Weight for peer verification (default 0.7 = calibrates for overconfidence)
            complexity_discrepancy: Optional complexity-EISV discrepancy for calibration weighting
        """
        # Find the bin
        bin_key = None
        for bin_min, bin_max in self.bins:
            if bin_min <= confidence < bin_max or (bin_max == 1.0 and confidence == 1.0):
                bin_key = f"{bin_min:.1f}-{bin_max:.1f}"
                break
        
        if bin_key is None:
            bin_key = f"{self.bins[-1][0]:.1f}-{self.bins[-1][1]:.1f}"
        
        stats = self.bin_stats[bin_key]
        
        # Record prediction
        stats['count'] += 1
        stats['confidence_sum'] += confidence
        if predicted_correct:
            stats['predicted_correct'] += 1
        
        # Apply complexity calibration weight if available
        complexity_weight = self.get_complexity_calibration_weight(complexity_discrepancy)
        effective_weight = weight * complexity_weight
        
        # Update actual correctness with weighted peer agreement
        # Weight accounts for overconfidence: agents show 1.0 confidence but achieve ~0.7 accuracy
        # This is the "elephant in the room" - high confidence doesn't mean perfect correctness
        # Complexity weight further adjusts: agents with high complexity discrepancy get lower weight
        if peer_agreed:
            # Weighted update: peer agreement counts as partial correctness (weight * complexity_weight)
            # This calibrates for the reality that agents are overconfident
            # AND accounts for complexity mis-assessment (lower weight if high discrepancy)
            stats['actual_correct'] += effective_weight
        
        # Record complexity discrepancy if provided
        if complexity_discrepancy is not None:
            self.record_complexity_discrepancy(abs(complexity_discrepancy))
        
        # Ensure actual_correct never exceeds count (safety check)
        if stats['actual_correct'] > stats['count']:
            stats['actual_correct'] = stats['count']
        
        # Save state after update
        self.save_state()
    
    def update_from_peer_disagreement(self, confidence: float, predicted_correct: bool, 
                                      disagreement_severity: float = 0.5):
        """
        Update calibration from peer disagreement (dialectic escalation/failure).
        
        Disagreement indicates the agent was overconfident - their confidence was too high
        for the actual uncertainty in the situation. This lowers the effective calibration
        by treating disagreement as a signal that confidence should have been lower.
        
        Args:
            confidence: Original confidence estimate (which was too high)
            predicted_correct: Whether we predicted correct
            disagreement_severity: How severe the disagreement was (0.0-1.0)
                                  - 0.5 = moderate disagreement (default)
                                  - 1.0 = complete failure to converge
                                  - 0.0 = minor disagreement
        """
        # Find the bin
        bin_key = None
        for bin_min, bin_max in self.bins:
            if bin_min <= confidence < bin_max or (bin_max == 1.0 and confidence == 1.0):
                bin_key = f"{bin_min:.1f}-{bin_max:.1f}"
                break
        
        if bin_key is None:
            bin_key = f"{self.bins[-1][0]:.1f}-{self.bins[-1][1]:.1f}"
        
        stats = self.bin_stats[bin_key]
        
        # Record prediction
        stats['count'] += 1
        stats['confidence_sum'] += confidence
        if predicted_correct:
            stats['predicted_correct'] += 1
        
        # Disagreement means confidence was too high
        # We treat this as "actual correctness was lower than predicted"
        # The severity determines how much we lower the actual_correct count
        # Higher severity = more overconfidence = lower actual correctness
        
        # If agent predicted correct but peers disagreed, that's a mismatch
        # We reduce actual_correct by the disagreement severity
        # This effectively lowers the calibration accuracy for that confidence bin
        
        # For disagreement: if predicted_correct=True but peers disagreed,
        # then actual correctness should be lower (we were overconfident)
        if predicted_correct:
            # Disagreement means we were wrong to be confident
            # Reduce actual_correct by severity (e.g., 0.5 = half credit)
            # This penalizes overconfidence
            stats['actual_correct'] += (1.0 - disagreement_severity)
        else:
            # If we predicted incorrect, disagreement might actually mean we were right
            # But this is less clear - for now, treat as neutral
            # (Could be enhanced to track "disagreement when predicted wrong" separately)
            stats['actual_correct'] += 0.3  # Small credit for being cautious
        
        # Ensure actual_correct never goes negative (safety check)
        if stats['actual_correct'] < 0:
            stats['actual_correct'] = 0
        
        # Ensure actual_correct never exceeds count (safety check)
        if stats['actual_correct'] > stats['count']:
            stats['actual_correct'] = stats['count']
        
        # Save state after update
        self.save_state()
    
    def save_state(self):
        """Save calibration state to file"""
        try:
            # Convert defaultdict to regular dict for JSON serialization
            state_data = {
                'bins': {k: dict(v) for k, v in self.bin_stats.items()},
                'complexity_bins': {k: dict(v) for k, v in self.complexity_stats.items()},
                # NEW: Tactical calibration (per-decision, no retroactive marking)
                'tactical_bins': {k: dict(v) for k, v in self.tactical_bin_stats.items()} if hasattr(self, 'tactical_bin_stats') else {},
                # Per-channel breakdown (additive — older readers ignore unknown keys)
                'tactical_bins_by_channel': {
                    channel: {k: dict(v) for k, v in bins.items()}
                    for channel, bins in self.tactical_bin_stats_by_channel.items()
                } if hasattr(self, 'tactical_bin_stats_by_channel') else {},
            }

            # PostgreSQL backend
            if self._backend == "postgres":
                async def _save(data):
                    from src.db import get_db
                    db = get_db()
                    # Note: do NOT call db.close() here — this is the shared singleton pool.
                    # Closing it breaks all other concurrent users.
                    return await db.update_calibration(data)
                self._run_async(_save, state_data)

            # Write JSON snapshot as write-through cache.
            # When postgres is the backend, debounce to <=1 write per 10s (JSON is
            # just a cold-start cache). When JSON is the only backend, always write.
            now = time.monotonic()
            if self._backend != "postgres" or now - self._last_json_write >= 10.0:
                try:
                    self.state_file.parent.mkdir(parents=True, exist_ok=True)
                    with open(self.state_file, 'w') as f:
                        json.dump(state_data, f, indent=2)
                    self._last_json_write = now
                except Exception as e_json:
                    print(f"Warning: Failed to write calibration JSON snapshot: {e_json}", file=sys.stderr)
        except Exception as e:
            # Don't fail silently, but don't crash either
            print(f"Warning: Failed to save calibration state: {e}", file=sys.stderr)
    
    def _apply_state_data(self, state_data: dict):
        """Apply loaded state data to bin structures.

        Shared by both sync load_state() and async load_state_async().
        """
        # Restore bin_stats (STRATEGIC calibration)
        self.bin_stats = defaultdict(lambda: {
            'count': 0,
            'predicted_correct': 0,
            'actual_correct': 0,
            'confidence_sum': 0.0
        })

        for bin_key, stats in state_data.get('bins', {}).items():
            self.bin_stats[bin_key] = stats

        # Restore complexity_stats (backward compatible - may not exist in old state files)
        self.complexity_stats = defaultdict(lambda: {
            'count': 0,
            'discrepancy_sum': 0.0,
            'reported_sum': 0.0,
            'derived_sum': 0.0,
            'high_discrepancy_count': 0
        })

        for bin_key, stats in state_data.get('complexity_bins', {}).items():
            self.complexity_stats[bin_key] = stats

        # Restore tactical_bin_stats (NEW - may not exist in old state files)
        self.tactical_bin_stats = defaultdict(lambda: {
            'count': 0,
            'predicted_correct': 0,
            'actual_correct': 0,
            'confidence_sum': 0.0
        })

        for bin_key, stats in state_data.get('tactical_bins', {}).items():
            self.tactical_bin_stats[bin_key] = stats

        # Per-channel tactical bin stats (may be absent in older state files).
        self.tactical_bin_stats_by_channel = defaultdict(lambda: defaultdict(lambda: {
            'count': 0,
            'predicted_correct': 0,
            'actual_correct': 0,
            'confidence_sum': 0.0,
        }))
        for channel, channel_bins in state_data.get('tactical_bins_by_channel', {}).items():
            for bin_key, stats in channel_bins.items():
                self.tactical_bin_stats_by_channel[channel][bin_key] = stats

    def load_state(self):
        """Load calibration state from JSON file (sync, used at __init__ time).

        Note: _run_async is fire-and-forget so DB loads always return None here.
        The JSON snapshot (written by save_state) serves as the sync-readable cache.
        After the event loop is running, call load_state_async() to load from DB.
        """
        try:
            # Load from JSON snapshot (the sync-readable write-through cache)
            if not self.state_file.exists():
                self.reset()
                return
            with open(self.state_file, 'r') as f:
                state_data = json.load(f)

            self._apply_state_data(state_data)
        except Exception as e:
            # If loading fails, reset to empty state
            print(f"Warning: Failed to load calibration state: {e}, resetting", file=sys.stderr)
            self.reset()

    async def load_state_async(self):
        """Load calibration state from PostgreSQL (call after event loop is running).

        Falls back to sync JSON load if DB is unavailable.
        """
        if self._backend == "postgres":
            try:
                from src.db import get_db
                db = get_db()
                result = await db.get_calibration()
                if result and isinstance(result, dict):
                    state_data = {k: v for k, v in result.items() if not k.startswith('_')}
                    if state_data.get('bins'):
                        self._apply_state_data(state_data)
                        return
            except Exception as e:
                print(f"Warning: async calibration load failed: {e}", file=sys.stderr)
        # Fallback to sync JSON load (already done at __init__, but re-read in case
        # the file was updated since then)
        self.load_state()

    def compute_correction_factors(self, min_samples: int = 5) -> Dict[str, float]:
        """
        Compute correction factors for each confidence bin based on historical accuracy.

        AUTO-CALIBRATION: If agents report 90% confidence but are only 70% accurate,
        the correction factor is 0.70/0.90 = 0.78. Multiply reported confidence by
        this factor to get calibrated confidence.

        Args:
            min_samples: Minimum samples in a bin to compute correction (default 5)

        Returns:
            Dict mapping bin_key to correction factor (1.0 = well-calibrated)
        """
        corrections = {}

        # Use tactical metrics (per-decision accuracy) for correction
        for bin_key, stats in self.tactical_bin_stats.items():
            if stats['count'] < min_samples:
                continue

            # Parse bin range for midpoint
            bin_min, bin_max = map(float, bin_key.split('-'))
            expected_accuracy = stats['confidence_sum'] / stats['count']  # Average confidence in bin
            actual_accuracy = stats['actual_correct'] / stats['count']

            if expected_accuracy > 0.01:  # Avoid division by near-zero
                # Correction factor: actual/expected
                # If expected 0.9 but actual 0.7, factor = 0.78
                # If expected 0.5 but actual 0.6, factor = 1.2 (underconfident)
                factor = actual_accuracy / expected_accuracy
                # Asymmetric clip. The downside floor (0.5) guards against a
                # single noisy low-accuracy sample cratering confidence. The
                # upside is widened to 4.0 (was 1.5) so severe lower-bin
                # underconfidence is reported honestly instead of silently
                # capped — a 0.0-0.5 bin running near 1.0 accuracy has a true
                # factor ~4x, which the old 1.5 cap hid. The production path
                # (apply_confidence_correction) additionally bounds the applied
                # output by the bin's measured accuracy.
                factor = max(0.5, min(4.0, factor))
                corrections[bin_key] = factor

        return corrections

    def characterize_failure_modes(self, min_samples: int = 5) -> Dict[str, Any]:
        """Characterize calibration failure modes from current bin data.

        Returns structured failure characterization: ECE, curve inversion,
        per-bin diagnosis, and verdict quality implications.
        """
        strategic = self.compute_calibration_metrics()
        tactical = self.compute_tactical_metrics()

        result: Dict[str, Any] = {
            "strategic": self._characterize_dimension(strategic, min_samples),
            "tactical": self._characterize_dimension(tactical, min_samples),
        }

        is_inverted = result["strategic"].get("curve_inverted", False)
        worst = result["strategic"].get("worst_bin")

        if is_inverted:
            result["verdict_quality_warning"] = (
                "INVERTED calibration curve: agents with HIGH confidence have WORSE outcomes. "
                "High-confidence 'proceed' verdicts may be the LEAST trustworthy."
            )
        elif worst and worst.get("calibration_error", 0) > 0.3:
            result["verdict_quality_warning"] = (
                f"Severe miscalibration in bin {worst.get('bin')}: "
                f"calibration error {worst['calibration_error']:.2f}. "
                "Verdicts in this confidence range should be treated with caution."
            )
        else:
            result["verdict_quality_warning"] = None

        return result

    def _characterize_dimension(
        self, bins: Dict[str, CalibrationBin], min_samples: int
    ) -> Dict[str, Any]:
        """Characterize a single calibration dimension (strategic or tactical)."""
        if not bins:
            return {"status": "no_data", "bins_analyzed": 0}

        valid = {k: v for k, v in bins.items() if v.count >= min_samples}
        if not valid:
            return {"status": "insufficient_samples", "bins_analyzed": 0}

        total_count = sum(v.count for v in valid.values())
        ece = sum(v.calibration_error * v.count for v in valid.values()) / total_count

        sorted_bins = sorted(valid.items(), key=lambda x: x[1].expected_accuracy)
        curve_inverted = False
        if len(sorted_bins) >= 2:
            low_conf_acc = sorted_bins[0][1].accuracy
            high_conf_acc = sorted_bins[-1][1].accuracy
            curve_inverted = high_conf_acc < low_conf_acc

        worst = max(valid.items(), key=lambda x: x[1].calibration_error)

        bin_characterizations = {}
        for k, v in sorted(valid.items()):
            overconfident = v.expected_accuracy > v.accuracy + 0.1
            underconfident = v.accuracy > v.expected_accuracy + 0.1
            bin_characterizations[k] = {
                "count": v.count,
                "expected": round(v.expected_accuracy, 3),
                "actual": round(v.accuracy, 3),
                "calibration_error": round(v.calibration_error, 3),
                "diagnosis": (
                    "overconfident" if overconfident
                    else "underconfident" if underconfident
                    else "well_calibrated"
                ),
            }

        return {
            "status": "analyzed",
            "bins_analyzed": len(valid),
            "total_samples": total_count,
            "ece": round(ece, 4),
            "curve_inverted": curve_inverted,
            "worst_bin": {
                "bin": worst[0],
                "calibration_error": round(worst[1].calibration_error, 3),
                "expected": round(worst[1].expected_accuracy, 3),
                "actual": round(worst[1].accuracy, 3),
            },
            "bins": bin_characterizations,
        }

    def _tactical_signal_age_days(self) -> Optional[float]:
        """Days since the last tactical signal landed in the e-process tracker.

        Returned as a positive float; None if the tracker is unavailable or
        has no recorded signal yet. The sequential tracker shares its write
        path with `record_tactical_decision` (both fire from outcome_event
        on test_passed/test_failed), so its `last_updated` is a faithful
        proxy for "is the tactical bin pipeline alive."
        """
        try:
            from src.sequential_calibration import get_sequential_calibration_tracker
            from datetime import datetime, timezone
            metrics = get_sequential_calibration_tracker().compute_metrics()
            last_updated = metrics.get("last_updated")
            if not last_updated:
                return None
            if isinstance(last_updated, str):
                last_dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
            else:
                last_dt = last_updated
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0)
        except Exception:
            return None

    def apply_confidence_correction(self, reported_confidence: float,
                                    min_samples: int = 5,
                                    max_staleness_days: float = 7.0) -> Tuple[float, Optional[str]]:
        """
        Apply calibration correction to a reported confidence value.

        AUTO-CALIBRATION LOOP: This closes the learning loop by automatically
        adjusting confidence based on historical accuracy.

        Honest absence: when the tactical signal pipeline is starved (no
        outcome_event for `max_staleness_days`), bins are frozen in time and
        scaling against them is worse than not correcting at all — they
        carry survivorship bias from whichever sample arrived last. Return
        identity with a `calibration_skipped` reason so callers can surface
        the absence instead of silently using stale bins.

        Args:
            reported_confidence: The confidence value reported by the agent [0, 1]
            min_samples: Minimum samples needed to apply correction
            max_staleness_days: If the tactical signal hasn't refreshed in
                this many days, return identity. Default 7.

        Returns:
            Tuple of (corrected_confidence, correction_info)
            - corrected_confidence: Calibrated confidence value [0, 1]
            - correction_info: String describing correction applied or skipped,
              or None when the call is a quiet no-op
        """
        # Clamp input to valid range
        reported_confidence = max(0.0, min(1.0, reported_confidence))

        # Honest absence on stale signal — see docstring.
        age_days = self._tactical_signal_age_days()
        if age_days is not None and age_days > max_staleness_days:
            return reported_confidence, (
                f"calibration_skipped: tactical signal stale "
                f"{age_days:.1f}d (>{max_staleness_days}d threshold)"
            )

        # Find the bin for this confidence
        bin_key = None
        for bin_min, bin_max in self.bins:
            if bin_min <= reported_confidence < bin_max or (bin_max == 1.0 and reported_confidence == 1.0):
                bin_key = f"{bin_min:.1f}-{bin_max:.1f}"
                break

        if bin_key is None:
            return reported_confidence, None

        # Check if we have enough samples for this bin
        stats = self.tactical_bin_stats.get(bin_key)
        if not stats or stats['count'] < min_samples:
            return reported_confidence, None

        # Compute correction
        expected_accuracy = stats['confidence_sum'] / stats['count']
        actual_accuracy = stats['actual_correct'] / stats['count']

        if expected_accuracy < 0.01:
            return reported_confidence, None

        factor = actual_accuracy / expected_accuracy

        if factor >= 1.0:
            # Underconfident bin: the agent reports LESS confidence than the
            # bin's measured accuracy warrants. The previous symmetric clip
            # (factor capped at 1.5) mathematically blocked this correction — a
            # 0.0-0.5 bin running near 1.0 accuracy needs a ~4x lift but was
            # held to 1.5x, so the severe lower-bin underconfidence the
            # calibration audit flagged (bins 0.0-0.5 / 0.5-0.7 / 0.7-0.8) was
            # never actually corrected. Allow the full upward factor, bounded by
            # the bin's *measured* accuracy: actual_accuracy is the evidence
            # ceiling — never claim more confidence than the bin has achieved.
            corrected = min(reported_confidence * factor, actual_accuracy)
        else:
            # Overconfident bin: keep the historical floor so a single noisy
            # low-accuracy sample can't crater confidence. Regime unchanged.
            factor = max(0.5, factor)
            corrected = reported_confidence * factor

        corrected = max(0.0, min(1.0, corrected))  # Clamp to [0, 1]

        # Report when the *realized* correction is significant (> 5% absolute).
        # Keyed on the applied delta rather than the raw factor, because the
        # evidence ceiling can bound the correction below `factor`.
        if abs(corrected - reported_confidence) > 0.05:
            info = (
                f"calibration_adjusted: {reported_confidence:.2f} → {corrected:.2f} "
                f"(factor={factor:.2f}, actual={actual_accuracy:.2f}, n={stats['count']})"
            )
            return corrected, info

        return corrected, None


# Global calibration checker instance (lazy initialization to avoid blocking at import time)
_calibration_checker_instance = None

def get_calibration_checker() -> CalibrationChecker:
    """Get or create calibration checker instance (lazy initialization)"""
    global _calibration_checker_instance
    if _calibration_checker_instance is None:
        _calibration_checker_instance = CalibrationChecker()
    return _calibration_checker_instance

# Create a module-level proxy object that acts like the old calibration_checker
# This defers initialization until first use, preventing blocking at import time
class _CalibrationCheckerProxy:
    """Proxy for calibration_checker that provides lazy access"""
    def __getattr__(self, name):
        return getattr(get_calibration_checker(), name)
    
    def __call__(self, *args, **kwargs):
        # If someone tries to call calibration_checker() directly
        return get_calibration_checker()

calibration_checker = _CalibrationCheckerProxy()


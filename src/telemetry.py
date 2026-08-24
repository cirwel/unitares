"""
Telemetry and Metrics for Governance System
Surfaces skip rates, confidence distributions, and suspicious patterns.
"""

from typing import Dict, List, Optional
from datetime import datetime, timedelta
import json

from src.audit_log import audit_logger
from src.calibration import calibration_checker, resolve_calibration_status
from src.telemetry_cache import get_telemetry_cache


class TelemetryCollector:
    """Collects and surfaces governance telemetry with caching"""
    
    def __init__(self):
        self.audit_logger = audit_logger
        self.calibration_checker = calibration_checker
        self.cache = get_telemetry_cache()
    
    def get_skip_rate_metrics(self, agent_id: Optional[str] = None,
                             window_hours: int = 24) -> Dict:
        """
        Get skip rate metrics from audit log (cached).
        
        Cache TTL: 30 seconds
        Rationale: Skip rates change frequently as agents make decisions. Short TTL ensures
        recent skip patterns are visible quickly. 30s balances freshness vs cache hit rate.
        """
        # Check cache first
        cached = self.cache.get('skip_rate', agent_id, window_hours)
        if cached is not None:
            return cached
        
        # Fetch from audit log
        result = self.audit_logger.get_skip_rate_metrics(agent_id, window_hours)
        
        # Cache result (shorter TTL for skip rates - 30s)
        self.cache.set('skip_rate', result, agent_id, window_hours, ttl_seconds=30)
        
        return result
    
    def get_confidence_distribution(self, agent_id: Optional[str] = None,
                                    window_hours: int = 24) -> Dict:
        """
        Get confidence distribution statistics (non-blocking optimized, cached).
        
        Cache TTL: 60 seconds
        Rationale: Confidence distributions are more stable than skip rates but still change
        as agents adapt. 60s provides good balance - fresh enough to see trends, cached long
        enough to reduce file I/O. Error responses cached for 60s to avoid repeated failures.
        """
        # Check cache first
        cached = self.cache.get('confidence_dist', agent_id, window_hours)
        if cached is not None:
            return cached
        
        if not self.audit_logger.log_file.exists():
            result = {"error": "No audit log data"}
            self.cache.set('confidence_dist', result, agent_id, window_hours, ttl_seconds=60)
            return result
        
        cutoff_time = datetime.now() - timedelta(hours=window_hours)
        
        # IMPORTANT: Only include confidence values from events where "confidence"
        # actually represents a confidence estimate (auto_attest decisions).
        #
        # Some audit events include a placeholder `confidence=1.0` purely because the
        # AuditEntry schema requires the field (e.g., complexity_derivation, auto_resume).
        # Including those would falsely saturate distributions at 1.0 and break
        # calibration/telemetry interpretation.
        allowed_event_types = {"auto_attest"}
        confidences: List[float] = []
        
        try:
            # Optimize: Read file in chunks, early exit if possible
            # For large files, this prevents blocking the event loop too long
            with open(self.audit_logger.log_file, 'r') as f:
                # Read last N lines first (most recent data)
                # This is a heuristic - most recent entries are at the end
                lines = f.readlines()
                # Process in reverse (newest first) for early exit optimization
                for line in reversed(lines[-1000:]):  # Limit to last 1000 lines for performance
                    try:
                        entry_dict = json.loads(line.strip())
                        entry_time = datetime.fromisoformat(entry_dict['timestamp'])
                        
                        if entry_time < cutoff_time:
                            # Since we're reading newest first, if we hit cutoff, we can stop
                            # (older entries won't be in window)
                            break
                        
                        if agent_id and entry_dict['agent_id'] != agent_id:
                            continue
                        
                        if entry_dict.get('event_type') not in allowed_event_types:
                            continue
                        
                        if 'confidence' in entry_dict:
                            confidences.append(entry_dict['confidence'])
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception as e:
            result = {"error": str(e)}
            # Error responses cached for 30s to prevent repeated failures but allow recovery
            self.cache.set('confidence_dist', result, agent_id, window_hours, ttl_seconds=30)
            return result
        
        if not confidences:
            result = {"error": "No confidence data"}
            # Empty data cached for 60s (same as success case) - data won't appear instantly
            self.cache.set('confidence_dist', result, agent_id, window_hours, ttl_seconds=60)
            return result
        
        import numpy as np
        confidences_array = np.array(confidences)

        # Confidence saturation visibility:
        # Even after fixing default/placeholder sources, we want a quick indicator of whether
        # the system is drifting back toward "always 1.0".
        # Use an epsilon threshold to tolerate float formatting differences.
        saturation_threshold = 0.99
        confidence_saturation_rate = float(np.mean(confidences_array >= saturation_threshold))
        
        result = {
            "count": len(confidences),
            "mean": float(np.mean(confidences_array)),
            "median": float(np.median(confidences_array)),
            "std": float(np.std(confidences_array)),
            "min": float(np.min(confidences_array)),
            "max": float(np.max(confidences_array)),
            "confidence_saturation_rate": confidence_saturation_rate,
            "confidence_saturation_threshold": saturation_threshold,
            "percentiles": {
                "p25": float(np.percentile(confidences_array, 25)),
                "p50": float(np.percentile(confidences_array, 50)),
                "p75": float(np.percentile(confidences_array, 75)),
                "p90": float(np.percentile(confidences_array, 90)),
                "p95": float(np.percentile(confidences_array, 95))
            },
            "low_confidence_rate": float(np.mean(confidences_array < 0.8)),
            "high_confidence_rate": float(np.mean(confidences_array >= 0.8))
        }
        
        # Cache result (60s TTL for confidence distributions - see docstring for rationale)
        self.cache.set('confidence_dist', result, agent_id, window_hours, ttl_seconds=60)
        
        return result
    
    def get_calibration_metrics(self) -> Dict:
        """Get calibration metrics with the authoritative assessability state."""
        is_calibrated, metrics = self.calibration_checker.check_calibration()
        return {
            **metrics,
            "is_calibrated": is_calibrated,
            "calibration_status": resolve_calibration_status(is_calibrated, metrics),
        }
    
    def detect_suspicious_patterns(self, agent_id: Optional[str] = None) -> Dict:
        """
        Detect suspicious patterns:
        - Low skip rate but low average confidence (suggests agreeableness)
        - High skip rate but high average confidence (suggests over-conservatism)
        """
        skip_metrics = self.get_skip_rate_metrics(agent_id)
        conf_dist = self.get_confidence_distribution(agent_id)
        
        if "error" in skip_metrics or "error" in conf_dist:
            return {"error": "Insufficient data"}
        
        patterns = []
        
        # Use configurable thresholds
        from config.governance_config import config
        
        # Pattern 1: Low skip rate + low confidence = suspicious (agreeableness)
        if (skip_metrics['skip_rate'] < config.SUSPICIOUS_LOW_SKIP_RATE and 
            conf_dist['mean'] < config.SUSPICIOUS_LOW_CONFIDENCE):
            patterns.append({
                "pattern": "low_skip_low_confidence",
                "severity": "high",
                "description": "Low skip rate but low average confidence suggests agreeableness (auto-approving everything)",
                "skip_rate": skip_metrics['skip_rate'],
                "avg_confidence": conf_dist['mean'],
                "thresholds_used": {
                    "skip_rate": config.SUSPICIOUS_LOW_SKIP_RATE,
                    "confidence": config.SUSPICIOUS_LOW_CONFIDENCE
                }
            })
        
        # Pattern 2: High skip rate + high confidence = suspicious (over-conservatism)
        if (skip_metrics['skip_rate'] > config.SUSPICIOUS_HIGH_SKIP_RATE and 
            conf_dist['mean'] > config.SUSPICIOUS_HIGH_CONFIDENCE):
            patterns.append({
                "pattern": "high_skip_high_confidence",
                "severity": "medium",
                "description": "High skip rate despite high confidence suggests over-conservatism",
                "skip_rate": skip_metrics['skip_rate'],
                "avg_confidence": conf_dist['mean'],
                "thresholds_used": {
                    "skip_rate": config.SUSPICIOUS_HIGH_SKIP_RATE,
                    "confidence": config.SUSPICIOUS_HIGH_CONFIDENCE
                }
            })
        
        return {
            "suspicious_patterns": patterns,
            "skip_metrics": skip_metrics,
            "confidence_distribution": conf_dist
        }
    
    def get_comprehensive_metrics(self, agent_id: Optional[str] = None) -> Dict:
        """Get comprehensive telemetry metrics"""
        return {
            "skip_rate": self.get_skip_rate_metrics(agent_id),
            "confidence_distribution": self.get_confidence_distribution(agent_id),
            "calibration": self.get_calibration_metrics(),
            "suspicious_patterns": self.detect_suspicious_patterns(agent_id)
        }


# Global telemetry collector instance
telemetry_collector = TelemetryCollector()

"""
Tests for src/telemetry.py - TelemetryCollector and suspicious pattern detection.

Tests caching behavior, confidence distribution parsing, suspicious pattern logic,
and error handling paths.
"""

import json
import os
import time
import pytest
import sys
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, PropertyMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# ============================================================================
# TelemetryCollector - init
# ============================================================================

class TestTelemetryCollectorInit:

    def test_creates_instance(self):
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()
        assert tc is not None
        assert tc.audit_logger is not None
        assert tc.calibration_checker is not None
        assert tc.cache is not None


# ============================================================================
# get_skip_rate_metrics - caching
# ============================================================================

class TestGetSkipRateMetrics:

    def test_returns_result(self):
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()
        result = tc.get_skip_rate_metrics()
        assert isinstance(result, dict)

    def test_cache_hit(self):
        """Second call should hit cache."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        # Prime cache
        result1 = tc.get_skip_rate_metrics(agent_id="test_cache_agent")
        # Second call should be cached
        result2 = tc.get_skip_rate_metrics(agent_id="test_cache_agent")
        assert result1 == result2

    def test_different_agents_different_cache(self):
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()
        r1 = tc.get_skip_rate_metrics(agent_id="agent_A_unique")
        r2 = tc.get_skip_rate_metrics(agent_id="agent_B_unique")
        # Both should return dict results (may be same content but separate cache entries)
        assert isinstance(r1, dict)
        assert isinstance(r2, dict)


# ============================================================================
# get_confidence_distribution
# ============================================================================

class TestGetConfidenceDistribution:

    def test_no_audit_log_returns_error(self, tmp_path):
        """When audit log doesn't exist, should return error."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        # Point to non-existent log
        tc.audit_logger.log_file = tmp_path / "nonexistent.jsonl"
        tc.cache = MagicMock()
        tc.cache.get.return_value = None

        result = tc.get_confidence_distribution()
        assert "error" in result

    def test_parses_valid_entries(self, tmp_path):
        """Should parse auto_attest entries and compute statistics."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        # Create a mock audit log
        log_file = tmp_path / "audit.jsonl"
        now = datetime.now()
        entries = []
        for conf in [0.7, 0.8, 0.9, 0.95, 0.6]:
            entries.append(json.dumps({
                "timestamp": now.isoformat(),
                "agent_id": "test_agent",
                "event_type": "auto_attest",
                "confidence": conf,
            }))
        log_file.write_text("\n".join(entries))

        tc.audit_logger.log_file = log_file
        tc.cache = MagicMock()
        tc.cache.get.return_value = None

        result = tc.get_confidence_distribution()
        assert "error" not in result
        assert result["count"] == 5
        assert 0.6 <= result["mean"] <= 0.95
        assert result["min"] == 0.6
        assert result["max"] == 0.95
        assert "percentiles" in result
        assert "confidence_saturation_rate" in result

    def test_filters_non_attest_events(self, tmp_path):
        """Only auto_attest events should be counted."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        log_file = tmp_path / "audit.jsonl"
        now = datetime.now()
        entries = [
            json.dumps({
                "timestamp": now.isoformat(),
                "agent_id": "test_agent",
                "event_type": "auto_attest",
                "confidence": 0.8,
            }),
            json.dumps({
                "timestamp": now.isoformat(),
                "agent_id": "test_agent",
                "event_type": "complexity_derivation",
                "confidence": 1.0,
            }),
            json.dumps({
                "timestamp": now.isoformat(),
                "agent_id": "test_agent",
                "event_type": "auto_resume",
                "confidence": 1.0,
            }),
        ]
        log_file.write_text("\n".join(entries))

        tc.audit_logger.log_file = log_file
        tc.cache = MagicMock()
        tc.cache.get.return_value = None

        result = tc.get_confidence_distribution()
        assert result["count"] == 1  # Only auto_attest

    def test_filters_by_agent_id(self, tmp_path):
        """Should filter by agent_id when provided."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        log_file = tmp_path / "audit.jsonl"
        now = datetime.now()
        entries = [
            json.dumps({
                "timestamp": now.isoformat(),
                "agent_id": "agent_A",
                "event_type": "auto_attest",
                "confidence": 0.8,
            }),
            json.dumps({
                "timestamp": now.isoformat(),
                "agent_id": "agent_B",
                "event_type": "auto_attest",
                "confidence": 0.9,
            }),
        ]
        log_file.write_text("\n".join(entries))

        tc.audit_logger.log_file = log_file
        tc.cache = MagicMock()
        tc.cache.get.return_value = None

        result = tc.get_confidence_distribution(agent_id="agent_A")
        assert result["count"] == 1
        assert result["mean"] == 0.8

    def test_empty_confidence_returns_error(self, tmp_path):
        """No matching entries should return error."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        log_file = tmp_path / "audit.jsonl"
        now = datetime.now()
        entries = [
            json.dumps({
                "timestamp": now.isoformat(),
                "agent_id": "test_agent",
                "event_type": "complexity_derivation",
                "confidence": 1.0,
            }),
        ]
        log_file.write_text("\n".join(entries))

        tc.audit_logger.log_file = log_file
        tc.cache = MagicMock()
        tc.cache.get.return_value = None

        result = tc.get_confidence_distribution()
        assert "error" in result
        assert "No confidence data" in result["error"]

    def test_cutoff_time_filtering(self, tmp_path):
        """Entries outside the window should be excluded."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        log_file = tmp_path / "audit.jsonl"
        now = datetime.now()
        old = now - timedelta(hours=48)
        entries = [
            json.dumps({
                "timestamp": old.isoformat(),
                "agent_id": "test_agent",
                "event_type": "auto_attest",
                "confidence": 0.5,
            }),
            json.dumps({
                "timestamp": now.isoformat(),
                "agent_id": "test_agent",
                "event_type": "auto_attest",
                "confidence": 0.9,
            }),
        ]
        log_file.write_text("\n".join(entries))

        tc.audit_logger.log_file = log_file
        tc.cache = MagicMock()
        tc.cache.get.return_value = None

        result = tc.get_confidence_distribution(window_hours=24)
        assert result["count"] == 1
        assert result["mean"] == 0.9

    def test_corrupted_json_lines_skipped(self, tmp_path):
        """Corrupted JSON lines should be skipped without error."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        log_file = tmp_path / "audit.jsonl"
        now = datetime.now()
        lines = [
            "not valid json",
            json.dumps({
                "timestamp": now.isoformat(),
                "agent_id": "test_agent",
                "event_type": "auto_attest",
                "confidence": 0.75,
            }),
            "{incomplete",
        ]
        log_file.write_text("\n".join(lines))

        tc.audit_logger.log_file = log_file
        tc.cache = MagicMock()
        tc.cache.get.return_value = None

        result = tc.get_confidence_distribution()
        assert result["count"] == 1
        assert result["mean"] == 0.75

    def test_saturation_rate_calculation(self, tmp_path):
        """Confidence >= 0.99 should count as saturated."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        log_file = tmp_path / "audit.jsonl"
        now = datetime.now()
        entries = []
        # 3 saturated, 2 not
        for conf in [0.99, 1.0, 0.995, 0.5, 0.7]:
            entries.append(json.dumps({
                "timestamp": now.isoformat(),
                "agent_id": "test_agent",
                "event_type": "auto_attest",
                "confidence": conf,
            }))
        log_file.write_text("\n".join(entries))

        tc.audit_logger.log_file = log_file
        tc.cache = MagicMock()
        tc.cache.get.return_value = None

        result = tc.get_confidence_distribution()
        assert result["confidence_saturation_rate"] == pytest.approx(0.6, abs=0.01)

    def test_file_read_error_returns_error(self, tmp_path):
        """File read errors should return error dict."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        # Create a directory with the log name (will cause read error)
        log_path = tmp_path / "audit.jsonl"
        log_path.mkdir()

        tc.audit_logger.log_file = log_path
        tc.cache = MagicMock()
        tc.cache.get.return_value = None

        result = tc.get_confidence_distribution()
        assert "error" in result


# ============================================================================
# detect_suspicious_patterns
# ============================================================================

class TestDetectSuspiciousPatterns:

    def test_insufficient_data(self):
        """Should return error when data is insufficient."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        # Mock methods to return errors
        tc.get_skip_rate_metrics = MagicMock(return_value={"error": "No data"})
        tc.get_confidence_distribution = MagicMock(return_value={"mean": 0.8})

        result = tc.detect_suspicious_patterns()
        assert "error" in result

    def test_no_suspicious_patterns(self):
        """Normal metrics should produce no patterns."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        # Normal: moderate skip rate, moderate confidence
        tc.get_skip_rate_metrics = MagicMock(return_value={"skip_rate": 0.3})
        tc.get_confidence_distribution = MagicMock(return_value={"mean": 0.75})

        result = tc.detect_suspicious_patterns()
        assert "suspicious_patterns" in result
        assert len(result["suspicious_patterns"]) == 0

    def test_agreeableness_pattern(self):
        """Low skip rate + low confidence = agreeableness."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        # Mock: very low skip rate and low confidence
        tc.get_skip_rate_metrics = MagicMock(return_value={"skip_rate": 0.01})
        tc.get_confidence_distribution = MagicMock(return_value={"mean": 0.4})

        result = tc.detect_suspicious_patterns()
        patterns = result["suspicious_patterns"]
        assert len(patterns) >= 1
        assert any(p["pattern"] == "low_skip_low_confidence" for p in patterns)
        assert any(p["severity"] == "high" for p in patterns)

    def test_over_conservatism_pattern(self):
        """High skip rate + high confidence = over-conservatism."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        # Mock: very high skip rate and high confidence
        tc.get_skip_rate_metrics = MagicMock(return_value={"skip_rate": 0.9})
        tc.get_confidence_distribution = MagicMock(return_value={"mean": 0.95})

        result = tc.detect_suspicious_patterns()
        patterns = result["suspicious_patterns"]
        assert len(patterns) >= 1
        assert any(p["pattern"] == "high_skip_high_confidence" for p in patterns)
        assert any(p["severity"] == "medium" for p in patterns)

    def test_includes_metrics_in_response(self):
        """Response should include skip_metrics and confidence_distribution."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        tc.get_skip_rate_metrics = MagicMock(return_value={"skip_rate": 0.5})
        tc.get_confidence_distribution = MagicMock(return_value={"mean": 0.7})

        result = tc.detect_suspicious_patterns()
        assert "skip_metrics" in result
        assert "confidence_distribution" in result

    def test_pattern_includes_thresholds(self):
        """Detected patterns should include the thresholds used."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        tc.get_skip_rate_metrics = MagicMock(return_value={"skip_rate": 0.01})
        tc.get_confidence_distribution = MagicMock(return_value={"mean": 0.4})

        result = tc.detect_suspicious_patterns()
        for p in result["suspicious_patterns"]:
            assert "thresholds_used" in p
            assert "skip_rate" in p
            assert "avg_confidence" in p


# ============================================================================
# get_comprehensive_metrics
# ============================================================================

class TestGetComprehensiveMetrics:

    def test_returns_all_sections(self):
        """Should include skip_rate, confidence, calibration, suspicious."""
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        result = tc.get_comprehensive_metrics()
        assert "skip_rate" in result
        assert "confidence_distribution" in result
        assert "calibration" in result
        assert "suspicious_patterns" in result


# ============================================================================
# get_calibration_metrics
# ============================================================================

class TestGetCalibrationMetrics:

    def test_returns_dict_with_is_calibrated(self):
        from src.telemetry import TelemetryCollector
        tc = TelemetryCollector()

        result = tc.get_calibration_metrics()
        assert isinstance(result, dict)
        assert "is_calibrated" in result

    def test_surfaces_tri_state_instead_of_reinterpreting_legacy_boolean(self):
        from src.telemetry import TelemetryCollector

        tc = TelemetryCollector()
        tc.calibration_checker = MagicMock()
        tc.calibration_checker.check_calibration.return_value = (
            True,
            {
                "calibration_status": "unassessed",
                "assessability": {
                    "overall": False,
                    "strategic": True,
                    "tactical": False,
                },
            },
        )

        result = tc.get_calibration_metrics()

        assert result["is_calibrated"] is True  # legacy field remains compatible
        assert result["calibration_status"] == "unassessed"
        assert result["assessability"]["tactical"] is False

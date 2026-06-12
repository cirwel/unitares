"""
Comprehensive tests for src/audit_log.py

Tests AuditEntry dataclass, AuditLogger log methods, _write_entry,
rotate_log, query_audit_log, and get_skip_rate_metrics.

All tests disable SQLite (UNITARES_AUDIT_WRITE_SQLITE=0,
UNITARES_AUDIT_QUERY_BACKEND=jsonl) so only JSONL is exercised.
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import asdict
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Environment setup (legacy SQLite fixtures removed - Postgres only now)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _read_jsonl(path):
    """Read all JSONL lines from a file and return list of dicts."""
    entries = []
    if not path.exists():
        return entries
    with open(path, "r") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                entries.append(json.loads(stripped))
    return entries


def _make_logger(tmp_path):
    """Create an AuditLogger pointing at a temp directory."""
    from src.audit_log import AuditLogger
    log_file = tmp_path / "audit_log.jsonl"
    return AuditLogger(log_file=log_file)


# ===========================================================================
# AuditEntry dataclass
# ===========================================================================
class TestAuditEntry:
    def test_basic_creation(self):
        from src.audit_log import AuditEntry
        entry = AuditEntry(
            timestamp="2025-01-01T00:00:00",
            agent_id="agent-1",
            event_type="lambda1_skip",
            confidence=0.85,
            details={"threshold": 0.9},
        )
        assert entry.timestamp == "2025-01-01T00:00:00"
        assert entry.agent_id == "agent-1"
        assert entry.event_type == "lambda1_skip"
        assert entry.confidence == 0.85
        assert entry.details == {"threshold": 0.9}
        assert entry.metadata is None
        assert entry.session_id is None

    def test_with_metadata(self):
        from src.audit_log import AuditEntry
        entry = AuditEntry(
            timestamp="2025-01-01T00:00:00",
            agent_id="agent-1",
            event_type="auto_attest",
            confidence=0.9,
            details={},
            metadata={"source": "test"},
        )
        assert entry.metadata == {"source": "test"}

    def test_asdict_roundtrip(self):
        from src.audit_log import AuditEntry
        entry = AuditEntry(
            timestamp="2025-01-01T00:00:00",
            agent_id="agent-1",
            event_type="auto_attest",
            confidence=0.9,
            details={"key": "value"},
            metadata={"m": 1},
        )
        d = asdict(entry)
        assert d["timestamp"] == "2025-01-01T00:00:00"
        assert d["details"]["key"] == "value"
        assert d["metadata"]["m"] == 1

    def test_serialisable_to_json(self):
        from src.audit_log import AuditEntry
        entry = AuditEntry(
            timestamp="2025-01-01T12:00:00",
            agent_id="a",
            event_type="test",
            confidence=0.5,
            details={"nested": {"x": [1, 2, 3]}},
        )
        text = json.dumps(asdict(entry))
        restored = json.loads(text)
        assert restored["details"]["nested"]["x"] == [1, 2, 3]


# ===========================================================================
# log_identity_resolution_observed
# ===========================================================================
class TestLogIdentityResolutionObserved:
    def test_writes_full_event(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_identity_resolution_observed(
            agent_uuid="11111111-2222-3333-4444-555555555555",
            resolution_source="continuity_token",
            pin_match_scope=None,
            pin_entry_present=True,
            pin_fingerprint_match=False,
            pin_entry_age_seconds=420,
            token_iat=1700_000_000,
            token_exp=1700_003_600,
            token_age_seconds=900,
        )
        entries = _read_jsonl(logger.log_file)
        assert len(entries) == 1
        e = entries[0]
        assert e["event_type"] == "identity_resolution_observed"
        assert e["agent_id"] == "11111111-2222-3333-4444-555555555555"
        d = e["details"]
        assert d["resolution_source"] == "continuity_token"
        assert d["pin_entry_present"] is True
        assert d["pin_fingerprint_match"] is False
        assert d["pin_entry_age_seconds"] == 420
        assert d["token_iat"] == 1700_000_000
        assert d["token_exp"] == 1700_003_600
        assert d["token_age_seconds"] == 900

    def test_accepts_none_agent_uuid(self, tmp_path):
        """Resolutions that don't bind to an agent (pre-bind diagnostic paths)
        must not crash on None — verifies the AuditEntry.agent_id Optional fix."""
        logger = _make_logger(tmp_path)
        logger.log_identity_resolution_observed(
            agent_uuid=None,
            resolution_source="ip_ua_fingerprint",
        )
        entries = _read_jsonl(logger.log_file)
        assert entries[0]["agent_id"] is None
        assert entries[0]["event_type"] == "identity_resolution_observed"

    def test_pin_only_event(self, tmp_path):
        """When PATH 7 wins, only pin_match_scope is meaningful — token fields stay None."""
        logger = _make_logger(tmp_path)
        logger.log_identity_resolution_observed(
            agent_uuid="agent-pin",
            resolution_source="pinned_onboard_session",
            pin_match_scope="client_model",
        )
        entries = _read_jsonl(logger.log_file)
        d = entries[0]["details"]
        assert d["resolution_source"] == "pinned_onboard_session"
        assert d["pin_match_scope"] == "client_model"
        assert d["token_iat"] is None
        assert d["token_exp"] is None


# ===========================================================================
# log_session_resolve_miss_observed
# ===========================================================================
class TestLogSessionResolveMissObserved:
    def test_writes_queryable_session_miss_event(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_session_resolve_miss_observed(
            session_key="agent-no-row-test",
            resolution_source="explicit_client_session_id",
            reason="pg_session_missing",
            resume=True,
            force_new=False,
            token_agent_uuid_present=False,
            client_hint="codex",
            model_type="gpt-5-codex",
        )

        entries = _read_jsonl(logger.log_file)
        assert len(entries) == 1
        e = entries[0]
        assert e["event_type"] == "session_resolve_miss_observed"
        assert e["agent_id"] is None
        assert e["session_id"] == "agent-no-row-test"
        d = e["details"]
        assert d["session_key_prefix"] == "agent-no-row-test"
        assert d["resolution_source"] == "explicit_client_session_id"
        assert d["reason"] == "pg_session_missing"
        assert d["resume"] is True
        assert d["force_new"] is False
        assert d["token_agent_uuid_present"] is False
        assert d["client_hint"] == "codex"
        assert d["model_type"] == "gpt-5-codex"


# ===========================================================================
# AuditLogger.__init__ and internal flags
# ===========================================================================
class TestAuditLoggerInit:
    def test_creates_log_directory(self, tmp_path):
        from src.audit_log import AuditLogger
        deep_path = tmp_path / "a" / "b" / "c" / "audit.jsonl"
        logger = AuditLogger(log_file=deep_path)
        assert deep_path.parent.exists()



# ===========================================================================
# log_lambda1_skip
# ===========================================================================
class TestLogLambda1Skip:
    def test_writes_valid_jsonl(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_lambda1_skip(
            agent_id="agent-1",
            confidence=0.65,
            threshold=0.8,
            update_count=5,
        )
        entries = _read_jsonl(logger.log_file)
        assert len(entries) == 1
        e = entries[0]
        assert e["event_type"] == "lambda1_skip"
        assert e["agent_id"] == "agent-1"
        assert e["confidence"] == 0.65
        assert e["details"]["threshold"] == 0.8
        assert e["details"]["update_count"] == 5

    def test_default_reason(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_lambda1_skip("a", 0.5, 0.8, 3)
        entries = _read_jsonl(logger.log_file)
        assert "confidence 0.500 < threshold 0.800" in entries[0]["details"]["reason"]

    def test_custom_reason(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_lambda1_skip("a", 0.5, 0.8, 3, reason="custom reason")
        entries = _read_jsonl(logger.log_file)
        assert entries[0]["details"]["reason"] == "custom reason"

    def test_timestamp_is_iso(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_lambda1_skip("a", 0.5, 0.8, 1)
        entries = _read_jsonl(logger.log_file)
        dt = datetime.fromisoformat(entries[0]["timestamp"])
        assert isinstance(dt, datetime)


# ===========================================================================
# log_auto_attest
# ===========================================================================
class TestLogAutoAttest:
    def test_writes_valid_jsonl(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_auto_attest(
            agent_id="agent-2",
            confidence=0.95,
            ci_passed=True,
            risk_score=0.1,
            decision="approved",
        )
        entries = _read_jsonl(logger.log_file)
        assert len(entries) == 1
        e = entries[0]
        assert e["event_type"] == "auto_attest"
        assert e["details"]["ci_passed"] is True
        assert e["details"]["risk_score"] == 0.1
        assert e["details"]["decision"] == "approved"

    def test_with_extra_details(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_auto_attest(
            "a", 0.9, True, 0.2, "ok",
            details={"extra_key": "extra_value"},
        )
        entries = _read_jsonl(logger.log_file)
        assert entries[0]["details"]["extra_key"] == "extra_value"

    def test_no_extra_details(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_auto_attest("a", 0.9, False, 0.5, "rejected")
        entries = _read_jsonl(logger.log_file)
        assert entries[0]["details"]["ci_passed"] is False


# ===========================================================================
# log_complexity_derivation
# ===========================================================================
# ===========================================================================
# log_calibration_check
# ===========================================================================
# ===========================================================================
# log_auto_resume
# ===========================================================================
# ===========================================================================
# log_dialectic_nudge
# ===========================================================================
# ===========================================================================
# log_cross_device_call
# ===========================================================================
# ===========================================================================
# log_orchestration_request
# ===========================================================================
# ===========================================================================
# log_orchestration_complete
# ===========================================================================
# ===========================================================================
# log_device_health_check
# ===========================================================================
# ===========================================================================
# log_eisv_sync
# ===========================================================================
# ===========================================================================
# _write_entry internals
# ===========================================================================
class TestWriteEntry:
    def test_multiple_entries_append(self, tmp_path):
        logger = _make_logger(tmp_path)
        for i in range(5):
            logger.log_lambda1_skip(f"agent-{i}", 0.5, 0.8, i)
        entries = _read_jsonl(logger.log_file)
        assert len(entries) == 5
        for i, e in enumerate(entries):
            assert e["agent_id"] == f"agent-{i}"

    def test_entries_are_one_per_line(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_lambda1_skip("a", 0.5, 0.8, 1)
        logger.log_auto_attest("b", 0.9, True, 0.1, "ok")
        with open(logger.log_file, "r") as f:
            lines = f.readlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line.strip())

    def test_metadata_none_serialized(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_lambda1_skip("a", 0.5, 0.8, 1)
        entries = _read_jsonl(logger.log_file)
        assert entries[0]["metadata"] is None

    def test_jsonl_disabled(self, tmp_path, monkeypatch):
        """When JSONL writing is disabled, no file is written."""
        monkeypatch.setenv("UNITARES_AUDIT_WRITE_JSONL", "0")
        from src.audit_log import AuditLogger
        log_file = tmp_path / "disabled.jsonl"
        logger = AuditLogger(log_file=log_file)
        logger.log_lambda1_skip("a", 0.5, 0.8, 1)
        assert not log_file.exists()

    def test_write_entry_exception_does_not_crash(self, tmp_path):
        """_write_entry should not raise even if writing fails."""
        logger = _make_logger(tmp_path)
        # Make the log_file path a directory to cause write failure
        logger.log_file.mkdir(parents=True, exist_ok=True)
        # Should not raise
        logger.log_lambda1_skip("a", 0.5, 0.8, 1)


# ===========================================================================
# query_audit_log
# ===========================================================================
class TestQueryAuditLog:
    def _populate(self, logger, count=10):
        """Write a mix of events with known timestamps."""
        base = datetime(2025, 6, 1, 12, 0, 0)
        for i in range(count):
            ts = (base + timedelta(hours=i)).isoformat()
            agent = f"agent-{i % 3}"
            if i % 2 == 0:
                entry_line = json.dumps({
                    "timestamp": ts,
                    "agent_id": agent,
                    "event_type": "lambda1_skip",
                    "confidence": 0.5 + i * 0.01,
                    "details": {"threshold": 0.8, "update_count": i, "reason": "test"},
                    "metadata": None,
                })
            else:
                entry_line = json.dumps({
                    "timestamp": ts,
                    "agent_id": agent,
                    "event_type": "auto_attest",
                    "confidence": 0.9,
                    "details": {"ci_passed": True, "risk_score": 0.1, "decision": "ok"},
                    "metadata": None,
                })
            with open(logger.log_file, "a") as f:
                f.write(entry_line + "\n")

    def test_query_no_filters(self, tmp_path):
        logger = _make_logger(tmp_path)
        self._populate(logger, 10)
        results = logger.query_audit_log()
        assert len(results) == 10

    def test_query_by_agent_id(self, tmp_path):
        logger = _make_logger(tmp_path)
        self._populate(logger, 10)
        results = logger.query_audit_log(agent_id="agent-0")
        assert all(r["agent_id"] == "agent-0" for r in results)
        assert len(results) > 0

    def test_query_by_event_type(self, tmp_path):
        logger = _make_logger(tmp_path)
        self._populate(logger, 10)
        results = logger.query_audit_log(event_type="lambda1_skip")
        assert all(r["event_type"] == "lambda1_skip" for r in results)
        assert len(results) == 5

    def test_query_by_time_range(self, tmp_path):
        logger = _make_logger(tmp_path)
        self._populate(logger, 10)
        start = "2025-06-01T14:00:00"
        end = "2025-06-01T17:00:00"
        results = logger.query_audit_log(start_time=start, end_time=end)
        for r in results:
            ts = datetime.fromisoformat(r["timestamp"])
            assert ts >= datetime.fromisoformat(start)
            assert ts <= datetime.fromisoformat(end)

    def test_query_with_limit(self, tmp_path):
        logger = _make_logger(tmp_path)
        self._populate(logger, 20)
        results = logger.query_audit_log(limit=5)
        assert len(results) == 5

    def test_query_combined_filters(self, tmp_path):
        logger = _make_logger(tmp_path)
        self._populate(logger, 10)
        results = logger.query_audit_log(
            agent_id="agent-0",
            event_type="lambda1_skip",
        )
        assert all(r["agent_id"] == "agent-0" for r in results)
        assert all(r["event_type"] == "lambda1_skip" for r in results)

    def test_query_no_file_returns_empty(self, tmp_path):
        logger = _make_logger(tmp_path)
        results = logger.query_audit_log()
        assert results == []

    def test_query_ignores_malformed_lines(self, tmp_path):
        logger = _make_logger(tmp_path)
        with open(logger.log_file, "w") as f:
            f.write("not valid json\n")
            f.write(json.dumps({
                "timestamp": "2025-01-01T00:00:00",
                "agent_id": "a",
                "event_type": "test",
                "confidence": 0.5,
                "details": {},
                "metadata": None,
            }) + "\n")
            f.write("{}\n")
        results = logger.query_audit_log()
        assert len(results) >= 1


# ===========================================================================
# rotate_log
# ===========================================================================
class TestRotateLog:
    def test_rotates_old_entries(self, tmp_path):
        logger = _make_logger(tmp_path)
        now = datetime.now()
        old_ts = (now - timedelta(days=60)).isoformat()
        recent_ts = now.isoformat()
        with open(logger.log_file, "w") as f:
            f.write(json.dumps({
                "timestamp": old_ts,
                "agent_id": "old",
                "event_type": "lambda1_skip",
                "confidence": 0.5,
                "details": {},
                "metadata": None,
            }) + "\n")
            f.write(json.dumps({
                "timestamp": recent_ts,
                "agent_id": "recent",
                "event_type": "auto_attest",
                "confidence": 0.9,
                "details": {},
                "metadata": None,
            }) + "\n")

        count, archive_file = logger.rotate_log(max_age_days=30)
        assert count == 1
        remaining = _read_jsonl(logger.log_file)
        assert len(remaining) == 1
        assert remaining[0]["agent_id"] == "recent"

        assert archive_file.exists()
        archived = _read_jsonl(archive_file)
        assert len(archived) == 1
        assert archived[0]["agent_id"] == "old"

    def test_rotate_no_file(self, tmp_path):
        logger = _make_logger(tmp_path)
        result = logger.rotate_log()
        assert result is None

    def test_rotate_all_recent(self, tmp_path):
        logger = _make_logger(tmp_path)
        now = datetime.now()
        with open(logger.log_file, "w") as f:
            for i in range(3):
                ts = (now - timedelta(hours=i)).isoformat()
                f.write(json.dumps({
                    "timestamp": ts,
                    "agent_id": f"agent-{i}",
                    "event_type": "test",
                    "confidence": 0.5,
                    "details": {},
                    "metadata": None,
                }) + "\n")

        count, archive_file = logger.rotate_log(max_age_days=1)
        assert count == 3
        remaining = _read_jsonl(logger.log_file)
        assert len(remaining) == 3

    def test_rotate_all_old(self, tmp_path):
        logger = _make_logger(tmp_path)
        old = datetime.now() - timedelta(days=100)
        with open(logger.log_file, "w") as f:
            for i in range(3):
                ts = (old + timedelta(hours=i)).isoformat()
                f.write(json.dumps({
                    "timestamp": ts,
                    "agent_id": f"agent-{i}",
                    "event_type": "test",
                    "confidence": 0.5,
                    "details": {},
                    "metadata": None,
                }) + "\n")

        count, archive_file = logger.rotate_log(max_age_days=30)
        assert count == 0
        remaining = _read_jsonl(logger.log_file)
        assert len(remaining) == 0
        archived = _read_jsonl(archive_file)
        assert len(archived) == 3

    def test_archive_directory_created(self, tmp_path):
        logger = _make_logger(tmp_path)
        old_ts = (datetime.now() - timedelta(days=60)).isoformat()
        with open(logger.log_file, "w") as f:
            f.write(json.dumps({
                "timestamp": old_ts,
                "agent_id": "a",
                "event_type": "test",
                "confidence": 0.5,
                "details": {},
                "metadata": None,
            }) + "\n")
        logger.rotate_log(max_age_days=30)
        archive_dir = logger.log_file.parent / "audit_log_archive"
        assert archive_dir.exists()
        assert archive_dir.is_dir()


# ===========================================================================
# get_skip_rate_metrics
# ===========================================================================
class TestGetSkipRateMetrics:
    def _write_entries(self, logger, skips, attests, hours_ago=0):
        """Write skip and attest events with timestamps relative to now."""
        now = datetime.now()
        ts = (now - timedelta(hours=hours_ago)).isoformat()
        with open(logger.log_file, "a") as f:
            for i in range(skips):
                f.write(json.dumps({
                    "timestamp": ts,
                    "agent_id": "agent-1",
                    "event_type": "lambda1_skip",
                    "confidence": 0.5,
                    "details": {},
                    "metadata": None,
                }) + "\n")
            for i in range(attests):
                f.write(json.dumps({
                    "timestamp": ts,
                    "agent_id": "agent-1",
                    "event_type": "auto_attest",
                    "confidence": 0.9,
                    "details": {},
                    "metadata": None,
                }) + "\n")

    @patch("config.governance_config.config")
    def test_basic_metrics(self, mock_config, tmp_path):
        mock_config.SUSPICIOUS_LOW_SKIP_RATE = 0.1
        mock_config.SUSPICIOUS_LOW_CONFIDENCE = 0.7
        logger = _make_logger(tmp_path)
        self._write_entries(logger, skips=3, attests=7)
        metrics = logger.get_skip_rate_metrics(agent_id="agent-1", window_hours=24)
        assert metrics["total_skips"] == 3
        assert metrics["total_updates"] == 7
        assert metrics["skip_rate"] == pytest.approx(0.3)
        assert metrics["avg_confidence"] == pytest.approx(0.5)
        assert metrics["suspicious"] is False

    @patch("config.governance_config.config")
    def test_no_data(self, mock_config, tmp_path):
        mock_config.SUSPICIOUS_LOW_SKIP_RATE = 0.1
        mock_config.SUSPICIOUS_LOW_CONFIDENCE = 0.7
        logger = _make_logger(tmp_path)
        metrics = logger.get_skip_rate_metrics()
        assert metrics["total_skips"] == 0
        assert metrics["total_updates"] == 0
        assert metrics["skip_rate"] == 0.0
        assert metrics["avg_confidence"] == 0.0
        assert metrics["suspicious"] is False

    @patch("config.governance_config.config")
    def test_suspicious_pattern(self, mock_config, tmp_path):
        """Low skip rate + low confidence + enough events = suspicious."""
        mock_config.SUSPICIOUS_LOW_SKIP_RATE = 0.1
        mock_config.SUSPICIOUS_LOW_CONFIDENCE = 0.7
        logger = _make_logger(tmp_path)
        now = datetime.now()
        ts = now.isoformat()
        with open(logger.log_file, "w") as f:
            f.write(json.dumps({
                "timestamp": ts,
                "agent_id": "agent-x",
                "event_type": "lambda1_skip",
                "confidence": 0.3,
                "details": {},
                "metadata": None,
            }) + "\n")
            for _ in range(20):
                f.write(json.dumps({
                    "timestamp": ts,
                    "agent_id": "agent-x",
                    "event_type": "auto_attest",
                    "confidence": 0.9,
                    "details": {},
                    "metadata": None,
                }) + "\n")
        metrics = logger.get_skip_rate_metrics(agent_id="agent-x", window_hours=24)
        assert metrics["suspicious"] is True

    @patch("config.governance_config.config")
    def test_not_suspicious_high_skip_rate(self, mock_config, tmp_path):
        """High skip rate means not suspicious."""
        mock_config.SUSPICIOUS_LOW_SKIP_RATE = 0.1
        mock_config.SUSPICIOUS_LOW_CONFIDENCE = 0.7
        logger = _make_logger(tmp_path)
        now = datetime.now()
        ts = now.isoformat()
        with open(logger.log_file, "w") as f:
            for _ in range(15):
                f.write(json.dumps({
                    "timestamp": ts,
                    "agent_id": "agent-y",
                    "event_type": "lambda1_skip",
                    "confidence": 0.3,
                    "details": {},
                    "metadata": None,
                }) + "\n")
            for _ in range(5):
                f.write(json.dumps({
                    "timestamp": ts,
                    "agent_id": "agent-y",
                    "event_type": "auto_attest",
                    "confidence": 0.9,
                    "details": {},
                    "metadata": None,
                }) + "\n")
        metrics = logger.get_skip_rate_metrics(agent_id="agent-y", window_hours=24)
        assert metrics["suspicious"] is False

    @patch("config.governance_config.config")
    def test_window_excludes_old_entries(self, mock_config, tmp_path):
        mock_config.SUSPICIOUS_LOW_SKIP_RATE = 0.1
        mock_config.SUSPICIOUS_LOW_CONFIDENCE = 0.7
        logger = _make_logger(tmp_path)
        now = datetime.now()
        old_ts = (now - timedelta(hours=48)).isoformat()
        recent_ts = now.isoformat()
        with open(logger.log_file, "w") as f:
            f.write(json.dumps({
                "timestamp": old_ts,
                "agent_id": "agent-1",
                "event_type": "lambda1_skip",
                "confidence": 0.5,
                "details": {},
                "metadata": None,
            }) + "\n")
            f.write(json.dumps({
                "timestamp": recent_ts,
                "agent_id": "agent-1",
                "event_type": "lambda1_skip",
                "confidence": 0.6,
                "details": {},
                "metadata": None,
            }) + "\n")
        metrics = logger.get_skip_rate_metrics(agent_id="agent-1", window_hours=24)
        assert metrics["total_skips"] == 1

    @patch("config.governance_config.config")
    def test_agent_id_filter(self, mock_config, tmp_path):
        mock_config.SUSPICIOUS_LOW_SKIP_RATE = 0.1
        mock_config.SUSPICIOUS_LOW_CONFIDENCE = 0.7
        logger = _make_logger(tmp_path)
        now = datetime.now()
        ts = now.isoformat()
        with open(logger.log_file, "w") as f:
            f.write(json.dumps({
                "timestamp": ts, "agent_id": "agent-A",
                "event_type": "lambda1_skip", "confidence": 0.5,
                "details": {}, "metadata": None,
            }) + "\n")
            f.write(json.dumps({
                "timestamp": ts, "agent_id": "agent-B",
                "event_type": "lambda1_skip", "confidence": 0.4,
                "details": {}, "metadata": None,
            }) + "\n")
        metrics = logger.get_skip_rate_metrics(agent_id="agent-A", window_hours=24)
        assert metrics["total_skips"] == 1
        assert metrics["avg_confidence"] == pytest.approx(0.5)

    @patch("config.governance_config.config")
    def test_no_agent_filter_counts_all(self, mock_config, tmp_path):
        mock_config.SUSPICIOUS_LOW_SKIP_RATE = 0.1
        mock_config.SUSPICIOUS_LOW_CONFIDENCE = 0.7
        logger = _make_logger(tmp_path)
        now = datetime.now()
        ts = now.isoformat()
        with open(logger.log_file, "w") as f:
            f.write(json.dumps({
                "timestamp": ts, "agent_id": "agent-A",
                "event_type": "lambda1_skip", "confidence": 0.5,
                "details": {}, "metadata": None,
            }) + "\n")
            f.write(json.dumps({
                "timestamp": ts, "agent_id": "agent-B",
                "event_type": "lambda1_skip", "confidence": 0.4,
                "details": {}, "metadata": None,
            }) + "\n")
        metrics = logger.get_skip_rate_metrics(window_hours=24)
        assert metrics["total_skips"] == 2

    @patch("config.governance_config.config")
    def test_window_hours_parameter(self, mock_config, tmp_path):
        mock_config.SUSPICIOUS_LOW_SKIP_RATE = 0.1
        mock_config.SUSPICIOUS_LOW_CONFIDENCE = 0.7
        logger = _make_logger(tmp_path)
        now = datetime.now()
        ts_6h = (now - timedelta(hours=6)).isoformat()
        ts_2h = (now - timedelta(hours=2)).isoformat()
        with open(logger.log_file, "w") as f:
            f.write(json.dumps({
                "timestamp": ts_6h, "agent_id": "a",
                "event_type": "lambda1_skip", "confidence": 0.5,
                "details": {}, "metadata": None,
            }) + "\n")
            f.write(json.dumps({
                "timestamp": ts_2h, "agent_id": "a",
                "event_type": "lambda1_skip", "confidence": 0.5,
                "details": {}, "metadata": None,
            }) + "\n")
        metrics = logger.get_skip_rate_metrics(window_hours=4)
        assert metrics["total_skips"] == 1

    @patch("config.governance_config.config")
    def test_not_suspicious_too_few_events(self, mock_config, tmp_path):
        """Even with low skip rate + low confidence, fewer than 10 events is not suspicious."""
        mock_config.SUSPICIOUS_LOW_SKIP_RATE = 0.1
        mock_config.SUSPICIOUS_LOW_CONFIDENCE = 0.7
        logger = _make_logger(tmp_path)
        now = datetime.now()
        ts = now.isoformat()
        with open(logger.log_file, "w") as f:
            f.write(json.dumps({
                "timestamp": ts, "agent_id": "a",
                "event_type": "lambda1_skip", "confidence": 0.3,
                "details": {}, "metadata": None,
            }) + "\n")
            for _ in range(5):
                f.write(json.dumps({
                    "timestamp": ts, "agent_id": "a",
                    "event_type": "auto_attest", "confidence": 0.9,
                    "details": {}, "metadata": None,
                }) + "\n")
        metrics = logger.get_skip_rate_metrics(agent_id="a", window_hours=24)
        assert metrics["suspicious"] is False


# ===========================================================================
# Edge cases and integration-like tests
# ===========================================================================
class TestEdgeCases:
    def test_concurrent_writes_basic(self, tmp_path):
        """Multiple sequential writes produce correct file."""
        logger = _make_logger(tmp_path)
        for i in range(50):
            logger.log_lambda1_skip(f"agent-{i}", 0.5, 0.8, i)
        entries = _read_jsonl(logger.log_file)
        assert len(entries) == 50

    def test_unicode_in_details(self, tmp_path):
        logger = _make_logger(tmp_path)
        logger.log_auto_attest(
            "agent-unicode", 0.9, True, 0.1, "ok",
            details={"message": "Hello, world! Emoji test - sharp"},
        )
        entries = _read_jsonl(logger.log_file)
        assert entries[0]["details"]["message"] == "Hello, world! Emoji test - sharp"

    def test_large_details_dict(self, tmp_path):
        logger = _make_logger(tmp_path)
        big_details = {f"key_{i}": f"value_{i}" for i in range(100)}
        logger.log_auto_attest("a", 0.9, True, 0.1, "ok", details=big_details)
        entries = _read_jsonl(logger.log_file)
        assert len(entries[0]["details"]) > 100

    def test_mixed_event_types_query(self, tmp_path):
        """Write different event types and query each."""
        logger = _make_logger(tmp_path)
        logger.log_lambda1_skip("a", 0.5, 0.8, 1)
        logger.log_auto_attest("a", 0.9, True, 0.1, "ok")
        logger.log_attest_gap_suppressed("a", 5.0, 0.1, "low_risk", 2)

        all_entries = logger.query_audit_log()
        assert len(all_entries) == 3

        skips = logger.query_audit_log(event_type="lambda1_skip")
        assert len(skips) == 1

        attests = logger.query_audit_log(event_type="auto_attest")
        assert len(attests) == 1

    def test_query_start_time_only(self, tmp_path):
        logger = _make_logger(tmp_path)
        now = datetime.now()
        with open(logger.log_file, "w") as f:
            for i in range(5):
                ts = (now - timedelta(hours=10 - i)).isoformat()
                f.write(json.dumps({
                    "timestamp": ts, "agent_id": "a",
                    "event_type": "test", "confidence": 0.5,
                    "details": {}, "metadata": None,
                }) + "\n")
        start = (now - timedelta(hours=7)).isoformat()
        results = logger.query_audit_log(start_time=start)
        assert len(results) > 0
        for r in results:
            assert datetime.fromisoformat(r["timestamp"]) >= datetime.fromisoformat(start)

    def test_query_end_time_only(self, tmp_path):
        logger = _make_logger(tmp_path)
        now = datetime.now()
        with open(logger.log_file, "w") as f:
            for i in range(5):
                ts = (now - timedelta(hours=10 - i)).isoformat()
                f.write(json.dumps({
                    "timestamp": ts, "agent_id": "a",
                    "event_type": "test", "confidence": 0.5,
                    "details": {}, "metadata": None,
                }) + "\n")
        end = (now - timedelta(hours=8)).isoformat()
        results = logger.query_audit_log(end_time=end)
        assert len(results) > 0
        for r in results:
            assert datetime.fromisoformat(r["timestamp"]) <= datetime.fromisoformat(end)

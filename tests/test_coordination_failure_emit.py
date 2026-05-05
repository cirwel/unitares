"""Tests for src/coordination_failure_emit.py — Wave 0 step 2A (sync pivot).

Pins the sync-emit contract that 2A relies on:
  - failure-safe (never raises) — caller is inside an `except` clause
  - writes via the existing audit_logger._write_entry path (sidesteps anyio)
  - validates event_type prefix client-side
  - falls back to 'governance_mcp' on unknown service (logs WARNING but emits)
  - mocks audit_logger so the test doesn't depend on a writable JSONL file
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.coordination_failure_emit import (  # noqa: E402
    SERVICES,
    emit_coordination_failure_sync,
)


def test_known_services_match_documented_set():
    """Drift guard: SERVICES MUST mirror PR #342's coordination_events service
    enum (so when/if Wave 0 step 3 promotes events to the dedicated table, the
    values port unchanged)."""
    assert SERVICES == frozenset({
        "sentinel",
        "governance_mcp",
        "lease_plane",
        "vigil",
        "chronicler",
        "watcher",
    })


def test_emit_writes_via_audit_logger_with_correct_envelope():
    """Happy path: sync emit lands an AuditEntry via _write_entry."""
    fake_logger = MagicMock()
    with patch.dict("sys.modules"):  # noqa: SIM117 — preserve modules
        with patch("src.audit_log.audit_logger", fake_logger), \
             patch("src.audit_log.AuditEntry") as fake_entry_cls:
            emit_coordination_failure_sync(
                service="governance_mcp",
                event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
                payload={"tool_name": "process_agent_update", "timeout_s": 45.0, "elapsed_s": 45.1},
                agent_id="some-uuid",
            )
    fake_entry_cls.assert_called_once()
    call_kwargs = fake_entry_cls.call_args.kwargs
    assert call_kwargs["agent_id"] == "some-uuid"
    assert call_kwargs["event_type"] == "coordination_failure.mcp_handler_timeout.tool_decorator"
    assert call_kwargs["details"]["service"] == "governance_mcp"
    assert call_kwargs["details"]["payload"]["tool_name"] == "process_agent_update"
    fake_logger._write_entry.assert_called_once()


def test_emit_skips_silently_on_event_type_prefix_mismatch():
    """event_type without 'coordination_failure.' prefix is rejected; no write."""
    fake_logger = MagicMock()
    with patch("src.audit_log.audit_logger", fake_logger):
        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type="not_a_coordination_event",
            payload={},
        )
    fake_logger._write_entry.assert_not_called()


def test_emit_skips_silently_on_non_string_event_type():
    """Defensive: type-check event_type, swallow."""
    fake_logger = MagicMock()
    with patch("src.audit_log.audit_logger", fake_logger):
        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type=12345,  # type: ignore[arg-type]
            payload={},
        )
    fake_logger._write_entry.assert_not_called()


def test_emit_falls_back_on_unknown_service():
    """Unknown service still writes — under fallback service. Missing the event
    would be worse than emitting under a generic service identity."""
    fake_logger = MagicMock()
    with patch("src.audit_log.audit_logger", fake_logger), \
         patch("src.audit_log.AuditEntry") as fake_entry_cls:
        emit_coordination_failure_sync(
            service="not_a_real_service",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
            payload={"tool_name": "x"},
        )
    fake_entry_cls.assert_called_once()
    assert fake_entry_cls.call_args.kwargs["details"]["service"] == "governance_mcp"


def test_emit_swallows_audit_logger_exceptions():
    """RFC §7.13.4 + council BLOCK-2 contract: emit MUST NOT raise. Caller is
    inside an `except` clause; a raising emit would replace the original
    exception with the emit-failure traceback. This test forces the underlying
    logger to raise and verifies no exception escapes."""
    fake_logger = MagicMock()
    fake_logger._write_entry.side_effect = RuntimeError("disk full")
    with patch("src.audit_log.audit_logger", fake_logger), \
         patch("src.audit_log.AuditEntry"):
        emit_coordination_failure_sync(  # MUST NOT raise
            service="governance_mcp",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
            payload={},
        )


def test_emit_swallows_import_errors():
    """If src.audit_log is unimportable for any reason (test environment,
    partial install), emit MUST NOT raise. Defensive against the same caller-
    in-except-clause contract."""
    with patch.dict(sys.modules, {"src.audit_log": None}):  # ImportError on attribute access
        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
            payload={},
        )


def test_emit_handles_empty_payload():
    """payload=None coalesces to empty dict in details.payload."""
    fake_logger = MagicMock()
    with patch("src.audit_log.audit_logger", fake_logger), \
         patch("src.audit_log.AuditEntry") as fake_entry_cls:
        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
        )
    assert fake_entry_cls.call_args.kwargs["details"]["payload"] == {}


def test_emit_handles_none_agent_id():
    """agent_id=None passes through to AuditEntry as None (column is nullable)."""
    fake_logger = MagicMock()
    with patch("src.audit_log.audit_logger", fake_logger), \
         patch("src.audit_log.AuditEntry") as fake_entry_cls:
        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
            payload={"tool_name": "x"},
            agent_id=None,
        )
    assert fake_entry_cls.call_args.kwargs["agent_id"] is None


def test_emit_passes_session_id_to_audit_entry():
    """session_id flows through to AuditEntry so it lands on audit.events.session_id.
    Without this, every coord-failure event has a NULL session column and cross-event
    correlation in the same caller session is impossible."""
    fake_logger = MagicMock()
    with patch("src.audit_log.audit_logger", fake_logger), \
         patch("src.audit_log.AuditEntry") as fake_entry_cls:
        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
            payload={"tool_name": "x"},
            agent_id="some-uuid",
            session_id="session-key-abc",
        )
    assert fake_entry_cls.call_args.kwargs["session_id"] == "session-key-abc"


def test_emit_session_id_defaults_to_none():
    """When caller omits session_id (e.g., out-of-context emit path), AuditEntry
    receives session_id=None — column is nullable."""
    fake_logger = MagicMock()
    with patch("src.audit_log.audit_logger", fake_logger), \
         patch("src.audit_log.AuditEntry") as fake_entry_cls:
        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
            payload={"tool_name": "x"},
        )
    assert fake_entry_cls.call_args.kwargs["session_id"] is None

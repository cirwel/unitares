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


# ============================================================================
# Wave 2 §"audit.coordination_events routing fix" — dedicated-table dual-write
# ============================================================================
#
# Pre-Wave-2: events landed only in audit.events (a generic table) under a
# `coordination_failure.*` event_type namespace. The dedicated
# audit.coordination_events table existed (PR #342, migration 035) but was
# empty because production deliberately didn't write to it — direct
# asyncpg-await from the @mcp_tool decorator's except clause was BLOCKED by
# council on anyio task-group deadlock grounds. Wave 2's routing fix is
# fire-and-forget dual-write: the sync audit.events path stays intact, and
# the dedicated table is populated in parallel via loop.create_task. These
# tests pin (1) the dual-write fires when an event loop is reachable,
# (2) it's silent when no loop is reachable (CLI / executor thread without
# captured loop), and (3) failure on the dedicated-table side never raises.


@pytest.mark.asyncio
async def test_emit_schedules_dual_write_when_loop_running():
    """Happy path: in async context, emit_coordination_failure_sync schedules
    a coroutine that calls coordination_events.emit_event with the same
    service/event_type/payload/agent_id. The audit.events sync write happens
    first (failure-safe truth), then the dedicated-table coroutine is
    scheduled on the running loop."""
    import asyncio

    import asyncpg

    fake_logger = MagicMock()
    seen_calls = []

    async def fake_emit_event(pool, **kwargs):
        seen_calls.append(kwargs)
        return "fake-uuid"

    # spec=asyncpg.Pool so the isinstance gate in
    # _emit_to_coordination_events_async accepts the test pool. Without
    # spec=, the gate would correctly reject a bare MagicMock.
    fake_db = MagicMock()
    fake_db._pool = MagicMock(spec=asyncpg.Pool)
    fake_db.init = MagicMock()  # not awaited because _pool is set

    with patch("src.audit_log.audit_logger", fake_logger), \
         patch("src.audit_log.AuditEntry"), \
         patch("src.coordination_events.emit_event", side_effect=fake_emit_event), \
         patch("src.db.get_db", return_value=fake_db):
        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
            payload={"tool_name": "process_agent_update"},
            agent_id="some-uuid",
        )
        # Dedicated-table write is scheduled, not awaited. Yield to let the
        # scheduled task run before the patches unwind.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # audit.events path still primary.
    fake_logger._write_entry.assert_called_once()
    # Dedicated-table path also fired with the same envelope.
    assert len(seen_calls) == 1, (
        f"Wave 2 dual-write must fire once on the running loop; got {len(seen_calls)} calls"
    )
    call = seen_calls[0]
    assert call["service"] == "governance_mcp"
    assert call["event_type"] == "coordination_failure.mcp_handler_timeout.tool_decorator"
    assert call["payload"] == {"tool_name": "process_agent_update"}
    assert call["agent_id"] == "some-uuid"


def test_emit_dual_write_silent_when_no_loop_reachable():
    """No running loop AND no captured main loop → dedicated-table write is
    dropped silently. emit_coordination_failure_sync must still complete
    successfully (audit.events write is the durable path)."""
    fake_logger = MagicMock()

    # Force the captured-loop fallback path to also be unavailable.
    class _StubAuditLogger:
        _event_loop = None

    with patch("src.audit_log.audit_logger", fake_logger), \
         patch("src.audit_log.AuditEntry"), \
         patch("src.audit_log.AuditLogger", _StubAuditLogger), \
         patch("src.coordination_events.emit_event") as fake_emit:
        # MUST NOT raise.
        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
            payload={},
        )

    # Sync truth path landed.
    fake_logger._write_entry.assert_called_once()
    # Dedicated-table path was never invoked.
    fake_emit.assert_not_called()


@pytest.mark.asyncio
async def test_emit_dual_write_swallows_pool_acquisition_failure():
    """If get_db()/pool acquisition fails (DB down at the moment of emit),
    the dedicated-table coroutine logs WARNING and returns. The audit.events
    row is already durable; dropping the dedicated-table write is acceptable
    by the failure-safe contract."""
    import asyncio

    fake_logger = MagicMock()
    fake_db = MagicMock()
    fake_db._pool = None

    async def fake_init():
        # Simulate db.init() also failing — pool stays None.
        raise RuntimeError("db down")

    fake_db.init = fake_init

    with patch("src.audit_log.audit_logger", fake_logger), \
         patch("src.audit_log.AuditEntry"), \
         patch("src.coordination_events.emit_event") as fake_emit, \
         patch("src.db.get_db", return_value=fake_db):
        emit_coordination_failure_sync(  # MUST NOT raise
            service="governance_mcp",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
            payload={},
        )
        # Let the scheduled task run + hit the swallowed exception.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # audit.events still landed.
    fake_logger._write_entry.assert_called_once()
    # emit_event itself never reached (pool unavailable).
    fake_emit.assert_not_called()


@pytest.mark.asyncio
async def test_emit_dual_write_swallows_emit_event_exception():
    """If coordination_events.emit_event itself raises (e.g., schema drift,
    transient PG error), the failure is logged at WARNING and swallowed.
    Pins the contract that a dedicated-table failure never propagates."""
    import asyncio

    import asyncpg

    fake_logger = MagicMock()
    fake_db = MagicMock()
    fake_db._pool = MagicMock(spec=asyncpg.Pool)

    async def raising_emit(pool, **kwargs):
        raise RuntimeError("connection lost mid-write")

    with patch("src.audit_log.audit_logger", fake_logger), \
         patch("src.audit_log.AuditEntry"), \
         patch("src.coordination_events.emit_event", side_effect=raising_emit), \
         patch("src.db.get_db", return_value=fake_db):
        emit_coordination_failure_sync(  # MUST NOT raise
            service="governance_mcp",
            event_type="coordination_failure.mcp_handler_timeout.tool_decorator",
            payload={},
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # audit.events still landed.
    fake_logger._write_entry.assert_called_once()

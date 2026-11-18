"""A write refused by a pause leaves an audit row.

Both pause gates used to return ``AGENT_PAUSED`` and write nothing, so the
audit trail went blank for exactly the agent under a pause and no reader could
count how often a pause gated a write. Each gate now calls
``record_paused_refusal`` before refusing. The refusal itself is unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.agent_metadata_model import AgentMetadata
from src.mcp_handlers.support.agent_auth import check_agent_can_operate
from src.mcp_handlers.support.pause_ttl import record_paused_refusal
from src.mcp_handlers.updates.context import UpdateContext
from src.mcp_handlers.updates.phases import resolve_identity_and_guards
from tests.helpers import parse_result
from tests.test_eisv_pause_end_to_end import AGENT_UUID, _patch_checkin_identity


def _meta(status: str) -> AgentMetadata:
    now = datetime.now(timezone.utc).isoformat()
    meta = AgentMetadata(
        agent_id=AGENT_UUID,
        status=status,
        created_at=now,
        last_update=now,
        label="paused-audit-agent",
    )
    if status == "paused":
        meta.paused_at = now  # fresh, so the TTL does not auto-expire it
    return meta


async def _both_gates(monkeypatch, meta: AgentMetadata):
    _patch_checkin_identity(monkeypatch)
    server = SimpleNamespace(agent_metadata={AGENT_UUID: meta})
    with patch("src.mcp_handlers.shared.get_mcp_server", return_value=server):
        kg_refusal = check_agent_can_operate(AGENT_UUID)
    ctx = UpdateContext(arguments={"agent_id": AGENT_UUID})
    ctx.mcp_server = server
    checkin_refusal = await resolve_identity_and_guards(ctx)
    return kg_refusal, checkin_refusal


@pytest.mark.asyncio
async def test_each_paused_gate_writes_one_audit_row(monkeypatch):
    meta = _meta("paused")
    log = MagicMock()
    with patch("src.audit_log.audit_logger.log_paused_write_refused", log):
        kg_refusal, checkin_refusal = await _both_gates(monkeypatch, meta)

    for refusal in (kg_refusal, checkin_refusal):
        assert refusal is not None
        assert parse_result(refusal)["error_code"] == "AGENT_PAUSED"

    surfaces = sorted(call.kwargs["surface"] for call in log.call_args_list)
    assert surfaces == ["check_in", "shared_memory_write"]
    for call in log.call_args_list:
        assert call.kwargs["agent_id"] == AGENT_UUID
        assert call.kwargs["paused_at"] == meta.paused_at


@pytest.mark.asyncio
async def test_active_agent_writes_no_refusal_row(monkeypatch):
    log = MagicMock()
    with patch("src.audit_log.audit_logger.log_paused_write_refused", log):
        kg_refusal, checkin_refusal = await _both_gates(monkeypatch, _meta("active"))

    assert kg_refusal is None and checkin_refusal is None
    log.assert_not_called()


def test_audit_failure_never_changes_the_refusal():
    boom = MagicMock(side_effect=RuntimeError("audit down"))
    with patch("src.audit_log.audit_logger.log_paused_write_refused", boom):
        record_paused_refusal(AGENT_UUID, _meta("paused"), "check_in")  # must not raise
    boom.assert_called_once()


def test_audit_row_shape():
    from src.audit_log import audit_logger

    captured = []
    with patch.object(audit_logger, "_write_entry", side_effect=captured.append):
        audit_logger.log_paused_write_refused(
            agent_id=AGENT_UUID, surface="check_in", paused_at="2026-11-18T00:00:00+00:00"
        )
    (entry,) = captured
    assert entry.event_type == "paused_write_refused"
    assert entry.details == {
        "surface": "check_in",
        "paused_at": "2026-11-18T00:00:00+00:00",
    }

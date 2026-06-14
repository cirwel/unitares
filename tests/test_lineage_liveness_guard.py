"""Declaration-time liveness guard in `_r2_pre_check_and_declare`.

A child declaring `parent_agent_id` attests ancestry, not that the parent
exited. If the named parent is a CURRENTLY-LIVE process, the declarant is a
concurrent sibling, not a successor — minting the edge produced the 2026-06-14
false-archival chain. The guard rejects such declarations (`rejected_coincidental`),
mirroring the cross-role reject path, and is symmetric with PR #720's
archival-time liveness guard.

Exemptions: `subagent` (dispatcher alive by design) and `compaction` (same live
session continuing past a context boundary) legitimately have a live parent.

Tests use a cross-role rejection as a short-circuit to assert the post-liveness
path was reached without mocking the full declare path.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.identity.handlers import _r2_pre_check_and_declare


def _meta():
    # tags non-empty → successor_class resolves without a DB read
    return SimpleNamespace(
        tags=["ephemeral"], parent_agent_id="parent-uuid", spawn_reason=None
    )


@pytest.mark.asyncio
async def test_new_session_live_parent_rejected_coincidental():
    """new_session declaring a LIVE parent → rejected_coincidental (the bug)."""
    backend = AsyncMock()
    backend.get_identity = AsyncMock(return_value={"id": "parent-uuid"})
    backend.clear_lineage_declaration = AsyncMock()
    with patch("src.db.get_db", return_value=backend), \
         patch("src.mcp_handlers.identity.process_binding.get_live_bindings",
               new=AsyncMock(return_value=[{"pid": 123}])) as mock_live, \
         patch("src.identity.lineage_lifecycle._emit_audit", new=AsyncMock()) as mock_audit, \
         patch("src.identity.lineage_lifecycle.pre_check_cross_role", new=AsyncMock(return_value=None)) as mock_cross:
        state, _ = await _r2_pre_check_and_declare(
            "child-uuid", "parent-uuid", None, _meta(), "new_session"
        )

    assert state == "rejected_coincidental"
    backend.clear_lineage_declaration.assert_awaited_once_with("child-uuid")
    # audited as coincidental, and we never reached the cross-role check
    assert mock_audit.await_args.args[0] == "lineage_coincidental_rejected"
    mock_cross.assert_not_awaited()
    mock_live.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("exempt_reason", ["subagent", "compaction"])
async def test_exempt_spawn_reasons_skip_liveness(exempt_reason):
    """subagent/compaction never run the liveness check — a live parent is
    legitimate for them (dispatcher alive / same live session continuing)."""
    backend = AsyncMock()
    backend.get_identity = AsyncMock(return_value={"id": "parent-uuid"})
    backend.clear_lineage_declaration = AsyncMock()
    with patch("src.db.get_db", return_value=backend), \
         patch("src.mcp_handlers.identity.process_binding.get_live_bindings",
               new=AsyncMock(return_value=[{"pid": 123}])) as mock_live, \
         patch("src.identity.lineage_lifecycle._emit_audit", new=AsyncMock()), \
         patch("src.identity.lineage_lifecycle.pre_check_cross_role",
               new=AsyncMock(return_value={"parent_class": "persistent",
                                           "successor_class": "ephemeral",
                                           "reason": "role_envelope_mismatch"})):
        # cross-role rejection short-circuits the declare path
        state, _ = await _r2_pre_check_and_declare(
            "child-uuid", "parent-uuid", None, _meta(), exempt_reason
        )

    # liveness never consulted; proceeded straight to the cross-role check
    mock_live.assert_not_awaited()
    assert state == "rejected_cross_role"


@pytest.mark.asyncio
async def test_new_session_dead_parent_not_coincidental():
    """new_session declaring a DEAD parent is NOT liveness-rejected — it stays
    on the normal path (provisional → R1), preserving genuine serial handoffs."""
    backend = AsyncMock()
    backend.get_identity = AsyncMock(return_value={"id": "parent-uuid"})
    backend.clear_lineage_declaration = AsyncMock()
    with patch("src.db.get_db", return_value=backend), \
         patch("src.mcp_handlers.identity.process_binding.get_live_bindings",
               new=AsyncMock(return_value=[])) as mock_live, \
         patch("src.identity.lineage_lifecycle._emit_audit", new=AsyncMock()), \
         patch("src.identity.lineage_lifecycle.pre_check_cross_role",
               new=AsyncMock(return_value={"parent_class": "persistent",
                                           "successor_class": "ephemeral",
                                           "reason": "role_envelope_mismatch"})):
        state, _ = await _r2_pre_check_and_declare(
            "child-uuid", "parent-uuid", None, _meta(), "new_session"
        )

    # liveness WAS consulted (returned empty), edge not coincidental-rejected;
    # falls through to the normal cross-role path (here a cross-role reject).
    mock_live.assert_awaited_once()
    assert state == "rejected_cross_role"
    backend.clear_lineage_declaration.assert_awaited_once()  # by cross-role, not coincidental

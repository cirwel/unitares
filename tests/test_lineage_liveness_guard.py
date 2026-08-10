"""Declaration-time liveness guard in `_r2_pre_check_and_declare`.

A child declaring `parent_agent_id` attests ancestry, not that the parent
exited. If the named parent is a CURRENTLY-LIVE process, the declarant is a
concurrent sibling, not a successor — minting the edge produced the 2026-06-14
false-archival chain. The guard rejects such declarations (`rejected_coincidental`),
mirroring the cross-role reject path, and is symmetric with PR #720's
archival-time liveness guard.

Liveness = process binding OR agent:/ presence lease. Bindings only exist for
callers that sent `process_fingerprint` at onboard — ephemeral agents never
do, so a binding-only check is structurally dead for them (verified live
2026-08-01: `core.agent_process_bindings` empty server-wide, guard never
fired). The lease is the liveness signal those agents DO produce.

Exemptions come from the canonical parent-relationship registry. Dispatched
children (including `dialectic_reviewer`) and context continuations legitimately
have a live parent; succession and unknown reasons do not.

Tests use a cross-role rejection as a short-circuit to assert the post-liveness
path was reached without mocking the full declare path.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_handlers.identity.handlers import _r2_pre_check_and_declare
from src.identity.lineage_semantics import NON_SUCCESSION_SPAWN_REASONS


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
         patch("src.mcp_handlers.identity.process_binding.has_live_agent_lease",
               new=AsyncMock(return_value=False)) as mock_lease, \
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
    # a live binding already proves liveness — the lease is not consulted
    mock_lease.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_session_lease_live_parent_rejected_coincidental():
    """Bindings empty (ephemeral parent — never sent process_fingerprint)
    but the parent holds a live agent:/ presence lease → still rejected.
    Binding-only liveness is structurally blind to ephemeral agents; the
    lease is the signal they DO produce (the #721 dormant-guard repair)."""
    backend = AsyncMock()
    backend.get_identity = AsyncMock(return_value={"id": "parent-uuid"})
    backend.clear_lineage_declaration = AsyncMock()
    with patch("src.db.get_db", return_value=backend), \
         patch("src.mcp_handlers.identity.process_binding.get_live_bindings",
               new=AsyncMock(return_value=[])) as mock_live, \
         patch("src.mcp_handlers.identity.process_binding.has_live_agent_lease",
               new=AsyncMock(return_value=True)) as mock_lease, \
         patch("src.identity.lineage_lifecycle._emit_audit", new=AsyncMock()) as mock_audit, \
         patch("src.identity.lineage_lifecycle.pre_check_cross_role", new=AsyncMock(return_value=None)) as mock_cross:
        state, _ = await _r2_pre_check_and_declare(
            "child-uuid", "parent-uuid", None, _meta(), "new_session"
        )

    assert state == "rejected_coincidental"
    backend.clear_lineage_declaration.assert_awaited_once_with("child-uuid")
    assert mock_audit.await_args.args[0] == "lineage_coincidental_rejected"
    assert mock_audit.await_args.kwargs["details"]["live_lease"] is True
    assert mock_audit.await_args.kwargs["details"]["live_binding_count"] == 0
    mock_live.assert_awaited_once()
    mock_lease.assert_awaited_once_with("parent-uuid")
    mock_cross.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("exempt_reason", NON_SUCCESSION_SPAWN_REASONS)
async def test_exempt_spawn_reasons_skip_liveness(exempt_reason):
    """Non-succession reasons never liveness-reject their expected live parent."""
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
async def test_dialectic_reviewer_live_parent_preserves_lineage():
    """A reviewer is dispatched by the parent whose verdict it is producing.

    The parent stays live while awaiting that verdict, so the reviewer must
    reach the declaration path without consulting the concurrent-sibling guard.
    Regression for #1485's five erased reviewer edges.
    """
    backend = AsyncMock()
    backend.clear_lineage_declaration = AsyncMock()
    backend.read_lineage_state = AsyncMock(return_value=None)
    backend.declare_lineage = AsyncMock(return_value=True)
    with patch("src.db.get_db", return_value=backend), \
         patch("src.mcp_handlers.identity.process_binding.get_live_bindings",
               new=AsyncMock(return_value=[{"pid": 123}])) as mock_live, \
         patch("src.mcp_handlers.identity.process_binding.has_live_agent_lease",
               new=AsyncMock(return_value=True)) as mock_lease, \
         patch("src.identity.lineage_lifecycle._emit_audit", new=AsyncMock()) as mock_audit, \
         patch("src.identity.lineage_lifecycle.pre_check_cross_role",
               new=AsyncMock(return_value=None)):
        state, rejection = await _r2_pre_check_and_declare(
            "reviewer-uuid",
            "parent-uuid",
            "DialecticReviewer",
            _meta(),
            "dialectic_reviewer",
        )

    assert (state, rejection) == ("provisional", None)
    mock_live.assert_not_awaited()
    mock_lease.assert_not_awaited()
    backend.clear_lineage_declaration.assert_not_awaited()
    backend.declare_lineage.assert_awaited_once_with("reviewer-uuid")
    assert mock_audit.await_args.args[0] == "lineage_declared"


@pytest.mark.asyncio
async def test_unknown_spawn_reason_live_parent_rejected_fail_closed():
    """A typo or novel unregistered reason must not bypass the liveness guard."""
    backend = AsyncMock()
    backend.get_identity = AsyncMock(return_value={"id": "parent-uuid"})
    backend.clear_lineage_declaration = AsyncMock()
    with patch("src.db.get_db", return_value=backend), \
         patch("src.mcp_handlers.identity.process_binding.get_live_bindings",
               new=AsyncMock(return_value=[{"pid": 123}])), \
         patch("src.identity.lineage_lifecycle._emit_audit", new=AsyncMock()) as mock_audit, \
         patch("src.identity.lineage_lifecycle.pre_check_cross_role",
               new=AsyncMock(return_value=None)) as mock_cross:
        state, _ = await _r2_pre_check_and_declare(
            "child-uuid",
            "parent-uuid",
            None,
            _meta(),
            "dialectic_revewer",
        )

    assert state == "rejected_coincidental"
    assert mock_audit.await_args.kwargs["details"]["parent_relationship"] == "unknown"
    backend.clear_lineage_declaration.assert_awaited_once_with("child-uuid")
    mock_cross.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_session_dead_parent_not_coincidental():
    """new_session declaring a DEAD parent is NOT liveness-rejected — it stays
    on the normal path (provisional → R1), preserving genuine serial handoffs.
    Dead = no process binding AND no live agent:/ presence lease."""
    backend = AsyncMock()
    backend.get_identity = AsyncMock(return_value={"id": "parent-uuid"})
    backend.clear_lineage_declaration = AsyncMock()
    with patch("src.db.get_db", return_value=backend), \
         patch("src.mcp_handlers.identity.process_binding.get_live_bindings",
               new=AsyncMock(return_value=[])) as mock_live, \
         patch("src.mcp_handlers.identity.process_binding.has_live_agent_lease",
               new=AsyncMock(return_value=False)) as mock_lease, \
         patch("src.identity.lineage_lifecycle._emit_audit", new=AsyncMock()), \
         patch("src.identity.lineage_lifecycle.pre_check_cross_role",
               new=AsyncMock(return_value={"parent_class": "persistent",
                                           "successor_class": "ephemeral",
                                           "reason": "role_envelope_mismatch"})):
        state, _ = await _r2_pre_check_and_declare(
            "child-uuid", "parent-uuid", None, _meta(), "new_session"
        )

    # BOTH liveness signals WERE consulted (both negative), edge not
    # coincidental-rejected; falls through to the normal cross-role path
    # (here a cross-role reject).
    mock_live.assert_awaited_once()
    mock_lease.assert_awaited_once()
    assert state == "rejected_cross_role"
    backend.clear_lineage_declaration.assert_awaited_once()  # by cross-role, not coincidental

"""Wiring lock: the check-in path reconciles resident tags by meta.label.

`reconcile_resident_tags` (the server-side resume-equivalent of the SDK's
`_reconcile_resident_tags`, #754/#774) is the only thing that keeps a
BEAM-migrated resident — which NEVER calls onboard, only attaches
`agent_id=<uuid>` to `process_agent_update` — correctly tagged
(`persistent`/`autonomous`). The classifier functions have thorough unit
coverage in `test_onboard_classifier.py`, but nothing guards the *wiring*:
delete the reconcile block from `resolve_identity_and_guards` and every unit
test still passes while the BEAM Sentinel silently loses `autonomous` (→ loop
detection pattern-4 starts emitting pause verdicts that starve its state
writes; the 2026-04-20 Steward incident).

This locks two properties of that wiring, in the exact BEAM shape:
  1. the check-in path INVOKES reconcile for an established (non-new) agent, and
  2. it classifies by `meta.label` ("Sentinel"), NOT the `agent_id` the BEAM
     resident passes (which is the bare UUID) — the crux of the #774 fix.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

import src.mcp_handlers.context as ctxmod
import src.mcp_handlers.identity_bootstrap as ib
import src.db as dbmod
import src.grounding.onboard_classifier as classifier
import src.mcp_handlers.identity.handlers as id_handlers
from src.mcp_handlers.updates.phases import resolve_identity_and_guards
from src.mcp_handlers.updates.context import UpdateContext


RESIDENT_UUID = "11111111-2222-3333-4444-555555555555"


def _patch_ctx(monkeypatch, *, agent_uuid):
    # Resolve cleanly with a proof_origin that bypasses the strict-write gate
    # (only "server_inferred" triggers it), so the test isolates the reconcile.
    monkeypatch.setattr(ctxmod, "get_context_agent_id", lambda: agent_uuid)
    monkeypatch.setattr(ctxmod, "get_context_session_key", lambda: "k")
    monkeypatch.setattr(ctxmod, "get_session_resolution_source",
                        lambda: "explicit_client_session_id")
    monkeypatch.setattr(ctxmod, "get_session_proof_origin", lambda: "caller_asserted")
    monkeypatch.setattr(ctxmod, "get_trajectory_confidence", lambda: None)
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: False)
    # Lazy-persist + label-store touch the DB inside the function; stub them.
    monkeypatch.setattr(id_handlers, "ensure_agent_persisted",
                        AsyncMock(return_value=False))

    class _DB:
        async def update_agent_fields(self, *a, **k):
            return None
    monkeypatch.setattr(dbmod, "get_db", lambda: _DB())


def _beam_resident_ctx(agent_uuid, *, label, tags):
    """A check-in shaped like the BEAM Sentinel: agent_id is the bare UUID,
    the canonical resident label lives only on the in-memory metadata."""
    ctx = UpdateContext(arguments={"agent_id": agent_uuid})
    ctx.mcp_server = MagicMock()
    meta = MagicMock()
    meta.status = "active"
    meta.label = label
    meta.tags = list(tags)
    # Established agent → present in agent_metadata → is_new_agent is False,
    # which is the precondition for the reconcile branch to run.
    ctx.mcp_server.agent_metadata = {agent_uuid: meta}
    return ctx, meta


@pytest.mark.asyncio
async def test_checkin_invokes_resident_reconcile_by_meta_label(monkeypatch):
    _patch_ctx(monkeypatch, agent_uuid=RESIDENT_UUID)
    spy = AsyncMock(return_value=None)
    monkeypatch.setattr(classifier, "reconcile_resident_tags", spy)

    ctx, meta = _beam_resident_ctx(
        RESIDENT_UUID, label="Sentinel", tags=["persistent"]  # missing 'autonomous'
    )
    result = await resolve_identity_and_guards(ctx)

    assert result is None, "healthy resident check-in should continue, not early-exit"
    spy.assert_awaited_once()
    args, kwargs = spy.call_args
    # Positional: (agent_uuid, resident_label). The label MUST be the canonical
    # 'Sentinel', not the UUID the BEAM resident passed as agent_id.
    assert args[0] == RESIDENT_UUID
    assert args[1] == "Sentinel", (
        "reconcile must classify by meta.label, not the agent_id UUID — "
        "this is the BEAM-bypass fix (#774)"
    )
    assert kwargs.get("meta") is meta


@pytest.mark.asyncio
async def test_checkin_reconcile_failure_is_nonfatal(monkeypatch):
    """The reconcile is wrapped non-fatal: a fault must not reject the check-in."""
    _patch_ctx(monkeypatch, agent_uuid=RESIDENT_UUID)
    monkeypatch.setattr(
        classifier, "reconcile_resident_tags",
        AsyncMock(side_effect=RuntimeError("simulated reconcile fault")),
    )
    ctx, _ = _beam_resident_ctx(RESIDENT_UUID, label="Sentinel", tags=["persistent"])
    # Must not raise; must continue (return None).
    result = await resolve_identity_and_guards(ctx)
    assert result is None

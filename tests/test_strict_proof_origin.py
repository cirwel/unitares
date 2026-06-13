"""Strict-identity must key on caller-PROVEN binding, not binding-presence.

Regression for the silent cross-agent write: under STRICT_IDENTITY_REQUIRED a
write whose identity was resolved by a server-inferred fingerprint/pin/injected
CSID (a concurrent same-host sibling's binding) must be refused — except for
substrate-earned residents, which legitimately re-resolve by fingerprint.
"""

import json

import pytest
from unittest.mock import MagicMock

import src.mcp_handlers.context as ctxmod
import src.mcp_handlers.identity_bootstrap as ib
import src.db as dbmod
from src.mcp_handlers.updates.phases import (
    _compute_identity_assurance,
    resolve_identity_and_guards,
)
from src.mcp_handlers.updates.context import UpdateContext


# ── Pure: assurance honesty ────────────────────────────────────────────

def test_server_inferred_is_never_strong():
    """An injected CSID wears the explicit_client_session_id label but is
    server-inferred — it must NOT be reported strong (the mislabel bug)."""
    a = _compute_identity_assurance(
        "explicit_client_session_id", None, proof_origin="server_inferred"
    )
    assert a["tier"] == "weak"
    assert a["caller_proven"] is False
    assert a["proof_origin"] == "server_inferred"


def test_caller_asserted_is_proven_and_strong():
    a = _compute_identity_assurance(
        "explicit_client_session_id", None, proof_origin="caller_asserted"
    )
    assert a["caller_proven"] is True
    assert a["tier"] == "strong"


def test_unknown_origin_is_not_proven_but_not_downgraded():
    """Backward-compat: no proof_origin → caller_proven False, tier unchanged
    (the gate fails OPEN on unknown, so legacy paths are untouched)."""
    a = _compute_identity_assurance("explicit_client_session_id", None)
    assert a["caller_proven"] is False
    assert a["tier"] == "strong"


# ── Gate: strict write precondition ────────────────────────────────────

def _patch_ctx(monkeypatch, *, agent_uuid, source, proof_origin):
    monkeypatch.setattr(ctxmod, "get_context_agent_id", lambda: agent_uuid)
    monkeypatch.setattr(ctxmod, "get_context_session_key", lambda: "k")
    monkeypatch.setattr(ctxmod, "get_session_resolution_source", lambda: source)
    monkeypatch.setattr(ctxmod, "get_session_proof_origin", lambda: proof_origin)
    monkeypatch.setattr(ctxmod, "get_trajectory_confidence", lambda: None)


def _patch_db(monkeypatch, *, earned):
    class _DB:
        async def is_substrate_earned(self, agent_id):
            return earned
    monkeypatch.setattr(dbmod, "get_db", lambda: _DB())


def _is_refusal(result):
    if not result:
        return False
    text = "".join(getattr(item, "text", "") or "" for item in result)
    try:
        return "identity_required" in text
    except Exception:
        return False


def _new_ctx():
    ctx = UpdateContext(arguments={})
    ctx.mcp_server = MagicMock()
    ctx.mcp_server.agent_metadata = {}
    return ctx


@pytest.mark.asyncio
async def test_strict_refuses_server_inferred_write(monkeypatch):
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: True)
    _patch_ctx(monkeypatch, agent_uuid="sibling-uuid",
               source="pinned_onboard_session", proof_origin="server_inferred")
    _patch_db(monkeypatch, earned=False)
    result = await resolve_identity_and_guards(_new_ctx())
    assert _is_refusal(result), "server-inferred write should be refused under strict"


@pytest.mark.asyncio
async def test_strict_refuses_injected_csid_write(monkeypatch):
    """The exact incident: injected CSID labeled explicit_client_session_id but
    server-inferred → must refuse, not write under the sibling."""
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: True)
    _patch_ctx(monkeypatch, agent_uuid="sibling-uuid",
               source="explicit_client_session_id", proof_origin="server_inferred")
    _patch_db(monkeypatch, earned=False)
    result = await resolve_identity_and_guards(_new_ctx())
    assert _is_refusal(result)


@pytest.mark.asyncio
async def test_strict_allows_caller_asserted_write(monkeypatch):
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: True)
    _patch_ctx(monkeypatch, agent_uuid="my-uuid",
               source="explicit_client_session_id", proof_origin="caller_asserted")
    _patch_db(monkeypatch, earned=False)
    result = await resolve_identity_and_guards(_new_ctx())
    assert not _is_refusal(result), "caller-proven write must not be refused"


@pytest.mark.asyncio
async def test_strict_exempts_substrate_earned_resident(monkeypatch):
    """A resident resolving by fingerprint (server_inferred) still writes —
    the exemption is keyed on the resolved agent being substrate-earned."""
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: True)
    _patch_ctx(monkeypatch, agent_uuid="lumen-uuid",
               source="ip_ua_fingerprint", proof_origin="server_inferred")
    _patch_db(monkeypatch, earned=True)
    result = await resolve_identity_and_guards(_new_ctx())
    assert not _is_refusal(result), "substrate-earned resident must be exempt"


@pytest.mark.asyncio
async def test_substrate_predicate_failure_is_fail_closed(monkeypatch):
    """If is_substrate_earned raises, a server-inferred write is refused."""
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: True)
    _patch_ctx(monkeypatch, agent_uuid="x",
               source="ip_ua_fingerprint", proof_origin="server_inferred")

    class _DB:
        async def is_substrate_earned(self, agent_id):
            raise RuntimeError("db down")
    monkeypatch.setattr(dbmod, "get_db", lambda: _DB())
    result = await resolve_identity_and_guards(_new_ctx())
    assert _is_refusal(result)


@pytest.mark.asyncio
async def test_non_strict_does_not_refuse_server_inferred(monkeypatch):
    """Flag off (today's default): server-inferred write still lands."""
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: False)
    _patch_ctx(monkeypatch, agent_uuid="sibling",
               source="pinned_onboard_session", proof_origin="server_inferred")
    _patch_db(monkeypatch, earned=False)
    result = await resolve_identity_and_guards(_new_ctx())
    assert not _is_refusal(result)


# ── Carrier: injected vs caller-sent CSID classification ───────────────

@pytest.mark.asyncio
async def test_caller_sent_csid_classified_caller_asserted():
    from src.mcp_handlers.identity.session import derive_session_key
    from src.mcp_handlers.context import (
        get_session_proof_origin, set_csid_transport_injected,
    )
    set_csid_transport_injected(False)  # caller sent it; no injection
    await derive_session_key(arguments={"client_session_id": "agent-abc123"})
    assert get_session_proof_origin() == "caller_asserted"


@pytest.mark.asyncio
async def test_injected_csid_classified_server_inferred():
    """The fix's crux: a transport-injected CSID wears the explicit label but
    must classify as server_inferred."""
    from src.mcp_handlers.identity.session import derive_session_key
    from src.mcp_handlers.context import (
        get_session_proof_origin, set_csid_transport_injected,
    )
    set_csid_transport_injected(True)  # transport synthesized it
    await derive_session_key(arguments={"client_session_id": "agent-abc123"})
    assert get_session_proof_origin() == "server_inferred"

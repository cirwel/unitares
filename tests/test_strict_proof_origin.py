"""Strict-identity must key on caller-PROVEN binding, not binding-presence.

Regression for the silent cross-agent write: under STRICT_IDENTITY_REQUIRED a
write whose identity was resolved by a server-inferred fingerprint/pin/injected
CSID (a concurrent same-host sibling's binding) must be refused — except for
substrate-earned residents, which legitimately re-resolve by fingerprint.
"""

import json
import re

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


# ── #732: write-path assurance carries the how_to_strengthen breadcrumb ──

def test_strong_write_assurance_omits_strengthen_breadcrumb():
    a = _compute_identity_assurance(
        "explicit_client_session_id", None, proof_origin="caller_asserted"
    )
    assert a["tier"] == "strong"
    assert "how_to_strengthen" not in a


def test_weak_write_assurance_carries_strengthen_breadcrumb():
    a = _compute_identity_assurance(
        "explicit_client_session_id", None, proof_origin="server_inferred"
    )
    assert a["tier"] == "weak"
    hint = a["how_to_strengthen"]
    assert "continuity_token" in hint
    assert "client_session_id" in hint
    assert hint.index("client_session_id") < hint.index("continuity_token")
    assert "ordinary tool calls" in hint


# ── Gate: strict write precondition ────────────────────────────────────

def _patch_ctx(monkeypatch, *, agent_uuid, source, proof_origin):
    monkeypatch.setattr(ctxmod, "get_context_agent_id", lambda: agent_uuid)
    monkeypatch.setattr(ctxmod, "get_context_session_key", lambda: "k")
    monkeypatch.setattr(ctxmod, "get_session_resolution_source", lambda: source)
    monkeypatch.setattr(ctxmod, "get_session_proof_origin", lambda: proof_origin)
    monkeypatch.setattr(ctxmod, "get_trajectory_confidence", lambda: None)


def _patch_db(monkeypatch, *, earned, dedicated=False):
    class _DB:
        async def is_substrate_earned(self, agent_id):
            return earned
    monkeypatch.setattr(dbmod, "get_db", lambda: _DB())
    import src.identity.substrate as submod

    async def _verify(agent_uuid, **kw):
        return {"conditions": {"dedicated_substrate": dedicated}}
    monkeypatch.setattr(submod, "verify_substrate_earned", _verify)


def _is_refusal(result):
    if not result:
        return False
    text = "".join(getattr(item, "text", "") or "" for item in result)
    try:
        return "identity_required" in text
    except Exception:
        return False


def _refusal_payload(result):
    if not result:
        return {}
    text = "".join(getattr(item, "text", "") or "" for item in result)
    return json.loads(text)


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
async def test_strict_refusal_payload_leads_with_client_session_id(monkeypatch):
    """A denied write points the caller back to its own client_session_id.

    The refusal is reached mostly in the dropped-id case, so its routes lead
    with the id start_session returned; continuity_token appears only inside
    an identity(..., resume=true) rebind, which then hands back the id to
    retry with. It used to lead with continuity_token on the write itself,
    contradicting the contract and the how_to_strengthen block it embeds. A
    mint is offered last, and only to a process that never called
    start_session (the pin can resolve such a process to a sibling).
    """
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: True)
    _patch_ctx(monkeypatch, agent_uuid="sibling-uuid",
               source="explicit_client_session_id", proof_origin="server_inferred")
    _patch_db(monkeypatch, earned=False)

    result = await resolve_identity_and_guards(_new_ctx())
    payload = _refusal_payload(result)

    assert payload["status"] == "identity_required"
    hint = payload["hint"]
    assert "continuity_token" in hint
    assert "client_session_id" in hint
    assert hint.index("client_session_id") < hint.index("continuity_token")
    next_step = payload["next_step"]
    assert next_step.index("client_session_id") < next_step.index("continuity_token")
    assert next_step.index("continuity_token") < next_step.index("never called start_session")
    assert next_step.index("never called start_session") < next_step.index("force_new=true")

    options = payload["safe_options"]
    assert options[0]["action"] == "retry_with_client_session_id"
    assert "client_session_id=" in options[0]["call"]
    # The only mint is the last option, for a process with no identity of its
    # own; it declares no lineage and retries with the id the mint returns.
    mint = options[-1]
    assert mint["action"] == "start_session_first"
    assert "never called start_session" in mint["when"]
    assert mint["call"].startswith("start_session(force_new=true)")
    assert "parent_agent_id" not in mint["call"]
    assert re.search(r"then sync_state\(.*client_session_id=<from start_session>", mint["call"])
    for option in options:
        call = option["call"]
        # A write never carries the token; only identity() does.
        for write in re.findall(r"(?:sync_state|process_agent_update)\([^)]*\)", call):
            assert "continuity_token" not in write, option
        for token_use in re.finditer("continuity_token", call):
            opener = call.rfind("identity(", 0, token_use.start())
            assert opener != -1 and ")" not in call[opener:token_use.start()], option
        if option is not mint:
            assert "force_new" not in call, option
    rebind = next(o for o in options if "identity(" in o["call"])
    assert "resume=true" in rebind["call"]
    # The rebind is not a dead end: the retry after it carries the id identity() returns.
    assert re.search(r"then (?:sync_state|process_agent_update)\(.*client_session_id=", rebind["call"])
    read_only = next(o for o in options if o["action"] == "stay_read_only")
    assert read_only["call"].startswith("check_working_state(client_session_id=")

    do_not = " ".join(payload["do_not"])
    assert "bare identity" in do_not
    assert "force_new=true" in do_not

    assert payload["identity_assurance"]["proof_origin"] == "server_inferred"
    assert payload["surface_context"]["session_resolution_source"] == "explicit_client_session_id"
    assert payload["surface_context"]["proof_origin"] == "server_inferred"


@pytest.mark.asyncio
async def test_strict_refusal_hint_agrees_with_its_embedded_assurance(monkeypatch):
    """The refusal quotes the breadcrumb its own identity_assurance carries."""
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: True)
    _patch_ctx(monkeypatch, agent_uuid="sibling-uuid",
               source="pinned_onboard_session", proof_origin="server_inferred")
    _patch_db(monkeypatch, earned=False)

    payload = _refusal_payload(await resolve_identity_and_guards(_new_ctx()))

    breadcrumb = payload["identity_assurance"]["how_to_strengthen"]
    # Anchor on the remedy, not on the prefix separator, which differs by tier.
    assert "pass the client_session_id" in breadcrumb
    remedy = breadcrumb[breadcrumb.index("pass the client_session_id"):]
    assert remedy in payload["hint"]


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
    the exemption is keyed on the resolved agent being substrate-earned
    (substrate_claims path)."""
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: True)
    _patch_ctx(monkeypatch, agent_uuid="sentinel-uuid",
               source="ip_ua_fingerprint", proof_origin="server_inferred")
    _patch_db(monkeypatch, earned=True)
    result = await resolve_identity_and_guards(_new_ctx())
    assert not _is_refusal(result), "substrate-earned resident must be exempt"


@pytest.mark.asyncio
async def test_strict_exempts_embodied_resident_not_in_substrate_claims(monkeypatch):
    """Lumen case: embodied tag (dedicated_substrate) but NOT in substrate_claims.
    is_substrate_earned returns False; verify_substrate_earned's dedicated_substrate
    must still exempt it so an embodied resident isn't refused on deploy."""
    monkeypatch.setattr(ib, "is_strict_identity_required", lambda: True)
    _patch_ctx(monkeypatch, agent_uuid="lumen-uuid",
               source="ip_ua_fingerprint", proof_origin="server_inferred")
    _patch_db(monkeypatch, earned=False, dedicated=True)
    result = await resolve_identity_and_guards(_new_ctx())
    assert not _is_refusal(result), "embodied resident must be exempt via dedicated_substrate"


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


@pytest.mark.asyncio
async def test_per_request_reset_prevents_injection_flag_leak():
    """Self-heal contract: a prior request that injected (flag True) must not
    bleed into a later caller-sent CSID. The transport resets the flag per
    request (http_call_tool / wrapper_generator); with that reset a genuine
    caller CSID is correctly caller_asserted, not falsely server_inferred."""
    from src.mcp_handlers.identity.session import derive_session_key
    from src.mcp_handlers.context import (
        get_session_proof_origin, set_csid_transport_injected,
    )
    set_csid_transport_injected(True)   # prior request injected (would leak)
    set_csid_transport_injected(False)  # transport's per-request reset
    await derive_session_key(arguments={"client_session_id": "agent-real-caller"})
    assert get_session_proof_origin() == "caller_asserted"

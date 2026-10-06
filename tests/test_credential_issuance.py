"""An agent's credentials go only to a caller that proved it owns the agent.

``identity()`` and ``onboard()`` return the agent's ``client_session_id`` and a
signed ``continuity_token``; either lets the holder act as the agent. Under
strict identity a request matched to an agent only by inference (an onboard
pin keyed on the User-Agent, a transport fingerprint, a name) must not receive
them: on a Docker bridge or behind a tunnel every client shares one address,
and a User-Agent is whatever the client sends.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.context import (
    set_credential_proof_uuid,
    set_session_proof_origin,
    set_session_resolution_source,
)
from src.mcp_handlers.identity.credential_issuance import credentials_issuable, note_session_proof

VICTIM = "5e728ecb-1234-4abc-8def-0123456789ab"
OTHER = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def _clean_context(monkeypatch):
    monkeypatch.setenv("UNITARES_CONTINUITY_TOKEN_SECRET", "credential-issuance-test-secret")
    for setter in (set_credential_proof_uuid, set_session_proof_origin, set_session_resolution_source):
        setter(None)
    yield
    for setter in (set_credential_proof_uuid, set_session_proof_origin, set_session_resolution_source):
        setter(None)


def _inferred(source: str = "pinned_onboard_session") -> None:
    set_session_resolution_source(source)
    set_session_proof_origin("server_inferred")


# --- the rule ----------------------------------------------------------------

@pytest.mark.parametrize("source", ["pinned_onboard_session", "ip_ua_fingerprint", "context_session_key"])
def test_an_inferred_match_gets_no_credentials(source):
    _inferred(source)
    assert credentials_issuable(VICTIM) == (False, f"inferred:{source}")


def test_a_mint_always_gets_its_credentials():
    _inferred()
    assert credentials_issuable(VICTIM, minted=True) == (True, "minted")


def _caller_session_resolved_to(agent_uuid: str) -> None:
    """What the resolver does when the caller's own session key resolves
    through a stored binding (resolution._resumed_identity_result)."""
    set_session_resolution_source("explicit_client_session_id")
    set_session_proof_origin("caller_asserted")
    note_session_proof(agent_uuid)


def test_a_session_the_caller_sent_gets_credentials_for_its_agent():
    _caller_session_resolved_to(VICTIM)
    assert credentials_issuable(VICTIM) == (True, "proven_uuid")


def test_a_caller_asserted_session_proves_only_the_agent_it_resolved_to():
    """The flag is per request; the proof is per agent. A caller's own session
    for OTHER must not vouch for VICTIM, reached by a UUID claim or recovery."""
    _caller_session_resolved_to(OTHER)
    assert credentials_issuable(VICTIM)[0] is False
    set_session_proof_origin("caller_asserted")  # origin alone, no resolution
    set_credential_proof_uuid(None)
    assert credentials_issuable(VICTIM)[0] is False


def test_an_inferred_session_resolution_records_no_proof():
    _inferred()
    note_session_proof(VICTIM)
    assert credentials_issuable(VICTIM)[0] is False


def test_a_proof_counts_only_for_the_agent_it_proved():
    _inferred()
    set_credential_proof_uuid(OTHER)
    assert credentials_issuable(VICTIM)[0] is False
    set_credential_proof_uuid(VICTIM)
    assert credentials_issuable(VICTIM) == (True, "proven_uuid")


def test_an_operator_gets_credentials():
    _inferred()
    with patch("src.mcp_handlers.identity.credential_issuance.is_operator_caller", return_value=True):
        assert credentials_issuable(VICTIM) == (True, "operator")


@pytest.mark.legacy_identity_defaults
def test_the_permissive_posture_is_unchanged():
    _inferred()
    assert credentials_issuable(VICTIM) == (True, "permissive_posture")


# --- identity() --------------------------------------------------------------

def _identity_payload():
    from src.mcp_handlers.identity.handlers import _build_identity_diag_payload_for_request

    return _build_identity_diag_payload_for_request(
        {}, None, agent_uuid=VICTIM, agent_id=VICTIM, label=None, status="resumed",
    )


def test_identity_withholds_credentials_from_an_inferred_match():
    _inferred()
    payload = _identity_payload()

    assert payload["client_session_id"] is None
    assert "continuity_token" not in payload
    assert payload["credentials_withheld"]["basis"] == "inferred:pinned_onboard_session"
    assert "start_session(force_new=true)" in payload["credentials_withheld"]["hint"]
    assert VICTIM[:12] not in json.dumps({k: v for k, v in payload.items() if k != "uuid"
                                          and k != "agent_id" and k != "bound_identity"
                                          and k != "identity_context" and k != "identity_assurance"})


def test_identity_returns_credentials_after_a_proven_resume():
    _inferred()
    set_credential_proof_uuid(VICTIM)  # PATH 0 with a matching token or UDS attestation
    payload = _identity_payload()

    assert payload["client_session_id"]
    assert payload["continuity_token"]
    assert "credentials_withheld" not in payload


# --- onboard() ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_onboard_refuses_a_resume_reached_by_inference():
    """A name or agent_id on onboard counts as a proof signal for the freshness
    gate, but resolves through the User-Agent-keyed onboard pin: it must not
    resume, bind or issue anything."""
    from src.mcp_handlers.identity import handlers

    existing = {"agent_uuid": VICTIM, "agent_id": VICTIM, "label": "victim", "created": False}
    cache = AsyncMock()
    bind = AsyncMock()

    async def resolve(*args, **kwargs):
        _inferred()
        return dict(existing)

    label = AsyncMock(return_value="attacker-chosen")
    rebadge = AsyncMock()
    with patch.object(handlers, "resolve_session_identity", side_effect=resolve), \
         patch.object(handlers, "derive_session_key", AsyncMock(return_value="pin:shared-ua")), \
         patch.object(handlers, "_cache_session", cache), \
         patch.object(handlers, "_perform_session_bind", bind), \
         patch.object(handlers, "set_agent_label_resolved", label), \
         patch.object(handlers, "_persist_rebadged_agent_id", rebadge), \
         patch.object(handlers, "_should_rebadge_agent_id", return_value=True):
        # A different name: the label write is the mutation a refusal must precede.
        result = await handlers.handle_onboard_v2({"name": "attacker-chosen", "resume": True})

    body = json.loads(result[0].text)
    assert body["status"] == "resume_proof_required", body
    assert body["rollout_flag"] == "STRICT_IDENTITY_REQUIRED"
    assert "continuity_token" not in body and "client_session_id" not in json.dumps(body).replace(
        "Pass the client_session_id", "")
    cache.assert_not_awaited()
    bind.assert_not_awaited()
    label.assert_not_awaited()
    rebadge.assert_not_awaited()


@pytest.mark.asyncio
async def test_onboard_resumes_when_the_caller_sent_its_session():
    from src.mcp_handlers.identity import handlers

    existing = {"agent_uuid": VICTIM, "agent_id": VICTIM, "label": "mine", "created": False}

    async def resolve(*args, **kwargs):
        _caller_session_resolved_to(VICTIM)
        return dict(existing)

    with patch.object(handlers, "resolve_session_identity", side_effect=resolve), \
         patch.object(handlers, "derive_session_key", AsyncMock(return_value="agent-mine")), \
         patch.object(handlers, "_cache_session", AsyncMock()), \
         patch.object(handlers, "_perform_session_bind", AsyncMock()):
        result = await handlers.handle_onboard_v2({"client_session_id": "agent-mine", "resume": True})

    body = json.loads(result[0].text)
    assert body.get("status") != "resume_proof_required", body


def test_log_mode_issues_but_records_what_it_would_withhold(monkeypatch, caplog):
    monkeypatch.setenv("UNITARES_CREDENTIAL_ISSUANCE", "log")
    _inferred()
    with caplog.at_level("WARNING"):
        allowed, basis = credentials_issuable(VICTIM)
    assert allowed is True and basis == "log_only:inferred:pinned_onboard_session"
    assert "mode=log" in caplog.text


@pytest.mark.parametrize("value", ["", "enforce", "LOGG", "off", "0"])
def test_any_other_mode_value_enforces(monkeypatch, value):
    monkeypatch.setenv("UNITARES_CREDENTIAL_ISSUANCE", value)
    _inferred()
    assert credentials_issuable(VICTIM)[0] is False


@pytest.mark.asyncio
async def test_onboard_does_not_unarchive_on_an_inferred_resume():
    from src.mcp_handlers.identity import handlers

    archived = {"agent_uuid": VICTIM, "agent_id": VICTIM, "label": "victim",
                "created": False, "archived": True}
    db = MagicMock()
    db.update_agent_fields = AsyncMock()
    db.update_identity_status = AsyncMock()

    async def resolve(*args, **kwargs):
        _inferred()
        return dict(archived)

    with patch.object(handlers, "resolve_session_identity", side_effect=resolve), \
         patch.object(handlers, "derive_session_key", AsyncMock(return_value="pin:shared-ua")), \
         patch.object(handlers, "get_db", return_value=db), \
         patch.object(handlers, "_cache_session", AsyncMock()):
        result = await handlers.handle_onboard_v2({"name": "victim", "resume": True})

    assert json.loads(result[0].text)["status"] == "resume_proof_required"
    db.update_agent_fields.assert_not_awaited()
    db.update_identity_status.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin, renamed", [("server_inferred", False), ("caller_asserted", True)])
async def test_identity_name_renames_only_with_proof(origin, renamed):
    from src.mcp_handlers.identity import handlers

    async def resolve(*args, **kwargs):
        if origin == "server_inferred":
            _inferred()
        else:
            _caller_session_resolved_to(VICTIM)
        return {"agent_uuid": VICTIM, "agent_id": VICTIM, "label": "victim", "created": False}

    label = AsyncMock(return_value=True)
    with patch.object(handlers, "resolve_session_identity", side_effect=resolve), \
         patch.object(handlers, "set_agent_label", label):
        result = await handlers.handle_identity_v2({"name": "attacker-chosen"}, "some-key")

    assert label.await_count == (1 if renamed else 0)
    assert result["display_name"] == ("attacker-chosen" if renamed else "victim")


@pytest.mark.asyncio
@pytest.mark.parametrize("origin, renamed", [("server_inferred", False), ("caller_asserted", True)])
async def test_identity_adapter_session_resume_renames_only_with_proof(origin, renamed):
    """The adapter's session-key resume (STEP 1) writes a requested name before
    returning; it must not for a caller matched to the agent by inference."""
    from src.mcp_handlers.identity import handlers

    existing = {"agent_uuid": VICTIM, "agent_id": VICTIM, "label": "victim", "created": False}

    async def resolve(*args, **kwargs):
        if origin == "server_inferred":
            _inferred()
        else:
            _caller_session_resolved_to(VICTIM)
        return dict(existing)

    label = AsyncMock(return_value=True)
    with patch.object(handlers, "resolve_session_identity", side_effect=resolve), \
         patch.object(handlers, "derive_session_key", AsyncMock(return_value="pin:shared-ua")), \
         patch.object(handlers, "set_agent_label", label), \
         patch.object(handlers, "_cache_session", AsyncMock()), \
         patch.object(handlers, "_perform_session_bind", AsyncMock()):
        await handlers.handle_identity_adapter({"name": "attacker-chosen", "resume": True})

    assert label.await_count == (1 if renamed else 0)


@pytest.mark.asyncio
async def test_a_callers_own_session_does_not_vouch_for_a_claimed_uuid(monkeypatch):
    """IDENTITY_STRICT=log lets a bare agent_uuid resume proceed (with a hijack
    log). The caller's own client_session_id made the request caller-asserted,
    but the agent returned comes from the UUID claim, so it must not unlock the
    victim's credentials."""
    from src.mcp_handlers.identity import handlers

    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "log")
    set_session_resolution_source("explicit_client_session_id")
    set_session_proof_origin("caller_asserted")  # from the caller's OWN session

    server = MagicMock()
    server.monitors = {VICTIM: MagicMock()}
    with patch("src.mcp_handlers.shared.get_mcp_server", return_value=server), \
         patch.object(handlers, "_emit_identity_hijack_event", AsyncMock()):
        result = await handlers.handle_identity_adapter(
            {"agent_uuid": VICTIM, "client_session_id": "agent-attacker-own", "resume": True}
        )

    body = json.loads(result[0].text)
    assert body.get("uuid") == VICTIM, body
    assert body.get("client_session_id") is None, body
    assert "continuity_token" not in body
    assert body["credentials_withheld"]["basis"].startswith("inferred:")


@pytest.mark.asyncio
async def test_an_unproven_uuid_resume_does_not_bind_the_callers_session(monkeypatch):
    """Outside strict mode a bare agent_uuid resume proceeds on the slow path.
    It must not map the caller's session key to the claimed agent: a later
    call presenting that key would then resolve, caller-asserted, to it."""
    from src.mcp_handlers.identity import handlers

    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "log")
    server = MagicMock()
    server.monitors = {}  # not cached: take the DB-backed slow path
    cache = AsyncMock()
    db = MagicMock()
    db.read_lineage_state = AsyncMock(return_value=None)
    with patch("src.mcp_handlers.shared.get_mcp_server", return_value=server), \
         patch.object(handlers, "_emit_identity_hijack_event", AsyncMock()), \
         patch.object(handlers, "_agent_exists_in_postgres", AsyncMock(return_value=True)), \
         patch.object(handlers, "_get_agent_status", AsyncMock(return_value="active")), \
         patch.object(handlers, "_get_agent_id_from_metadata", AsyncMock(return_value=VICTIM)), \
         patch.object(handlers, "_get_agent_label", AsyncMock(return_value="victim")), \
         patch.object(handlers, "get_db", return_value=db), \
         patch.object(handlers, "_cache_session", cache):
        result = await handlers.handle_identity_adapter(
            {"agent_uuid": VICTIM, "client_session_id": "agent-attacker-own", "resume": True}
        )

    body = json.loads(result[0].text)
    assert body.get("uuid") == VICTIM and body.get("client_session_id") is None, body
    cache.assert_not_awaited()

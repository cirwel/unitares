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
from src.mcp_handlers.identity.credential_issuance import credentials_issuable

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


def test_a_session_the_caller_sent_gets_credentials():
    set_session_resolution_source("explicit_client_session_id")
    set_session_proof_origin("caller_asserted")
    assert credentials_issuable(VICTIM) == (True, "caller_asserted_session")


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
        set_session_resolution_source("explicit_client_session_id")
        set_session_proof_origin("caller_asserted")
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
        set_session_resolution_source("pinned_onboard_session" if origin == "server_inferred"
                                      else "explicit_client_session_id")
        set_session_proof_origin(origin)
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
        set_session_resolution_source("pinned_onboard_session" if origin == "server_inferred"
                                      else "explicit_client_session_id")
        set_session_proof_origin(origin)
        return dict(existing)

    label = AsyncMock(return_value=True)
    with patch.object(handlers, "resolve_session_identity", side_effect=resolve), \
         patch.object(handlers, "derive_session_key", AsyncMock(return_value="pin:shared-ua")), \
         patch.object(handlers, "set_agent_label", label), \
         patch.object(handlers, "_cache_session", AsyncMock()), \
         patch.object(handlers, "_perform_session_bind", AsyncMock()):
        await handlers.handle_identity_adapter({"name": "attacker-chosen", "resume": True})

    assert label.await_count == (1 if renamed else 0)

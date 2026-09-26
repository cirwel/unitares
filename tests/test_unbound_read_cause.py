"""An unbound metrics read offers the recovery the strict refusal gives for
the same resolver result.

The unbound read (core.unbound_metrics_payload) used to branch only on
whether the caller sent an id. A caller that sent a valid client_session_id
could still read unbound because the server failed (a PostgreSQL session
lookup that raised: session_resolve_miss with reason pg_lookup_exception),
or because a hijack guard refused its resume. It was then told its id
"names no identity on this server" and offered a rebind or a mint. The strict
refusal for the same result already said the right thing: retry, or name the
guard.

The resolver's result now reaches both read handlers. The REST prebind
records it and the /mcp/ identity step records it the same way
(context.set_unbound_resolution); both reads classify it with
identity_bootstrap.unbound_cause and take the strict refusal's sentences for
that cause (unbound_call_refusal). Each case runs the read, and the write the
strict refusal answers, through the real transport path
(tests/helpers/read_transport.py) with the same resolver result.
"""

from __future__ import annotations

import pytest

from src.mcp_handlers.identity_bootstrap import (
    RESOLUTION_FAILED_DO_NOT,
    RESOLUTION_FAILED_NEXT_STEP,
    SESSION_ID_NAMES_NO_IDENTITY,
    TOKEN_FAILED_VERIFICATION,
    USE_THIS_PROCESS_LATEST_TOKEN,
)
from tests.helpers import read_transport as rt
from tests.no_live_redis import no_live_redis  # noqa: F401

pytestmark = pytest.mark.usefixtures("no_live_redis")

PG_FAILURE = {
    "resume_failed": True,
    "error": "session_resolve_miss",
    "session_key": rt.AGENT_SESSION,
    "reason": "pg_lookup_exception",
    "message": "PostgreSQL session lookup failed.",
}
HIJACK = {
    "resume_failed": True,
    "error": "resume_rejected_hijack_guard",
    "reason": "fingerprint_mismatch",
    "session_key": rt.AGENT_SESSION,
    "message": "Resume was rejected by the identity hijack guard.",
}
MISS = {
    "resume_failed": True,
    "error": "session_resolve_miss",
    "session_key": rt.AGENT_SESSION,
}
SUBSTRATE = {
    "resume_failed": True,
    "error": "substrate_anchored_uuid_requires_uds",
    "agent_uuid": rt.AGENT_UUID,
    "message": "This resident is substrate-anchored and must resume over UDS.",
}
INACTIVE_TOKEN = {
    "resume_failed": True,
    "error": "resume_failed",
    "message": "Continuity token references agent 1856bb5c... which is not active.",
}
PROOF = {"client_session_id": rt.AGENT_SESSION}


async def _mcp_read(monkeypatch, read, resolve_result, arguments=PROOF):
    seen = await rt.mcp_call(
        monkeypatch, "direct", read, rt.signals(),
        target_arguments=dict(arguments), resolve_result=resolve_result,
    )
    assert rt.is_unbound(seen["result"]), seen["result"]
    assert seen["metrics_served"] is False
    return seen["result"]


async def _mcp_strict_write(monkeypatch, resolve_result, arguments=PROOF):
    seen = await rt.mcp_call(
        monkeypatch, "direct", "sync_state", rt.signals(),
        target_arguments=dict(arguments), resolve_result=resolve_result, strict=True,
    )
    refusal = seen["result"]
    assert refusal["rollout_flag"] == "STRICT_IDENTITY_REQUIRED", refusal
    return refusal


async def _rest_read(monkeypatch, read, resolve_result, arguments=PROOF):
    payload, metrics, _resolve = await rt.rest_call(
        monkeypatch, read, dict(arguments), resolve_result=resolve_result,
    )
    assert rt.is_unbound(payload), payload
    assert metrics.await_count == 0
    return payload


async def _rest_strict_write(monkeypatch, resolve_result, arguments=PROOF):
    refusal, _metrics, _resolve = await rt.rest_call(
        monkeypatch, "process_agent_update", dict(arguments),
        resolve_result=resolve_result, strict=True,
    )
    assert refusal["rollout_flag"] == "STRICT_IDENTITY_REQUIRED", refusal
    return refusal


def _surface_keys(refusal: dict) -> dict:
    """The cause keys unbound_call_refusal adds to a refusal's surface."""
    surface = refusal["surface_context"]
    return {
        key: surface[key]
        for key in (
            "identity_resolution",
            "identity_resolution_failure",
            "resume_rejected_reason",
            "continuity_token_invalid",
        )
        if key in surface
    }


# ─── a server-side failure: retry, no onboarding ─────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_a_failed_session_lookup_is_retried_not_reported_as_an_unknown_id(
    monkeypatch, transport
):
    read = _mcp_read if transport == "mcp" else _rest_read
    write = _mcp_strict_write if transport == "mcp" else _rest_strict_write

    payload = await read(monkeypatch, "get_governance_metrics", PG_FAILURE)
    refusal = await write(monkeypatch, PG_FAILURE)

    action = payload["next_action"]
    assert SESSION_ID_NAMES_NO_IDENTITY not in action["note"]
    assert "start_session" not in action["tool"]
    assert action["example"] == "the same call, unchanged"
    # The strict refusal's own step and warning, word for word.
    assert refusal["next_step"] == RESOLUTION_FAILED_NEXT_STEP
    assert RESOLUTION_FAILED_NEXT_STEP in action["note"]
    assert refusal["do_not"] == [RESOLUTION_FAILED_DO_NOT]
    assert RESOLUTION_FAILED_DO_NOT in action["note"]
    assert payload["unbound_reason"] == _surface_keys(refusal) == {
        "identity_resolution": "failed",
        "identity_resolution_failure": "pg_lookup_exception",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_the_digest_read_carries_the_same_next_step(monkeypatch, transport):
    """check_working_state lifts the payload's next_action into its envelope."""
    read = _mcp_read if transport == "mcp" else _rest_read

    from src.mcp_handlers.middleware.envelope_step import _friendly_action_hint

    envelope = await read(monkeypatch, "check_working_state", PG_FAILURE)
    raw = await read(monkeypatch, "get_governance_metrics", PG_FAILURE)

    # The envelope renames canonical tools in hints; nothing else changes.
    assert envelope["next_action"] == _friendly_action_hint(raw["next_action"])
    assert "missing start_session" not in envelope["next_action"]["note"]


@pytest.mark.asyncio
async def test_a_resolver_that_raised_on_mcp_is_a_server_failure(monkeypatch):
    payload = await _mcp_read(
        monkeypatch, "get_governance_metrics", RuntimeError("resolver down"),
    )
    refusal = await _mcp_strict_write(monkeypatch, RuntimeError("resolver down"))

    assert RESOLUTION_FAILED_NEXT_STEP in payload["next_action"]["note"]
    assert payload["unbound_reason"] == _surface_keys(refusal) == {
        "identity_resolution": "failed",
        "identity_resolution_failure": "exception",
    }


# ─── a refused resume: the refusal's own reason ──────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_a_hijack_guard_rejection_names_the_guard(monkeypatch, transport):
    read = _mcp_read if transport == "mcp" else _rest_read
    write = _mcp_strict_write if transport == "mcp" else _rest_strict_write

    payload = await read(monkeypatch, "get_governance_metrics", HIJACK)
    refusal = await write(monkeypatch, HIJACK)

    action = payload["next_action"]
    assert SESSION_ID_NAMES_NO_IDENTITY not in action["note"]
    assert action["note"].startswith(refusal["hint"])
    assert "fingerprint_mismatch" in action["note"]
    assert action["tool"] == "start_session"
    assert "parent_agent_id" in action["example"]
    assert "continuity_token" in action["otherwise"]
    assert payload["unbound_reason"] == _surface_keys(refusal) == {
        "resume_rejected_reason": "fingerprint_mismatch",
    }


@pytest.mark.asyncio
async def test_a_substrate_resident_reading_over_rest_is_sent_to_its_socket(monkeypatch):
    """/mcp/ stops a hard refusal before any handler runs; on REST the read
    runs unbound, so it must name the channel as the strict refusal does."""
    payload = await _rest_read(monkeypatch, "get_governance_metrics", SUBSTRATE)
    refusal = await _rest_strict_write(monkeypatch, SUBSTRATE)

    action = payload["next_action"]
    assert action["note"] == refusal["hint"] + " " + refusal["next_step"]
    assert action["example"] == refusal["safe_options"][0]["call"]
    assert action["do_not"] == refusal["do_not"]
    assert payload["unbound_reason"] == _surface_keys(refusal)


@pytest.mark.asyncio
async def test_a_token_naming_an_inactive_agent_gets_the_resolvers_reason(monkeypatch):
    payload = await _rest_read(monkeypatch, "get_governance_metrics", INACTIVE_TOKEN)
    refusal = await _rest_strict_write(monkeypatch, INACTIVE_TOKEN)

    action = payload["next_action"]
    assert action["note"].startswith(refusal["hint"])
    assert action["tool"] == "start_session"
    assert payload["unbound_reason"] == _surface_keys(refusal) == {
        "resume_rejected_reason": "resume_failed",
    }


# ─── a session miss keeps its recovery ───────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_a_session_miss_still_says_the_id_names_no_identity(monkeypatch, transport):
    from src.mcp_handlers.core import unbound_metrics_payload

    read = _mcp_read if transport == "mcp" else _rest_read

    payload = await read(monkeypatch, "get_governance_metrics", MISS)

    assert payload["next_action"] == unbound_metrics_payload(
        caller_sent_session_id=True
    )["next_action"]
    assert payload["next_action"]["note"].startswith(SESSION_ID_NAMES_NO_IDENTITY)
    assert "unbound_reason" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_a_miss_with_a_failed_token_says_the_token_failed(monkeypatch, transport):
    read = _mcp_read if transport == "mcp" else _rest_read
    write = _mcp_strict_write if transport == "mcp" else _rest_strict_write
    arguments = {**PROOF, "continuity_token": "v1.not-a-token"}

    payload = await read(monkeypatch, "get_governance_metrics", MISS, arguments)
    refusal = await write(monkeypatch, MISS, arguments)

    note = payload["next_action"]["note"]
    assert note.startswith(TOKEN_FAILED_VERIFICATION)
    assert refusal["hint"].startswith(TOKEN_FAILED_VERIFICATION)
    # The rebind leads for a caller-sent id; it asks for a current token.
    assert note.endswith(USE_THIS_PROCESS_LATEST_TOKEN)
    assert payload["unbound_reason"] == _surface_keys(refusal) == {
        "continuity_token_invalid": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["mcp", "rest"])
async def test_a_read_without_proof_records_no_resolution(monkeypatch, transport):
    """Nothing resolved, so nothing is recorded: the no-id recovery, as
    before."""
    from src.mcp_handlers.core import unbound_metrics_payload

    read = _mcp_read if transport == "mcp" else _rest_read

    payload = await read(monkeypatch, "get_governance_metrics", PG_FAILURE, {})

    assert payload["next_action"] == unbound_metrics_payload()["next_action"]
    assert "unbound_reason" not in payload


def test_the_default_payload_is_unchanged():
    """No resolution and no id: the payload every earlier caller got."""
    from src.mcp_handlers.core import unbound_metrics_payload

    payload = unbound_metrics_payload()

    assert set(payload) == {"status", "verdict", "guidance", "next_action", "related_tools"}
    assert payload["guidance"] == "Establish identity before reading agent metrics."
    assert payload["next_action"]["tool"] == "check_working_state"

"""Party A's signature on the peer-review path is never made with the caller's key.

Companion to ``TestNoForgeableFallbackKey`` in
``test_dialectic_attestation_descriptor.py``, which pins the same rule for the
LLM-assisted finalize path (#2155).

The peer-review path finalizes inside ``handle_submit_synthesis``. When the
paused agent had no api_key on file, it used to fall back to ``api_key`` -- the
SYNTHESIS CALLER's argument, usually the reviewer's. That wrote a
``signature_a`` the paused agent never produced, keyed on the other party's
secret, and ``describe_attestation`` read it as an attestation by party A.

These drive the real handler to convergence (not ``finalize_resolution``
directly), because the defect was in how the handler chose the key.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.dialectic_protocol import (
    ATTESTATION_SINGLE_SIGNER,
    ATTESTATION_UNSIGNED,
    DialecticPhase,
    DialecticSession,
    Resolution,
    describe_attestation,
)

DIALECTIC = "src.mcp_handlers.dialectic.handlers"
CALLER_KEY = "reviewer-supplied-key"


@pytest.fixture(autouse=True)
def _synthesis_caller_bound_as_named_agent():
    """Caller binding is tested in test_dialectic_synthesis_bound_caller.py."""
    with patch("src.mcp_handlers.dialectic.auth.caller_is_bound_as", return_value=True):
        yield


def _meta(status, api_key):
    return SimpleNamespace(
        status=status,
        label="Test",
        api_key=api_key,
        last_update=datetime.now().isoformat(),
        paused_at=None,
        structured_id=None,
    )


async def _reviewer_converges(paused_key, reviewer_key):
    """The reviewer submits an agreeing synthesis, passing its own api_key.

    Returns the resolution dict handed to the terminal write, and the parsed
    handler response.
    """
    from src.mcp_handlers.dialectic.handlers import ACTIVE_SESSIONS, handle_submit_synthesis

    server = MagicMock()
    server.agent_metadata = {
        "agent-paused": _meta("paused", paused_key),
        "agent-reviewer": _meta("active", reviewer_key),
    }
    server.monitors = {}
    server.load_metadata = MagicMock()
    server.load_metadata_async = AsyncMock()
    server.project_root = str(project_root)

    session = DialecticSession(
        paused_agent_id="agent-paused",
        reviewer_agent_id="agent-reviewer",
        dispute_type="verification",
    )
    session.phase = DialecticPhase.SYNTHESIS
    session.synthesis_round = 1
    ACTIVE_SESSIONS[session.session_id] = session

    async def _quiet_save(session, *, defer_terminal=False):
        return None

    pg_resolve = AsyncMock(return_value=True)
    try:
        with patch(f"{DIALECTIC}.mcp_server", server), \
             patch("src.mcp_handlers.shared.get_mcp_server", return_value=server), \
             patch(f"{DIALECTIC}.load_session", new_callable=AsyncMock, return_value=session), \
             patch(f"{DIALECTIC}.save_session", new=_quiet_save), \
             patch(f"{DIALECTIC}.pg_add_message", new_callable=AsyncMock), \
             patch(f"{DIALECTIC}.pg_update_phase", new_callable=AsyncMock), \
             patch(f"{DIALECTIC}.pg_resolve_session", pg_resolve), \
             patch(f"{DIALECTIC}.beam_update_phase", new_callable=AsyncMock, return_value=None), \
             patch(f"{DIALECTIC}.beam_resolve", new_callable=AsyncMock, return_value=None), \
             patch(f"{DIALECTIC}.execute_resolution", new_callable=AsyncMock,
                   return_value={"success": True}), \
             patch("src.mcp_handlers.context.get_context_agent_id", return_value="agent-reviewer"):
            response = await handle_submit_synthesis({
                "session_id": session.session_id,
                "agent_id": "agent-reviewer",
                "proposed_conditions": ["Lower threshold to 0.5"],
                "root_cause": "Complexity spike",
                "reasoning": "Conditions agreed",
                "agrees": True,
                "api_key": CALLER_KEY,
            })
    finally:
        ACTIVE_SESSIONS.pop(session.session_id, None)

    assert session.phase == DialecticPhase.RESOLVED and session.resolution is not None
    pg_resolve.assert_awaited()
    stored = pg_resolve.await_args.kwargs["resolution"]
    return stored, json.loads(response[0].text)


def _payload_of(stored):
    """Rebuild the canonical payload the handler signed, from the stored row."""
    fields = {k: stored[k] for k in (
        "action", "conditions", "root_cause", "reasoning", "timestamp",
    )}
    return Resolution(signature_a="", signature_b="", signature_version=2, **fields).canonical_payload()


@pytest.mark.asyncio
async def test_no_key_on_file_leaves_party_a_unsigned_even_when_the_caller_passes_one():
    stored, response = await _reviewer_converges(paused_key=None, reviewer_key=None)

    assert stored["signature_a"] == "", (
        "party A was signed with the synthesis caller's api_key"
    )
    assert stored["signature_b"] == ""
    assert describe_attestation(stored)["state"] == ATTESTATION_UNSIGNED
    assert response["attestation"]["state"] == ATTESTATION_UNSIGNED


@pytest.mark.asyncio
async def test_the_callers_key_never_lands_in_party_as_slot():
    """With the reviewer's own key on file too, the old fallback made A == B."""
    stored, response = await _reviewer_converges(paused_key=None, reviewer_key=CALLER_KEY)

    assert stored["signature_a"] == ""
    assert stored["signature_b"] == Resolution.compute_signature(_payload_of(stored), CALLER_KEY)
    assert stored["signature_b"] != ""
    assert describe_attestation(stored)["state"] == ATTESTATION_SINGLE_SIGNER
    assert response["attestation"]["signer_count"] == 1


@pytest.mark.asyncio
async def test_a_paused_agent_with_a_key_on_file_still_signs_with_its_own_key():
    stored, _ = await _reviewer_converges(paused_key="paused-own-key", reviewer_key=None)

    expected = Resolution.compute_signature(_payload_of(stored), "paused-own-key")
    assert expected != ""
    assert stored["signature_a"] == expected
    assert stored["signature_a"] != Resolution.compute_signature(_payload_of(stored), CALLER_KEY)
    assert describe_attestation(stored)["state"] == ATTESTATION_SINGLE_SIGNER

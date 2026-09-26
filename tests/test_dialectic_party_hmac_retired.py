"""Party-HMAC minting is retired; history stays readable (#2449).

Decision D5 in ``docs/proposals/active/federation-trust-decisions-2026-09-25.md``:
new dialectic resolutions stop receiving v2 party-HMAC signatures, because a
symmetric HMAC the server computes with a key the server stores attests only
the server's own write. The constraints that decision binds:

- no finalize path mints, and minting stops inside ``finalize_resolution``
  itself rather than by relying on keys being absent (some legacy paths still
  generate an api_key);
- ``signature_a`` / ``signature_b`` stay present and empty, so the record's key
  set, ``Resolution.hash()`` and the drr.v1 field set do not change;
- a new row reads as ``unsigned``, and here also as unsigned *by design*;
- historical rows classify exactly as before and still verify.

The three finalize paths are driven through their real handlers, each with an
api_key on file for every party, so a regression that looked a key up again
would show up as a non-empty signature.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.dialectic_protocol import (
    ATTESTATION_BILATERAL,
    ATTESTATION_LEGACY_V1,
    ATTESTATION_SINGLE_SIGNER,
    ATTESTATION_UNSIGNED,
    SIGNATURE_VERSION_PARTY_HMAC,
    SIGNATURE_VERSION_PARTY_HMAC_RETIRED,
    DialecticMessage,
    DialecticPhase,
    DialecticSession,
    Resolution,
    describe_attestation,
)
from src.dialectic_receipt import REQUIRED_RECORD_FIELDS

DIALECTIC = "src.mcp_handlers.dialectic.handlers"
LLM = "src.mcp_handlers.support.llm_delegation"
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# The stored record's key set before and after retirement. A field added or
# removed here would move resolution_hash for every new row and change what a
# drr.v1 receipt covers.
RECORD_KEYS = {
    "action", "conditions", "root_cause", "reasoning",
    "signature_a", "signature_b", "timestamp", "signature_version",
}


def _converged_session():
    s = DialecticSession(
        paused_agent_id="agent-a",
        reviewer_agent_id="agent-b",
        session_type="recovery",
    )
    now = datetime.now(timezone.utc).isoformat()
    s.transcript.append(DialecticMessage(
        phase="thesis", agent_id="agent-a", timestamp=now,
        root_cause="initial cause", proposed_conditions=["c1"], reasoning="initial",
    ))
    s.transcript.append(DialecticMessage(
        phase="antithesis", agent_id="agent-b", timestamp=now,
        reasoning="counter", concerns=["c"],
    ))
    for agent in ("agent-a", "agent-b"):
        s.transcript.append(DialecticMessage(
            phase="synthesis", agent_id=agent, timestamp=now,
            proposed_conditions=["agreed"], root_cause="agreed cause",
            reasoning=f"from {agent}", agrees=True,
        ))
    s.phase = DialecticPhase.RESOLVED
    return s


def _assert_retired_shape(record):
    """What every new resolution must look like, whichever path wrote it."""
    assert set(record) - {"receipt"} == RECORD_KEYS
    assert record["signature_a"] == ""
    assert record["signature_b"] == ""
    assert record["signature_version"] == SIGNATURE_VERSION_PARTY_HMAC_RETIRED
    described = describe_attestation(record)
    assert described["state"] == ATTESTATION_UNSIGNED
    assert described["signer_count"] == 0
    assert described["unsigned_by_design"] is True


# ── the protocol ───────────────────────────────────────────────────────────


class TestFinalizeMintsNothing:
    def test_finalize_takes_no_key(self):
        """No api_key can reach finalize, so none can be minted with."""
        params = list(inspect.signature(DialecticSession.finalize_resolution).parameters)
        assert params == ["self"]

    def test_a_new_resolution_is_unsigned_by_design(self):
        res = _converged_session().finalize_resolution()
        _assert_retired_shape(res.to_dict())
        assert describe_attestation(res) == describe_attestation(res.to_dict())

    def test_a_new_resolution_never_verifies(self):
        res = _converged_session().finalize_resolution()
        assert res.verify_signatures("key-a", "key-b") is False
        assert res.verify_signatures("", "") is False

    def test_the_record_still_carries_every_field_a_receipt_covers(self):
        record = _converged_session().finalize_resolution().to_dict()
        assert set(REQUIRED_RECORD_FIELDS) <= set(record)

    def test_the_hash_formula_is_unchanged_for_a_new_record(self):
        res = _converged_session().finalize_resolution()
        expected = hashlib.sha256(
            json.dumps(res.to_dict(), sort_keys=True).encode()
        ).hexdigest()
        assert res.hash() == expected

    def test_the_session_holds_the_resolution_it_returned(self):
        s = _converged_session()
        res = s.finalize_resolution()
        assert s.resolution is res

    def test_no_production_code_mints_a_party_hmac(self):
        """compute_signature stays for reading history; only the verifier calls it."""
        callers = []
        for path in (PROJECT_ROOT / "src").rglob("*.py"):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if "compute_signature(" in code and "def compute_signature" not in code:
                    callers.append((path.relative_to(PROJECT_ROOT).as_posix(), i, code.strip()))
        assert callers, "expected verify_signatures to still recompute history"
        assert all(
            path == "src/dialectic_protocol.py" and "expected_" in code
            for path, _, code in callers
        ), f"a production caller mints a party HMAC again: {callers}"


class TestHashStability:
    """resolution_hash for an unchanged record must not move.

    Pinned values were computed on master before this change, so a drift in
    the hash formula or in the record's serialisation fails here.
    """

    def test_a_historical_bilateral_row_hashes_as_it_always_did(self):
        historical = Resolution(
            action="resume",
            conditions=["hold complexity at 0.3", "re-read before each edit"],
            root_cause="agreed cause",
            reasoning="Agent A: a\nAgent B: b",
            signature_a="a" * 64,
            signature_b="b" * 64,
            timestamp="2026-06-01T12:00:00+00:00",
            signature_version=2,
        )
        assert historical.hash() == (
            "a0b80102057c611278e5509db9979f48df414aed32c5b7cb41b8aed18522d7b9"
        )

    def test_a_historical_unsigned_row_hashes_as_it_always_did(self):
        historical = Resolution(
            action="resume", conditions=["c"], root_cause="rc", reasoning="r",
            signature_a="", signature_b="",
            timestamp="2026-09-08T00:00:00+00:00", signature_version=2,
        )
        assert historical.hash() == (
            "30ab4f4aba580ccf2e875df70f89b27de409e67f380d54d3bf487b1cedda009b"
        )


class TestHistoryStaysReadable:
    """Every historical shape classifies exactly as before, and none reads as by-design."""

    @pytest.mark.parametrize("record,state", [
        ({"signature_version": 2, "signature_a": "a" * 64, "signature_b": "b" * 64}, ATTESTATION_BILATERAL),
        ({"signature_version": 2, "signature_a": "a" * 64, "signature_b": ""}, ATTESTATION_SINGLE_SIGNER),
        ({"signature_version": 2, "signature_a": "", "signature_b": ""}, ATTESTATION_UNSIGNED),
        ({"signature_version": 1, "signature_a": "sa", "signature_b": "sb"}, ATTESTATION_LEGACY_V1),
        ({"signature_version": 1, "signature_a": "", "signature_b": ""}, ATTESTATION_UNSIGNED),
        ({"signature_a": "sa", "signature_b": "sb"}, ATTESTATION_LEGACY_V1),
    ])
    def test_historical_rows_classify_as_before(self, record, state):
        described = describe_attestation(record)
        assert described["state"] == state
        assert described["unsigned_by_design"] is False

    def test_a_historical_bilateral_row_still_verifies(self):
        proto = Resolution(
            action="resume", conditions=["c"], root_cause="rc", reasoning="r",
            signature_a="", signature_b="",
            timestamp="2026-06-01T12:00:00+00:00",
            signature_version=SIGNATURE_VERSION_PARTY_HMAC,
        )
        payload = proto.canonical_payload()
        signed = Resolution(**{
            **proto.to_dict(),
            "signature_a": Resolution.compute_signature(payload, "key-a"),
            "signature_b": Resolution.compute_signature(payload, "key-b"),
        })
        assert signed.verify_signatures("key-a", "key-b") is True
        assert describe_attestation(signed)["state"] == ATTESTATION_BILATERAL

    def test_a_v3_row_carrying_a_signature_is_not_read_as_attested(self):
        """finalize cannot produce this; if it appears, it reads as unverifiable."""
        described = describe_attestation({
            "signature_version": SIGNATURE_VERSION_PARTY_HMAC_RETIRED,
            "signature_a": "a" * 64, "signature_b": "b" * 64,
        })
        assert described["state"] == ATTESTATION_LEGACY_V1
        assert described["unsigned_by_design"] is False

    def test_the_read_path_reconstructs_a_new_row_as_v3(self):
        from src.mcp_handlers.dialectic.session import _reconstruct_session_from_dict

        s = _converged_session()
        record = s.finalize_resolution().to_dict()
        reloaded = _reconstruct_session_from_dict(s.session_id, {
            "paused_agent_id": "agent-a",
            "reviewer_agent_id": "agent-b",
            "paused_agent_state": {},
            "phase": "resolved",
            "resolution": json.loads(json.dumps(record)),
        })
        assert reloaded.resolution.signature_version == SIGNATURE_VERSION_PARTY_HMAC_RETIRED
        _assert_retired_shape(reloaded.resolution.to_dict())


# ── the three finalize paths, driven through their handlers ────────────────


def _meta(status, api_key):
    return SimpleNamespace(
        status=status,
        label="Test",
        api_key=api_key,
        last_update=datetime.now().isoformat(),
        paused_at=None,
        structured_id=None,
    )


@pytest.fixture(autouse=True)
def _clear_sessions():
    from src.mcp_handlers.dialectic.session import ACTIVE_SESSIONS
    ACTIVE_SESSIONS.clear()
    yield
    ACTIVE_SESSIONS.clear()


@pytest.mark.asyncio
async def test_peer_review_synthesis_path_mints_nothing():
    """Both parties hold a key and the caller passes one: still unsigned."""
    from src.mcp_handlers.dialectic.handlers import ACTIVE_SESSIONS, handle_submit_synthesis

    server = MagicMock()
    server.agent_metadata = {
        "agent-paused": _meta("paused", "paused-key"),
        "agent-reviewer": _meta("active", "reviewer-key"),
    }
    server.monitors = {}
    server.load_metadata = MagicMock()
    server.load_metadata_async = AsyncMock()
    server.project_root = str(PROJECT_ROOT)

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
    with patch("src.mcp_handlers.dialectic.auth.caller_is_bound_as", return_value=True), \
         patch(f"{DIALECTIC}.mcp_server", server), \
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
            "api_key": "reviewer-key",
        })

    assert session.phase == DialecticPhase.RESOLVED
    pg_resolve.assert_awaited()
    _assert_retired_shape(pg_resolve.await_args.kwargs["resolution"])
    served = json.loads(response[0].text)
    assert served["attestation"]["state"] == ATTESTATION_UNSIGNED
    assert served["attestation"]["unsigned_by_design"] is True


def _synthetic_antithesis():
    return {
        "concerns": ["risk_score alone ignores trajectory"],
        "counter_reasoning": "Low instantaneous risk is not a safe trajectory.",
        "grounding_cited": "coherence 0.38",
        "position": "refine",
        "suggested_conditions": ["gate on coherence > 0.85"],
        "_structured": True,
    }


def _synthetic_synthesis():
    return {
        "agreed_root_cause": "Attention-management failure",
        "reasoning": "Integrates the reviewer's checkpoint concern.",
        "merged_conditions": ["Re-read before each edit"],
        "recommendation": "RESUME",
        "_structured": True,
    }


@pytest.mark.asyncio
async def test_synthetic_reviewer_path_mints_nothing():
    """submit_thesis with an open reviewer slot resolves via the synthetic reviewer."""
    from src.mcp_handlers.dialectic.handlers import ACTIVE_SESSIONS, handle_submit_thesis

    server = MagicMock()
    server.agent_metadata = {"agent-paused": _meta("paused", "paused-key")}
    server.monitors = {}

    session = DialecticSession(
        paused_agent_id="agent-paused", reviewer_agent_id=None, session_type="recovery",
    )
    session.phase = DialecticPhase.THESIS
    ACTIVE_SESSIONS[session.session_id] = session

    pg_resolve = AsyncMock(return_value=True)
    with contextlib.ExitStack() as stack:
        for p in (
            patch(f"{DIALECTIC}.mcp_server", server),
            patch(f"{DIALECTIC}._resolve_dialectic_agent_id",
                  new=AsyncMock(return_value=("agent-paused", None))),
            patch(f"{DIALECTIC}.load_session", new=AsyncMock(return_value=None)),
            patch(f"{DIALECTIC}.pg_add_message", new=AsyncMock()),
            patch(f"{DIALECTIC}.pg_update_phase", new=AsyncMock()),
            patch(f"{DIALECTIC}.pg_update_awaiting_facilitation", new=AsyncMock()),
            patch(f"{DIALECTIC}.pg_resolve_session", new=pg_resolve),
            patch(f"{DIALECTIC}.save_session", new=AsyncMock()),
            patch("src.mcp_handlers.context.get_context_agent_id", return_value=None),
            patch(f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)),
            patch(f"{LLM}.generate_antithesis", new=AsyncMock(return_value=_synthetic_antithesis())),
            patch(f"{LLM}.generate_synthesis", new=AsyncMock(return_value=_synthetic_synthesis())),
            patch("src.mcp_handlers.dialectic.orchestrator_dispatch.dispatch_orchestrated_review",
                  new=AsyncMock()),
        ):
            stack.enter_context(p)
        await handle_submit_thesis({
            "session_id": session.session_id,
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit without re-reading the file",
            "proposed_conditions": ["Re-read before each edit"],
            "reasoning": "Assumed file state unchanged",
            "api_key": "paused-key",
        })

    assert session.phase == DialecticPhase.RESOLVED
    pg_resolve.assert_awaited()
    _assert_retired_shape(pg_resolve.await_args.kwargs["resolution"])


@pytest.mark.asyncio
async def test_llm_assisted_path_mints_nothing():
    """handle_llm_assisted_dialectic with the agent's api_key on file: still unsigned."""
    from src.mcp_handlers.dialectic.handlers import handle_llm_assisted_dialectic

    server = MagicMock()
    server.agent_metadata = {"agent-paused": _meta("paused", "paused-key")}
    server.monitors = {}

    full_dialectic = {
        "success": True,
        "recommendation": "RESUME",
        "antithesis": _synthetic_antithesis(),
        "synthesis": _synthetic_synthesis(),
    }
    pg_resolve = AsyncMock(return_value=True)
    with contextlib.ExitStack() as stack:
        for p in (
            patch(f"{DIALECTIC}.mcp_server", server),
            patch(f"{DIALECTIC}.require_registered_agent", return_value=("agent-paused", None)),
            patch(f"{DIALECTIC}.resolve_agent_uuid", return_value="agent-paused"),
            patch(f"{LLM}.is_llm_available", new=AsyncMock(return_value=True)),
            patch(f"{LLM}.run_full_dialectic", new=AsyncMock(return_value=full_dialectic)),
            patch(f"{DIALECTIC}.pg_create_session", new=AsyncMock()),
            patch(f"{DIALECTIC}.pg_add_message", new=AsyncMock()),
            patch(f"{DIALECTIC}.pg_update_phase", new=AsyncMock()),
            patch(f"{DIALECTIC}.pg_resolve_session", new=pg_resolve),
            patch(f"{DIALECTIC}._emit_dialectic_event", new=AsyncMock()),
        ):
            stack.enter_context(p)
        await handle_llm_assisted_dialectic({
            "agent_id": "agent-paused",
            "root_cause": "Repeated a failing edit without re-reading the file",
            "proposed_conditions": ["Re-read before each edit"],
            "api_key": "paused-key",
        })

    pg_resolve.assert_awaited()
    _assert_retired_shape(pg_resolve.await_args.kwargs["resolution"])

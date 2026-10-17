"""
Tests for the BEAM dialectic-resolve client (dialectic-on-BEAM Slice 1.2).

The client must be fail-safe: it returns None (caller falls back to the Python
pg_resolve_session path) whenever the flag is off, the lease plane is not
configured, a required field is missing, or BEAM returns an ordinary non-OK.
Authoritative governance-policy, transcript-frontier, and in-flight-saga
refusals raise rather than fall back to a second writer.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.mcp_handlers.dialectic import beam_resolve_client as brc


def _fake_httpx(status_code, body):
    """Build a fake httpx module whose Client().post() returns the given response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b"x"
    resp.json.return_value = body
    client_cm = MagicMock()
    client_cm.__enter__.return_value.post.return_value = resp
    client_cm.__exit__.return_value = False
    fake = MagicMock()
    fake.Client.return_value = client_cm
    return fake


@pytest.mark.asyncio
async def test_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", raising=False)
    out = await brc.beam_resolve("s1", "p", "r", {"verdict": "resume"})
    assert out is None


@pytest.mark.asyncio
async def test_enabled_but_no_bearer_returns_none(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)
    out = await brc.beam_resolve("s1", "p", "r", {"verdict": "resume"})
    assert out is None


@pytest.mark.asyncio
async def test_enabled_missing_reviewer_returns_none(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    out = await brc.beam_resolve("s1", "p", None, {"verdict": "resume"})
    assert out is None


@pytest.mark.asyncio
async def test_missing_transcript_frontier_never_calls_beam(monkeypatch):
    """A new Python worker must not send an unfenced write to any BEAM version."""
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    fake = _fake_httpx(
        200,
        {"ok": True, "status": "resolved", "saga_id": "unsafe", "origin": "new"},
    )

    with patch.dict(sys.modules, {"httpx": fake}):
        out = await brc.beam_resolve(
            "s1", "p", "r", {"verdict": "resume"}, expected_phase="synthesis"
        )

    assert out is None
    fake.Client.assert_not_called()


@pytest.mark.asyncio
async def test_success_returns_body(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "true")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    body = {"ok": True, "status": "resolved", "saga_id": "abc", "origin": "new"}
    fake = _fake_httpx(200, body)
    with patch.dict(sys.modules, {"httpx": fake}):
        out = await brc.beam_resolve(
            "s1", "p", "r", {"verdict": "resume"},
            expected_phase="synthesis", expected_transcript_revision=0,
        )
    assert out == body
    post = fake.Client.return_value.__enter__.return_value.post
    assert post.call_args.args[0].endswith("/v2/dialectic/resolve-with-effect")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"ok": True},
        {"ok": True, "status": "failed", "saga_id": "abc", "origin": "new"},
        {"ok": True, "status": "resolved", "saga_id": None, "origin": "new"},
        {"ok": True, "status": "resolved", "saga_id": "abc", "origin": "mystery"},
        {
            "ok": True,
            "status": "active",
            "saga_id": None,
            "origin": "already_terminal",
        },
    ],
)
async def test_invalid_positive_ack_is_commit_unknown(monkeypatch, body):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    detail = {}

    with patch.dict(sys.modules, {"httpx": _fake_httpx(200, body)}):
        out = await brc.beam_resolve(
            "s1",
            "p",
            "r",
            {"verdict": "resume"},
            expected_phase="synthesis",
            expected_transcript_revision=0,
            detail=detail,
        )

    assert out is None
    assert detail == {
        "commit_unknown": True,
        "http_status": 200,
        "invalid_ack": True,
    }


@pytest.mark.asyncio
async def test_missing_effect_route_is_definitive_fallback(monkeypatch):
    """A pre-protocol BEAM returns 404 without touching the session row."""
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    detail = {}
    with patch.dict(
        sys.modules,
        {"httpx": _fake_httpx(404, {"ok": False, "error": "not_found"})},
    ):
        out = await brc.beam_resolve(
            "s1", "p", "r", {"verdict": "resume"}, detail=detail,
            expected_phase="synthesis", expected_transcript_revision=0,
        )

    assert out is None
    assert detail == {}


@pytest.mark.asyncio
async def test_saga_in_flight_never_falls_back(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    body = {"ok": False, "error": "saga_in_flight"}
    detail = {}
    with patch.dict(sys.modules, {"httpx": _fake_httpx(409, body)}):
        with pytest.raises(brc.BeamDialecticSagaInFlight):
            await brc.beam_resolve(
                "s1", "p", "r", {"verdict": "resume"}, detail=detail,
                expected_phase="synthesis", expected_transcript_revision=0,
            )
    assert detail == {}


@pytest.mark.asyncio
async def test_transcript_conflict_never_falls_back(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    body = {"ok": False, "error": "session_revision_mismatch"}
    with patch.dict(sys.modules, {"httpx": _fake_httpx(409, body)}):
        with pytest.raises(brc.BeamDialecticTranscriptConflict):
            await brc.beam_resolve(
                "s1",
                "p",
                "r",
                {"verdict": "resume"},
                expected_phase="synthesis",
                expected_transcript_revision=4,
            )


@pytest.mark.asyncio
async def test_pause_bound_self_review_refusal_never_falls_back(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    body = {"ok": False, "error": "self_review_not_authorizing"}
    with patch.dict(sys.modules, {"httpx": _fake_httpx(409, body)}):
        with pytest.raises(brc.BeamDialecticPolicyRefusal) as exc:
            await brc.beam_resolve(
                "s1", "p", "p", {"verdict": "resume"},
                expected_phase="synthesis", expected_transcript_revision=0,
            )
    assert exc.value.error_code == "self_review_not_authorizing"


@pytest.mark.asyncio
async def test_postcommit_503_marks_commit_unknown(monkeypatch):
    """The row can commit before saga finalization returns a 5xx."""
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    detail = {}
    with patch.dict(sys.modules, {"httpx": _fake_httpx(503, {"ok": False})}):
        out = await brc.beam_resolve(
            "s1", "p", "r", {"verdict": "resume"}, detail=detail,
            expected_phase="synthesis", expected_transcript_revision=0,
        )
    assert out is None
    assert detail == {"commit_unknown": True, "http_status": 503}


@pytest.mark.asyncio
async def test_status_failed_is_sent_and_validated(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    fake = _fake_httpx(200, {"ok": True, "status": "failed", "saga_id": "x", "origin": "new"})
    with patch.dict(sys.modules, {"httpx": fake}):
        out = await brc.beam_resolve(
            "s1", "p", "r", {"reason": "boom"}, status="failed",
            expected_transcript_revision=0,
        )
    assert out is not None and out["status"] == "failed"
    # The status was forwarded in the POST body.
    sent = fake.Client.return_value.__enter__.return_value.post.call_args.kwargs["json"]
    assert sent["status"] == "failed"


@pytest.mark.asyncio
async def test_invalid_status_returns_none(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    out = await brc.beam_resolve("s1", "p", "r", {"x": 1}, status="bogus")
    assert out is None


@pytest.mark.asyncio
async def test_create_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", raising=False)
    out = await brc.beam_create_session("s1", "p", {"reason": "x"})
    assert out is None


@pytest.mark.asyncio
async def test_create_success_filters_none_fields(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    fake = _fake_httpx(201, {"ok": True, "session_id": "s1", "created": True})
    with patch.dict(sys.modules, {"httpx": fake}):
        out = await brc.beam_create_session(
            "s1", "p", {"reviewer_agent_id": "r", "topic": None, "reason": "boom"}
        )
    assert out is not None and out["created"] is True
    sent = fake.Client.return_value.__enter__.return_value.post.call_args.kwargs["json"]
    assert sent["reviewer_agent_id"] == "r"
    assert "topic" not in sent  # None-valued fields are dropped


@pytest.mark.asyncio
async def test_create_non_ok_falls_back(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    with patch.dict(sys.modules, {"httpx": _fake_httpx(503, {"ok": False})}):
        out = await brc.beam_create_session("s1", "p", {"reason": "x"})
    assert out is None


@pytest.mark.asyncio
async def test_create_authorship_policy_refusal_never_falls_back(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    body = {"ok": False, "error": "reviewer_recused"}
    with patch.dict(sys.modules, {"httpx": _fake_httpx(409, body)}):
        with pytest.raises(brc.BeamDialecticPolicyRefusal) as exc:
            await brc.beam_create_session("s1", "p", {"reviewer_agent_id": "r"})
    assert exc.value.error_code == "reviewer_recused"


@pytest.mark.asyncio
async def test_update_phase_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", raising=False)
    assert await brc.beam_update_phase("s1", "antithesis") is None


@pytest.mark.asyncio
async def test_update_phase_success(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    fake = _fake_httpx(200, {"ok": True, "session_id": "s1", "phase": "antithesis"})
    with patch.dict(sys.modules, {"httpx": fake}):
        out = await brc.beam_update_phase("s1", "antithesis")
    assert out is not None and out["phase"] == "antithesis"


@pytest.mark.asyncio
async def test_update_phase_non_ok_falls_back(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    with patch.dict(sys.modules, {"httpx": _fake_httpx(422, {"ok": False})}):
        assert await brc.beam_update_phase("s1", "resolved") is None


@pytest.mark.asyncio
async def test_update_reviewer_disabled_returns_none(monkeypatch):
    monkeypatch.delenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", raising=False)
    assert await brc.beam_update_reviewer("s1", "rev") is None


@pytest.mark.asyncio
async def test_update_reviewer_success(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    fake = _fake_httpx(200, {"ok": True, "session_id": "s1", "reviewer_agent_id": "rev"})
    with patch.dict(sys.modules, {"httpx": fake}):
        out = await brc.beam_update_reviewer("s1", "rev")
    assert out is not None and out["reviewer_agent_id"] == "rev"


@pytest.mark.asyncio
async def test_update_reviewer_open_slot_claim_is_sent(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    fake = _fake_httpx(200, {"ok": True, "session_id": "s1", "reviewer_agent_id": "rev"})
    with patch.dict(sys.modules, {"httpx": fake}):
        out = await brc.beam_update_reviewer(
            "s1", "rev", claim_if_unassigned=True
        )

    assert out is not None
    post = fake.Client.return_value.__enter__.return_value.post
    assert post.call_args.args[0].endswith("/v1/dialectic/reviewer/claim")
    sent = post.call_args.kwargs["json"]
    assert sent == {
        "session_id": "s1",
        "reviewer_agent_id": "rev",
        "claim_if_unassigned": True,
    }


@pytest.mark.asyncio
async def test_update_reviewer_open_slot_old_beam_404_is_safe_fallback(
    monkeypatch,
):
    """A pre-claim-route BEAM returns 404 without touching the session row."""
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    detail = {}
    fake = _fake_httpx(404, {"ok": False, "error": "not_found"})

    with patch.dict(sys.modules, {"httpx": fake}):
        out = await brc.beam_update_reviewer(
            "s1",
            "rev",
            claim_if_unassigned=True,
            detail=detail,
        )

    assert out is None
    assert detail == {}
    post = fake.Client.return_value.__enter__.return_value.post
    assert post.call_args.args[0].endswith("/v1/dialectic/reviewer/claim")


@pytest.mark.asyncio
async def test_update_reviewer_blank_returns_none(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    # missing reviewer -> short-circuits to None (no HTTP)
    assert await brc.beam_update_reviewer("s1", None) is None


@pytest.mark.asyncio
async def test_update_reviewer_authorship_policy_refusal_never_falls_back(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    body = {"ok": False, "error": "invalid_review_authorship"}
    with patch.dict(sys.modules, {"httpx": _fake_httpx(409, body)}):
        with pytest.raises(brc.BeamDialecticPolicyRefusal) as exc:
            await brc.beam_update_reviewer("s1", "rev")
    assert exc.value.error_code == "invalid_review_authorship"


@pytest.mark.asyncio
async def test_update_reviewer_lost_response_marks_commit_unknown(monkeypatch):
    """A response decode failure can follow a committed reviewer CAS."""
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    fake = _fake_httpx(200, {"ok": True})
    response = fake.Client.return_value.__enter__.return_value.post.return_value
    response.json.side_effect = RuntimeError("response body lost")
    detail = {}

    with patch.dict(sys.modules, {"httpx": fake}):
        out = await brc.beam_update_reviewer(
            "s1", "rev", claim_if_unassigned=True, detail=detail
        )

    assert out is None
    assert detail == {"commit_unknown": True}


@pytest.mark.asyncio
async def test_update_reviewer_503_marks_commit_unknown(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    detail = {}
    with patch.dict(sys.modules, {"httpx": _fake_httpx(503, {"ok": False})}):
        out = await brc.beam_update_reviewer("s1", "rev", detail=detail)

    assert out is None
    assert detail == {"commit_unknown": True, "http_status": 503}


@pytest.mark.asyncio
async def test_resolve_identity_policy_refusal_never_falls_back(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    body = {"ok": False, "error": "session_identity_mismatch"}
    with patch.dict(sys.modules, {"httpx": _fake_httpx(409, body)}):
        with pytest.raises(brc.BeamDialecticPolicyRefusal) as exc:
            await brc.beam_resolve(
                "s1", "p", "r", {"action": "resume"},
                expected_phase="synthesis", expected_transcript_revision=0,
            )
    assert exc.value.error_code == "session_identity_mismatch"


@pytest.mark.asyncio
async def test_transport_error_falls_back_to_none(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    boom = MagicMock()
    boom.Client.side_effect = RuntimeError("connection refused")
    with patch.dict(sys.modules, {"httpx": boom}):
        detail = {}
        out = await brc.beam_resolve(
            "s1", "p", "r", {"verdict": "resume"}, detail=detail,
            expected_phase="synthesis", expected_transcript_revision=0,
        )
    assert out is None
    # Client construction failed before post() was entered, so there is no
    # ambiguous server-side commit to reconcile.
    assert detail == {}


@pytest.mark.asyncio
async def test_response_decode_failure_after_post_marks_commit_unknown(monkeypatch):
    monkeypatch.setenv("UNITARES_DIALECTIC_BEAM_RESOLUTION", "1")
    monkeypatch.setenv("LEASE_PLANE_BEARER_TOKEN", "tok")
    fake = _fake_httpx(200, {"ok": True})
    response = fake.Client.return_value.__enter__.return_value.post.return_value
    response.json.side_effect = ValueError("truncated JSON")
    detail = {}

    with patch.dict(sys.modules, {"httpx": fake}):
        out = await brc.beam_resolve(
            "s1", "p", "r", {"verdict": "resume"}, detail=detail,
            expected_phase="synthesis", expected_transcript_revision=0,
        )

    assert out is None
    assert detail == {"commit_unknown": True}


# --- synthesis_round on the phase wire (2026-08-16) -------------------------
#
# The round was incremented in memory (src/dialectic_protocol.py:705,742) and
# never persisted: this client sent phase only, and BEAM's update_phase wrote
# only phase/updated_at. Every row read synthesis_round=0 across all 36 sessions
# in the trailing 30 days, so the max_synthesis_rounds budget check reset on
# every rehydration.


def test_phase_payload_omits_round_when_absent():
    """Wire shape must stay byte-identical to the pre-fix request when no round
    is tracked, so a lease-plane that predates the field is unaffected."""
    from src.mcp_handlers.dialectic.beam_resolve_client import _phase_payload

    assert _phase_payload("s1", "synthesis", None) == {
        "session_id": "s1",
        "phase": "synthesis",
    }


def test_phase_payload_includes_round_when_present():
    from src.mcp_handlers.dialectic.beam_resolve_client import _phase_payload

    assert _phase_payload("s1", "synthesis", 2) == {
        "session_id": "s1",
        "phase": "synthesis",
        "synthesis_round": 2,
    }


def test_phase_payload_coerces_round_to_int():
    """A float or numeric string must not reach the wire as a non-int; the BEAM
    guard requires is_integer/1 and would 422 the whole phase update."""
    from src.mcp_handlers.dialectic.beam_resolve_client import _phase_payload

    assert _phase_payload("s1", "synthesis", 3.0)["synthesis_round"] == 3
    assert isinstance(_phase_payload("s1", "synthesis", 3.0)["synthesis_round"], int)


def test_phase_payload_keeps_zero_not_dropped():
    """0 is a real round, not 'absent' — a falsy check here would silently stop
    persisting the first round."""
    from src.mcp_handlers.dialectic.beam_resolve_client import _phase_payload

    assert _phase_payload("s1", "thesis", 0)["synthesis_round"] == 0

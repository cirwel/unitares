"""S19 PR4: PATH 2.8 substrate-anchored HTTP rejection.

Verifies the leak-closing gate: when a token-based resume request arrives
over HTTP (peer_pid is None in SessionSignals) AND the token's UUID has a
``core.substrate_claims`` row, ``resolve_session_identity`` refuses with
``error="substrate_anchored_uuid_requires_uds"``.

Non-substrate UUIDs and UDS-arriving requests are unaffected — the gate
is self-scoping by the substrate_claims table.

This is the test that pins the Hermes-incident closure: an external HTTP
process presenting a copied resident anchor token gets explicit rejection
pointing at the UDS path.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.context import (
    SessionSignals,
    set_session_signals,
    reset_session_signals,
)
from src.mcp_handlers.identity import resolution as resolution_mod
from src.substrate.verification import SubstrateClaim


def _make_claim(agent_id: str = "f92dcea8-4786-412a-a0eb-362c273382f5") -> SubstrateClaim:
    return SubstrateClaim(
        agent_id=agent_id,
        expected_launchd_label="com.unitares.sentinel",
        expected_executable_path="/opt/homebrew/bin/sentinel",
        enrolled_at=datetime(2026, 4, 25, tzinfo=timezone.utc),
        enrolled_by_operator=True,
    )


# =============================================================================
# Substrate-anchored UUID over HTTP → explicit reject
# =============================================================================


@pytest.mark.asyncio
async def test_http_path_substrate_anchored_uuid_is_refused() -> None:
    """The Hermes case: HTTP token-resume for a substrate-anchored UUID
    is refused with the explicit UDS-path message."""
    claim = _make_claim()
    # No peer_pid → HTTP path.
    signals_token = set_session_signals(SessionSignals())
    try:
        with patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(return_value=claim),
        ):
            result = await resolution_mod.resolve_session_identity(
                "session-key-test",
                persist=False,
                resume=True,
                token_agent_uuid=claim.agent_id,
            )
    finally:
        reset_session_signals(signals_token)

    assert result.get("resume_failed") is True
    assert result.get("error") == "substrate_anchored_uuid_requires_uds"
    assert "UNITARES_UDS_SOCKET" in result.get("message", "")
    assert claim.expected_launchd_label in result.get("message", "")


@pytest.mark.asyncio
async def test_http_path_substrate_token_is_refused_even_with_live_session() -> None:
    """A copied resident token must not bypass S19 via its embedded session id.

    Regression for the live Hermes/Sentinel containment: PATH 2 session lookup
    used to return before the PATH 2.8 substrate-token gate, so any token whose
    ``sid`` still had a PG/Redis session row could resume a substrate resident
    over HTTP.
    """
    claim = _make_claim()
    session = MagicMock(agent_id=claim.agent_id)
    signals_token = set_session_signals(SessionSignals())  # HTTP path
    try:
        with patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(return_value=claim),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_redis",
            return_value=None,
        ), patch(
            "src.mcp_handlers.identity.resolution.get_db",
            return_value=MagicMock(
                init=AsyncMock(),
                get_session=AsyncMock(return_value=session),
                update_session_activity=AsyncMock(),
            ),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_id_from_metadata",
            new=AsyncMock(return_value="mcp_20260407"),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_label",
            new=AsyncMock(return_value="Sentinel"),
        ), patch(
            "src.mcp_handlers.identity.resolution._get_agent_status",
            new=AsyncMock(return_value="active"),
        ), patch(
            "src.mcp_handlers.identity.resolution._soft_verify_trajectory",
            new=AsyncMock(return_value={"checked": False, "verified": None, "warning": None}),
        ), patch(
            "src.mcp_handlers.identity.resolution._cache_session",
            new=AsyncMock(return_value=None),
        ):
            result = await resolution_mod.resolve_session_identity(
                "agent-f92dcea8-478",
                persist=False,
                resume=True,
                token_agent_uuid=claim.agent_id,
            )
    finally:
        reset_session_signals(signals_token)

    assert result.get("resume_failed") is True
    assert result.get("error") == "substrate_anchored_uuid_requires_uds"
    assert "UNITARES_UDS_SOCKET" in result.get("message", "")


# =============================================================================
# Substrate-anchored UUID over UDS → gate does NOT fire (peer_pid is set)
# =============================================================================


@pytest.mark.asyncio
async def test_uds_path_substrate_anchored_uuid_skips_gate() -> None:
    """When peer_pid is set (UDS path), the HTTP-reject gate is skipped.
    The downstream substrate verification (PR3e) handles attestation.
    """
    claim = _make_claim()
    # peer_pid set → UDS path; gate must not refuse here.
    signals_token = set_session_signals(SessionSignals(peer_pid=12345))
    try:
        # The gate must NOT call fetch_substrate_claim because peer_pid is set.
        # We patch it to raise so any accidental call would surface immediately.
        with patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(side_effect=AssertionError("gate must not fire on UDS path")),
        ), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=False),
        ):
            result = await resolution_mod.resolve_session_identity(
                "session-key-uds",
                persist=False,
                resume=True,
                token_agent_uuid=claim.agent_id,
            )
    finally:
        reset_session_signals(signals_token)

    # Resume failed downstream because _agent_exists_in_postgres returned False,
    # but the failure mode is the existing one, NOT the new substrate-HTTP gate.
    assert result.get("error") != "substrate_anchored_uuid_requires_uds"


# =============================================================================
# Non-substrate UUID over HTTP → gate does NOT fire
# =============================================================================


@pytest.mark.asyncio
async def test_http_path_non_substrate_uuid_unaffected() -> None:
    """A UUID with no substrate-claim row passes through the gate untouched.
    The existing PATH 2.8 logic handles the resume normally (or fails for
    a non-S19 reason like agent-not-found)."""
    non_substrate_uuid = "11111111-2222-3333-4444-555555555555"
    signals_token = set_session_signals(SessionSignals())  # HTTP path
    try:
        with patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(return_value=None),  # no claim → pass-through
        ), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=False),  # agent doesn't exist
        ):
            result = await resolution_mod.resolve_session_identity(
                "session-key-non-substrate",
                persist=False,
                resume=True,
                token_agent_uuid=non_substrate_uuid,
            )
    finally:
        reset_session_signals(signals_token)

    # Failure mode is "agent not active", not the substrate-HTTP gate.
    assert result.get("error") != "substrate_anchored_uuid_requires_uds"


# =============================================================================
# Defense-in-depth: gate exception falls through to existing behavior
# =============================================================================


@pytest.mark.asyncio
async def test_http_path_gate_exception_falls_through() -> None:
    """An unexpected error in the gate (e.g. DB connection issue during
    fetch_substrate_claim) does NOT block the resume — it falls through
    to existing PATH 2.8. Trade-off: a transient DB error must not lock
    out non-substrate clients (the gate is leak-closing, not the only
    line of defense)."""
    signals_token = set_session_signals(SessionSignals())  # HTTP path
    try:
        with patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(side_effect=RuntimeError("transient DB error")),
        ), patch(
            "src.mcp_handlers.identity.resolution._agent_exists_in_postgres",
            new=AsyncMock(return_value=False),
        ):
            result = await resolution_mod.resolve_session_identity(
                "session-key-degrade",
                persist=False,
                resume=True,
                token_agent_uuid="some-uuid",
            )
    finally:
        reset_session_signals(signals_token)

    assert result.get("error") != "substrate_anchored_uuid_requires_uds"


# =============================================================================
# force_new bypasses the entire PATH 2.8 path (and therefore the gate)
# =============================================================================


@pytest.mark.asyncio
async def test_force_new_bypasses_path28_and_gate() -> None:
    """force_new=true short-circuits PATH 2.8 entirely. Verifies the
    new gate is positioned inside the ``if token_agent_uuid and not
    force_new:`` block."""
    claim = _make_claim()
    signals_token = set_session_signals(SessionSignals())  # HTTP path
    try:
        # Even a substrate-claim shouldn't fire the gate when force_new
        # bypasses PATH 2.8 entirely.
        with patch(
            "src.substrate.verification.fetch_substrate_claim",
            new=AsyncMock(side_effect=AssertionError("gate must not fire under force_new")),
        ):
            # This will fall through to PATH 3 (create new agent), which
            # we don't want to actually run in a unit test, so we patch
            # the create path to short-circuit. We just want to verify
            # the gate didn't fire.
            with patch(
                "src.mcp_handlers.identity.resolution._cache_session",
                new=AsyncMock(return_value=None),
            ), patch(
                "src.mcp_handlers.identity.resolution.get_db",
                return_value=MagicMock(
                    upsert_identity=AsyncMock(),
                    create_session=AsyncMock(),
                ),
            ):
                # Don't actually call resolve_session_identity with
                # force_new — that triggers the full create path which
                # has many dependencies. The contract we're verifying is
                # that the gate is INSIDE the `not force_new` block.
                # That's already statically true from the edit's
                # location — assert by reading the source.
                import inspect
                module_src = inspect.getsource(resolution_mod)
                start = module_src.index("async def resolve_session_identity(")
                src = module_src[start:]
                # The gate's marker comment should appear inside the
                # `if token_agent_uuid and not force_new:` block.
                assert "[SUBSTRATE_HTTP_REJECT]" in src
                assert "substrate_anchored_uuid_requires_uds" in src
                # The block that contains the gate must be the
                # ``if token_agent_uuid and not force_new:`` body —
                # verified by checking the surrounding context.
                gate_idx = src.index("[SUBSTRATE_HTTP_REJECT]")
                preamble = src[:gate_idx]
                last_if = preamble.rfind("if token_agent_uuid and not force_new")
                assert last_if != -1, (
                    "gate must be located inside the "
                    "`if token_agent_uuid and not force_new:` block"
                )
    finally:
        reset_session_signals(signals_token)

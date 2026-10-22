"""S19 handler-side substrate-claim verification gate.

Coverage:
- ``peer_pid is None`` → returns None (HTTP path bypass).
- No substrate-claim row → returns None (non-substrate UUID).
- Claim exists, verify accepts → VerificationResult(accepted=True).
- Claim exists, verify rejects → VerificationResult(accepted=False, ...).
- DB lookup raises → returns None (graceful degrade; no false-accept).
- Verify raises → returns synthetic rejection (attestation_failed; no
  default-accept under exception).

All injectable dependencies (``fetch_fn``, ``verify_fn``, ``pa_module``,
``cache``) are passed in by tests so no DB / no subprocess / no ctypes
calls happen under test.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.substrate.handler_gate import (
    reset_cache_for_testing,
    verify_substrate_at_resume,
)
from src.substrate.verification import (
    SubstrateClaim,
    VerificationResult,
    VerifiedPairsCache,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_claim(agent_id: str = "02e168be-4264-527b-9643-9c737dd309e4") -> SubstrateClaim:
    return SubstrateClaim(
        agent_id=agent_id,
        expected_launchd_label="com.unitares.sentinel",
        expected_executable_path="/opt/homebrew/bin/sentinel",
        enrolled_at=datetime(2026, 4, 25, tzinfo=timezone.utc),
        enrolled_by_operator=True,
    )


@pytest.fixture(autouse=True)
def _isolate_cache():
    """Each test starts with a clean module-level cache."""
    reset_cache_for_testing()
    yield
    reset_cache_for_testing()


# =============================================================================
# Bypass paths (return None)
# =============================================================================


@pytest.mark.asyncio
async def test_peer_pid_none_returns_none() -> None:
    """HTTP path: peer_pid is None → no verification."""
    fetch = AsyncMock(side_effect=AssertionError("fetch must not be called"))
    verify = MagicMock(side_effect=AssertionError("verify must not be called"))

    result = await verify_substrate_at_resume(
        "any-uuid", peer_pid=None,
        fetch_fn=fetch, verify_fn=verify,
    )
    assert result is None


@pytest.mark.asyncio
async def test_no_substrate_claim_returns_none() -> None:
    """Non-substrate-anchored UUID: fetch returns None → no verification."""
    fetch = AsyncMock(return_value=None)
    verify = MagicMock(side_effect=AssertionError("verify must not be called"))

    result = await verify_substrate_at_resume(
        "non-substrate-uuid", peer_pid=12345,
        fetch_fn=fetch, verify_fn=verify,
    )
    assert result is None
    fetch.assert_awaited_once_with("non-substrate-uuid")


# =============================================================================
# Verify paths (returns VerificationResult)
# =============================================================================


@pytest.mark.asyncio
async def test_claim_exists_and_verify_accepts() -> None:
    claim = _make_claim()
    fetch = AsyncMock(return_value=claim)
    accepted = VerificationResult(accepted=True, reason="ok")
    verify = MagicMock(return_value=accepted)

    result = await verify_substrate_at_resume(
        claim.agent_id, peer_pid=12345,
        fetch_fn=fetch, verify_fn=verify,
    )
    assert result is accepted
    # Verify was called with the right arguments
    verify.assert_called_once()
    args, kwargs = verify.call_args
    assert args[0] is claim
    assert args[1] == 12345


@pytest.mark.asyncio
async def test_claim_exists_and_verify_rejects() -> None:
    claim = _make_claim()
    fetch = AsyncMock(return_value=claim)
    rejected = VerificationResult(
        accepted=False,
        reason="launchd label mismatch: registered ... observed ...",
        failure_code="label_mismatch",
    )
    verify = MagicMock(return_value=rejected)

    result = await verify_substrate_at_resume(
        claim.agent_id, peer_pid=12345,
        fetch_fn=fetch, verify_fn=verify,
    )
    assert result is rejected
    assert result.accepted is False
    assert result.failure_code == "label_mismatch"


# =============================================================================
# Failure modes (graceful degrade vs. fail-closed)
# =============================================================================


@pytest.mark.asyncio
async def test_db_lookup_raises_degrades_to_none() -> None:
    """DB error during fetch_substrate_claim degrades to None (caller falls
    through to existing token-based gating). Honest tradeoff: a transient
    DB failure must not lock substrate residents out, AND must not
    over-trust the claim we couldn't read."""
    fetch = AsyncMock(side_effect=RuntimeError("connection refused"))
    verify = MagicMock(side_effect=AssertionError("verify must not be called"))

    result = await verify_substrate_at_resume(
        "any-uuid", peer_pid=12345,
        fetch_fn=fetch, verify_fn=verify,
    )
    assert result is None


@pytest.mark.asyncio
async def test_verify_raises_returns_synthetic_rejection() -> None:
    """An unhandled exception in the executor MUST NOT default-accept.

    The gate returns a synthetic VerificationResult(accepted=False) so the
    caller refuses the resume, with a clear ``attestation_failed`` code.
    """
    claim = _make_claim()
    fetch = AsyncMock(return_value=claim)
    verify = MagicMock(side_effect=RuntimeError("launchctl unavailable"))

    result = await verify_substrate_at_resume(
        claim.agent_id, peer_pid=12345,
        fetch_fn=fetch, verify_fn=verify,
    )
    assert result is not None
    assert result.accepted is False
    assert result.failure_code == "attestation_failed"
    assert "launchctl unavailable" in result.reason


# =============================================================================
# anyio-asyncio integration: verify is offloaded via run_in_executor
# =============================================================================


@pytest.mark.asyncio
async def test_verify_runs_in_executor_not_in_event_loop() -> None:
    """The synchronous verify call must run on a thread, not the event loop.

    Verified by checking that the verify callable observes a different
    thread ident than the calling coroutine.
    """
    import threading

    main_thread_ident = threading.get_ident()
    observed: dict[str, Any] = {}

    def fake_verify(claim, peer_pid, **kwargs):
        observed["thread_ident"] = threading.get_ident()
        return VerificationResult(accepted=True, reason="ok")

    fetch = AsyncMock(return_value=_make_claim())

    result = await verify_substrate_at_resume(
        "uuid", peer_pid=12345,
        fetch_fn=fetch, verify_fn=fake_verify,
    )
    assert result is not None and result.accepted
    assert "thread_ident" in observed
    assert observed["thread_ident"] != main_thread_ident, (
        "verify ran on the main event loop thread; would block anyio task group"
    )


# =============================================================================
# Cache injection (production uses module-level singleton; tests can override)
# =============================================================================


@pytest.mark.asyncio
async def test_explicit_cache_argument_is_used() -> None:
    """When ``cache=`` is passed, it overrides the module-level singleton."""
    claim = _make_claim()
    fetch = AsyncMock(return_value=claim)
    accepted = VerificationResult(accepted=True, reason="ok")

    received_caches: list[Any] = []

    def fake_verify(claim, peer_pid, *, pa_module=None, cache=None):
        received_caches.append(cache)
        return accepted

    custom_cache = VerifiedPairsCache()

    await verify_substrate_at_resume(
        claim.agent_id, peer_pid=12345,
        fetch_fn=fetch, verify_fn=fake_verify, cache=custom_cache,
    )

    assert len(received_caches) == 1
    assert received_caches[0] is custom_cache

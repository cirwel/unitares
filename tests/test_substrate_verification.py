"""S19: substrate-claim verification logic.

Tests cover:
- VerifiedPairsCache: pin/match/update/clear semantics, thread safety
- verify_substrate_claim:
  - no_claim rejection
  - label_mismatch rejection (label query mismatch or returns None)
  - exec_mismatch rejection
  - attestation_failed rejection (start-time read returns None)
  - pid_reuse rejection (Q3(e) — same PID, different start_tvsec)
  - happy path: cache pinned on first verified connect
  - legitimate restart: different PID accepted, cache updated
  - cache no-op: same PID + same start_tvsec returns accepted

Pure-Python tests; ``peer_attestation`` is mocked via the ``pa_module``
parameter (no subprocess / ctypes calls under test).
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

from src.substrate.verification import (
    SubstrateClaim,
    VerifiedPairsCache,
    VerificationResult,
    verify_substrate_claim,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_claim(
    agent_id: str = "f92dcea8-4786-412a-a0eb-362c273382f5",
    label: str = "com.unitares.sentinel",
    executable: str = "/opt/homebrew/bin/sentinel",
) -> SubstrateClaim:
    return SubstrateClaim(
        agent_id=agent_id,
        expected_launchd_label=label,
        expected_executable_path=executable,
        enrolled_at=datetime(2026, 4, 25, tzinfo=timezone.utc),
        enrolled_by_operator=True,
    )


def _fake_pa(
    *,
    label: Optional[str] = None,
    executable: Optional[str] = None,
    start_time: Optional[int] = 1_777_167_717,
) -> SimpleNamespace:
    return SimpleNamespace(
        read_service_label=lambda pid: label,
        read_executable_path=lambda pid: executable,
        read_process_start_time=lambda pid: start_time,
    )


# =============================================================================
# VerifiedPairsCache
# =============================================================================


def test_cache_get_returns_none_when_uncached() -> None:
    cache = VerifiedPairsCache()
    assert cache.get("any-uuid") is None


def test_cache_record_then_get_round_trip() -> None:
    cache = VerifiedPairsCache()
    cache.record("uuid-1", 100, 1_777_000_000)
    assert cache.get("uuid-1") == (100, 1_777_000_000)


def test_cache_record_overwrites() -> None:
    cache = VerifiedPairsCache()
    cache.record("uuid-1", 100, 1_777_000_000)
    cache.record("uuid-1", 200, 1_777_000_500)
    assert cache.get("uuid-1") == (200, 1_777_000_500)


def test_cache_clear_drops_all_entries() -> None:
    cache = VerifiedPairsCache()
    cache.record("uuid-1", 100, 1_777_000_000)
    cache.record("uuid-2", 200, 1_777_000_500)
    assert len(cache) == 2
    cache.clear()
    assert len(cache) == 0


def test_cache_concurrent_record_is_safe() -> None:
    """Two threads racing to record the same UUID land cleanly (lock-protected)."""
    cache = VerifiedPairsCache()

    def writer(start: int) -> None:
        for _ in range(200):
            cache.record("uuid-race", 1234, start)

    t1 = threading.Thread(target=writer, args=(1_777_000_000,))
    t2 = threading.Thread(target=writer, args=(1_777_000_999,))
    t1.start(); t2.start()
    t1.join(); t2.join()

    cached = cache.get("uuid-race")
    assert cached is not None
    assert cached[0] == 1234
    assert cached[1] in (1_777_000_000, 1_777_000_999)


# =============================================================================
# verify_substrate_claim — failure modes
# =============================================================================


def test_no_claim_rejection() -> None:
    """``claim=None`` → no_claim with operator-actionable message."""
    result = verify_substrate_claim(None, peer_pid=1234, pa_module=_fake_pa())
    assert result.accepted is False
    assert result.failure_code == "no_claim"
    assert "enroll_resident.py" in result.reason


def test_label_mismatch_when_pa_returns_different_label() -> None:
    claim = _make_claim()
    pa = _fake_pa(
        label="com.someone.else",
        executable=claim.expected_executable_path,
    )
    result = verify_substrate_claim(claim, peer_pid=1234, pa_module=pa)
    assert result.accepted is False
    assert result.failure_code == "label_mismatch"
    assert "com.someone.else" in result.reason


def test_label_mismatch_when_pa_returns_none() -> None:
    """PID is not under any launchd job: read_service_label returns None."""
    claim = _make_claim()
    pa = _fake_pa(label=None, executable=claim.expected_executable_path)
    result = verify_substrate_claim(claim, peer_pid=1234, pa_module=pa)
    assert result.accepted is False
    assert result.failure_code == "label_mismatch"
    assert "None" in result.reason


def test_exec_mismatch_when_actual_path_differs() -> None:
    claim = _make_claim(executable="/opt/homebrew/bin/sentinel")
    pa = _fake_pa(
        label=claim.expected_launchd_label,
        executable="$HOME/projects/unitares/agents/sentinel/agent.py",
    )
    result = verify_substrate_claim(claim, peer_pid=1234, pa_module=pa)
    assert result.accepted is False
    assert result.failure_code == "exec_mismatch"


def test_attestation_failed_when_start_time_read_returns_none() -> None:
    """proc_pidinfo failure: classified attestation_failed, not pid_reuse."""
    claim = _make_claim()
    pa = _fake_pa(
        label=claim.expected_launchd_label,
        executable=claim.expected_executable_path,
        start_time=None,
    )
    result = verify_substrate_claim(claim, peer_pid=1234, pa_module=pa)
    assert result.accepted is False
    assert result.failure_code == "attestation_failed"


# =============================================================================
# verify_substrate_claim — happy paths and cache semantics
# =============================================================================


def test_happy_path_pins_cache_on_first_verified_connect() -> None:
    claim = _make_claim()
    pa = _fake_pa(
        label=claim.expected_launchd_label,
        executable=claim.expected_executable_path,
        start_time=1_777_000_000,
    )
    cache = VerifiedPairsCache()

    result = verify_substrate_claim(claim, peer_pid=1234, pa_module=pa, cache=cache)

    assert result.accepted is True
    assert result.failure_code is None
    assert cache.get(claim.agent_id) == (1234, 1_777_000_000)


def test_same_pid_same_start_time_is_noop_match() -> None:
    """Reconnect under the same process: still accepted; cache stays put."""
    claim = _make_claim()
    pa = _fake_pa(
        label=claim.expected_launchd_label,
        executable=claim.expected_executable_path,
        start_time=1_777_000_000,
    )
    cache = VerifiedPairsCache()
    cache.record(claim.agent_id, 1234, 1_777_000_000)

    result = verify_substrate_claim(claim, peer_pid=1234, pa_module=pa, cache=cache)

    assert result.accepted is True
    assert cache.get(claim.agent_id) == (1234, 1_777_000_000)


def test_pid_reuse_rejection_for_same_pid_different_start_time() -> None:
    """Q3(e) PID-reuse: same PID, different start_tvsec → reject."""
    claim = _make_claim()
    pa = _fake_pa(
        label=claim.expected_launchd_label,
        executable=claim.expected_executable_path,
        start_time=1_777_000_999,  # different from cached
    )
    cache = VerifiedPairsCache()
    cache.record(claim.agent_id, 1234, 1_777_000_000)  # earlier process

    result = verify_substrate_claim(claim, peer_pid=1234, pa_module=pa, cache=cache)

    assert result.accepted is False
    assert result.failure_code == "pid_reuse"
    assert "1_777_000_000" not in result.reason  # underscore form not used
    assert "1777000000" in result.reason
    assert "1777000999" in result.reason
    # Cache must NOT be updated on rejection.
    assert cache.get(claim.agent_id) == (1234, 1_777_000_000)


def test_legitimate_restart_with_different_pid_accepted_and_cache_updated() -> None:
    """Resident restarted; new PID. Cache update with new (pid, start)."""
    claim = _make_claim()
    pa = _fake_pa(
        label=claim.expected_launchd_label,
        executable=claim.expected_executable_path,
        start_time=1_777_001_000,  # later, new process
    )
    cache = VerifiedPairsCache()
    cache.record(claim.agent_id, 1234, 1_777_000_000)  # prior process

    result = verify_substrate_claim(claim, peer_pid=5678, pa_module=pa, cache=cache)

    assert result.accepted is True
    assert cache.get(claim.agent_id) == (5678, 1_777_001_000)


def test_cache_optional_verify_works_without_cache() -> None:
    """``cache=None`` is allowed; verify still completes with accept/reject."""
    claim = _make_claim()
    pa = _fake_pa(
        label=claim.expected_launchd_label,
        executable=claim.expected_executable_path,
        start_time=1_777_000_000,
    )

    result = verify_substrate_claim(claim, peer_pid=1234, pa_module=pa, cache=None)

    assert result.accepted is True
    assert result.failure_code is None


def test_check_order_label_then_exec_then_start() -> None:
    """When multiple things are wrong, label-mismatch reports first.

    Verifies the proposal's documented check order: cheap checks first, so
    a mismatched-everything peer gets the label-mismatch error (not a
    confusing exec-mismatch on top of an already-bad label).
    """
    claim = _make_claim()
    pa = _fake_pa(
        label="wrong-label",
        executable="wrong-path",
        start_time=None,
    )
    result = verify_substrate_claim(claim, peer_pid=1234, pa_module=pa)
    assert result.failure_code == "label_mismatch"

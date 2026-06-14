"""Tests for the helper that turns check results into Vigil's state-dict fragments.

This is the glue between the registry-based checks and the existing state file
format. The helper must preserve the keys that run_cycle and _handle_cycle_result
already depend on (governance_healthy, lumen_healthy, gov_up_cycles, ...) while
also merging plugin-persistent data from result.detail (e.g. lumen_last_ok_url).
"""

from __future__ import annotations

import pytest

from agents.vigil.checks.base import CheckResult


class _FakeCheck:
    def __init__(self, name, service_key):
        self.name = name
        self.service_key = service_key


def test_governance_ok_populates_healthy_and_uptime():
    from agents.vigil.agent import _collect_health_state

    gov_check = _FakeCheck("governance_health", "governance")
    gov_result = CheckResult(ok=True, summary="Governance: ok (12ms)")

    state = _collect_health_state([(gov_check, gov_result)], prev_state={})
    assert state["governance_healthy"] is True
    assert state["governance_detail"] == "Governance: ok (12ms)"
    assert state["gov_up_cycles"] == 1


def test_governance_unhealthy_does_not_count_uptime():
    from agents.vigil.agent import _collect_health_state

    gov_check = _FakeCheck("governance_health", "governance")
    gov_result = CheckResult(
        ok=False, summary="Governance: UNHEALTHY", severity="critical",
        fingerprint_key="governance_down",
    )
    state = _collect_health_state([(gov_check, gov_result)], prev_state={"gov_up_cycles": 10})
    assert state["governance_healthy"] is False
    assert state["gov_up_cycles"] == 10  # not incremented


def test_no_lumen_check_defaults_to_healthy_and_no_streak():
    """Agnostic user (no anima-mcp plugin): Vigil must still produce sensible state."""
    from agents.vigil.agent import _collect_health_state

    gov_check = _FakeCheck("governance_health", "governance")
    gov_result = CheckResult(ok=True, summary="Governance: ok")

    state = _collect_health_state([(gov_check, gov_result)], prev_state={})
    # Lumen defaults: healthy=True so uptime counter progresses but streak stays 0
    assert state["lumen_healthy"] is True
    assert state["lumen_down_streak"] == 0
    # No lumen_last_ok_url since no plugin to populate it
    assert "lumen_last_ok_url" not in state


def test_lumen_result_detail_merges_into_state():
    """LumenHealth plugin stores lumen_last_ok_url in result.detail — must persist."""
    from agents.vigil.agent import _collect_health_state

    gov_check = _FakeCheck("governance_health", "governance")
    gov_result = CheckResult(ok=True, summary="Governance: ok")
    lumen_check = _FakeCheck("lumen_health", "lumen")
    lumen_result = CheckResult(
        ok=True,
        summary="Lumen: ok (8ms)",
        detail={"lumen_last_ok_url": "http://192.168.1.165:8766/health"},
    )

    state = _collect_health_state(
        [(gov_check, gov_result), (lumen_check, lumen_result)],
        prev_state={},
    )
    assert state["lumen_last_ok_url"] == "http://192.168.1.165:8766/health"
    assert state["lumen_healthy"] is True
    assert state["lumen_up_cycles"] == 1


def test_lumen_down_streak_increments_when_unhealthy():
    from agents.vigil.agent import _collect_health_state

    gov_check = _FakeCheck("governance_health", "governance")
    gov_result = CheckResult(ok=True, summary="ok")
    lumen_check = _FakeCheck("lumen_health", "lumen")
    lumen_result = CheckResult(
        ok=False, summary="Lumen: UNREACHABLE",
        severity="critical", fingerprint_key="lumen_unreachable",
    )

    state = _collect_health_state(
        [(gov_check, gov_result), (lumen_check, lumen_result)],
        prev_state={"lumen_down_streak": 2},
    )
    assert state["lumen_down_streak"] == 3
    assert state["lumen_healthy"] is False


def test_lumen_down_streak_resets_on_recovery():
    from agents.vigil.agent import _collect_health_state

    gov_check = _FakeCheck("governance_health", "governance")
    gov_result = CheckResult(ok=True, summary="ok")
    lumen_check = _FakeCheck("lumen_health", "lumen")
    lumen_result = CheckResult(ok=True, summary="Lumen: ok")

    state = _collect_health_state(
        [(gov_check, gov_result), (lumen_check, lumen_result)],
        prev_state={"lumen_down_streak": 5},
    )
    assert state["lumen_down_streak"] == 0


def test_arbitrary_service_gets_full_bookkeeping():
    """A deployment-specific health check (not governance/lumen) gets the same
    per-service keys, so it participates in uptime + change-note tracking."""
    from agents.vigil.agent import _collect_health_state

    gov_check = _FakeCheck("governance_health", "governance")
    gov_result = CheckResult(ok=True, summary="ok")
    redis_check = _FakeCheck("redis_health", "redis")
    redis_result = CheckResult(
        ok=False, summary="Redis: UNREACHABLE", severity="critical",
        fingerprint_key="redis_down",
    )

    state = _collect_health_state(
        [(gov_check, gov_result), (redis_check, redis_result)],
        prev_state={"redis_up_cycles": 4, "redis_down_streak": 1},
    )
    assert state["redis_healthy"] is False
    assert state["redis_detail"] == "Redis: UNREACHABLE"
    assert state["redis_up_cycles"] == 4  # not incremented while down
    assert state["redis_down_streak"] == 2

    # Recovery clears the streak and advances uptime.
    state2 = _collect_health_state(
        [(gov_check, gov_result),
         (redis_check, CheckResult(ok=True, summary="Redis: ok"))],
        prev_state={"redis_up_cycles": 4, "redis_down_streak": 2},
    )
    assert state2["redis_healthy"] is True
    assert state2["redis_up_cycles"] == 5
    assert state2["redis_down_streak"] == 0

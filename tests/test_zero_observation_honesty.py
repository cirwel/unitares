"""Zero-observation honesty for the read-only metrics surface.

Dogfood 2026-06-10 (cold-caller probe, trust-contract review): a
zero-observation agent's get_governance_metrics response asserted
`summary: "moderate | building_alone | high basin"`, a mode block
reading the seed vector as "focused independent work", a
`stability.stable=True` Lyapunov analysis, `regime: divergence`, a
seed phi, and unlabeled fleet-wide calibration numbers — all beside an
honest `status: uninitialized` and pending coherence/risk. These values
are functions of the default seed vector: identical for every fresh
agent, zero information about THIS one.

`get_governance_metrics_data` now gates every assessment on the same
`is_uninitialized` flag the status/coherence/risk treatment already
used.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# Settle the handler import chain BEFORE anything pulls
# src.services.runtime_queries: importing it while governance_monitor's
# own import is mid-flight trips the runtime_queries ↔ observability/
# outcome_events cycle (`_build_eisv_semantics` from a partially
# initialized module).
import src.mcp_handlers.core  # noqa: F401  (import-order anchor)


def _server_for(monitor):
    return SimpleNamespace(
        get_or_create_monitor=lambda aid: monitor,
        agent_metadata={},
    )


def _fresh_monitor(agent_id="test-zeroobs-fresh"):
    # Deferred import — runtime_queries participates in an import cycle
    # (its own consumers import it inside functions for the same reason).
    from src.governance_monitor import UNITARESMonitor
    return UNITARESMonitor(agent_id, load_state=False)


@pytest.fixture(autouse=True)
def _no_db_hydration():
    with patch(
        "src.agent_monitor_state.hydrate_from_db_if_fresh",
        new=AsyncMock(return_value=False),
    ):
        yield


@pytest.mark.asyncio
async def test_uninitialized_full_shape_withholds_assessments():
    monitor = _fresh_monitor()
    from src.services.runtime_queries import get_governance_metrics_data
    data = await get_governance_metrics_data(
        "test-zeroobs-fresh", {"lite": False}, server=_server_for(monitor)
    )

    assert data["summary"] == "uninitialized | no observations yet"
    assert data["state"]["status"] == "pending (first check-in required)"
    # No fabricated interpretation keys on the pending state block.
    for key in ("health", "mode", "basin", "trajectory", "guidance"):
        assert key not in data["state"]
    # Seed-derived analysis blocks are withheld, not reported as measurement.
    # stability is ALWAYS present in the full shape (get_monitor_metrics
    # emits it unconditionally) — assert presence so a regression that
    # drops the block entirely cannot silently skip these checks.
    assert "stability" in data
    assert data["stability"]["status"] == "pending (first check-in required)"
    assert "stable" not in data["stability"]
    assert data.get("regime") is None
    assert data.get("phi") is None
    # The nested ode_diagnostics block must not contradict the top-level
    # nulls with seed values (review fold).
    ode_diag = data.get("ode_diagnostics")
    if isinstance(ode_diag, dict):
        assert ode_diag.get("phi") is None
        assert ode_diag.get("regime") is None
    v41 = data.get("unitares_v41")
    if isinstance(v41, dict):
        assert v41.get("basin") is None
    # Fleet-scoped calibration has no place in a zero-history response.
    assert "calibration_feedback" not in data


@pytest.mark.asyncio
async def test_uninitialized_lite_shape_withholds_mode_and_basin():
    monitor = _fresh_monitor()
    from src.services.runtime_queries import get_governance_metrics_data
    data = await get_governance_metrics_data(
        "test-zeroobs-fresh", {"lite": True}, server=_server_for(monitor)
    )

    assert data["status"] == "⚪ uninitialized"
    assert data["summary"] == "uninitialized | no observations yet"
    assert "mode" not in data
    assert "basin" not in data
    # The existing honest fields stay honest.
    assert data["verdict"]["value"] == "uninitialized"
    assert data["guidance"] == "Submit one check-in to activate governance."
    assert data["coherence"]["value"] is None
    assert data["risk_score"]["value"] is None


@pytest.mark.asyncio
async def test_uninitialized_standard_shape_withholds_mode_and_basin():
    monitor = _fresh_monitor()
    from src.services.runtime_queries import get_governance_metrics_data
    data = await get_governance_metrics_data(
        "test-zeroobs-fresh", {"verbosity": "standard"}, server=_server_for(monitor)
    )

    assert data["summary"] == "uninitialized | no observations yet"
    assert data["guidance"] == "Submit one check-in to activate governance."
    assert "basin" not in data
    assert "mode" not in data


@pytest.mark.asyncio
async def test_initialized_agent_keeps_interpretation():
    """Regression guard: one real check-in restores the full
    interpretation surface — the gate must not over-suppress."""
    monitor = _fresh_monitor("test-zeroobs-active")
    monitor.process_update({
        "response_text": "Working through the zero-observation honesty fix.",
        "complexity": 0.5,
    })

    from src.services.runtime_queries import get_governance_metrics_data
    data = await get_governance_metrics_data(
        "test-zeroobs-active", {"lite": False}, server=_server_for(monitor)
    )

    assert " | " in data["summary"]
    assert data["summary"].endswith("basin")
    for key in ("health", "mode", "basin", "trajectory"):
        assert key in data["state"]
    assert data.get("phi") is not None
    if "stability" in data:
        assert "stable" in data["stability"]

    lite = await get_governance_metrics_data(
        "test-zeroobs-active", {"lite": True}, server=_server_for(monitor)
    )
    assert "mode" in lite
    assert "basin" in lite


def test_fleet_calibration_feedback_carries_scope_label():
    """The fleet-wide calibration numbers must self-identify as fleet
    data even when the cache-gated explanatory message is absent."""
    from src.mcp_handlers.introspection.feedback import get_calibration_feedback

    fake_metrics = {
        "bins": {
            "0.8-1.0": {"count": 10, "accuracy": 0.9, "expected_accuracy": 0.95},
        }
    }
    with patch(
        "src.calibration.calibration_checker.check_calibration",
        return_value=(False, fake_metrics),
    ):
        feedback = get_calibration_feedback(include_complexity=False)

    assert feedback["confidence"]["scope"] == "fleet"
    assert "system_accuracy" in feedback["confidence"]

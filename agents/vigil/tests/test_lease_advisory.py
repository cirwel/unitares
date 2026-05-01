"""Phase A advisory-mode lease-plane wiring on Vigil's run_cycle (RFC v0.5 §6.1).

These tests verify the wiring without depending on the full cycle running —
the advisory lease is acquired before any cycle work, so we can short-circuit
the body via a raising fake scope and inspect the kwargs passed to
``lease_advisory_scope``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
VIGIL_PATH = REPO_ROOT / "agents" / "vigil" / "agent.py"
sys.path.insert(0, str(REPO_ROOT))


def _load_vigil_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "vigil_agent_lease_under_test", VIGIL_PATH
    )
    assert spec and spec.loader, f"cannot load {VIGIL_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def vigil_module() -> ModuleType:
    return _load_vigil_module()


@pytest.fixture
def agent(vigil_module, tmp_path, monkeypatch):
    log_file = tmp_path / "vigil.log"
    session_file = tmp_path / "vigil_session.json"
    state_file = tmp_path / "vigil_state.json"
    monkeypatch.setattr(vigil_module, "LOG_FILE", log_file)
    monkeypatch.setattr(vigil_module, "SESSION_FILE", session_file)
    monkeypatch.setattr(vigil_module, "STATE_FILE", state_file)
    return vigil_module.VigilAgent(
        mcp_url="http://localhost:65535/mcp/",
        label="VigilTest",
        force_new=False,
    )


@pytest.mark.asyncio
async def test_run_cycle_invokes_lease_advisory_with_expected_surface(
    vigil_module, agent, monkeypatch
):
    """Surface contract — locked down so a future refactor that renames the
    surface gets a noisy test failure instead of silent telemetry drift."""
    from src.lease_plane import advisory as advisory_module

    captured: dict = {}

    @contextlib.contextmanager
    def fake_scope(**kwargs):
        captured.update(kwargs)
        # Short-circuit the body — we're testing the wrapper invocation,
        # not the cycle internals (those have their own test suite).
        raise _CycleStopMarker()
        yield  # noqa: unreachable — keep the contextmanager shape valid

    monkeypatch.setattr(advisory_module, "lease_advisory_scope", fake_scope)

    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)

    client = AsyncMock()
    with pytest.raises(_CycleStopMarker):
        await agent.run_cycle(client)

    assert captured["surface_id"] == "vigil:cycle"
    assert captured["surface_kind"] == "vigil_cycle"
    assert captured["ttl_s"] == 300
    assert captured.get("intent") == "vigil heartbeat cycle"


@pytest.mark.asyncio
async def test_run_cycle_runs_when_lease_held_by_other(
    vigil_module, agent, monkeypatch
):
    """Phase A non-enforcement: held_by_other MUST NOT block the cycle.
    The body runs identically to no-lease conditions."""
    from src.lease_plane import advisory as advisory_module

    @contextlib.contextmanager
    def held_by_other_scope(**_kwargs):
        # advisory_scope yields (outcome, lease_id_or_none) under contention
        yield ("held_by_other", None)

    monkeypatch.setattr(advisory_module, "lease_advisory_scope", held_by_other_scope)
    monkeypatch.setattr(agent, "_run_cycle_inner", AsyncMock(return_value="cycle ran"))

    client = AsyncMock()
    result = await agent.run_cycle(client)

    assert result == "cycle ran"
    agent._run_cycle_inner.assert_awaited_once_with(client)


@pytest.mark.asyncio
async def test_run_cycle_runs_when_lease_service_unavailable(
    vigil_module, agent, monkeypatch
):
    """Disabled-client / governance-down path: the wrapper returns
    service_unavailable and the cycle body still runs."""
    from src.lease_plane import advisory as advisory_module

    @contextlib.contextmanager
    def service_unavailable_scope(**_kwargs):
        yield ("service_unavailable", None)

    monkeypatch.setattr(advisory_module, "lease_advisory_scope", service_unavailable_scope)
    monkeypatch.setattr(agent, "_run_cycle_inner", AsyncMock(return_value="cycle ran"))

    client = AsyncMock()
    result = await agent.run_cycle(client)

    assert result == "cycle ran"


class _CycleStopMarker(Exception):
    """Sentinel exception used to short-circuit the cycle body in tests."""

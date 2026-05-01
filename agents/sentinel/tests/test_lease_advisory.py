"""Phase A advisory-mode lease wiring on Sentinel's run_cycle (RFC v0.5 §6.1).

Mirrors the Vigil regression-lock pattern: verify the wrapper invocation
without depending on the full cycle running. Sentinel's cycle internals
have their own test suite; here we only assert the wrapping shape.
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
SENTINEL_PATH = REPO_ROOT / "agents" / "sentinel" / "agent.py"
sys.path.insert(0, str(REPO_ROOT))


def _load_sentinel_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "sentinel_agent_lease_under_test", SENTINEL_PATH
    )
    assert spec and spec.loader, f"cannot load {SENTINEL_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sentinel_module() -> ModuleType:
    return _load_sentinel_module()


@pytest.fixture
def agent(sentinel_module, tmp_path, monkeypatch):
    log_file = tmp_path / "sentinel.log"
    session_file = tmp_path / "sentinel_session.json"
    monkeypatch.setattr(sentinel_module, "LOG_FILE", log_file)
    monkeypatch.setattr(sentinel_module, "SESSION_FILE", session_file)
    return sentinel_module.SentinelAgent(
        mcp_url="http://localhost:65535/mcp/",
        label="SentinelTest",
    )


@pytest.mark.asyncio
async def test_run_cycle_invokes_lease_advisory_with_expected_surface(
    sentinel_module, agent, monkeypatch
):
    """Surface contract — locked down so a future refactor renaming
    surface_id/kind/ttl_s gets a noisy test failure, not silent telemetry drift."""
    from src.lease_plane import advisory as advisory_module

    captured: dict = {}

    @contextlib.contextmanager
    def fake_scope(**kwargs):
        captured.update(kwargs)
        raise _CycleStopMarker()
        yield  # noqa: unreachable — keep the contextmanager shape valid

    monkeypatch.setattr(advisory_module, "lease_advisory_scope", fake_scope)
    monkeypatch.delenv("LEASE_PLANE_BEARER_TOKEN", raising=False)

    with pytest.raises(_CycleStopMarker):
        await agent.run_cycle()

    assert captured["surface_id"] == "resident:/sentinel_cycle"
    # surface_kind no longer passed (PR 2.5 — derived server-side from scheme prefix
    # via migration 026's generated column).
    assert "surface_kind" not in captured
    assert captured["ttl_s"] == 300
    assert captured.get("intent") == "sentinel analysis cycle"


@pytest.mark.asyncio
async def test_run_cycle_runs_when_lease_held_by_other(
    sentinel_module, agent, monkeypatch
):
    """Phase A non-enforcement invariant — body runs regardless of lease."""
    from src.lease_plane import advisory as advisory_module

    @contextlib.contextmanager
    def held_by_other_scope(**_kwargs):
        yield ("held_by_other", None)

    monkeypatch.setattr(advisory_module, "lease_advisory_scope", held_by_other_scope)
    monkeypatch.setattr(agent, "_run_cycle_inner", AsyncMock(return_value="cycle ran"))

    result = await agent.run_cycle()

    assert result == "cycle ran"
    agent._run_cycle_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_cycle_runs_when_lease_service_unavailable(
    sentinel_module, agent, monkeypatch
):
    """service_unavailable (governance down / unconfigured) — body runs."""
    from src.lease_plane import advisory as advisory_module

    @contextlib.contextmanager
    def service_unavailable_scope(**_kwargs):
        yield ("service_unavailable", None)

    monkeypatch.setattr(advisory_module, "lease_advisory_scope", service_unavailable_scope)
    monkeypatch.setattr(agent, "_run_cycle_inner", AsyncMock(return_value="cycle ran"))

    result = await agent.run_cycle()

    assert result == "cycle ran"


class _CycleStopMarker(Exception):
    """Sentinel exception used to short-circuit the cycle body in tests."""

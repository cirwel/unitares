"""Phase A advisory-mode lease wiring on Chronicler's run_cycle (RFC v0.5 §6.1).

Verifies the wrapper is invoked with the agreed surface contract and that
Phase A non-enforcement holds — the body runs even when held_by_other or
service_unavailable.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def agent(tmp_path, monkeypatch):
    from agents.chronicler.agent import ChroniclerAgent

    return ChroniclerAgent(
        base_url="http://localhost:65535",
        token=None,
        repo_root=tmp_path,
        dry_run=True,
    )


@pytest.mark.asyncio
async def test_run_cycle_invokes_lease_advisory_with_expected_surface(
    agent, monkeypatch
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
        await agent.run_cycle(client=None)

    assert captured["surface_id"] == "resident:/chronicler_scrape"
    # surface_kind no longer passed (PR 2.5 — derived server-side from scheme prefix
    # via migration 026's generated column); should not appear in kwargs.
    assert "surface_kind" not in captured
    assert captured["ttl_s"] == 120
    assert captured.get("intent") == "chronicler daily scrape"


@pytest.mark.asyncio
async def test_run_cycle_runs_when_lease_held_by_other(agent, monkeypatch):
    """Phase A non-enforcement invariant — body runs regardless of lease."""
    from src.lease_plane import advisory as advisory_module

    @contextlib.contextmanager
    def held_by_other_scope(**_kwargs):
        yield ("held_by_other", None)

    monkeypatch.setattr(advisory_module, "lease_advisory_scope", held_by_other_scope)
    monkeypatch.setattr(agent, "_run_cycle_inner", AsyncMock(return_value="cycle ran"))

    result = await agent.run_cycle(client=None)

    assert result == "cycle ran"
    agent._run_cycle_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_cycle_runs_when_lease_service_unavailable(agent, monkeypatch):
    """service_unavailable (governance down / unconfigured) — body runs."""
    from src.lease_plane import advisory as advisory_module

    @contextlib.contextmanager
    def service_unavailable_scope(**_kwargs):
        yield ("service_unavailable", None)

    monkeypatch.setattr(advisory_module, "lease_advisory_scope", service_unavailable_scope)
    monkeypatch.setattr(agent, "_run_cycle_inner", AsyncMock(return_value="cycle ran"))

    result = await agent.run_cycle(client=None)

    assert result == "cycle ran"


class _CycleStopMarker(Exception):
    """Sentinel exception used to short-circuit the cycle body in tests."""

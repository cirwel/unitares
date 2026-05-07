"""locked_update overhead fix PR #2 — baseline preload moved out of the lock.

`ensure_baseline_loaded(agent_id)` was previously awaited inside
`execute_locked_update` (phases.py:833), holding the per-agent lock across
the cold-start PostgreSQL roundtrip. The function has an in-memory
`_baselines` cache, so calling it ahead of time in `prepare_unlocked_inputs`
primes the cache; the in-lock call then hits the cache instead of waiting
on PG. Estimated savings: ~500ms on first call per agent (cache miss).

This is the same fix-shape as PR #360's `_hydrate_metadata_cache_async` —
sequential PG awaits in our own loop moved outside the critical section,
NOT an anyio/asyncio coupling pattern. Per `project_locked-update-overhead-
fix.md` Falsifying Evidence: if `locked_update` stays >5s after this lands,
the bottleneck is somewhere else (most likely the ODE update itself).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


def _make_minimal_ctx(agent_id: str = "test-agent-uuid"):
    """Build the smallest UpdateContext that lets prepare_unlocked_inputs
    run end-to-end without touching the behavioral sensor branch (which
    requires monitor.state.decision_history >= 3) or grounding policy
    that touches files."""
    from src.mcp_handlers.updates.context import UpdateContext

    ctx = UpdateContext()
    ctx.agent_id = agent_id
    ctx.label = "test_label"
    ctx.arguments = {}
    ctx.ethical_drift = []
    ctx.response_text = "hello"
    ctx.complexity = 0.0
    ctx.dialectic_enforcement_warning = None

    # mcp_server with empty monitors so the behavioral_sensor branch is skipped
    ctx.mcp_server = MagicMock()
    ctx.mcp_server.monitors = {}  # .get(agent_id) returns None
    return ctx


@pytest.mark.asyncio
async def test_prepare_unlocked_primes_baseline_cache():
    """`prepare_unlocked_inputs` MUST await `ensure_baseline_loaded` so the
    in-lock call at execute_locked_update:833 hits the cache instead of a
    cold PG roundtrip."""
    from src.mcp_handlers.updates import phases

    ctx = _make_minimal_ctx()

    fake_baseline_loader = AsyncMock(return_value=MagicMock())
    with patch(
        "src.agent_behavioral_baseline.ensure_baseline_loaded",
        fake_baseline_loader,
    ):
        await phases.prepare_unlocked_inputs(ctx)

    fake_baseline_loader.assert_awaited_with(ctx.agent_id)


@pytest.mark.asyncio
async def test_baseline_preload_failure_does_not_break_prepare_unlocked():
    """If the baseline loader raises (PG offline, cold-start before migration,
    etc.), prepare_unlocked MUST still return cleanly. Baseline failure is
    recoverable: the in-lock anomaly check has its own try/except already
    (phases.py:828-872) and just skips entropy injection."""
    from src.mcp_handlers.updates import phases

    ctx = _make_minimal_ctx()

    fake_baseline_loader = AsyncMock(side_effect=RuntimeError("PG offline"))
    with patch(
        "src.agent_behavioral_baseline.ensure_baseline_loaded",
        fake_baseline_loader,
    ):
        # MUST NOT raise — failure is logged at debug and prepare_unlocked
        # falls through to policy checks etc.
        await phases.prepare_unlocked_inputs(ctx)

    fake_baseline_loader.assert_awaited_once_with(ctx.agent_id)


@pytest.mark.asyncio
async def test_baseline_preload_runs_unlocked():
    """Pin that the preload happens during prepare_unlocked, not during
    execute_locked_update. We assert this indirectly: by the time
    prepare_unlocked returns, ensure_baseline_loaded has been called,
    so any subsequent in-lock call is a cache hit (no second PG roundtrip)."""
    from src.mcp_handlers.updates import phases

    ctx = _make_minimal_ctx()

    call_order = []

    async def loader(agent_id):
        call_order.append(("loader", agent_id))
        return MagicMock()

    with patch(
        "src.agent_behavioral_baseline.ensure_baseline_loaded",
        side_effect=loader,
    ):
        await phases.prepare_unlocked_inputs(ctx)

    # Loader was called exactly once during prepare_unlocked
    assert len(call_order) == 1
    assert call_order[0] == ("loader", ctx.agent_id)

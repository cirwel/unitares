"""Tests for reconstruct_eisv_series — the StateMixin method that R1's
score_trajectory_continuity primitive (PR 2) consumes.

v3.1 ("New helper") and §v3.3-I
(table name + column corrections). The helper is a normal async mixin method
using the existing self.acquire() pool pattern; the anyio safety wrapper lives
at the handler layer, not here (consistent with sibling methods like
get_latest_agent_state and record_agent_state).
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_reconstruct_eisv_series_exists_on_state_mixin():
    """Contract: StateMixin exposes reconstruct_eisv_series.

    Fails (AttributeError) until the method is added in src/db/mixins/state.py.
    """
    from src.db.mixins.state import StateMixin
    assert hasattr(StateMixin, "reconstruct_eisv_series"), (
        "StateMixin must expose reconstruct_eisv_series for R1 score_trajectory_continuity"
    )


@pytest.mark.asyncio
async def test_reconstruct_eisv_series_maps_rows_to_per_dim_lists():
    """Behavior: rows in window get bucketed into {E, I, S, V} → list[float]
    in recorded_at-ascending order.

    Verifies dimension-key mapping per db/base.py:50-57:
      state_json.E → E, integrity → I, entropy → S, volatility → V.
    The stability_index column was retired in commit 20684dd1 (2026-03-26)
    and is no longer read. The earlier version of this test had E and S
    swapped — entropy was mapped to E and the dead stability_index column
    was mapped to S — which silently produced wrong R1 components for ~2
    months until the reader was corrected.
    """
    from src.db.mixins.state import StateMixin

    class _Stub(StateMixin):
        def acquire(self):
            return _AcquireCtx()

    class _AcquireCtx:
        async def __aenter__(self):
            conn = AsyncMock()
            # Three rows ordered ascending by recorded_at; columns match the
            # SQL projection in the helper. Distinct values per channel let
            # any mismapping surface in the assertions below.
            conn.fetch = AsyncMock(return_value=[
                _row(entropy=0.5, integrity=0.6, volatility=0.1,
                     state_json={"E": 0.9}),
                _row(entropy=0.6, integrity=0.7, volatility=0.2,
                     state_json={"E": 0.85}),
                _row(entropy=0.7, integrity=0.8, volatility=0.3,
                     state_json={"E": 0.8}),
            ])
            return conn

        async def __aexit__(self, *args):
            return None

    backend = _Stub()
    result = await backend.reconstruct_eisv_series(
        agent_id="test-uuid",
        window=timedelta(days=30),
        epoch=3,
    )

    assert set(result.keys()) == {"E", "I", "S", "V"}
    assert result["E"] == [0.9, 0.85, 0.8]
    assert result["I"] == [0.6, 0.7, 0.8]
    assert result["S"] == [0.5, 0.6, 0.7]
    assert result["V"] == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_reconstruct_eisv_series_empty_when_no_rows():
    """Behavior: empty per-dim lists when no rows in window.

    Caller (score_trajectory_continuity, PR 2) treats empty as 'skip dimension
    from average' per v3.3-H.C4. Empty here is the helper's responsibility to
    return; the skip-from-average decision is the caller's.
    """
    from src.db.mixins.state import StateMixin

    class _Stub(StateMixin):
        def acquire(self):
            return _EmptyAcquire()

    class _EmptyAcquire:
        async def __aenter__(self):
            conn = AsyncMock()
            conn.fetch = AsyncMock(return_value=[])
            return conn

        async def __aexit__(self, *args):
            return None

    backend = _Stub()
    result = await backend.reconstruct_eisv_series(
        agent_id="no-rows-uuid",
        window=timedelta(days=30),
        epoch=3,
    )

    assert set(result.keys()) == {"E", "I", "S", "V"}
    assert all(v == [] for v in result.values())


@pytest.mark.asyncio
async def test_reconstruct_eisv_series_uses_current_epoch_when_unspecified():
    """Behavior: epoch defaults to GovernanceConfig.CURRENT_EPOCH when not passed.

    Verifies the SQL receives the current-epoch param (not a literal/wrong value).
    """
    from src.db.mixins.state import StateMixin
    from config.governance_config import GovernanceConfig

    captured_args: list = []

    class _Stub(StateMixin):
        def acquire(self):
            return _CapturingAcquire(captured_args)

    class _CapturingAcquire:
        def __init__(self, sink):
            self._sink = sink

        async def __aenter__(self):
            conn = AsyncMock()

            async def _fetch(sql, *args):
                self._sink.append(args)
                return []

            conn.fetch = _fetch
            return conn

        async def __aexit__(self, *args):
            return None

    backend = _Stub()
    await backend.reconstruct_eisv_series(
        agent_id="test-uuid",
        window=timedelta(days=30),
        # epoch not passed
    )

    assert len(captured_args) == 1
    args = captured_args[0]
    # Args order per helper SQL: (agent_id, epoch, window). Lock all three
    # positions so a parameter-order refactor surfaces in this test.
    assert args[0] == "test-uuid"
    assert args[1] == GovernanceConfig.CURRENT_EPOCH
    assert args[2] == timedelta(days=30)


def _row(**fields):
    """Build a dict-like row stub.

    Returns a plain dict supporting subscript access (`row["entropy"]`) only,
    which matches how `reconstruct_eisv_series` reads asyncpg Records. asyncpg
    Records also support attribute-style access (`row.entropy`); if a future
    author adds attribute-style access to the production code, this helper
    must change accordingly.
    """
    return fields

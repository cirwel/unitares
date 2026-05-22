"""Unit coverage for StateMixin agent-state read contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest


def _state_row(**overrides):
    row = {
        "state_id": 11,
        "identity_id": 22,
        "agent_id": "agent-state-mixin",
        "recorded_at": datetime.now(timezone.utc),
        "entropy": 0.2,
        "integrity": 0.8,
        "stability_index": 0.0,
        "volatility": 0.1,
        "regime": "nominal",
        "coherence": 0.7,
        "state_json": {"E": 0.6},
        "epistemic_class": "agent_report",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_all_latest_falls_back_when_matview_lacks_epistemic_class():
    """Migration 040 avoids DROP/CREATE on the matview.

    Existing deployments may still have a materialized view without
    epistemic_class. The read path should catch that failure and use the base
    table projection, preserving the forward-only label from the base row.
    """
    from src.db.mixins.state import StateMixin

    calls = []

    class _Stub(StateMixin):
        def acquire(self):
            return _Acquire()

    class _Acquire:
        async def __aenter__(self):
            conn = AsyncMock()

            async def _fetch(sql, *args):
                calls.append(sql)
                if "FROM core.mv_latest_agent_states" in sql:
                    raise Exception('column "epistemic_class" does not exist')
                return [_state_row(epistemic_class="substrate_interpretation")]

            conn.fetch = _fetch
            return conn

        async def __aexit__(self, *args):
            return None

    rows = await _Stub().get_all_latest_agent_states()

    assert len(rows) == 1
    assert rows[0].epistemic_class == "substrate_interpretation"
    assert rows[0].state_json["epistemic_class"] == "substrate_interpretation"
    assert any("core.mv_latest_agent_states" in sql for sql in calls)
    assert any("FROM core.agent_state s" in sql for sql in calls)

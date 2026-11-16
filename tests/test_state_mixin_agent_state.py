"""Unit coverage for StateMixin agent-state read contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
        "volatility": 0.1,
        "regime": "nominal",
        "coherence": 0.7,
        "state_json": {"E": 0.6},
        "epistemic_class": "agent_report",
    }
    row.update(overrides)
    return row


def _stub_with_fetch(fetch):
    from src.db.mixins.state import StateMixin

    class _Acquire:
        async def __aenter__(self):
            conn = AsyncMock()
            conn.fetch = fetch
            return conn

        async def __aexit__(self, *args):
            return None

    class _Stub(StateMixin):
        def acquire(self):
            return _Acquire()

    return _Stub()


@pytest.mark.asyncio
async def test_all_latest_reads_base_table_through_the_index_not_the_matview():
    """The read must not depend on the matview's projection or freshness.

    The matview has no epistemic_class column on deployments that predate
    migration 040. Selecting it there raised on every call and sent every call
    to a DISTINCT ON sort of all of core.agent_state. One lateral probe per
    identity returns the same rows from the base table.
    """
    from config.governance_config import GovernanceConfig

    calls = []

    async def _fetch(sql, *args):
        calls.append((sql, args))
        return [_state_row(epistemic_class="substrate_interpretation")]

    rows = await _stub_with_fetch(_fetch).get_all_latest_agent_states()

    assert len(rows) == 1
    assert rows[0].epistemic_class == "substrate_interpretation"
    assert rows[0].state_json["epistemic_class"] == "substrate_interpretation"
    assert len(calls) == 1
    sql, args = calls[0]
    assert "mv_latest_agent_states" not in sql
    assert "DISTINCT ON" not in sql
    assert "CROSS JOIN LATERAL" in sql
    assert "a.synthetic = false" in sql
    assert "ORDER BY a.recorded_at DESC" in sql
    assert "LIMIT 1" in sql
    assert "s.epoch = $1" in sql
    assert args == (GovernanceConfig.CURRENT_EPOCH,)


def test_server_no_longer_reads_or_refreshes_the_latest_state_matview():
    """Nothing in src/ may read or refresh core.mv_latest_agent_states.

    The periodic refresh re-sorted and rewrote every state row once a minute.
    A new reader would silently get data nothing keeps current.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    offenders = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if any(
            "mv_latest_agent_states" in line and not line.lstrip().startswith("#")
            and "core.mv_latest_agent_states`" not in line
            for line in path.read_text().splitlines()
        )
    ]
    assert offenders == []


@pytest.mark.asyncio
async def test_reconstruct_eisv_series_column_to_dimension_mapping():
    """Per db/base.py:50-57 the column↔EISV mapping is:
    state_json.E → E, integrity → I, entropy → S, volatility → V.

    Pre-fix the reader read entropy as E and the dead stability_index column
    as S; this test would have failed under that mapping for E (got 0.2,
    expected 0.6) and for S (got 0.0, expected 0.2).
    """
    from src.db.mixins.state import StateMixin

    class _Stub(StateMixin):
        def acquire(self):
            return _Acquire()

    class _Acquire:
        async def __aenter__(self):
            conn = AsyncMock()

            async def _fetch(sql, *args):
                return [
                    _state_row(
                        entropy=0.2, integrity=0.8, volatility=0.1,
                        state_json={"E": 0.6}, ),
                ]

            conn.fetch = _fetch
            return conn

        async def __aexit__(self, *args):
            return None

    series = await _Stub().reconstruct_eisv_series(
        agent_id="agent-state-mixin", window=timedelta(hours=1), epoch=1,
    )

    assert series["E"] == [0.6]
    assert series["I"] == [0.8]
    assert series["S"] == [0.2]
    assert series["V"] == [0.1]


@pytest.mark.asyncio
async def test_reconstruct_eisv_series_handles_state_json_as_string():
    """asyncpg can return state_json as either a dict or a JSON string
    depending on codec registration. The reader must tolerate both
    (mirrors _row_to_agent_state at db/mixins/state.py:425-444).
    """
    from src.db.mixins.state import StateMixin

    class _Stub(StateMixin):
        def acquire(self):
            return _Acquire()

    class _Acquire:
        async def __aenter__(self):
            conn = AsyncMock()

            async def _fetch(sql, *args):
                return [_state_row(state_json='{"E": 0.42}')]

            conn.fetch = _fetch
            return conn

        async def __aexit__(self, *args):
            return None

    series = await _Stub().reconstruct_eisv_series(
        agent_id="agent-state-mixin", window=timedelta(hours=1), epoch=1,
    )

    assert series["E"] == [0.42]


@pytest.mark.asyncio
async def test_reconstruct_eisv_series_defaults_missing_e_to_neutral():
    """When state_json lacks an 'E' key, default to 0.5 (matches
    _row_to_agent_state.energy default in db/base.py:63).
    """
    from src.db.mixins.state import StateMixin

    class _Stub(StateMixin):
        def acquire(self):
            return _Acquire()

    class _Acquire:
        async def __aenter__(self):
            conn = AsyncMock()

            async def _fetch(sql, *args):
                return [_state_row(state_json={})]

            conn.fetch = _fetch
            return conn

        async def __aexit__(self, *args):
            return None

    series = await _Stub().reconstruct_eisv_series(
        agent_id="agent-state-mixin", window=timedelta(hours=1), epoch=1,
    )

    assert series["E"] == [0.5]

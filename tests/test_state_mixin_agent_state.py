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
async def test_all_latest_reads_matview_without_selecting_epistemic_class_from_it():
    """Migration 040 avoids DROP/CREATE on the matview.

    Existing deployments keep a materialized view without epistemic_class.
    Selecting that column from the matview failed on every call and sent every
    call to the base-table sort. The read takes the label and the epoch filter
    from the base row by primary key, so one query serves either projection.
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
    assert len(calls) == 1, "the matview read must not fall through to the base table"
    sql, args = calls[0]
    assert "FROM core.mv_latest_agent_states m" in sql
    assert "JOIN core.agent_state s ON s.state_id = m.state_id" in sql
    assert "s.epistemic_class" in sql
    assert "m.epistemic_class" not in sql
    assert "DISTINCT ON" not in sql
    assert "s.epoch = $1" in sql
    assert args == (GovernanceConfig.CURRENT_EPOCH,)


@pytest.mark.asyncio
async def test_all_latest_falls_back_and_warns_once_when_matview_is_missing(
    monkeypatch, caplog
):
    import logging

    from src.db.mixins import state as state_module

    monkeypatch.setattr(state_module, "_LATEST_STATES_FALLBACK_WARNED", False)
    calls = []

    async def _fetch(sql, *args):
        calls.append(sql)
        if "core.mv_latest_agent_states" in sql:
            raise Exception('relation "core.mv_latest_agent_states" does not exist')
        return [_state_row(epistemic_class="substrate_interpretation")]

    stub = _stub_with_fetch(_fetch)
    with caplog.at_level(logging.WARNING):
        first = await stub.get_all_latest_agent_states()
        second = await stub.get_all_latest_agent_states()

    assert [r.epistemic_class for r in first + second] == [
        "substrate_interpretation",
        "substrate_interpretation",
    ]
    fallback_sql = [sql for sql in calls if "DISTINCT ON (s.identity_id)" in sql]
    assert len(fallback_sql) == 2
    assert all("s.synthetic = false" in sql for sql in fallback_sql)
    warnings = [
        r for r in caplog.records if "mv_latest_agent_states unreadable" in r.getMessage()
    ]
    assert len(warnings) == 1


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

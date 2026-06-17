"""Tests for the participated-only filter on IdentityMixin.list_identities.

An identity "participated" iff it has >=1 row in core.agent_state with
synthetic=false (the established measured-only convention). This filter is
view-only: it changes what listings return, never archiving or mutating rows.

These tests drive the mixin against a fake connection that (a) lets us assert
on the generated SQL and (b) simulates the EXISTS(synthetic=false) predicate
against in-memory rows so the three spec cases are covered without a live DB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.db.mixins.identity import IdentityMixin


def _row(agent_id: str, identity_id: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "identity_id": identity_id,
        "agent_id": agent_id,
        "api_key_hash": "h",
        "created_at": now,
        "updated_at": now,
        "status": "active",
        "parent_agent_id": None,
        "spawn_reason": None,
        "disabled_at": None,
        "last_activity_at": now,
        "metadata": {},
    }


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Backend(IdentityMixin):
    """IdentityMixin bound to a fake connection.

    The fake connection models core.identities (``identities``) joined
    against the set of identity_ids that have a measured (synthetic=false)
    agent_state row (``measured_ids``). When the query carries the
    participated EXISTS predicate, only measured identities are returned.
    """

    def __init__(self, identities, measured_ids):
        self._identities = identities
        self._measured_ids = set(measured_ids)
        conn = MagicMock()
        conn.fetch = AsyncMock(side_effect=self._fetch)
        self._conn = conn
        self.last_sql = None

    def acquire(self):
        return _Acquire(self._conn)

    async def _fetch(self, sql, *params):
        self.last_sql = sql
        participated = "s.synthetic = false" in sql
        rows = self._identities
        if participated:
            rows = [r for r in rows if r["identity_id"] in self._measured_ids]
        return rows


@pytest.mark.asyncio
async def test_participated_only_returns_identity_with_measured_state():
    """An identity with a non-synthetic agent_state row IS returned."""
    backend = _Backend([_row("a1", 1)], measured_ids={1})
    result = await backend.list_identities(participated_only=True)
    assert [r.agent_id for r in result] == ["a1"]


@pytest.mark.asyncio
async def test_participated_only_excludes_identity_with_zero_state_rows():
    """An identity with ZERO agent_state rows is NOT returned by default..."""
    backend = _Backend([_row("ghost", 2)], measured_ids=set())
    result = await backend.list_identities(participated_only=True)
    assert result == []


@pytest.mark.asyncio
async def test_unparticipated_returned_when_filter_off():
    """...but IS returned when participated_only=False (opt-out)."""
    backend = _Backend([_row("ghost", 2)], measured_ids=set())
    result = await backend.list_identities(participated_only=False)
    assert [r.agent_id for r in result] == ["ghost"]


@pytest.mark.asyncio
async def test_synthetic_only_identity_treated_as_unparticipated():
    """An identity whose only agent_state row is synthetic=true does not
    appear in measured_ids, so participated_only filters it out."""
    # measured_ids is empty: the synthetic row never satisfies synthetic=false.
    backend = _Backend([_row("boot", 3)], measured_ids=set())
    result = await backend.list_identities(participated_only=True)
    assert result == []


@pytest.mark.asyncio
async def test_predicate_present_only_when_requested():
    """The EXISTS(synthetic=false) predicate is emitted iff participated_only."""
    backend = _Backend([_row("a1", 1)], measured_ids={1})

    await backend.list_identities(participated_only=False)
    assert "s.synthetic = false" not in backend.last_sql

    await backend.list_identities(participated_only=True)
    assert "EXISTS" in backend.last_sql
    assert "s.synthetic = false" in backend.last_sql
    assert "s.identity_id = i.identity_id" in backend.last_sql


@pytest.mark.asyncio
async def test_predicate_lands_under_where_with_status_filter():
    """With a status filter, the predicate appends via AND to the WHERE."""
    backend = _Backend([_row("a1", 1)], measured_ids={1})
    await backend.list_identities(status="active", participated_only=True)
    sql = backend.last_sql
    assert "i.status = $1" in sql
    assert "AND EXISTS" in sql


@pytest.mark.asyncio
async def test_predicate_lands_under_fresh_where_without_status():
    """Without a status filter, the predicate introduces its own WHERE
    (no dangling AND)."""
    backend = _Backend([_row("a1", 1)], measured_ids={1})
    await backend.list_identities(participated_only=True)
    sql = backend.last_sql
    assert " WHERE EXISTS" in sql
    assert "AND EXISTS" not in sql

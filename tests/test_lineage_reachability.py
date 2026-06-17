"""Transitive lineage-succession reachability (de-risk capability, shadow mode).

Pins the recoverable-by-construction contract: any failure yields an EMPTY set
(caller keeps single-hop behavior), the CTE is authoritative, and the AGE walk is
advisory cross-check only — its failure can never affect the result.
"""

import pytest
from unittest.mock import AsyncMock, patch

from src.mcp_handlers.lifecycle import lineage_reachability as lr


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *a):
        return False


class _DbWithConn:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


# --------------------------------------------------------------------------
# reachable_ancestors — recoverability + CTE-authoritative contract
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_input_returns_empty():
    assert await lr.reachable_ancestors([]) == set()
    assert await lr.reachable_ancestors([None, ""]) == set()


@pytest.mark.asyncio
async def test_returns_cte_set_and_runs_age_crosscheck():
    age_mock = AsyncMock(return_value={"a", "b", "c"})  # AGE superset
    with patch.object(lr, "get_db", return_value=object()), \
         patch.object(lr, "_cte_ancestors", AsyncMock(return_value={"a", "b"})), \
         patch.object(lr, "_age_ancestors", age_mock):
        result = await lr.reachable_ancestors({"child1"})
    assert result == {"a", "b"}        # CTE drives the result, not AGE
    age_mock.assert_awaited_once()      # AGE cross-check still ran


@pytest.mark.asyncio
async def test_cte_failure_returns_empty_recoverable():
    with patch.object(lr, "get_db", return_value=object()), \
         patch.object(lr, "_cte_ancestors", AsyncMock(side_effect=RuntimeError("db down"))):
        assert await lr.reachable_ancestors({"child1"}) == set()


@pytest.mark.asyncio
async def test_age_crosscheck_failure_does_not_affect_result():
    # AGE is advisory: its failure must not perturb the authoritative CTE result.
    with patch.object(lr, "get_db", return_value=object()), \
         patch.object(lr, "_cte_ancestors", AsyncMock(return_value={"a"})), \
         patch.object(lr, "_age_ancestors", AsyncMock(side_effect=RuntimeError("age down"))):
        assert await lr.reachable_ancestors({"child1"}) == {"a"}


@pytest.mark.asyncio
async def test_get_db_failure_returns_empty():
    with patch.object(lr, "get_db", side_effect=RuntimeError("no db")):
        assert await lr.reachable_ancestors({"child1"}) == set()


# --------------------------------------------------------------------------
# _cte_ancestors — succession filter + parsing
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cte_ancestors_parses_and_filters_nulls():
    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[{"agent_id": "p1"}, {"agent_id": "gp1"}, {"agent_id": None}]
    )
    out = await lr._cte_ancestors(_DbWithConn(conn), {"c1"}, 5)
    assert out == {"p1", "gp1"}
    # The non-succession exclusion list is passed to the query.
    passed = conn.fetch.await_args.args
    assert list(lr._NON_SUCCESSION_SPAWN_REASONS) == passed[2]


@pytest.mark.asyncio
async def test_cte_depth_is_clamped():
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    await lr._cte_ancestors(_DbWithConn(conn), {"c1"}, 999)
    assert conn.fetch.await_args.args[3] == lr._MAX_DEPTH  # clamped


# --------------------------------------------------------------------------
# _age_ancestors — map-row parsing
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_age_ancestors_parses_map_rows():
    db = AsyncMock()
    db.graph_query = AsyncMock(
        return_value=[{"ancestor": "p1"}, {"ancestor": "gp1"}, {"other": 1}, None]
    )
    out = await lr._age_ancestors(db, {"c1"}, 5)
    assert out == {"p1", "gp1"}


# --------------------------------------------------------------------------
# measure_transitive_expansion — shadow delta
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_measure_reports_delta_beyond_single_hop():
    with patch.object(lr, "reachable_ancestors", AsyncMock(return_value={"p1", "gp1", "ggp1"})):
        summary = await lr.measure_transitive_expansion({"c1"}, {"p1"})
    assert summary == {
        "single_hop": 1,
        "transitive_total": 3,
        "new_beyond_single_hop": 2,
    }


@pytest.mark.asyncio
async def test_measure_no_expansion_when_subset():
    with patch.object(lr, "reachable_ancestors", AsyncMock(return_value={"p1"})):
        summary = await lr.measure_transitive_expansion({"c1"}, {"p1", "p2"})
    assert summary["new_beyond_single_hop"] == 0


# --------------------------------------------------------------------------
# agreement logging — exercises match + divergence paths without raising
# --------------------------------------------------------------------------

def test_record_agreement_paths_do_not_raise():
    lr._record_reachability_agreement({"a"}, {"a"})                      # match
    lr._record_reachability_agreement({"a", "coincidental"}, {"a", "missing"})  # divergence

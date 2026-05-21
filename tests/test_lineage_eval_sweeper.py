"""R2 PR 4: lineage_eval_sweeper_task tests.

Two layers:

1. Live-DB tests for ``select_lineage_eval_candidates`` — verify the
   WHERE/ORDER/LIMIT contract on real Postgres (skipped if
   governance_test isn't reachable). These reuse the ``seeded_pair``
   and ``confirmed_pair`` fixtures from ``tests/db/conftest.py`` via
   inline imports to keep the file self-contained at the top level.

2. Unit tests for ``_lineage_eval_sweep_once`` — verify the sweeper's
   inner cycle drives ``evaluate_lineage_for`` per candidate, swallows
   per-eval exceptions, and counts transitions correctly. These use
   the autouse mocked DB backend and patch the FSM entry point.

See: PR 4
 §"Evaluation triggers"
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Make `tests.db.conftest` importable for the live-DB section's inline
# fixture re-use. The top-level conftest already adds the project root
# to sys.path; this is just defense-in-depth.
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# ---------------------------------------------------------------------------
# Live-DB layer — select_lineage_eval_candidates
# ---------------------------------------------------------------------------

# Skip the live-DB section if asyncpg or governance_test is unavailable.
# Mirrors the gating in tests/db/test_lineage_lifecycle_storage.py.
try:
    import asyncpg  # noqa: F401
    from tests.test_db_utils import can_connect_to_test_db
    _LIVE_DB_AVAILABLE = can_connect_to_test_db()
except ImportError:
    _LIVE_DB_AVAILABLE = False


live_db = pytest.mark.skipif(
    not _LIVE_DB_AVAILABLE,
    reason="governance_test database not available",
)


# Pull the fixtures from tests/db/conftest.py at the live-DB layer so
# we get the same `seeded_pair`/`confirmed_pair` shapes the storage
# tests use. Re-export as module-level fixtures.
if _LIVE_DB_AVAILABLE:
    from tests.db.conftest import (  # noqa: F401
        seeded_pair,
        confirmed_pair,
    )
    # `live_postgres_backend` is defined at the top-level conftest;
    # pytest discovers it without re-import.


@live_db
@pytest.mark.asyncio
async def test_select_candidates_picks_up_provisional_pair(
    live_postgres_backend, seeded_pair,
):
    """Provisional row with NULL lineage_last_eval_at is selected."""
    candidates = await live_postgres_backend.select_lineage_eval_candidates()
    assert seeded_pair.successor_id in candidates


@live_db
@pytest.mark.asyncio
async def test_select_candidates_picks_up_confirmed_pair(
    live_postgres_backend, confirmed_pair,
):
    """Confirmed row with NULL lineage_last_eval_at is selected."""
    candidates = await live_postgres_backend.select_lineage_eval_candidates()
    assert confirmed_pair.successor_id in candidates


@live_db
@pytest.mark.asyncio
async def test_select_candidates_skips_recently_evaluated(
    live_postgres_backend, seeded_pair,
):
    """lineage_last_eval_at within sweep_cadence_hours → row excluded."""
    backend = live_postgres_backend
    await backend.stamp_lineage_eval(seeded_pair.successor_id)
    candidates = await backend.select_lineage_eval_candidates(
        sweep_cadence_hours=6,
    )
    assert seeded_pair.successor_id not in candidates


@live_db
@pytest.mark.asyncio
async def test_select_candidates_excludes_archived_rows(
    live_postgres_backend, seeded_pair,
):
    """lineage_archived_at IS NOT NULL → row excluded."""
    backend = live_postgres_backend
    await backend.archive_lineage(seeded_pair.successor_id)
    candidates = await backend.select_lineage_eval_candidates()
    assert seeded_pair.successor_id not in candidates


@live_db
@pytest.mark.asyncio
async def test_select_candidates_excludes_demoted_rows(
    live_postgres_backend, seeded_pair,
):
    """lineage_demoted_at IS NOT NULL → row excluded."""
    backend = live_postgres_backend
    await backend.demote_lineage(seeded_pair.successor_id, reason="test")
    candidates = await backend.select_lineage_eval_candidates()
    assert seeded_pair.successor_id not in candidates


@live_db
@pytest.mark.asyncio
async def test_select_candidates_respects_limit(
    live_postgres_backend, seeded_pair,
):
    """LIMIT bounds cycle size."""
    backend = live_postgres_backend
    candidates = await backend.select_lineage_eval_candidates(limit=1)
    assert len(candidates) <= 1


@live_db
@pytest.mark.asyncio
async def test_select_candidates_orders_nulls_first(
    live_postgres_backend, seeded_pair, confirmed_pair,
):
    """Never-evaluated rows (NULL lineage_last_eval_at) come before
    rows with a stamped (but stale) eval timestamp.

    Setup: stamp the confirmed_pair so it has a non-NULL eval ts in
    the past, then push that stamp back beyond the cadence window so
    it is still a candidate. The provisional seeded_pair stays NULL.
    Expect: seeded_pair (NULL) appears before confirmed_pair in the
    result list.
    """
    backend = live_postgres_backend
    # Stamp confirmed_pair eval-ts and push it past the 6h cadence guard.
    await backend.stamp_lineage_eval(confirmed_pair.successor_id)
    async with backend.acquire() as conn:
        await conn.execute(
            "UPDATE core.identities "
            "SET lineage_last_eval_at = now() - interval '12 hours' "
            "WHERE agent_id = $1",
            confirmed_pair.successor_id,
        )
    candidates = await backend.select_lineage_eval_candidates()
    assert seeded_pair.successor_id in candidates
    assert confirmed_pair.successor_id in candidates
    # NULLS FIRST: seeded_pair (NULL) precedes confirmed_pair (stale).
    assert candidates.index(seeded_pair.successor_id) < candidates.index(
        confirmed_pair.successor_id
    )


# ---------------------------------------------------------------------------
# Unit layer — _lineage_eval_sweep_once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_once_invokes_evaluate_for_each_candidate(monkeypatch):
    """One sweep cycle calls ``evaluate_lineage_for`` once per candidate
    returned by ``select_lineage_eval_candidates``."""
    from src.db import get_db
    from src.identity import lineage_lifecycle
    from src.identity.lineage_lifecycle import LineageEvalOutcome
    from src import background_tasks

    backend = get_db()
    backend.select_lineage_eval_candidates = AsyncMock(
        return_value=["agent-a", "agent-b", "agent-c"],
    )

    eval_calls: list[str] = []

    async def fake_evaluate(agent_id, **kwargs):
        eval_calls.append(agent_id)
        return LineageEvalOutcome(
            successor_id=agent_id, parent_id="p",
            terminal_state=None, transition=None,
            r1_verdict="inconclusive", skipped_reason=None,
        )

    # Patch where the sweeper imports it — it does a lazy import from
    # `src.identity.lineage_lifecycle`, so patching the module attribute
    # is the right scope.
    monkeypatch.setattr(
        lineage_lifecycle, "evaluate_lineage_for", fake_evaluate,
    )

    result = await background_tasks._lineage_eval_sweep_once()

    assert eval_calls == ["agent-a", "agent-b", "agent-c"]
    assert result == {"candidates": 3, "transitions": 0}


@pytest.mark.asyncio
async def test_sweep_once_counts_transitions(monkeypatch):
    """``transitions`` counts only outcomes whose ``transition`` field
    is non-None — inconclusive / skipped outcomes don't bump it."""
    from src.db import get_db
    from src.identity import lineage_lifecycle
    from src.identity.lineage_lifecycle import LineageEvalOutcome
    from src import background_tasks

    backend = get_db()
    backend.select_lineage_eval_candidates = AsyncMock(
        return_value=["a", "b", "c"],
    )

    outcomes = {
        "a": LineageEvalOutcome(
            successor_id="a", parent_id="p",
            terminal_state="confirmed",
            transition="provisional_to_confirmed",
            r1_verdict="plausible", skipped_reason=None,
        ),
        "b": LineageEvalOutcome(
            successor_id="b", parent_id="p",
            terminal_state=None, transition=None,
            r1_verdict="inconclusive", skipped_reason=None,
        ),
        "c": LineageEvalOutcome(
            successor_id="c", parent_id="p",
            terminal_state="demoted",
            transition="provisional_to_demoted",
            r1_verdict="unsupported", skipped_reason=None,
        ),
    }

    async def fake_evaluate(agent_id, **kwargs):
        return outcomes[agent_id]

    monkeypatch.setattr(
        lineage_lifecycle, "evaluate_lineage_for", fake_evaluate,
    )

    result = await background_tasks._lineage_eval_sweep_once()

    assert result == {"candidates": 3, "transitions": 2}


@pytest.mark.asyncio
async def test_sweep_once_swallows_per_eval_exceptions(monkeypatch, caplog):
    """If ``evaluate_lineage_for`` raises for one candidate, the sweep
    logs a warning and continues — remaining candidates still get
    evaluated, the cycle does not crash."""
    import logging
    from src.db import get_db
    from src.identity import lineage_lifecycle
    from src.identity.lineage_lifecycle import LineageEvalOutcome
    from src import background_tasks

    backend = get_db()
    backend.select_lineage_eval_candidates = AsyncMock(
        return_value=["a", "b", "c"],
    )

    eval_calls: list[str] = []

    async def fake_evaluate(agent_id, **kwargs):
        eval_calls.append(agent_id)
        if agent_id == "b":
            raise RuntimeError("simulated R1 failure")
        return LineageEvalOutcome(
            successor_id=agent_id, parent_id="p",
            terminal_state=None, transition=None,
            r1_verdict="inconclusive", skipped_reason=None,
        )

    monkeypatch.setattr(
        lineage_lifecycle, "evaluate_lineage_for", fake_evaluate,
    )

    with caplog.at_level(logging.WARNING, logger=background_tasks.logger.name):
        result = await background_tasks._lineage_eval_sweep_once()

    # All three candidates were attempted despite "b" raising.
    assert eval_calls == ["a", "b", "c"]
    assert result["candidates"] == 3
    # Per-eval warning was logged for the failure.
    assert any(
        "eval failed for" in rec.getMessage()
        and "simulated R1 failure" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_sweep_once_empty_candidate_list(monkeypatch):
    """Zero candidates is a valid no-op cycle, not an error."""
    from src.db import get_db
    from src.identity import lineage_lifecycle
    from src import background_tasks

    backend = get_db()
    backend.select_lineage_eval_candidates = AsyncMock(return_value=[])

    called = {"n": 0}

    async def fake_evaluate(agent_id, **kwargs):
        called["n"] += 1

    monkeypatch.setattr(
        lineage_lifecycle, "evaluate_lineage_for", fake_evaluate,
    )

    result = await background_tasks._lineage_eval_sweep_once()
    assert result == {"candidates": 0, "transitions": 0}
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# PR 4 council fixes — clear_lineage_declaration full reset
# ---------------------------------------------------------------------------


@live_db
@pytest.mark.asyncio
async def test_clear_lineage_declaration_clears_provisional_state(
    live_postgres_backend,
):
    """PR 4 council fix: ``clear_lineage_declaration`` must also clear
    ``provisional_lineage`` (and the rest of the lineage-state fields)
    so a cross-role-rejected row doesn't hot-loop in the sweeper's
    candidate set.

    Bug: PR 3's ``clear_lineage_declaration`` only cleared
    ``parent_agent_id`` and ``spawn_reason``. A row left with
    ``parent_agent_id=NULL AND provisional_lineage=TRUE`` matched the
    sweeper's WHERE filter every cycle, and the FSM's ``no_parent``
    short-circuit returned without stamping ``lineage_last_eval_at``
    — hot loop forever.
    """
    from tests.db.conftest import _cleanup, _uuid_suffix

    suffix = _uuid_suffix()
    pid = "r2-pr4-clear-prov-parent-" + suffix
    sid = "r2-pr4-clear-prov-succ-" + suffix
    try:
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key'), "
                "($2, 'test-key') ON CONFLICT (id) DO NOTHING",
                pid, sid,
            )
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash, status) "
                "VALUES ($1, 'test-hash', 'active')",
                pid,
            )
            await conn.execute(
                "INSERT INTO core.identities "
                "(agent_id, api_key_hash, status, parent_agent_id, "
                " spawn_reason, provisional_lineage, provisional_score_id, "
                " confirmed_at, lineage_declared_at) "
                "VALUES ($1, 'test-hash', 'active', $2, 'subagent', TRUE, "
                " '00000000-0000-0000-0000-000000000abc'::uuid, now(), now())",
                sid, pid,
            )
        ok = await live_postgres_backend.clear_lineage_declaration(sid)
        assert ok is True
        async with live_postgres_backend.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT parent_agent_id, spawn_reason, provisional_lineage, "
                "       provisional_score_id, confirmed_at, lineage_declared_at "
                "  FROM core.identities WHERE agent_id = $1",
                sid,
            )
        assert row["parent_agent_id"] is None
        assert row["spawn_reason"] is None
        assert row["provisional_lineage"] is False
        assert row["provisional_score_id"] is None
        assert row["confirmed_at"] is None
        assert row["lineage_declared_at"] is None
    finally:
        await _cleanup(live_postgres_backend, [pid, sid])


@live_db
@pytest.mark.asyncio
async def test_cross_role_rejected_row_excluded_from_sweeper_candidates(
    live_postgres_backend,
):
    """End-to-end: a row that was cross-role-rejected (parent cleared
    via ``clear_lineage_declaration``) must NOT appear in sweeper
    candidates. This is the user-visible end-state of the PR 4 fix —
    the sweeper's WHERE filter naturally excludes the row because
    ``provisional_lineage=FALSE AND confirmed_at IS NULL`` after the
    full reset."""
    from tests.db.conftest import _cleanup, _uuid_suffix

    suffix = _uuid_suffix()
    pid = "r2-pr4-rejected-parent-" + suffix
    sid = "r2-pr4-rejected-succ-" + suffix
    try:
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key'), "
                "($2, 'test-key') ON CONFLICT (id) DO NOTHING",
                pid, sid,
            )
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash, status) "
                "VALUES ($1, 'test-hash', 'active')",
                pid,
            )
            await conn.execute(
                "INSERT INTO core.identities "
                "(agent_id, api_key_hash, status, parent_agent_id, "
                " provisional_lineage) "
                "VALUES ($1, 'test-hash', 'active', $2, TRUE)",
                sid, pid,
            )
        await live_postgres_backend.clear_lineage_declaration(sid)
        candidates = await live_postgres_backend.select_lineage_eval_candidates()
        assert sid not in candidates
    finally:
        await _cleanup(live_postgres_backend, [pid, sid])

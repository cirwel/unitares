from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


class _AcquireCtx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return None


class _Backend:
    def __init__(self, conn):
        self.conn = conn
        self.confirm_lineage = AsyncMock(return_value=True)
        self.demote_lineage = AsyncMock(return_value=True)

    def acquire(self):
        return _AcquireCtx(self.conn)


def _score(verdict: str, score_id: str):
    return SimpleNamespace(verdict=verdict, score_id=score_id)


@pytest.mark.asyncio
async def test_sweep_provisional_lineage_reports_without_confirming():
    from src.identity.r1_maintenance import sweep_provisional_lineage

    conn = SimpleNamespace()
    conn.fetch = AsyncMock(return_value=[
        {
            "successor_id": "child-plausible",
            "parent_id": "parent",
            "provisional_score_id": "old-score",
        },
        {
            "successor_id": "child-unsupported",
            "parent_id": "parent",
            "provisional_score_id": None,
        },
        {
            "successor_id": "child-inconclusive",
            "parent_id": "parent",
            "provisional_score_id": None,
        },
    ])
    backend = _Backend(conn)

    async def score_fn(parent_id, successor_id):
        verdicts = {
            "child-plausible": "plausible",
            "child-unsupported": "unsupported",
            "child-inconclusive": "inconclusive",
        }
        return _score(verdicts[successor_id], f"score-{successor_id}")

    result = await sweep_provisional_lineage(db=backend, score_fn=score_fn)

    assert result["evaluated"] == 3
    assert result["would_confirm"] == 1
    assert result["orphan_candidates"] == 1
    assert result["orphan_demoted"] == 0
    assert result["blocked_inconclusive"] == 1
    backend.confirm_lineage.assert_not_awaited()
    backend.demote_lineage.assert_not_awaited()
    assert result["results"][0]["action"] == "would_confirm"
    assert result["results"][1]["action"] == "orphan_candidate"
    assert result["results"][2]["action"] == "blocked_inconclusive"


@pytest.mark.asyncio
async def test_sweep_provisional_lineage_apply_confirms_plausible_only():
    from src.identity.r1_maintenance import sweep_provisional_lineage

    conn = SimpleNamespace()
    conn.fetch = AsyncMock(return_value=[
        {"successor_id": "child", "parent_id": "parent", "provisional_score_id": None},
    ])
    backend = _Backend(conn)

    async def score_fn(parent_id, successor_id):
        return _score("plausible", "score-new")

    result = await sweep_provisional_lineage(
        apply=True,
        db=backend,
        score_fn=score_fn,
    )

    backend.confirm_lineage.assert_awaited_once_with("child")
    backend.demote_lineage.assert_not_awaited()
    assert result["confirmed"] == 1
    assert result["results"][0]["action"] == "confirmed"


@pytest.mark.asyncio
async def test_sweep_provisional_lineage_apply_orphans_demotes_unsupported():
    from src.identity.r1_maintenance import sweep_provisional_lineage

    conn = SimpleNamespace()
    conn.fetch = AsyncMock(return_value=[
        {
            "successor_id": "child-unsupported",
            "parent_id": "parent",
            "provisional_score_id": None,
        },
    ])
    backend = _Backend(conn)
    audit_fn = AsyncMock()

    async def score_fn(parent_id, successor_id):
        return _score("unsupported", "score-new")

    result = await sweep_provisional_lineage(
        apply_orphans=True,
        db=backend,
        score_fn=score_fn,
        audit_fn=audit_fn,
    )

    backend.confirm_lineage.assert_not_awaited()
    backend.demote_lineage.assert_awaited_once_with(
        "child-unsupported",
        reason="r1_unsupported",
    )
    assert result["apply_orphans"] is True
    assert result["orphan_demoted"] == 1
    assert result["orphan_candidates"] == 0
    assert result["results"][0]["action"] == "orphan_demoted"
    audit_fn.assert_awaited_once()
    assert audit_fn.await_args.args[0].successor_id == "child-unsupported"


@pytest.mark.asyncio
async def test_sweep_provisional_lineage_apply_orphans_reports_demote_failure():
    from src.identity.r1_maintenance import sweep_provisional_lineage

    conn = SimpleNamespace()
    conn.fetch = AsyncMock(return_value=[
        {
            "successor_id": "child-unsupported",
            "parent_id": "parent",
            "provisional_score_id": None,
        },
    ])
    backend = _Backend(conn)
    backend.demote_lineage.return_value = False
    audit_fn = AsyncMock()

    async def score_fn(parent_id, successor_id):
        return _score("unsupported", "score-new")

    result = await sweep_provisional_lineage(
        apply_orphans=True,
        db=backend,
        score_fn=score_fn,
        audit_fn=audit_fn,
    )

    assert result["orphan_demote_failed"] == 1
    assert result["results"][0]["action"] == "orphan_demote_failed"
    audit_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_stale_public_r1_scores_dry_run_reports_count_and_sample():
    from src.identity.r1_maintenance import archive_stale_public_r1_scores

    now = datetime(2026, 5, 5, tzinfo=timezone.utc)
    conn = SimpleNamespace()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": "r1_score:old",
            "agent_id": "child",
            "created_at": now,
            "updated_at": None,
            "status": "open",
        },
    ])
    conn.fetchval = AsyncMock(return_value=3)
    backend = _Backend(conn)

    result = await archive_stale_public_r1_scores(db=backend, dry_run=True)

    assert result["dry_run"] is True
    assert result["would_archive"] == 3
    assert result["sample"][0]["id"] == "r1_score:old"
    conn.fetchval.assert_awaited_once()


@pytest.mark.asyncio
async def test_archive_stale_public_r1_scores_apply_archives_rows():
    from src.identity.r1_maintenance import archive_stale_public_r1_scores

    now = datetime(2026, 5, 5, tzinfo=timezone.utc)
    conn = SimpleNamespace()
    conn.fetch = AsyncMock(return_value=[
        {
            "id": "r1_score:old",
            "agent_id": "child",
            "created_at": now,
            "updated_at": now,
            "status": "archived",
        },
    ])
    backend = _Backend(conn)

    result = await archive_stale_public_r1_scores(db=backend, dry_run=False, limit=1)

    assert result["dry_run"] is False
    assert result["archived"] == 1
    assert result["sample"][0]["status"] == "archived"
    sql = conn.fetch.await_args.args[0]
    assert "UPDATE knowledge.discoveries" in sql
    assert "LIMIT 1" in sql

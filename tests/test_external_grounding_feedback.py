"""External-grounding calibration feedback: the per-agent claims-vs-verified-outcomes
surface (mixin query + check-in enrichment).

Scores only verification_source='external_signal' outcomes against the agent's own
prior audit-trail confidence claims — self-reported outcomes are excluded so the
feedback cannot be self-referential. The enrichment floor counts distinct sessions,
not rows, because adjudication batches are not independent samples (unitares#1370).
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.mixins.tool_usage import ToolUsageMixin
from src.mcp_handlers.updates.enrichments import enrich_external_grounding


class _FakeConn:
    def __init__(self, row):
        self.row = row
        self.calls = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        if isinstance(self.row, Exception):
            raise self.row
        return self.row


class _FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _Harness(ToolUsageMixin):
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _FakeAcquire(self.conn)


# ─── Mixin query ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calibration_query_parses_row_and_casts_decimals():
    conn = _FakeConn({
        "n": 41, "n_batches": 4,
        "mean_claim": Decimal("0.85"),
        "success_rate": Decimal("0.9512"),
        "brier": Decimal("0.0488"),
    })
    db = _Harness(conn)

    stats = await db.get_agent_external_calibration("agent-1")

    assert stats == {
        "n": 41, "n_batches": 4,
        "mean_claim": 0.85, "success_rate": 0.951, "brier": 0.0488,
    }
    sql, args = conn.calls[0]
    assert "external_signal" in sql
    assert "audit.events" in sql  # claims come from the audit trail, per-agent
    assert args[0] == "agent-1"


@pytest.mark.asyncio
async def test_calibration_query_returns_none_when_nothing_accrued():
    conn = _FakeConn({"n": 0, "n_batches": 0, "mean_claim": None,
                      "success_rate": None, "brier": None})
    db = _Harness(conn)
    assert await db.get_agent_external_calibration("agent-1") is None


@pytest.mark.asyncio
async def test_calibration_query_fails_open_on_db_error():
    conn = _FakeConn(Exception("relation does not exist"))
    db = _Harness(conn)
    assert await db.get_agent_external_calibration("agent-1") is None


# ─── Enrichment ─────────────────────────────────────────────────────────


def _ctx(agent_id="agent-1"):
    ctx = MagicMock()
    ctx.agent_id = agent_id
    ctx.response_data = {}
    return ctx


def _db_returning(stats):
    db = MagicMock()
    db.get_agent_external_calibration = AsyncMock(return_value=stats)
    return db


@pytest.mark.asyncio
async def test_enrichment_reports_accruing_below_session_floor():
    """41 rows in 1 session is NOT grounded — batches aren't independent samples."""
    ctx = _ctx()
    db = _db_returning({"n": 41, "n_batches": 1, "mean_claim": 1.0,
                        "success_rate": 0.951, "brier": 0.0488})
    with patch("src.db.get_db", return_value=db):
        await enrich_external_grounding(ctx)

    block = ctx.response_data["calibration_feedback"]["external_grounding"]
    assert block["status"] == "accruing"
    assert "claim_gap" not in block
    assert "No calibration verdict" in block["message"]


@pytest.mark.asyncio
async def test_enrichment_grounded_verdict_with_overclaiming_direction():
    ctx = _ctx()
    db = _db_returning({"n": 30, "n_batches": 5, "mean_claim": 0.95,
                        "success_rate": 0.70, "brier": 0.11})
    with patch("src.db.get_db", return_value=db):
        await enrich_external_grounding(ctx)

    block = ctx.response_data["calibration_feedback"]["external_grounding"]
    assert block["status"] == "grounded"
    assert block["claim_gap"] == 0.25
    assert "over-claiming" in block["message"]


@pytest.mark.asyncio
async def test_enrichment_grounded_well_calibrated_direction():
    ctx = _ctx()
    db = _db_returning({"n": 12, "n_batches": 3, "mean_claim": 0.80,
                        "success_rate": 0.75, "brier": 0.05})
    with patch("src.db.get_db", return_value=db):
        await enrich_external_grounding(ctx)

    block = ctx.response_data["calibration_feedback"]["external_grounding"]
    assert block["status"] == "grounded"
    assert "well-calibrated" in block["message"]


@pytest.mark.asyncio
async def test_enrichment_silent_when_nothing_accrued():
    ctx = _ctx()
    with patch("src.db.get_db", return_value=_db_returning(None)):
        await enrich_external_grounding(ctx)
    assert "calibration_feedback" not in ctx.response_data


@pytest.mark.asyncio
async def test_enrichment_merges_into_existing_calibration_feedback():
    ctx = _ctx()
    ctx.response_data["calibration_feedback"] = {"complexity": {"reported": 0.5}}
    db = _db_returning({"n": 15, "n_batches": 4, "mean_claim": 0.6,
                        "success_rate": 0.8, "brier": 0.07})
    with patch("src.db.get_db", return_value=db):
        await enrich_external_grounding(ctx)

    feedback = ctx.response_data["calibration_feedback"]
    assert feedback["complexity"] == {"reported": 0.5}  # existing block preserved
    assert feedback["external_grounding"]["status"] == "grounded"
    assert "under-claiming" in feedback["external_grounding"]["message"]


@pytest.mark.asyncio
async def test_enrichment_fails_open_on_db_exception():
    ctx = _ctx()
    db = MagicMock()
    db.get_agent_external_calibration = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("src.db.get_db", return_value=db):
        await enrich_external_grounding(ctx)  # must not raise
    assert "calibration_feedback" not in ctx.response_data


@pytest.mark.asyncio
async def test_enrichment_skips_without_agent_id():
    ctx = _ctx(agent_id=None)
    db = _db_returning({"n": 99})
    with patch("src.db.get_db", return_value=db):
        await enrich_external_grounding(ctx)
    db.get_agent_external_calibration.assert_not_awaited()

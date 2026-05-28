"""Tests for temporal narrator."""
from config.governance_config import GovernanceConfig


def test_temporal_config_exists():
    """Temporal narrator thresholds are defined in config."""
    assert hasattr(GovernanceConfig, 'TEMPORAL_LONG_SESSION_HOURS')
    assert hasattr(GovernanceConfig, 'TEMPORAL_GAP_HOURS')
    assert hasattr(GovernanceConfig, 'TEMPORAL_IDLE_MINUTES')
    assert hasattr(GovernanceConfig, 'TEMPORAL_CROSS_AGENT_MINUTES')
    assert hasattr(GovernanceConfig, 'TEMPORAL_HIGH_CHECKIN_COUNT')
    assert hasattr(GovernanceConfig, 'TEMPORAL_HIGH_CHECKIN_WINDOW_MINUTES')


def test_temporal_config_values():
    """Config values are sensible defaults."""
    assert GovernanceConfig.TEMPORAL_LONG_SESSION_HOURS == 2
    assert GovernanceConfig.TEMPORAL_GAP_HOURS == 24
    assert GovernanceConfig.TEMPORAL_IDLE_MINUTES == 30
    assert GovernanceConfig.TEMPORAL_CROSS_AGENT_MINUTES == 60
    assert GovernanceConfig.TEMPORAL_HIGH_CHECKIN_COUNT == 10
    assert GovernanceConfig.TEMPORAL_HIGH_CHECKIN_WINDOW_MINUTES == 30


def test_get_last_inactive_session_exists():
    """SessionMixin has get_last_inactive_session method."""
    from src.db.mixins.session import SessionMixin
    assert hasattr(SessionMixin, 'get_last_inactive_session')


def test_cross_agent_activity_method_exists():
    """StateMixin has get_recent_cross_agent_activity method."""
    from src.db.mixins.state import StateMixin
    assert hasattr(StateMixin, 'get_recent_cross_agent_activity')


def test_kg_query_accepts_created_after():
    """kg_query accepts a created_after parameter."""
    import inspect
    from src.db.mixins.knowledge_graph import KnowledgeGraphMixin
    sig = inspect.signature(KnowledgeGraphMixin.kg_query)
    assert 'created_after' in sig.parameters


# ─── Duration formatter tests ─────────────────────────────────────

import asyncio
import time

import pytest
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone, timedelta
from src.temporal import build_temporal_context, _format_duration


def test_format_duration_seconds():
    assert _format_duration(timedelta(seconds=30)) == "30s"

def test_format_duration_minutes():
    assert _format_duration(timedelta(minutes=15)) == "15min"

def test_format_duration_hours():
    assert _format_duration(timedelta(hours=3, minutes=12)) == "3h 12min"

def test_format_duration_exact_hours():
    assert _format_duration(timedelta(hours=2)) == "2h"

def test_format_duration_one_day():
    assert _format_duration(timedelta(days=1)) == "1 day"

def test_format_duration_multiple_days():
    assert _format_duration(timedelta(days=5)) == "5 days"

def test_format_duration_zero():
    assert _format_duration(timedelta(seconds=0)) == "0s"


# ─── Core narrator tests ──────────────────────────────────────────

@pytest.fixture
def mock_db():
    db = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_temporal_silence_when_unremarkable(mock_db):
    """Returns None when nothing temporal is noteworthy."""
    now = datetime.now(timezone.utc)

    mock_db.get_identity.return_value = MagicMock(identity_id=1)
    mock_db.get_active_sessions_for_identity.return_value = [
        MagicMock(created_at=now - timedelta(minutes=30))
    ]
    mock_db.get_last_inactive_session.return_value = None
    mock_db.get_latest_agent_state.return_value = MagicMock(
        recorded_at=now - timedelta(minutes=2)
    )
    mock_db.get_recent_cross_agent_activity.return_value = []
    mock_db.get_agent_state_history.return_value = []
    mock_db.kg_query.return_value = []

    result = await build_temporal_context("test-uuid", mock_db, now=now)
    assert result is None


@pytest.mark.asyncio
async def test_temporal_long_session(mock_db):
    """Signals when session exceeds threshold."""
    now = datetime.now(timezone.utc)

    mock_db.get_identity.return_value = MagicMock(identity_id=1)
    mock_db.get_active_sessions_for_identity.return_value = [
        MagicMock(created_at=now - timedelta(hours=3, minutes=12))
    ]
    mock_db.get_last_inactive_session.return_value = None
    mock_db.get_latest_agent_state.return_value = MagicMock(
        recorded_at=now - timedelta(minutes=2)
    )
    mock_db.get_recent_cross_agent_activity.return_value = []
    mock_db.get_agent_state_history.return_value = []
    mock_db.kg_query.return_value = []

    result = await build_temporal_context("test-uuid", mock_db, now=now)
    assert result is not None
    assert "3h" in result


@pytest.mark.asyncio
async def test_temporal_long_gap(mock_db):
    """Signals when gap since last session is large."""
    now = datetime.now(timezone.utc)

    mock_db.get_identity.return_value = MagicMock(identity_id=1)
    mock_db.get_active_sessions_for_identity.return_value = [
        MagicMock(created_at=now - timedelta(minutes=5))
    ]
    mock_db.get_last_inactive_session.return_value = MagicMock(
        last_active=now - timedelta(days=2)
    )
    mock_db.get_latest_agent_state.return_value = MagicMock(
        recorded_at=now - timedelta(minutes=2)
    )
    mock_db.get_recent_cross_agent_activity.return_value = []
    mock_db.get_agent_state_history.return_value = []
    mock_db.kg_query.return_value = []

    result = await build_temporal_context("test-uuid", mock_db, now=now)
    assert result is not None
    assert "2 days" in result


@pytest.mark.asyncio
async def test_temporal_idle(mock_db):
    """Signals when idle within session exceeds threshold."""
    now = datetime.now(timezone.utc)

    mock_db.get_identity.return_value = MagicMock(identity_id=1)
    mock_db.get_active_sessions_for_identity.return_value = [
        MagicMock(created_at=now - timedelta(hours=1))
    ]
    mock_db.get_last_inactive_session.return_value = None
    mock_db.get_latest_agent_state.return_value = MagicMock(
        recorded_at=now - timedelta(minutes=45)
    )
    mock_db.get_recent_cross_agent_activity.return_value = []
    mock_db.get_agent_state_history.return_value = []
    mock_db.kg_query.return_value = []

    result = await build_temporal_context("test-uuid", mock_db, now=now)
    assert result is not None
    assert "45min" in result


@pytest.mark.asyncio
async def test_temporal_cross_agent(mock_db):
    """Surfaces cross-agent activity."""
    now = datetime.now(timezone.utc)

    mock_db.get_identity.return_value = MagicMock(identity_id=1)
    mock_db.get_active_sessions_for_identity.return_value = [
        MagicMock(created_at=now - timedelta(minutes=10))
    ]
    mock_db.get_last_inactive_session.return_value = None
    mock_db.get_latest_agent_state.return_value = MagicMock(
        recorded_at=now - timedelta(minutes=2)
    )
    mock_db.get_recent_cross_agent_activity.return_value = [
        {"agent_id": "other-agent", "recorded_at": now - timedelta(minutes=14), "count": 3}
    ]
    mock_db.get_agent_state_history.return_value = []
    mock_db.kg_query.return_value = []

    result = await build_temporal_context("test-uuid", mock_db, include_cross_agent=True, now=now)
    assert result is not None
    assert "agent" in result.lower()


@pytest.mark.asyncio
async def test_temporal_new_discoveries(mock_db):
    """Surfaces discoveries added since last session."""
    now = datetime.now(timezone.utc)

    mock_db.get_identity.return_value = MagicMock(identity_id=1)
    mock_db.get_active_sessions_for_identity.return_value = [
        MagicMock(created_at=now - timedelta(minutes=5))
    ]
    mock_db.get_last_inactive_session.return_value = MagicMock(
        last_active=now - timedelta(days=1, hours=2)
    )
    mock_db.get_latest_agent_state.return_value = MagicMock(
        recorded_at=now - timedelta(minutes=2)
    )
    mock_db.get_recent_cross_agent_activity.return_value = []
    mock_db.get_agent_state_history.return_value = []
    mock_db.kg_query.return_value = [
        {"id": "d1", "summary": "found a bug"},
        {"id": "d2", "summary": "pattern discovered"},
        {"id": "d3", "summary": "insight noted"},
    ]

    result = await build_temporal_context("test-uuid", mock_db, now=now)
    assert result is not None
    assert "3" in result
    assert "discover" in result.lower() or "knowledge" in result.lower()


@pytest.mark.asyncio
async def test_temporal_high_checkin_density(mock_db):
    """Signals when check-in density is high."""
    now = datetime.now(timezone.utc)

    mock_db.get_identity.return_value = MagicMock(identity_id=1)
    mock_db.get_active_sessions_for_identity.return_value = [
        MagicMock(created_at=now - timedelta(minutes=25))
    ]
    mock_db.get_last_inactive_session.return_value = None
    mock_db.get_latest_agent_state.return_value = MagicMock(
        recorded_at=now - timedelta(minutes=1)
    )
    # 14 check-ins in ~21 minutes — high density
    mock_db.get_agent_state_history.return_value = [
        MagicMock(recorded_at=now - timedelta(minutes=i * 1.5))
        for i in range(14)
    ]
    mock_db.get_recent_cross_agent_activity.return_value = []
    mock_db.kg_query.return_value = []

    result = await build_temporal_context("test-uuid", mock_db, now=now)
    assert result is not None
    assert "14" in result or "high" in result.lower()


@pytest.mark.asyncio
async def test_temporal_identity_not_found(mock_db):
    """Returns None gracefully when identity doesn't exist."""
    mock_db.get_identity.return_value = None

    result = await build_temporal_context("nonexistent-uuid", mock_db)
    assert result is None


@pytest.mark.asyncio
async def test_temporal_multiple_signals(mock_db):
    """Combines multiple temporal signals into one string."""
    now = datetime.now(timezone.utc)

    mock_db.get_identity.return_value = MagicMock(identity_id=1)
    mock_db.get_active_sessions_for_identity.return_value = [
        MagicMock(created_at=now - timedelta(hours=3))
    ]
    mock_db.get_last_inactive_session.return_value = MagicMock(
        last_active=now - timedelta(days=3)
    )
    mock_db.get_latest_agent_state.return_value = MagicMock(
        recorded_at=now - timedelta(minutes=2)
    )
    mock_db.get_agent_state_history.return_value = []
    mock_db.get_recent_cross_agent_activity.return_value = []
    mock_db.kg_query.return_value = []

    result = await build_temporal_context("test-uuid", mock_db, now=now)
    assert result is not None
    assert "3h" in result
    assert "3 days" in result


@pytest.mark.asyncio
async def test_temporal_partial_db_failure(mock_db):
    """One query failing doesn't crash the whole function."""
    now = datetime.now(timezone.utc)

    mock_db.get_identity.return_value = MagicMock(identity_id=1)
    # Session query raises
    mock_db.get_active_sessions_for_identity.side_effect = Exception("db down")
    # But gap query works and has signal
    mock_db.get_last_inactive_session.return_value = MagicMock(
        last_active=now - timedelta(days=5)
    )
    mock_db.get_latest_agent_state.return_value = MagicMock(
        recorded_at=now - timedelta(minutes=2)
    )
    mock_db.get_agent_state_history.return_value = []
    mock_db.get_recent_cross_agent_activity.return_value = []
    mock_db.kg_query.return_value = []

    result = await build_temporal_context("test-uuid", mock_db, now=now)
    assert result is not None
    assert "5 days" in result


from unittest.mock import patch, AsyncMock

@pytest.mark.asyncio
async def test_temporal_context_injected_into_onboard_result():
    """Temporal context is added to onboard result dict when relevant."""
    from src.temporal import build_temporal_context

    now = datetime.now(timezone.utc)
    mock_db = AsyncMock()
    mock_db.get_identity.return_value = MagicMock(identity_id=1)
    mock_db.get_active_sessions_for_identity.return_value = [
        MagicMock(created_at=now - timedelta(hours=4))
    ]
    mock_db.get_last_inactive_session.return_value = None
    mock_db.get_latest_agent_state.return_value = MagicMock(
        recorded_at=now - timedelta(minutes=2)
    )
    mock_db.get_agent_state_history.return_value = []
    mock_db.get_recent_cross_agent_activity.return_value = []
    mock_db.kg_query.return_value = []

    # Simulate what the onboard handler does
    result = {}
    temporal = await build_temporal_context("test-uuid", mock_db, now=now)
    if temporal:
        result["temporal_context"] = temporal

    assert "temporal_context" in result
    assert "4h" in result["temporal_context"]


@pytest.mark.asyncio
async def test_temporal_enrichment():
    """Temporal enrichment adds temporal_context to response_data."""
    from src.mcp_handlers.updates.context import UpdateContext

    ctx = UpdateContext()
    ctx.agent_uuid = "test-uuid"
    ctx.response_data = {}

    with patch("src.mcp_handlers.updates.enrichments.build_temporal_context", new_callable=AsyncMock) as mock_btc:
        mock_btc.return_value = "Session: 3h 12min."

        from src.mcp_handlers.updates.enrichments import enrich_temporal_context
        await enrich_temporal_context(ctx)

        assert ctx.response_data.get("temporal_context") == "Session: 3h 12min."


@pytest.mark.asyncio
async def test_temporal_enrichment_silence():
    """Temporal enrichment adds nothing when time is unremarkable."""
    from src.mcp_handlers.updates.context import UpdateContext

    ctx = UpdateContext()
    ctx.agent_uuid = "test-uuid"
    ctx.response_data = {}

    with patch("src.mcp_handlers.updates.enrichments.build_temporal_context", new_callable=AsyncMock) as mock_btc:
        mock_btc.return_value = None

        from src.mcp_handlers.updates.enrichments import enrich_temporal_context
        await enrich_temporal_context(ctx)

        assert "temporal_context" not in ctx.response_data


@pytest.mark.asyncio
async def test_temporal_phase_2_reads_run_concurrently():
    """The 4 per-identity reads (sessions/last_session/latest_state/history)
    must run concurrently via asyncio.gather, not sequentially.

    Under N-way concurrent enrichment, sequential awaits serialize through
    the shared executor thread and turn a ~5ms enricher into a ~500ms one
    (post-lock enrichment profile, 2026-05-28). This regression test
    inserts a 50ms sleep on each mocked read; if they run sequentially the
    total is ~200ms, if concurrently it's ~50ms. Allows ~120ms of headroom
    for asyncio scheduling and slow CI.
    """
    now = datetime.now(timezone.utc)

    db = AsyncMock()
    db.get_identity.return_value = MagicMock(identity_id=1)

    async def _slow(*args, **kwargs):
        await asyncio.sleep(0.05)
        return []

    db.get_active_sessions_for_identity.side_effect = _slow
    db.get_last_inactive_session.side_effect = _slow
    db.get_latest_agent_state.side_effect = _slow
    db.get_agent_state_history.side_effect = _slow
    db.kg_query.return_value = []

    start = time.perf_counter()
    await build_temporal_context("test-uuid", db, now=now)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.170, (
        f"phase 2 reads serialized; elapsed {elapsed:.3f}s suggests "
        f"sequential awaits instead of asyncio.gather"
    )


@pytest.mark.asyncio
async def test_temporal_phase_2_tolerates_single_failure(mock_db):
    """If one of the gathered reads raises, others still produce signals
    (matches the pre-gather per-call try/except semantics).
    """
    now = datetime.now(timezone.utc)
    mock_db.get_identity.return_value = MagicMock(identity_id=1)

    mock_db.get_active_sessions_for_identity.return_value = [
        MagicMock(created_at=now - timedelta(hours=3))
    ]
    mock_db.get_last_inactive_session.side_effect = RuntimeError("db go boom")
    mock_db.get_latest_agent_state.return_value = MagicMock(
        recorded_at=now - timedelta(minutes=2)
    )
    mock_db.get_agent_state_history.return_value = []
    mock_db.kg_query.return_value = []

    result = await build_temporal_context("test-uuid", mock_db, now=now)
    assert result is not None
    assert "Session: 3h" in result  # other reads still produced signal

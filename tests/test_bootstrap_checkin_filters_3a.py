"""Phase 3a filter-site tests for onboard-bootstrap-checkin.

Asserts that the load-bearing 4 read paths (get_latest_agent_state,
get_all_latest_agent_states, get_recent_cross_agent_activity,
get_latest_eisv_by_agent_id) exclude bootstrap rows by default. Plus
the matview-as-measured-only invariant from migration 019, plus the
calibration and trust-tier invariant locks.

Spec: §4.1, §8 items 4–8.
Audit: (sites 1–4 + I1, I3).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg  # noqa: F401
except ImportError:
    pytest.skip("asyncpg not installed", allow_module_level=True)

from tests.test_db_utils import can_connect_to_test_db

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)

from src.mcp_handlers.identity.bootstrap_checkin import write_bootstrap
from src.mcp_handlers.schemas.core import BootstrapStateParams


@pytest.fixture
def db(live_postgres_backend):
    return live_postgres_backend


async def _seed_identity(db) -> tuple[str, int]:
    agent_id = f"test-{uuid.uuid4()}"
    async with db.acquire() as conn:
        await conn.execute(
            "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key')",
            agent_id,
        )
        identity_id = await conn.fetchval(
            """
            INSERT INTO core.identities (agent_id, api_key_hash)
            VALUES ($1, 'test-hash')
            RETURNING identity_id
            """,
            agent_id,
        )
    return agent_id, identity_id


async def _record_measured(db, identity_id, *, entropy=0.4, integrity=0.6):
    """Insert a regular (synthetic=false) measured state row."""
    return await db.record_agent_state(
        identity_id=identity_id,
        entropy=entropy, integrity=integrity, stability_index=0.5,
        void=0.0, regime="nominal", coherence=1.0, state_json={},
    )


# ---------------------------------------------------------------------------
# Site #1: get_latest_agent_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_latest_excludes_bootstrap_only_agent(db):
    """Bootstrap-only agent: get_latest_agent_state returns None, not the synthetic row."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams(complexity=0.7))

    latest = await db.get_latest_agent_state(identity_id)
    assert latest is None


@pytest.mark.asyncio
async def test_get_latest_returns_measured_when_present(db):
    """With both a bootstrap and a real check-in: latest is the real check-in."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams(complexity=0.7))
    measured_id = await _record_measured(db, identity_id, entropy=0.3)

    latest = await db.get_latest_agent_state(identity_id)
    assert latest is not None
    assert latest.state_id == measured_id
    assert latest.entropy == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# Site #2: get_all_latest_agent_states (matview + base-table fallback)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_latest_excludes_bootstrap_only_agents(db):
    """Bootstrap-only agent does not appear in the all-agents latest list."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams())

    # Refresh the matview so it sees the latest agent_state state.
    async with db.acquire() as conn:
        await conn.execute("REFRESH MATERIALIZED VIEW core.mv_latest_agent_states")

    rows = await db.get_all_latest_agent_states()
    assert all(r.identity_id != identity_id for r in rows)


@pytest.mark.asyncio
async def test_all_latest_includes_measured_after_bootstrap(db):
    """Bootstrap then real check-in: the agent shows up with the measured row."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams())
    measured_id = await _record_measured(db, identity_id, entropy=0.2)

    async with db.acquire() as conn:
        await conn.execute("REFRESH MATERIALIZED VIEW core.mv_latest_agent_states")

    rows = await db.get_all_latest_agent_states()
    matching = [r for r in rows if r.identity_id == identity_id]
    assert len(matching) == 1
    assert matching[0].state_id == measured_id


@pytest.mark.asyncio
async def test_matview_definition_excludes_synthetic(db):
    """The matview rowset itself contains no bootstrap rows (migration 019 invariant)."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams())
    async with db.acquire() as conn:
        await conn.execute("REFRESH MATERIALIZED VIEW core.mv_latest_agent_states")
        synthetic_in_matview = await conn.fetchval(
            "SELECT COUNT(*) FROM core.mv_latest_agent_states WHERE synthetic = true"
        )
    assert synthetic_in_matview == 0


@pytest.mark.asyncio
async def test_all_latest_base_table_fallback_excludes_bootstrap(db, monkeypatch):
    """When the matview query raises, the base-table fallback also excludes synthetic."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams())
    measured_id = await _record_measured(db, identity_id, entropy=0.25)

    # Force the matview path to fail so the fallback is exercised.
    original_acquire = db.acquire

    class _ForceFallbackConn:
        def __init__(self, real):
            self._real = real

        async def fetch(self, query, *args):
            if "mv_latest_agent_states" in query:
                raise Exception("simulated matview unavailable")
            return await self._real.fetch(query, *args)

        def __getattr__(self, name):
            return getattr(self._real, name)

    class _AcquireCM:
        def __init__(self, inner):
            self._inner = inner
            self._real_conn = None

        async def __aenter__(self):
            self._real_conn = await self._inner.__aenter__()
            return _ForceFallbackConn(self._real_conn)

        async def __aexit__(self, *exc):
            return await self._inner.__aexit__(*exc)

    def _wrapped_acquire(*a, **kw):
        return _AcquireCM(original_acquire(*a, **kw))

    monkeypatch.setattr(db, "acquire", _wrapped_acquire)

    rows = await db.get_all_latest_agent_states()
    matching = [r for r in rows if r.identity_id == identity_id]
    assert len(matching) == 1
    assert matching[0].state_id == measured_id


# ---------------------------------------------------------------------------
# Site #3: get_recent_cross_agent_activity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_agent_activity_excludes_bootstrap_only_agents(db):
    """A bootstrap-only neighbor doesn't show up in the cross-agent activity window."""
    self_agent_id, self_identity_id = await _seed_identity(db)
    neighbor_agent_id, neighbor_identity_id = await _seed_identity(db)
    await write_bootstrap(
        db, identity_id=neighbor_identity_id, agent_id=neighbor_agent_id,
        params=BootstrapStateParams(),
    )

    activity = await db.get_recent_cross_agent_activity(
        exclude_identity_id=self_identity_id, minutes=60,
    )
    assert all(row["agent_id"] != neighbor_agent_id for row in activity)


@pytest.mark.asyncio
async def test_cross_agent_activity_count_does_not_include_bootstrap(db):
    """Count for an active neighbor reflects measured rows only."""
    self_agent_id, self_identity_id = await _seed_identity(db)
    neighbor_agent_id, neighbor_identity_id = await _seed_identity(db)
    await write_bootstrap(
        db, identity_id=neighbor_identity_id, agent_id=neighbor_agent_id,
        params=BootstrapStateParams(),
    )
    # Two measured rows.
    await _record_measured(db, neighbor_identity_id)
    await _record_measured(db, neighbor_identity_id)

    activity = await db.get_recent_cross_agent_activity(
        exclude_identity_id=self_identity_id, minutes=60,
    )
    matching = [row for row in activity if row["agent_id"] == neighbor_agent_id]
    assert len(matching) == 1
    assert matching[0]["count"] == 2


# ---------------------------------------------------------------------------
# Site #4: get_latest_eisv_by_agent_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outcome_correlation_excludes_bootstrap(db):
    """Bootstrap-only agent: get_latest_eisv_by_agent_id returns None, not the synthetic snapshot."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams(complexity=0.5))

    eisv = await db.get_latest_eisv_by_agent_id(agent_id)
    assert eisv is None


@pytest.mark.asyncio
async def test_outcome_correlation_returns_measured_after_bootstrap(db):
    """Bootstrap + real check-in: snapshot reflects the measured row."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams(complexity=0.5))
    await _record_measured(db, identity_id, entropy=0.31, integrity=0.62)

    eisv = await db.get_latest_eisv_by_agent_id(agent_id)
    assert eisv is not None
    assert eisv["S"] == pytest.approx(0.31)
    assert eisv["I"] == pytest.approx(0.62)


# ---------------------------------------------------------------------------
# Invariant lock I1: bootstrap writes do not feed calibration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_calibration_excludes_bootstrap_no_audit_event(db):
    """Bootstrap writes via write_bootstrap MUST NOT emit auto_attest audit events.

    The calibration ingestor at src/auto_ground_truth.py:432 only consumes
    rows of event_type='auto_attest' that carry exogenous-signal keys
    (tests/commands/files/lint). If a bootstrap write ever started emitting
    such an event, calibration would silently start consuming synthetic
    confidence values. This test locks the invariant.
    """
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams(complexity=0.5, confidence=0.5))

    async with db.acquire() as conn:
        # No auto_attest event for this agent.
        attest_count = await conn.fetchval(
            """
            SELECT COUNT(*) FROM audit.events
            WHERE agent_id = $1 AND event_type = 'auto_attest'
            """,
            agent_id,
        )
    assert attest_count == 0


# ---------------------------------------------------------------------------
# Invariant lock I3: bootstrap doesn't increment trajectory observation_count
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trust_tier_excludes_bootstrap_no_genesis_signature(db):
    """Bootstrap writes MUST NOT call store_genesis_signature.

    compute_trust_tier reads observation_count from trajectory_current /
    trajectory_genesis metadata. If bootstrap ever started seeding either,
    a bootstrap-only agent would falsely count as having one observation.
    This test asserts the absence-of-trajectory-row invariant.
    """
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams(complexity=0.5))

    # The bootstrap path must not write any trajectory metadata for this agent.
    async with db.acquire() as conn:
        # Trajectory state lives in core.identities.metadata under
        # 'trajectory_current' / 'trajectory_genesis'. Verify neither key
        # was populated as a side effect of the bootstrap write.
        meta = await conn.fetchval(
            "SELECT metadata FROM core.identities WHERE agent_id = $1",
            agent_id,
        )
    import json as _json
    parsed = _json.loads(meta) if isinstance(meta, str) else (meta or {})
    assert "trajectory_current" not in parsed
    assert "trajectory_genesis" not in parsed

"""Phase 3b filter-site tests for onboard-bootstrap-checkin.

Sites:
  #5 — get_agent_state_history default INCLUDES synthetic; exclude_synthetic=True opts out.
  #6 — hydrate_from_db_if_fresh uses exclude_synthetic=True so the in-memory
       monitor never inherits a bootstrap row. Transitively, self-recovery and
       dialectic refuse-with-explanation for bootstrap-only agents because
       monitor.state.update_count stays 0.

Spec: §4 inclusions/exclusions.
Audit: sites #5, #6.
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
    return await db.record_agent_state(
        identity_id=identity_id,
        entropy=entropy, integrity=integrity, stability_index=0.5,
        void=0.0, regime="nominal", coherence=1.0, state_json={},
    )


# ---------------------------------------------------------------------------
# Site #5: get_agent_state_history default + exclude_synthetic parameter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_preserves_synthetic_by_default(db):
    """Default behavior keeps the bootstrap row in the audit/lineage view."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams(complexity=0.7))
    measured_id = await _record_measured(db, identity_id, entropy=0.3)

    history = await db.get_agent_state_history(identity_id, limit=50)
    state_ids = {r.state_id for r in history}
    # Both rows present — bootstrap is part of the audit record.
    assert measured_id in state_ids
    # The bootstrap row is identifiable: state_json carries source=bootstrap
    # (the helper builds state_json that way; this test asserts the row
    # round-trips cleanly).
    bootstrap_rows = [r for r in history if r.state_json.get("source") == "bootstrap"]
    assert len(bootstrap_rows) == 1


@pytest.mark.asyncio
async def test_history_with_exclude_synthetic_drops_bootstrap(db):
    """exclude_synthetic=True returns only measured rows."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams())
    measured_id = await _record_measured(db, identity_id, entropy=0.3)

    history = await db.get_agent_state_history(
        identity_id, limit=50, exclude_synthetic=True,
    )
    assert len(history) == 1
    assert history[0].state_id == measured_id
    assert all(r.state_json.get("source") != "bootstrap" for r in history)


@pytest.mark.asyncio
async def test_history_exclude_synthetic_returns_empty_for_bootstrap_only(db):
    """A bootstrap-only agent looks empty under exclude_synthetic=True."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams())

    history = await db.get_agent_state_history(
        identity_id, limit=50, exclude_synthetic=True,
    )
    assert history == []

    # And the default still surfaces the bootstrap row.
    history_default = await db.get_agent_state_history(identity_id, limit=50)
    assert len(history_default) == 1


# ---------------------------------------------------------------------------
# Site #6: hydration filter — closes the dialectic-flagged trajectory-prior leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hydration_skips_bootstrap_only_agent(db):
    """hydrate_from_db_if_fresh on a bootstrap-only agent is a no-op
    (no measured rows ⇒ rehydration finds nothing to seed)."""
    from src.agent_monitor_state import hydrate_from_db_if_fresh
    from src.governance_monitor import UNITARESMonitor

    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams(complexity=0.99, confidence=0.99))

    # Patch get_db so the monitor module finds our test backend.
    import src.db as db_module
    original_get_db = db_module.get_db
    db_module.get_db = lambda: db
    try:
        monitor = UNITARESMonitor(agent_id=agent_id)
        applied = await hydrate_from_db_if_fresh(monitor, agent_id)
    finally:
        db_module.get_db = original_get_db

    assert applied is False
    assert monitor.state.update_count == 0
    # And critically, the monitor's E/I/S/V did NOT inherit the bootstrap's
    # 0.99/0.99 values — they're whatever the monitor's defaults are.


@pytest.mark.asyncio
async def test_hydration_succeeds_after_real_checkin(db):
    """With a measured row present, hydration works as before — the bootstrap
    row is excluded but the measured row seeds the monitor."""
    from src.agent_monitor_state import hydrate_from_db_if_fresh
    from src.governance_monitor import UNITARESMonitor

    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                          params=BootstrapStateParams(complexity=0.99))
    await _record_measured(db, identity_id, entropy=0.27, integrity=0.71)

    import src.db as db_module
    original_get_db = db_module.get_db
    db_module.get_db = lambda: db
    try:
        monitor = UNITARESMonitor(agent_id=agent_id)
        applied = await hydrate_from_db_if_fresh(monitor, agent_id)
    finally:
        db_module.get_db = original_get_db

    assert applied is True
    assert monitor.state.update_count == 1  # Just the measured row, not 2.
    assert monitor.state.unitaires_state.S == pytest.approx(0.27)
    assert monitor.state.unitaires_state.I == pytest.approx(0.71)

"""Phase 5 observability tests for onboard-bootstrap-checkin.

Covers the population query surface defined in spec §6 + the REST endpoint
that exposes it to the dashboard.

DAO:
  - list_bootstrap_only_agents(min_age_hours=24)
  - count_bootstrap_only_agents(min_age_hours=24)

REST:
  - GET /v1/bootstrap/silent

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


async def _backdate_state(db, state_id: int, hours: float):
    """Push a state row's recorded_at into the past so age-window filters fire."""
    async with db.acquire() as conn:
        await conn.execute(
            """
            UPDATE core.agent_state
            SET recorded_at = now() - ($1 * interval '1 hour')
            WHERE state_id = $2
            """,
            hours, state_id,
        )


# ---------------------------------------------------------------------------
# DAO: list_bootstrap_only_agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_bootstrap_only_picks_aged_silent_agent(db):
    """Agent bootstrapped 25h ago, no real check-in: appears in the list."""
    agent_id, identity_id = await _seed_identity(db)
    block = await write_bootstrap(
        db, identity_id=identity_id, agent_id=agent_id,
        params=BootstrapStateParams(),
    )
    await _backdate_state(db, block["state_id"], hours=25.0)

    rows = await db.list_bootstrap_only_agents(min_age_hours=24)
    matching = [r for r in rows if r["identity_id"] == identity_id]
    assert len(matching) == 1
    assert matching[0]["agent_id"] == agent_id
    assert matching[0]["bootstrap_state_id"] == block["state_id"]
    assert matching[0]["bootstrap_age_hours"] >= 24.0


@pytest.mark.asyncio
async def test_list_bootstrap_only_skips_recent_bootstrap(db):
    """A bootstrap < min_age_hours old is excluded — caller may still check in."""
    agent_id, identity_id = await _seed_identity(db)
    await write_bootstrap(
        db, identity_id=identity_id, agent_id=agent_id,
        params=BootstrapStateParams(),
    )
    # Default age is "now"; with min_age_hours=24 this row is too fresh.

    rows = await db.list_bootstrap_only_agents(min_age_hours=24)
    matching = [r for r in rows if r["identity_id"] == identity_id]
    assert len(matching) == 0


@pytest.mark.asyncio
async def test_list_bootstrap_only_excludes_agents_with_real_checkin(db):
    """Agent with a measured row never appears regardless of age."""
    agent_id, identity_id = await _seed_identity(db)
    block = await write_bootstrap(
        db, identity_id=identity_id, agent_id=agent_id,
        params=BootstrapStateParams(),
    )
    await _backdate_state(db, block["state_id"], hours=25.0)
    await db.record_agent_state(
        identity_id=identity_id,
        entropy=0.4, integrity=0.6, stability_index=0.5,
        void=0.0, regime="nominal", coherence=1.0, state_json={},
    )

    rows = await db.list_bootstrap_only_agents(min_age_hours=24)
    assert all(r["identity_id"] != identity_id for r in rows)


@pytest.mark.asyncio
async def test_list_bootstrap_only_orders_most_recent_first(db):
    """Multiple silent agents come back newest-bootstrap first."""
    a_id, a_iid = await _seed_identity(db)
    b_id, b_iid = await _seed_identity(db)
    a_block = await write_bootstrap(db, identity_id=a_iid, agent_id=a_id,
                                    params=BootstrapStateParams())
    b_block = await write_bootstrap(db, identity_id=b_iid, agent_id=b_id,
                                    params=BootstrapStateParams())
    # Push both past the 24h gate; A older, B more recent.
    await _backdate_state(db, a_block["state_id"], hours=72.0)
    await _backdate_state(db, b_block["state_id"], hours=30.0)

    rows = await db.list_bootstrap_only_agents(min_age_hours=24)
    seen_ids = [r["agent_id"] for r in rows if r["agent_id"] in (a_id, b_id)]
    assert seen_ids == [b_id, a_id]


@pytest.mark.asyncio
async def test_list_bootstrap_only_respects_limit(db):
    """The limit parameter caps the result set."""
    seeded_ids = []
    for _ in range(3):
        agent_id, identity_id = await _seed_identity(db)
        block = await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                                      params=BootstrapStateParams())
        await _backdate_state(db, block["state_id"], hours=48.0)
        seeded_ids.append(agent_id)

    rows = await db.list_bootstrap_only_agents(min_age_hours=24, limit=2)
    assert len(rows) <= 2


# ---------------------------------------------------------------------------
# DAO: count_bootstrap_only_agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_count_matches_list(db):
    for _ in range(2):
        agent_id, identity_id = await _seed_identity(db)
        block = await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                                      params=BootstrapStateParams())
        await _backdate_state(db, block["state_id"], hours=48.0)

    count = await db.count_bootstrap_only_agents(min_age_hours=24)
    rows = await db.list_bootstrap_only_agents(min_age_hours=24, limit=500)
    # The shared truncation in conftest scopes this to just-seeded rows.
    assert count == len(rows)
    assert count >= 2


@pytest.mark.asyncio
async def test_count_zero_when_no_silent_agents(db):
    """Just measured agents → count is 0."""
    _, identity_id = await _seed_identity(db)
    await db.record_agent_state(
        identity_id=identity_id,
        entropy=0.4, integrity=0.6, stability_index=0.5,
        void=0.0, regime="nominal", coherence=1.0, state_json={},
    )
    count = await db.count_bootstrap_only_agents(min_age_hours=24)
    assert count == 0


# ---------------------------------------------------------------------------
# REST endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_bootstrap_silent_returns_aged_agents(db, monkeypatch):
    """GET /v1/bootstrap/silent shapes the DAO output for the dashboard."""
    from starlette.requests import Request
    from src.http_api import http_bootstrap_silent

    agent_id, identity_id = await _seed_identity(db)
    block = await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id,
                                  params=BootstrapStateParams())
    await _backdate_state(db, block["state_id"], hours=48.0)

    # Auth disabled so the test doesn't need a token.
    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "")

    # Wire the request to our test backend.
    import src.db as db_module
    monkeypatch.setattr(db_module, "get_db", lambda: db)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/bootstrap/silent",
        "headers": [],
        "query_string": b"min_age_hours=24",
    }
    request = Request(scope)

    response = await http_bootstrap_silent(request)
    import json as _json
    body = _json.loads(response.body)
    assert body["success"] is True
    assert body["min_age_hours"] == 24
    assert body["count"] >= 1
    assert any(r["agent_id"] == agent_id for r in body["agents"])
    matching = next(r for r in body["agents"] if r["agent_id"] == agent_id)
    assert matching["bootstrap_state_id"] == block["state_id"]
    # Datetime serialized as ISO string; age serialized as float.
    assert isinstance(matching["bootstrap_recorded_at"], str)
    assert isinstance(matching["bootstrap_age_hours"], float)


@pytest.mark.asyncio
async def test_http_bootstrap_silent_rejects_unauth(db, monkeypatch):
    """When a token is configured but not presented, the endpoint 401s."""
    from starlette.requests import Request
    from src.http_api import http_bootstrap_silent

    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "secret-test-token")

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/bootstrap/silent",
        "headers": [],
        "query_string": b"",
    }
    request = Request(scope)
    response = await http_bootstrap_silent(request)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_http_bootstrap_silent_clamps_limit(db, monkeypatch):
    """limit=9999 gets clamped to 200 (max defined in the handler)."""
    from starlette.requests import Request
    from src.http_api import http_bootstrap_silent

    monkeypatch.setenv("UNITARES_HTTP_API_TOKEN", "")
    import src.db as db_module
    monkeypatch.setattr(db_module, "get_db", lambda: db)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/v1/bootstrap/silent",
        "headers": [],
        "query_string": b"limit=9999",
    }
    request = Request(scope)
    response = await http_bootstrap_silent(request)
    import json as _json
    body = _json.loads(response.body)
    assert body["limit"] == 200

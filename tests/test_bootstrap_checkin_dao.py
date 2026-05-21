"""Integration tests for bootstrap-checkin DAO + write_bootstrap helper.

These exercise the end-to-end path the onboard handler calls. Handler-level
HTTP/MCP tests live separately and depend on more wiring; the contract this
file pins is the DB-touching surface that the handler trusts.

"""

from __future__ import annotations

import asyncio
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

from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)

from src.mcp_handlers.identity.bootstrap_checkin import (
    PI_RESIDENT_ALLOWLIST,
    write_bootstrap,
)
from src.mcp_handlers.schemas.core import BootstrapStateParams

# Reuse the canonical live-DB fixture from conftest.py — that fixture
# bootstraps the schema, truncates between tests, and handles teardown.
# Alias to `db` so the test bodies read naturally.


@pytest.fixture
def db(live_postgres_backend):
    return live_postgres_backend


async def _seed_identity(db) -> tuple[str, int]:
    """Insert a fresh agent + identity, return (agent_id, identity_id)."""
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


# ---------------------------------------------------------------------------
# DAO contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_bootstrap_state_writes_synthetic_row(db):
    _, identity_id = await _seed_identity(db)
    state_id, was_written = await db.record_bootstrap_state(
        identity_id=identity_id,
        entropy=0.5, integrity=0.5, stability_index=0.5, void=0.0,
        regime="nominal", coherence=1.0,
        state_json={"source": "bootstrap", "bootstrap_digest": "abc"},
    )
    assert was_written is True
    assert state_id is not None

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT synthetic, state_json FROM core.agent_state WHERE state_id = $1",
            state_id,
        )
    assert row["synthetic"] is True


@pytest.mark.asyncio
async def test_record_bootstrap_state_idempotent_returns_existing(db):
    _, identity_id = await _seed_identity(db)
    state_id_1, written_1 = await db.record_bootstrap_state(
        identity_id=identity_id,
        entropy=0.5, integrity=0.5, stability_index=0.5, void=0.0,
        regime="nominal", coherence=1.0,
        state_json={"source": "bootstrap", "bootstrap_digest": "first"},
    )
    state_id_2, written_2 = await db.record_bootstrap_state(
        identity_id=identity_id,
        entropy=0.7, integrity=0.3, stability_index=0.6, void=0.1,
        regime="warning", coherence=0.5,
        state_json={"source": "bootstrap", "bootstrap_digest": "second"},
    )
    assert written_1 is True
    assert written_2 is False
    assert state_id_1 == state_id_2

    async with db.acquire() as conn:
        digest = await conn.fetchval(
            "SELECT state_json->>'bootstrap_digest' FROM core.agent_state WHERE state_id = $1",
            state_id_1,
        )
    # First write wins; second-call payload was discarded.
    assert digest == "first"


@pytest.mark.asyncio
async def test_get_bootstrap_state_round_trips(db):
    _, identity_id = await _seed_identity(db)
    state_id, _ = await db.record_bootstrap_state(
        identity_id=identity_id,
        entropy=0.5, integrity=0.5, stability_index=0.5, void=0.0,
        regime="nominal", coherence=1.0,
        state_json={"source": "bootstrap", "bootstrap_digest": "abc123"},
    )
    fetched = await db.get_bootstrap_state(identity_id)
    assert fetched is not None
    assert fetched["state_id"] == state_id
    assert fetched["state_json"]["bootstrap_digest"] == "abc123"
    assert fetched["state_json"]["source"] == "bootstrap"

    # No bootstrap → None.
    _, identity_id_2 = await _seed_identity(db)
    assert await db.get_bootstrap_state(identity_id_2) is None


@pytest.mark.asyncio
async def test_is_substrate_earned_via_substrate_claims(db):
    """An agent in core.substrate_claims is substrate-earned."""
    agent_id, _ = await _seed_identity(db)
    assert await db.is_substrate_earned(agent_id) is False

    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO core.substrate_claims
                (agent_id, expected_launchd_label, expected_executable_path)
            VALUES ($1, 'test.label', '/usr/local/bin/test')
            """,
            agent_id,
        )
    assert await db.is_substrate_earned(agent_id) is True


@pytest.mark.asyncio
async def test_is_substrate_earned_via_pi_allowlist(db, monkeypatch):
    """An agent in the Pi-resident allowlist is substrate-earned without a substrate_claims row."""
    agent_id, _ = await _seed_identity(db)
    assert await db.is_substrate_earned(agent_id) is False

    monkeypatch.setattr(
        "src.mcp_handlers.identity.bootstrap_checkin.PI_RESIDENT_ALLOWLIST",
        frozenset({agent_id}),
    )
    assert await db.is_substrate_earned(agent_id) is True


# ---------------------------------------------------------------------------
# write_bootstrap end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_bootstrap_first_call_writes_row(db):
    agent_id, identity_id = await _seed_identity(db)
    block = await write_bootstrap(
        db,
        identity_id=identity_id,
        agent_id=agent_id,
        params=BootstrapStateParams(complexity=0.7, confidence=0.6),
        client_hint="test-harness",
    )
    assert block["written"] is True
    assert "state_id" in block
    assert "next_step" in block

    async with db.acquire() as conn:
        sj = await conn.fetchval(
            "SELECT state_json FROM core.agent_state WHERE state_id = $1",
            block["state_id"],
        )
    import json as _json
    parsed = _json.loads(sj) if isinstance(sj, str) else sj
    assert parsed["source"] == "bootstrap"
    assert parsed["complexity"] == 0.7
    assert parsed["confidence"] == 0.6
    assert "bootstrap_digest" in parsed


@pytest.mark.asyncio
async def test_write_bootstrap_second_call_with_same_payload_matches_digest(db):
    agent_id, identity_id = await _seed_identity(db)
    params = BootstrapStateParams(complexity=0.7, confidence=0.6)
    first = await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id, params=params)
    second = await write_bootstrap(db, identity_id=identity_id, agent_id=agent_id, params=params)

    assert first["written"] is True
    assert second["written"] is False
    assert second["state_id"] == first["state_id"]
    assert second["payload_digest_match"] is True


@pytest.mark.asyncio
async def test_write_bootstrap_second_call_with_different_payload_flags_mismatch(db):
    agent_id, identity_id = await _seed_identity(db)
    first = await write_bootstrap(
        db, identity_id=identity_id, agent_id=agent_id,
        params=BootstrapStateParams(complexity=0.7),
    )
    second = await write_bootstrap(
        db, identity_id=identity_id, agent_id=agent_id,
        params=BootstrapStateParams(complexity=0.2),
    )
    assert second["written"] is False
    assert second["payload_digest_match"] is False
    # Stored row keeps the original payload.
    stored = await db.get_bootstrap_state(identity_id)
    assert stored["state_json"]["complexity"] == 0.7


@pytest.mark.asyncio
async def test_write_bootstrap_substrate_earned_skips_write(db):
    agent_id, identity_id = await _seed_identity(db)
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO core.substrate_claims
                (agent_id, expected_launchd_label, expected_executable_path)
            VALUES ($1, 'test.label', '/usr/local/bin/test')
            """,
            agent_id,
        )

    block = await write_bootstrap(
        db, identity_id=identity_id, agent_id=agent_id,
        params=BootstrapStateParams(complexity=0.5),
    )
    assert block == {"written": False, "reason": "substrate-earned-exempt"}

    # Confirm no synthetic row was written.
    assert await db.get_bootstrap_state(identity_id) is None


@pytest.mark.asyncio
async def test_concurrent_write_bootstrap_at_most_one_row(db):
    """Two simultaneous write_bootstrap calls for the same identity produce
    exactly one synthetic row (DB-level race resolved by the unique partial
    index from migration 018)."""
    agent_id, identity_id = await _seed_identity(db)
    params = BootstrapStateParams(complexity=0.5)

    results = await asyncio.gather(
        write_bootstrap(db, identity_id=identity_id, agent_id=agent_id, params=params),
        write_bootstrap(db, identity_id=identity_id, agent_id=agent_id, params=params),
    )
    written_count = sum(1 for r in results if r.get("written") is True)
    assert written_count == 1

    async with db.acquire() as conn:
        row_count = await conn.fetchval(
            "SELECT COUNT(*) FROM core.agent_state WHERE identity_id = $1 AND synthetic = true",
            identity_id,
        )
    assert row_count == 1

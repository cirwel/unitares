"""Test fixtures for R2 lineage-lifecycle storage tests.

Reuses the canonical `live_postgres_backend` fixture from the top-level
conftest. Adds three lineage-pair fixtures:

- ``seeded_pair``           — provisional successor (parent_agent_id set,
                               provisional_lineage=TRUE, lineage_declared_at
                               stamped).
- ``confirmed_pair``         — confirmed successor (provisional_lineage=FALSE,
                               confirmed_at set).
- ``confirmed_pair_with_obs`` — confirmed successor + chain_obs_count=5.

Fixtures rely on R1's existing ``provisional_lineage`` / ``confirmed_at``
columns (migration 031) and the new R2 columns (migration 036).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest_asyncio


@dataclass
class LineagePair:
    parent_id: str
    successor_id: str


def _uuid_suffix() -> str:
    return uuid.uuid4().hex[:12]


async def _insert_identity(
    backend,
    agent_id: str,
    *,
    parent_agent_id: str | None = None,
    provisional_lineage: bool = False,
    confirmed: bool = False,
) -> None:
    """Seed (core.agents, core.identities) for a single agent.

    Mirrors the inline helper used in tests/test_bootstrap_checkin_dao.py
    (`_seed_identity`) and tests/test_class_promotion.py — direct
    INSERT INTO core.identities, then stamp the provisional/confirmed
    columns separately so this stays readable.
    """
    async with backend.acquire() as conn:
        await conn.execute(
            "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key') "
            "ON CONFLICT (id) DO NOTHING",
            agent_id,
        )
        await conn.execute(
            """
            INSERT INTO core.identities (
                agent_id, api_key_hash, parent_agent_id
            )
            VALUES ($1, 'test-hash', $2)
            ON CONFLICT (agent_id) DO UPDATE SET
                parent_agent_id = EXCLUDED.parent_agent_id
            """,
            agent_id,
            parent_agent_id,
        )
        if provisional_lineage:
            await conn.execute(
                """
                UPDATE core.identities
                   SET provisional_lineage = TRUE,
                       provisional_recorded_at = now(),
                       lineage_declared_at = now()
                 WHERE agent_id = $1
                """,
                agent_id,
            )
        elif confirmed:
            await conn.execute(
                """
                UPDATE core.identities
                   SET provisional_lineage = FALSE,
                       confirmed_at = now(),
                       lineage_declared_at = now()
                 WHERE agent_id = $1
                """,
                agent_id,
            )
        elif parent_agent_id is not None:
            # Parent set but neither provisional nor confirmed flag —
            # stamp lineage_declared_at so the row looks like a freshly
            # declared lineage edge.
            await conn.execute(
                "UPDATE core.identities SET lineage_declared_at = now() "
                "WHERE agent_id = $1",
                agent_id,
            )


async def _cleanup(backend, agent_ids: list[str]) -> None:
    async with backend.acquire() as conn:
        await conn.execute(
            "DELETE FROM core.identities WHERE agent_id = ANY($1::text[])",
            agent_ids,
        )
        await conn.execute(
            "DELETE FROM core.agents WHERE id = ANY($1::text[])",
            agent_ids,
        )


@pytest_asyncio.fixture
async def seeded_pair(live_postgres_backend):
    """Provisional pair: parent + successor with parent_agent_id set,
    provisional_lineage=TRUE, lineage_declared_at stamped."""
    parent_id = "test-parent-" + _uuid_suffix()
    successor_id = "test-successor-" + _uuid_suffix()
    await _insert_identity(live_postgres_backend, parent_id)
    await _insert_identity(
        live_postgres_backend, successor_id,
        parent_agent_id=parent_id, provisional_lineage=True,
    )
    yield LineagePair(parent_id, successor_id)
    await _cleanup(live_postgres_backend, [parent_id, successor_id])


@pytest_asyncio.fixture
async def confirmed_pair(live_postgres_backend):
    """Confirmed pair: provisional_lineage=FALSE, confirmed_at set."""
    parent_id = "test-parent-" + _uuid_suffix()
    successor_id = "test-successor-" + _uuid_suffix()
    await _insert_identity(live_postgres_backend, parent_id)
    await _insert_identity(
        live_postgres_backend, successor_id,
        parent_agent_id=parent_id, confirmed=True,
    )
    yield LineagePair(parent_id, successor_id)
    await _cleanup(live_postgres_backend, [parent_id, successor_id])


@pytest_asyncio.fixture
async def confirmed_pair_with_obs(live_postgres_backend, confirmed_pair):
    """Confirmed pair + chain_obs_count = 5."""
    for _ in range(5):
        await live_postgres_backend.increment_chain_obs_count(
            confirmed_pair.successor_id,
        )
    return confirmed_pair

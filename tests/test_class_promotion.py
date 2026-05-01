"""S8a Phase-2 promotion sweep tests — live DB.

Integration tests against the governance PostgreSQL instance. Per the
project rule (memory: integration-tests-with-real-db), promotion logic is
not mocked: a mock and a misnamed JSON path or jsonb operator silently
agree, while the real DB rejects.

Each test inserts identities under a unique ``test_promotion_*`` agent_id
prefix, runs the promotion, asserts tag transitions, and cleans up.
"""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio

from src.grounding.class_promotion import (
    PROMOTION_SOURCE_TAG,
    PROMOTION_TARGET_TAG,
    promote_engaged_ephemeral,
    stamp_untagged_identities,
)


pytestmark = pytest.mark.asyncio


async def _insert_identity(conn, *, agent_id: str, tags: list, total_updates: int, status: str = "active", label: str = None):
    """Insert a test identity. ``core.identities.agent_id`` has an FK to
    ``core.agents.agent_id``, so we ensure the agents row exists first."""
    metadata = {"tags": tags, "total_updates": total_updates}
    if label is not None:
        metadata["label"] = label
    await conn.execute(
        """
        INSERT INTO core.agents (id, api_key)
        VALUES ($1, $2)
        ON CONFLICT (id) DO NOTHING
        """,
        agent_id,
        f"test-agent-key-{agent_id}",
    )
    await conn.execute(
        """
        INSERT INTO core.identities (agent_id, api_key_hash, metadata, status)
        VALUES ($1, $2, $3::jsonb, $4)
        """,
        agent_id,
        f"test-key-hash-{agent_id}",
        json.dumps(metadata),
        status,
    )


async def _fetch_tags(conn, agent_id: str):
    row = await conn.fetchrow(
        "SELECT (metadata->'tags')::text AS tags FROM core.identities WHERE agent_id = $1",
        agent_id,
    )
    if not row or row["tags"] is None:
        return None
    return json.loads(row["tags"])


async def _fetch_status(conn, agent_id: str):
    return await conn.fetchval(
        "SELECT status FROM core.identities WHERE agent_id = $1",
        agent_id,
    )


async def _cleanup(conn, prefix: str):
    # ON DELETE CASCADE on identities_agent_id_fkey takes care of identities.
    await conn.execute(
        "DELETE FROM core.agents WHERE id LIKE $1",
        f"{prefix}%",
    )


@pytest_asyncio.fixture
async def db_conn(live_postgres_backend):
    """Live test DB connection with a per-test agent_id prefix.

    Bypasses the autouse ``_isolate_db_backend`` mock by going through the
    ``live_postgres_backend`` fixture (which connects to ``governance_test``
    on the same Postgres instance). Tests pass the backend to
    ``promote_engaged_ephemeral(..., db=backend)`` explicitly.
    """
    backend = live_postgres_backend
    prefix = f"test_promotion_{uuid.uuid4().hex[:8]}_"
    async with backend.acquire() as conn:
        yield conn, prefix, backend
        try:
            await _cleanup(conn, prefix)
        except Exception:
            # The truncate inside live_postgres_backend may already have
            # cleaned the test rows; we don't fail teardown on this.
            pass


class TestPromoteEphemeralToSessionLike:
    async def test_promotes_ephemeral_with_three_updates(self, db_conn):
        conn, prefix, backend = db_conn
        aid = f"{prefix}three_updates"
        await _insert_identity(conn, agent_id=aid, tags=["ephemeral"], total_updates=3)

        result = await promote_engaged_ephemeral(db=backend, threshold=3)

        assert result["promoted"] >= 1
        tags = await _fetch_tags(conn, aid)
        assert tags == [PROMOTION_TARGET_TAG]

    async def test_does_not_promote_below_threshold(self, db_conn):
        conn, prefix, backend = db_conn
        aid = f"{prefix}two_updates"
        await _insert_identity(conn, agent_id=aid, tags=["ephemeral"], total_updates=2)

        await promote_engaged_ephemeral(db=backend, threshold=3)

        tags = await _fetch_tags(conn, aid)
        assert tags == [PROMOTION_SOURCE_TAG]

    async def test_promotes_at_exact_threshold(self, db_conn):
        conn, prefix, backend = db_conn
        aid = f"{prefix}exact"
        await _insert_identity(conn, agent_id=aid, tags=["ephemeral"], total_updates=3)

        await promote_engaged_ephemeral(db=backend, threshold=3)

        tags = await _fetch_tags(conn, aid)
        assert tags == [PROMOTION_TARGET_TAG]

    async def test_skips_already_promoted(self, db_conn):
        """Idempotency: re-running the sweep doesn't double-promote."""
        conn, prefix, backend = db_conn
        aid = f"{prefix}already"
        await _insert_identity(conn, agent_id=aid, tags=[PROMOTION_TARGET_TAG], total_updates=10)

        result = await promote_engaged_ephemeral(db=backend, threshold=3)

        # The target row shouldn't be touched (it's not ephemeral).
        tags = await _fetch_tags(conn, aid)
        assert tags == [PROMOTION_TARGET_TAG]
        # And it shouldn't appear in the affected set if isolated tests pass.
        # (We assert via unchanged tags above; running tally varies with concurrent rows.)
        _ = result

    async def test_skips_resident_tagged(self, db_conn):
        conn, prefix, backend = db_conn
        aid = f"{prefix}resident"
        await _insert_identity(
            conn, agent_id=aid, tags=["persistent", "autonomous"], total_updates=50
        )

        await promote_engaged_ephemeral(db=backend, threshold=3)

        tags = await _fetch_tags(conn, aid)
        assert sorted(tags) == ["autonomous", "persistent"]

    async def test_skips_archived_by_default(self, db_conn):
        conn, prefix, backend = db_conn
        aid = f"{prefix}archived"
        await _insert_identity(
            conn, agent_id=aid, tags=["ephemeral"], total_updates=10, status="archived"
        )

        await promote_engaged_ephemeral(db=backend, threshold=3)

        tags = await _fetch_tags(conn, aid)
        assert tags == [PROMOTION_SOURCE_TAG]

    async def test_includes_archived_when_requested(self, db_conn):
        """Decision (d) backfill path: ``include_archived=True`` covers the
        ~3180 archived backlog."""
        conn, prefix, backend = db_conn
        aid = f"{prefix}archived_include"
        await _insert_identity(
            conn, agent_id=aid, tags=["ephemeral"], total_updates=10, status="archived"
        )

        await promote_engaged_ephemeral(db=backend, threshold=3, include_archived=True)

        tags = await _fetch_tags(conn, aid)
        assert tags == [PROMOTION_TARGET_TAG]
        # Status itself is not changed by promotion — only tags.
        assert await _fetch_status(conn, aid) == "archived"

    async def test_dry_run_does_not_write(self, db_conn):
        conn, prefix, backend = db_conn
        aid = f"{prefix}dryrun"
        await _insert_identity(conn, agent_id=aid, tags=["ephemeral"], total_updates=5)

        result = await promote_engaged_ephemeral(db=backend, threshold=3, dry_run=True)

        assert result["dry_run"] is True
        assert result["promoted"] == 0
        assert result["would_promote"] >= 1
        # Tag is unchanged.
        tags = await _fetch_tags(conn, aid)
        assert tags == [PROMOTION_SOURCE_TAG]

    async def test_dry_run_sample_includes_label(self, db_conn):
        conn, prefix, backend = db_conn
        aid = f"{prefix}sample"
        await _insert_identity(
            conn, agent_id=aid, tags=["ephemeral"], total_updates=7, label="claude_code-test"
        )

        result = await promote_engaged_ephemeral(db=backend, threshold=3, dry_run=True)

        sample_ids = [s["agent_id"] for s in result["sample"]]
        assert aid in sample_ids
        sample_for_aid = next(s for s in result["sample"] if s["agent_id"] == aid)
        assert sample_for_aid["label"] == "claude_code-test"
        assert sample_for_aid["total_updates"] == 7

    async def test_preserves_other_tags_alongside_ephemeral(self, db_conn):
        """``ephemeral`` is removed; coexisting tags survive the promotion.
        Edge case from the day-7 audit (``["Iris"]`` literal-as-tag bug
        produced rows with non-class tags; future bugs of that shape
        shouldn't drop data on promotion)."""
        conn, prefix, backend = db_conn
        aid = f"{prefix}coexist"
        await _insert_identity(
            conn,
            agent_id=aid,
            tags=["ephemeral", "experimental"],
            total_updates=10,
        )

        await promote_engaged_ephemeral(db=backend, threshold=3)

        tags = await _fetch_tags(conn, aid)
        assert PROMOTION_SOURCE_TAG not in tags
        assert PROMOTION_TARGET_TAG in tags
        assert "experimental" in tags

    async def test_limit_caps_promotions(self, db_conn):
        conn, prefix, backend = db_conn
        for i in range(5):
            aid = f"{prefix}limit_{i}"
            await _insert_identity(conn, agent_id=aid, tags=["ephemeral"], total_updates=10)

        result = await promote_engaged_ephemeral(db=backend, threshold=3, limit=2)

        # The exact count depends on concurrent test data, but our 5 rows
        # should be sorted by total_updates DESC and only 2 of them get promoted
        # ahead of any other concurrent ephemeral-with-≥3 rows. The contract
        # assertion is "≤ limit promotions occurred from our test set."
        promoted_count_in_test_set = 0
        for i in range(5):
            aid = f"{prefix}limit_{i}"
            tags = await _fetch_tags(conn, aid)
            if tags == [PROMOTION_TARGET_TAG]:
                promoted_count_in_test_set += 1
        assert promoted_count_in_test_set <= 2
        assert result["promoted"] >= promoted_count_in_test_set

    async def test_idempotent_double_run(self, db_conn):
        """Re-running the sweep on the same data is a no-op for already-promoted
        rows. Council finding (dialectic #6) tested directly: after promotion,
        the row's tags = [engaged_ephemeral] (no duplicate target tag, no
        residual ephemeral)."""
        conn, prefix, backend = db_conn
        aid = f"{prefix}idempotent"
        await _insert_identity(conn, agent_id=aid, tags=["ephemeral"], total_updates=5)

        await promote_engaged_ephemeral(db=backend, threshold=3)
        await promote_engaged_ephemeral(db=backend, threshold=3)

        tags = await _fetch_tags(conn, aid)
        assert tags == [PROMOTION_TARGET_TAG]
        # No duplicate target tag from running twice.
        assert tags.count(PROMOTION_TARGET_TAG) == 1

    async def test_empty_string_total_updates_is_treated_as_zero(self, db_conn):
        """Council finding (HIGH#3): bare ``::int`` cast crashes on
        empty-string ``total_updates``. With the NULLIF guard, an empty
        string is treated as 0 (so the row is not a promotion candidate)
        instead of crashing the whole sweep."""
        conn, prefix, backend = db_conn
        aid = f"{prefix}empty_str_updates"
        # Insert with literal empty-string total_updates to exercise the
        # NULLIF path. Use raw SQL so we can write the empty string.
        await conn.execute(
            "INSERT INTO core.agents (id, api_key) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            aid,
            f"test-key-{aid}",
        )
        await conn.execute(
            """
            INSERT INTO core.identities (agent_id, api_key_hash, metadata, status)
            VALUES ($1, $2, '{"tags":["ephemeral"],"total_updates":""}'::jsonb, 'active')
            """,
            aid,
            f"test-key-hash-{aid}",
        )

        # Should not crash:
        result = await promote_engaged_ephemeral(db=backend, threshold=3)

        # The empty-string row is not a candidate (treated as 0).
        tags = await _fetch_tags(conn, aid)
        assert tags == [PROMOTION_SOURCE_TAG]
        # And the function returns normally with a non-error result.
        assert "promoted" in result


class TestStampUntaggedIdentities:
    """Phase-1 stamp-gap backfill: stamp default class tags on identities
    that were created via a path that missed the live stamp."""

    async def test_stamps_anonymous_untagged_as_ephemeral(self, db_conn):
        conn, prefix, backend = db_conn
        aid = f"{prefix}untagged_anon"
        # Insert with no tags + no label.
        await conn.execute(
            "INSERT INTO core.agents (id, api_key) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            aid,
            f"test-key-{aid}",
        )
        await conn.execute(
            """
            INSERT INTO core.identities (agent_id, api_key_hash, metadata, status)
            VALUES ($1, $2, '{"total_updates":1}'::jsonb, 'active')
            """,
            aid,
            f"test-key-hash-{aid}",
        )

        result = await stamp_untagged_identities(db=backend)

        assert result["stamped"] >= 1
        tags = await _fetch_tags(conn, aid)
        assert tags == ["ephemeral"]

    async def test_stamps_known_resident_label_as_resident(self, db_conn):
        conn, prefix, backend = db_conn
        aid = f"{prefix}untagged_resident"
        await conn.execute(
            "INSERT INTO core.agents (id, api_key) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            aid,
            f"test-key-{aid}",
        )
        # Sentinel is in KNOWN_RESIDENT_LABELS.
        await conn.execute(
            """
            INSERT INTO core.identities (agent_id, api_key_hash, metadata, status)
            VALUES ($1, $2, $3::jsonb, 'active')
            """,
            aid,
            f"test-key-hash-{aid}",
            json.dumps({"label": "Sentinel", "total_updates": 50}),
        )

        await stamp_untagged_identities(db=backend)

        tags = await _fetch_tags(conn, aid)
        assert sorted(tags) == ["autonomous", "persistent"]

    async def test_does_not_overwrite_existing_tags(self, db_conn):
        conn, prefix, backend = db_conn
        aid = f"{prefix}has_tags"
        await _insert_identity(
            conn, agent_id=aid, tags=["ephemeral"], total_updates=2
        )

        await stamp_untagged_identities(db=backend)

        tags = await _fetch_tags(conn, aid)
        # Unchanged — the row already had a class tag.
        assert tags == ["ephemeral"]

    async def test_dry_run_does_not_write(self, db_conn):
        conn, prefix, backend = db_conn
        aid = f"{prefix}dry_untagged"
        await conn.execute(
            "INSERT INTO core.agents (id, api_key) VALUES ($1, $2) ON CONFLICT (id) DO NOTHING",
            aid,
            f"test-key-{aid}",
        )
        await conn.execute(
            """
            INSERT INTO core.identities (agent_id, api_key_hash, metadata, status)
            VALUES ($1, $2, '{}'::jsonb, 'active')
            """,
            aid,
            f"test-key-hash-{aid}",
        )

        result = await stamp_untagged_identities(db=backend, dry_run=True)

        assert result["dry_run"] is True
        assert result["stamped"] == 0
        assert result["would_stamp"] >= 1
        tags = await _fetch_tags(conn, aid)
        assert tags is None  # still untagged

"""R2 PR 3: cross-role pre-check tests.

Covers `pre_check_cross_role` in `src/identity/lineage_lifecycle.py`
and the new `read_class_tag` backend helper in
`src/db/mixins/identity.py`.

Mock-based tests (mocked DB backend) exercise every branch of the
pre-check decision tree. Live-DB tests verify the `read_class_tag`
helper's JSONB shape against postgres directly — this is the same
"mock and a misnamed jsonb operator silently agree" risk that
`test_class_promotion.py` documents, so we mirror that pattern.

See: docs/handoffs/2026-05-04-r2-implementation-plan.md PR 3
     docs/ontology/r2-honest-memory-integration.md §"Cross-role pre-check"
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.identity.lineage_lifecycle import pre_check_cross_role


# ---------------------------------------------------------------------------
# 1. Same-class → accept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_check_same_class_returns_none():
    """Parent and successor both ephemeral → pre-check accepts (None)."""
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value="ephemeral")

    result = await pre_check_cross_role("parent-uuid", "ephemeral")

    assert result is None
    backend.read_class_tag.assert_awaited_once_with("parent-uuid")


# ---------------------------------------------------------------------------
# 2. Different class → reject with rejection dict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_check_different_class_returns_rejection():
    """parent=persistent (resident), successor=ephemeral → rejection.

    The S8a default for resident agents is `["persistent",
    "autonomous"]` (residents need both — see
    `src/grounding/onboard_classifier.py:RESIDENT_DEFAULT_TAGS`), so
    a resident's primary class is `persistent`. An ephemeral
    declaring a resident as parent is the canonical cross-role case.
    """
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value="persistent")

    result = await pre_check_cross_role("resident-uuid", "ephemeral")

    assert result is not None
    assert result["parent_class"] == "persistent"
    assert result["successor_class"] == "ephemeral"
    assert result["reason"] == "role_envelope_mismatch"


# ---------------------------------------------------------------------------
# 3. Charitable default — parent has no class tag → accept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_check_orphan_parent_charitable_default():
    """Parent has no class tag (pre-S8a backfill) → accept."""
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value=None)

    result = await pre_check_cross_role("orphan-parent", "ephemeral")

    assert result is None


# ---------------------------------------------------------------------------
# 4. Charitable default — successor class is None → accept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_check_orphan_successor_charitable_default():
    """Successor class None (caller didn't classify) → accept.

    `default_tags_for_onboard` returns the existing tag list when
    caller-provided, or the inferred class when not. If neither
    surfaced a class, the cross-role check abstains rather than
    blocking lineage declaration.
    """
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value="persistent")

    result = await pre_check_cross_role("known-parent", None)

    assert result is None


# ---------------------------------------------------------------------------
# 5. Both sides None → accept (degenerate orphan-on-orphan)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_check_both_orphan_accepts():
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value=None)

    result = await pre_check_cross_role("orphan-parent", None)

    assert result is None


# ---------------------------------------------------------------------------
# 6. Embodied vs ephemeral — concrete cross-role envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_check_embodied_parent_rejects_ephemeral():
    """Lumen-style `embodied` parent + ephemeral successor → reject."""
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value="embodied")

    result = await pre_check_cross_role("lumen-uuid", "ephemeral")

    assert result is not None
    assert result["parent_class"] == "embodied"
    assert result["successor_class"] == "ephemeral"


# ---------------------------------------------------------------------------
# 7. read_class_tag — live DB shape (mirrors test_class_promotion.py
#    pattern; the mocked backend can't validate JSONB operators)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_class_tag_live_db_returns_first_tag(live_postgres_backend):
    """Identity with `metadata.tags = ["ephemeral"]` → returns "ephemeral"."""
    import json
    from tests.db.conftest import _cleanup, _uuid_suffix

    agent_id = "test-classtag-eph-" + _uuid_suffix()
    try:
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key') "
                "ON CONFLICT (id) DO NOTHING",
                agent_id,
            )
            await conn.execute(
                """
                INSERT INTO core.identities (agent_id, api_key_hash, metadata)
                VALUES ($1, 'test-hash', $2::jsonb)
                """,
                agent_id, json.dumps({"tags": ["ephemeral"]}),
            )
        result = await live_postgres_backend.read_class_tag(agent_id)
        assert result == "ephemeral"
    finally:
        await _cleanup(live_postgres_backend, [agent_id])


@pytest.mark.asyncio
async def test_read_class_tag_live_db_resident_tags(live_postgres_backend):
    """Resident-style `["persistent", "autonomous"]` → "persistent" (first)."""
    import json
    from tests.db.conftest import _cleanup, _uuid_suffix

    agent_id = "test-classtag-res-" + _uuid_suffix()
    try:
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key') "
                "ON CONFLICT (id) DO NOTHING",
                agent_id,
            )
            await conn.execute(
                """
                INSERT INTO core.identities (agent_id, api_key_hash, metadata)
                VALUES ($1, 'test-hash', $2::jsonb)
                """,
                agent_id, json.dumps({"tags": ["persistent", "autonomous"]}),
            )
        result = await live_postgres_backend.read_class_tag(agent_id)
        assert result == "persistent"
    finally:
        await _cleanup(live_postgres_backend, [agent_id])


@pytest.mark.asyncio
async def test_read_class_tag_live_db_missing_row(live_postgres_backend):
    """Unknown agent_id → None."""
    result = await live_postgres_backend.read_class_tag("nonexistent-xyz-9999")
    assert result is None


@pytest.mark.asyncio
async def test_read_class_tag_live_db_no_tags(live_postgres_backend):
    """Identity with metadata but no `tags` key → None."""
    import json
    from tests.db.conftest import _cleanup, _uuid_suffix

    agent_id = "test-classtag-notags-" + _uuid_suffix()
    try:
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key') "
                "ON CONFLICT (id) DO NOTHING",
                agent_id,
            )
            await conn.execute(
                """
                INSERT INTO core.identities (agent_id, api_key_hash, metadata)
                VALUES ($1, 'test-hash', $2::jsonb)
                """,
                agent_id, json.dumps({"other_key": "value"}),
            )
        result = await live_postgres_backend.read_class_tag(agent_id)
        assert result is None
    finally:
        await _cleanup(live_postgres_backend, [agent_id])


@pytest.mark.asyncio
async def test_read_class_tag_live_db_empty_tags(live_postgres_backend):
    """Identity with `metadata.tags = []` → None."""
    import json
    from tests.db.conftest import _cleanup, _uuid_suffix

    agent_id = "test-classtag-empty-" + _uuid_suffix()
    try:
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key') "
                "ON CONFLICT (id) DO NOTHING",
                agent_id,
            )
            await conn.execute(
                """
                INSERT INTO core.identities (agent_id, api_key_hash, metadata)
                VALUES ($1, 'test-hash', $2::jsonb)
                """,
                agent_id, json.dumps({"tags": []}),
            )
        result = await live_postgres_backend.read_class_tag(agent_id)
        assert result is None
    finally:
        await _cleanup(live_postgres_backend, [agent_id])

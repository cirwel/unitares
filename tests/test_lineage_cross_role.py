"""R2 PR 3: cross-role pre-check tests.

Covers `pre_check_cross_role` in `src/identity/lineage_lifecycle.py`
and the new `read_class_tag` backend helper in
`src/db/mixins/identity.py`.

Mock-based tests (mocked DB backend) exercise every branch of the
pre-check decision tree. Live-DB tests verify the `read_class_tag`
helper's JSONB shape against postgres directly — this is the same
"mock and a misnamed jsonb operator silently agree" risk that
`test_class_promotion.py` documents, so we mirror that pattern.

See: PR 3
 §"Cross-role pre-check"
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
# 6a. Role families — promotion is a lifecycle stage, not a role change
#     (2026-06-10: all 45 rejections on record were
#     engaged_ephemeral-parent false positives from raw tag equality)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_check_promoted_parent_accepts_ephemeral_successor():
    """parent=engaged_ephemeral (S8a-promoted), successor=ephemeral → accept.

    THE production false-positive case: S8a Phase-2 promotes an engaged
    parent's tag from `ephemeral` to `engaged_ephemeral`, after which
    every fresh spawn declaring it as parent was rejected under raw tag
    equality. Same role family → None.
    """
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value="engaged_ephemeral")

    result = await pre_check_cross_role("promoted-parent-uuid", "ephemeral")

    assert result is None


@pytest.mark.asyncio
async def test_pre_check_ephemeral_parent_accepts_promoted_successor():
    """Inverse direction: parent=ephemeral, successor=engaged_ephemeral → accept."""
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value="ephemeral")

    result = await pre_check_cross_role("plain-parent-uuid", "engaged_ephemeral")

    assert result is None


@pytest.mark.asyncio
async def test_pre_check_session_like_in_ephemeral_family():
    """Reserved `session_like` tag sits in the ephemeral family already."""
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value="engaged_ephemeral")

    result = await pre_check_cross_role("promoted-parent-uuid", "session_like")

    assert result is None


@pytest.mark.asyncio
async def test_pre_check_promoted_parent_still_rejects_substrate_successor():
    """Family fix does not loosen the substrate envelope.

    parent=persistent, successor=engaged_ephemeral → still cross-family.
    The rejection dict now carries both raw classes and their families
    so the audit event distinguishes tag-level from family-level data.
    """
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value="persistent")

    result = await pre_check_cross_role("resident-uuid", "engaged_ephemeral")

    assert result is not None
    assert result["parent_class"] == "persistent"
    assert result["successor_class"] == "engaged_ephemeral"
    assert result["parent_family"] == "persistent"
    assert result["successor_family"] == "ephemeral"
    assert result["reason"] == "role_envelope_mismatch"


@pytest.mark.asyncio
async def test_pre_check_substrate_tags_stay_distinct_families():
    """embodied vs persistent are different roles, not one substrate family.

    The family map merges only lifecycle stages of the SAME role
    (ephemeral cohort). Lumen declaring lineage to a resident — or vice
    versa — remains cross-role.
    """
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value="embodied")

    result = await pre_check_cross_role("lumen-uuid", "persistent")

    assert result is not None
    assert result["parent_family"] == "embodied"
    assert result["successor_family"] == "persistent"


@pytest.mark.asyncio
async def test_pre_check_unknown_tag_stays_strict():
    """A tag outside ROLE_FAMILIES maps to itself — strict by default.

    An unknown future tag never silently joins an existing family;
    against any known class it stays cross-family until explicitly
    placed in `ROLE_FAMILIES`.
    """
    from src.db import get_db
    backend = get_db()
    backend.read_class_tag = AsyncMock(return_value="quarantined")

    result = await pre_check_cross_role("odd-parent-uuid", "ephemeral")

    assert result is not None
    assert result["parent_class"] == "quarantined"
    assert result["parent_family"] == "quarantined"
    assert result["successor_family"] == "ephemeral"


def test_role_family_map_covers_class_tag_priority():
    """Every tag in `_CLASS_TAG_PRIORITY` has an explicit family.

    Drift guard: a new atomic class tag added to the priority tuple
    without a family placement would silently fall into
    strict-by-default and reproduce the promotion false-positive
    pattern this fix removed.
    """
    from src.identity.trajectory_continuity import (
        _CLASS_TAG_PRIORITY,
        ROLE_FAMILIES,
    )

    missing = [t for t in _CLASS_TAG_PRIORITY if t not in ROLE_FAMILIES]
    assert missing == []


def test_role_family_map_covers_promotion_path():
    """The S8a promotion source AND target tags share one family.

    Council fold (PR #601): the original bug was a tag RENAME
    (ephemeral → engaged_ephemeral) splitting parent from successor.
    `_CLASS_TAG_PRIORITY` is the scoring tuple, not the mutation path —
    a future promotion target added to `class_promotion.py` without a
    priority entry would dodge the guard above. Anchor the guard to the
    actual tag-mutation constants so any rename keeps both endpoints in
    the same family (a promotion must never sever lineage).
    """
    from src.grounding.class_promotion import (
        PROMOTION_SOURCE_TAG,
        PROMOTION_TARGET_TAG,
    )
    from src.identity.trajectory_continuity import ROLE_FAMILIES, role_family

    assert PROMOTION_SOURCE_TAG in ROLE_FAMILIES
    assert PROMOTION_TARGET_TAG in ROLE_FAMILIES
    assert role_family(PROMOTION_SOURCE_TAG) == role_family(PROMOTION_TARGET_TAG)


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


# ---------------------------------------------------------------------------
# PR 3 council fixes — re-declaration reset + symmetric rejection clear.
# Live-DB tests for the new mixin helpers `reset_lineage_for_redeclaration`
# (architect F1) and `clear_lineage_declaration` (reviewer #2). Both
# shapes need a real postgres because they exercise full row updates —
# a mocked `execute()` would silently agree with any SQL we wrote.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redeclaration_after_archive_resets_terminal_markers(
    live_postgres_backend,
):
    """Re-onboarding the same successor after archive should reset
    `lineage_archived_at` so the FSM can evaluate the new declaration.
    Without this, the FSM's terminal-state guard would permanently
    skip evaluation and the lineage would be silently dead while the
    response surfaces "provisional".
    """
    import json
    from tests.db.conftest import _cleanup, _uuid_suffix

    suffix = _uuid_suffix()
    pid = "r2-redecl-parent-" + suffix
    sid = "r2-redecl-succ-" + suffix
    try:
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key'), "
                "($2, 'test-key') ON CONFLICT (id) DO NOTHING",
                pid, sid,
            )
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash, status, metadata) "
                "VALUES ($1, 'test-hash', 'active', $2::jsonb)",
                pid, json.dumps({"tags": ["ephemeral"]}),
            )
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash, status, metadata, "
                "parent_agent_id, lineage_archived_at) "
                "VALUES ($1, 'test-hash', 'active', $2::jsonb, $3, now())",
                sid, json.dumps({"tags": ["ephemeral"]}), pid,
            )
        state_before = await live_postgres_backend.read_lineage_state(sid)
        assert state_before is not None
        assert state_before["lineage_archived_at"] is not None
        ok = await live_postgres_backend.reset_lineage_for_redeclaration(sid)
        assert ok is True
        state_after = await live_postgres_backend.read_lineage_state(sid)
        assert state_after is not None
        assert state_after["lineage_archived_at"] is None
        assert state_after["lineage_demoted_at"] is None
        assert state_after["confirmed_at"] is None
        assert state_after["provisional_lineage"] is False
        assert state_after["chain_obs_count"] == 0
        assert state_after["lineage_last_eval_at"] is None
        assert state_after["lineage_declared_at"] is None
    finally:
        await _cleanup(live_postgres_backend, [pid, sid])


@pytest.mark.asyncio
async def test_reset_lineage_no_op_for_active_row(live_postgres_backend):
    """Reset is a no-op (returns False) for rows not in terminal state."""
    from tests.db.conftest import _cleanup, _uuid_suffix

    sid = "r2-reset-noop-" + _uuid_suffix()
    try:
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key') "
                "ON CONFLICT (id) DO NOTHING",
                sid,
            )
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash, status) "
                "VALUES ($1, 'test-hash', 'active')",
                sid,
            )
        ok = await live_postgres_backend.reset_lineage_for_redeclaration(sid)
        assert ok is False
    finally:
        await _cleanup(live_postgres_backend, [sid])


@pytest.mark.asyncio
async def test_clear_lineage_declaration_clears_both_parent_and_spawn_reason(
    live_postgres_backend,
):
    """Cross-role rejection helper clears `parent_agent_id` AND
    `spawn_reason` symmetrically (per S8c convention)."""
    from tests.db.conftest import _cleanup, _uuid_suffix

    suffix = _uuid_suffix()
    pid = "r2-clear-parent-" + suffix
    sid = "r2-clear-test-" + suffix
    try:
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key'), "
                "($2, 'test-key') ON CONFLICT (id) DO NOTHING",
                pid, sid,
            )
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash, status) "
                "VALUES ($1, 'test-hash', 'active')",
                pid,
            )
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash, status, "
                "parent_agent_id, spawn_reason) "
                "VALUES ($1, 'test-hash', 'active', $2, 'subagent')",
                sid, pid,
            )
        ok = await live_postgres_backend.clear_lineage_declaration(sid)
        assert ok is True
        async with live_postgres_backend.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT parent_agent_id, spawn_reason FROM core.identities "
                "WHERE agent_id = $1",
                sid,
            )
        assert row["parent_agent_id"] is None
        assert row["spawn_reason"] is None
    finally:
        await _cleanup(live_postgres_backend, [pid, sid])


@pytest.mark.asyncio
async def test_r2_pre_check_and_declare_rejection_clears_parent_and_emits_audit(
    live_postgres_backend, monkeypatch,
):
    """End-to-end: cross-role rejection clears `parent_agent_id` AND
    `spawn_reason` and emits `lineage_cross_role_rejected` audit
    (closes reviewer-test-gap #4 from PR 3 council).

    Also wires `get_db()` to the live backend (the autouse
    `_isolate_db_backend` fixture replaces it with a no-op mock by
    default — `_r2_pre_check_and_declare` resolves the backend
    internally, so the mock would otherwise short-circuit
    `read_class_tag` to None and the cross-role check would
    charitably-accept).
    """
    import json
    from tests.db.conftest import _cleanup, _uuid_suffix
    import src.db as _db_mod
    monkeypatch.setattr(_db_mod, "get_db", lambda: live_postgres_backend)
    monkeypatch.setattr(_db_mod, "_db_instance", live_postgres_backend)
    from src.mcp_handlers.identity.handlers import _r2_pre_check_and_declare

    suffix = _uuid_suffix()
    pid = "r2-e2e-reject-parent-" + suffix
    sid = "r2-e2e-reject-succ-" + suffix
    try:
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "INSERT INTO core.agents (id, api_key) VALUES ($1, 'test-key'), "
                "($2, 'test-key') ON CONFLICT (id) DO NOTHING",
                pid, sid,
            )
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash, status, metadata) "
                "VALUES ($1, 'test-hash', 'active', $2::jsonb)",
                pid, json.dumps({"tags": ["embodied"]}),
            )
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash, status, metadata, "
                "parent_agent_id, spawn_reason) "
                "VALUES ($1, 'test-hash', 'active', $2::jsonb, $3, 'new_session')",
                sid, json.dumps({"tags": ["ephemeral"]}), pid,
            )

        class FakeMeta:
            tags = ["ephemeral"]
            parent_agent_id = pid
            spawn_reason = "new_session"

        meta = FakeMeta()
        result = await _r2_pre_check_and_declare(sid, pid, "test_succ", meta)
        state = result[0] if isinstance(result, tuple) else result
        assert state == "rejected_cross_role"
        # In-memory metadata cleared symmetrically.
        assert meta.parent_agent_id is None
        assert meta.spawn_reason is None
        # Storage row: both columns NULL.
        async with live_postgres_backend.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT parent_agent_id, spawn_reason FROM core.identities "
                "WHERE agent_id = $1",
                sid,
            )
            audit = await conn.fetch(
                "SELECT event_type, payload FROM audit.events "
                "WHERE agent_id = $1 AND event_type = 'lineage_cross_role_rejected'",
                sid,
            )
        assert row["parent_agent_id"] is None
        assert row["spawn_reason"] is None
        assert len(audit) >= 1
    finally:
        async with live_postgres_backend.acquire() as conn:
            await conn.execute(
                "DELETE FROM audit.events WHERE agent_id = $1", sid,
            )
        await _cleanup(live_postgres_backend, [pid, sid])

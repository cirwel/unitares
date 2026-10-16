"""Tests for the shared pause-TTL helpers in support/pause_ttl.py.

A stale pause (older than `PAUSE_AUTO_EXPIRE_SECONDS`) should not block
gate-traversal. The categorizer re-evaluates downstream and will
re-pause if a real problem persists. A fresh pause should still block.

Background: a pause set during a sleep-wake artifact persisted in the
in-memory agent_metadata until self_recovery was called. The 2026-05-09
→ 2026-05-18 Watcher/Sentinel/Lumen silence was caused by this; the
categorizer's gap-suppression doesn't help because the pause-status
gates run before the categorizer.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mcp_handlers.support.pause_ttl import (
    _apply_in_memory_expire,
    _pause_is_stale,
    maybe_auto_expire_pause_async,
    maybe_auto_expire_pause_sync,
    paused_refusal_recovery,
)


# ─── _pause_is_stale ───────────────────────────────────────────────────


def test_pause_is_stale_returns_false_for_none():
    assert _pause_is_stale(None) is False


def test_pause_is_stale_returns_false_for_empty_string():
    assert _pause_is_stale("") is False


def test_pause_is_stale_returns_false_for_unparseable():
    assert _pause_is_stale("not-a-timestamp") is False


def test_pause_is_stale_returns_false_for_fresh_aware_pause():
    """A pause set 1 minute ago is not stale (aware UTC form)."""
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert _pause_is_stale(fresh) is False


def test_pause_is_stale_returns_true_for_aged_aware_pause():
    """A pause set 73 hours ago (> default 72h TTL) is stale."""
    aged = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()
    assert _pause_is_stale(aged) is True


def test_pause_is_stale_handles_z_suffix():
    """ISO timestamps with Z suffix parse correctly."""
    aged = (
        datetime.now(timezone.utc) - timedelta(hours=73)
    ).isoformat().replace("+00:00", "Z")
    assert _pause_is_stale(aged) is True


def test_pause_is_stale_treats_naive_as_utc():
    """Legacy naive ISO strings are interpreted as UTC, not local.

    This is the regression guard for reviewer finding #1: an earlier
    draft compared naive paused_at to naive datetime.now(), which on a
    non-UTC host produced staleness errors up to the host's UTC offset.
    """
    aged_naive = (datetime.now(timezone.utc) - timedelta(hours=73)).replace(tzinfo=None).isoformat()
    assert _pause_is_stale(aged_naive) is True

    fresh_naive = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    assert _pause_is_stale(fresh_naive) is False


def test_pause_is_stale_respects_env_var_override(monkeypatch):
    """UNITARES_PAUSE_AUTO_EXPIRE_SECONDS env var is honored.

    Regression guard for reviewer finding #4: an earlier draft
    documented the env override but did not wire it.
    """
    monkeypatch.setenv("UNITARES_PAUSE_AUTO_EXPIRE_SECONDS", "60")
    # Re-import config so the class-attribute picks up the env value
    import importlib
    from config import governance_config
    importlib.reload(governance_config)
    # ALSO reload the pause_ttl module so its lazy import sees the new value
    from src.mcp_handlers.support import pause_ttl
    importlib.reload(pause_ttl)
    try:
        # 5 minutes ago, threshold is 60 seconds → stale
        aged = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        assert pause_ttl._pause_is_stale(aged) is True
    finally:
        # Clean up: restore default
        monkeypatch.delenv("UNITARES_PAUSE_AUTO_EXPIRE_SECONDS", raising=False)
        importlib.reload(governance_config)
        importlib.reload(pause_ttl)


# ─── _apply_in_memory_expire ───────────────────────────────────────────


def test_apply_in_memory_expire_flips_status_and_clears_paused_at():
    """Clears paused_at to preserve the system invariant
    (status==paused ⟺ paused_at truthy). Original is returned for audit."""
    meta = MagicMock()
    meta.status = "paused"
    meta.paused_at = "2026-05-09T04:38:00+00:00"
    meta.add_lifecycle_event = MagicMock()

    original = _apply_in_memory_expire(meta)

    assert meta.status == "active"
    assert meta.paused_at is None
    assert original == "2026-05-09T04:38:00+00:00"
    meta.add_lifecycle_event.assert_called_once()
    event_args = meta.add_lifecycle_event.call_args[0]
    assert event_args[0] == "pause_auto_expired"
    assert "aged out" in event_args[1]


# ─── maybe_auto_expire_pause_async (process_agent_update path) ─────────


@pytest.mark.asyncio
async def test_async_entry_point_no_op_on_fresh_pause():
    meta = MagicMock()
    meta.status = "paused"
    meta.paused_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    expired = await maybe_auto_expire_pause_async("uuid-fresh-12", meta)

    assert expired is False
    assert meta.status == "paused"
    assert meta.paused_at is not None


@pytest.mark.asyncio
async def test_async_entry_point_expires_stale_pause():
    meta = MagicMock()
    meta.status = "paused"
    meta.paused_at = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()
    meta.add_lifecycle_event = MagicMock()

    with patch(
        "src.agent_storage.expire_pause_if_token",
        new_callable=AsyncMock,
        return_value={"applied": True, "pause_token": meta.paused_at},
    ) as mock_persist:
        expired = await maybe_auto_expire_pause_async("uuid-stale-12", meta)

    assert expired is True
    assert meta.status == "active"
    assert meta.paused_at is None
    mock_persist.assert_awaited_once()
    persist_kwargs = mock_persist.call_args.kwargs
    assert persist_kwargs["expected_pause_token"]
    assert persist_kwargs["lifecycle_event"]["event"] == "pause_auto_expired"


@pytest.mark.asyncio
async def test_async_entry_point_reconciles_already_active_without_expiry_event():
    meta = MagicMock()
    meta.status = "paused"
    meta.paused_at = (
        datetime.now(timezone.utc) - timedelta(hours=73)
    ).isoformat()
    meta.add_lifecycle_event = MagicMock()

    with (
        patch(
            "src.agent_storage.expire_pause_if_token",
            new=AsyncMock(return_value={
                "applied": True,
                "already_active": True,
                "pause_token": meta.paused_at,
                "current_pause_token": None,
            }),
        ),
        patch(
            "src.mcp_handlers.support.pause_ttl._emit_audit"
        ) as emit_audit,
    ):
        expired = await maybe_auto_expire_pause_async(
            "uuid-active-12", meta
        )

    assert expired is True
    assert meta.status == "active"
    assert meta.paused_at is None
    meta.add_lifecycle_event.assert_not_called()
    emit_audit.assert_not_called()


@pytest.mark.asyncio
async def test_async_entry_point_reconciles_cleaned_stale_durable_token():
    meta = MagicMock()
    meta.status = "paused"
    meta.paused_at = (
        datetime.now(timezone.utc) - timedelta(hours=73)
    ).isoformat()
    meta.add_lifecycle_event = MagicMock()

    with (
        patch(
            "src.agent_storage.expire_pause_if_token",
            new=AsyncMock(return_value={
                "applied": True,
                "repaired_stale_pause_token": True,
                "pause_token": meta.paused_at,
            }),
        ),
        patch(
            "src.mcp_handlers.support.pause_ttl._emit_audit"
        ) as emit_audit,
    ):
        expired = await maybe_auto_expire_pause_async(
            "uuid-active-12", meta
        )

    assert expired is True
    assert meta.status == "active"
    assert meta.paused_at is None
    meta.add_lifecycle_event.assert_not_called()
    emit_audit.assert_not_called()


@pytest.mark.asyncio
async def test_async_entry_point_keeps_pause_when_durable_resume_fails():
    """A storage failure must not grant operation from cache alone."""
    meta = MagicMock()
    meta.status = "paused"
    meta.paused_at = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()
    meta.add_lifecycle_event = MagicMock()

    with patch(
        "src.agent_storage.expire_pause_if_token",
        new_callable=AsyncMock,
        side_effect=RuntimeError("DB down"),
    ):
        expired = await maybe_auto_expire_pause_async("uuid-12", meta)

    assert expired is False
    assert meta.status == "paused"
    assert meta.paused_at is not None


@pytest.mark.asyncio
async def test_async_entry_point_preserves_concurrent_newer_pause_generation():
    old_token = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()
    meta = MagicMock(status="paused", paused_at=old_token)
    meta.status = "paused"
    meta.paused_at = old_token
    meta.add_lifecycle_event = MagicMock()

    with patch(
        "src.agent_storage.expire_pause_if_token",
        new=AsyncMock(return_value={
            "applied": False,
            "reason": "pause_generation_superseded",
            "agent_status": "paused",
            "identity_status": "paused",
            "current_pause_token": "pause-new",
        }),
    ):
        expired = await maybe_auto_expire_pause_async("uuid-raced-12", meta)

    assert expired is False
    assert meta.status == "paused"
    assert meta.paused_at == "pause-new"
    meta.add_lifecycle_event.assert_not_called()


@pytest.mark.asyncio
async def test_async_entry_point_does_not_adopt_token_from_active_copies():
    old_token = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()
    meta = MagicMock(status="paused", paused_at=old_token)
    meta.status = "paused"
    meta.paused_at = old_token
    meta.add_lifecycle_event = MagicMock()

    with patch(
        "src.agent_storage.expire_pause_if_token",
        new=AsyncMock(return_value={
            "applied": False,
            "reason": "pause_generation_superseded",
            "agent_status": "active",
            "identity_status": "active",
            "current_pause_token": "pause-new",
        }),
    ):
        expired = await maybe_auto_expire_pause_async("uuid-raced-12", meta)

    assert expired is False
    assert meta.status == "paused"
    assert meta.paused_at == old_token
    meta.add_lifecycle_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_text"),
    [
        ("durable_identity_missing", "durable identity is missing"),
        (
            "operator_reconciliation_required",
            "status or pause-token copies disagree",
        ),
        ("terminal_agent_status", "durable status is terminal"),
    ],
)
async def test_async_entry_point_names_required_durable_repair(
    reason, expected_text
):
    token = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()
    meta = MagicMock(status="paused", paused_at=token)
    meta.status = "paused"
    meta.paused_at = token
    meta.add_lifecycle_event = MagicMock()

    with patch(
        "src.agent_storage.expire_pause_if_token",
        new=AsyncMock(return_value={
            "applied": False,
            "reason": reason,
            "agent_status": "paused",
            "identity_status": "active",
        }),
    ):
        expired = await maybe_auto_expire_pause_async("uuid-repair-12", meta)

    recovery = paused_refusal_recovery(meta, "uuid-repair-12")
    assert expired is False
    assert meta.status == "paused"
    assert expected_text in recovery["other_exits"]
    assert recovery["durable_reconciliation"] == {
        "required": True,
        "reason": reason,
        "agent_status": "paused",
        "identity_status": "active",
    }


# ─── maybe_auto_expire_pause_sync (check_agent_can_operate path) ───────


def test_sync_entry_point_no_op_on_fresh_pause():
    meta = MagicMock()
    meta.status = "paused"
    meta.paused_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()

    expired = maybe_auto_expire_pause_sync("uuid-fresh-12", meta)

    assert expired is False
    assert meta.status == "paused"


@pytest.mark.asyncio
async def test_sync_entry_point_expires_stale_pause_under_running_loop():
    """The current sync call stays blocked until the scheduled CAS commits."""
    meta = MagicMock()
    meta.status = "paused"
    meta.paused_at = (datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()
    meta.add_lifecycle_event = MagicMock()

    with patch(
        "src.agent_storage.expire_pause_if_token",
        new_callable=AsyncMock,
        return_value={"applied": True, "pause_token": meta.paused_at},
    ) as mock_persist:
        expired = maybe_auto_expire_pause_sync("uuid-stale-12", meta)
        assert expired is False
        assert meta.status == "paused"
        # Yield once so the scheduled task runs
        import asyncio
        await asyncio.sleep(0)

    assert meta.status == "active"
    assert meta.paused_at is None
    # Persistence was scheduled — the AsyncMock was awaited via the task
    mock_persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_entry_point_records_repair_guidance_after_failed_attempt():
    meta = MagicMock()
    meta.status = "paused"
    meta.paused_at = (
        datetime.now(timezone.utc) - timedelta(hours=73)
    ).isoformat()
    meta.add_lifecycle_event = MagicMock()

    with patch(
        "src.agent_storage.expire_pause_if_token",
        new=AsyncMock(return_value={
            "applied": False,
            "reason": "operator_reconciliation_required",
            "agent_status": "paused",
            "identity_status": "active",
        }),
    ):
        expired = maybe_auto_expire_pause_sync("uuid-repair-12", meta)
        assert expired is False
        import asyncio
        await asyncio.sleep(0)

    recovery = paused_refusal_recovery(meta, "uuid-repair-12")
    assert recovery["durable_reconciliation"]["required"] is True

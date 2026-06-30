"""Regression tests for ``check_agent_silence`` timezone handling.

Prior behaviour: ``meta.last_update`` was hydrated from postgres as a
tz-aware ISO string but compared against a naive ``datetime.now()``.
The resulting ``TypeError`` was silently swallowed, so any agent that
had not checked in since the last governance restart was invisible to
the silence monitor — the exact agents the monitor was meant to catch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src import background_tasks
from src.agent_metadata_model import AgentMetadata, agent_metadata


@pytest.fixture
def isolated_silence_state(monkeypatch):
    """Reset module-level silence alert sets and metadata dict for each test."""
    agent_metadata.clear()
    background_tasks._silence_alerted.clear()
    background_tasks._silence_critical_alerted.clear()
    background_tasks._silence_duplicate_warned.clear()
    # Pretend the server started 48h ago so pre-existing staleness cap
    # doesn't mask genuinely stale agents in tests.
    background_tasks._silence_server_start = datetime.now(timezone.utc) - timedelta(hours=48)

    broadcaster = AsyncMock()
    audit = AsyncMock()

    # Patch the broadcaster and audit at module lookup time.
    import src.broadcaster as broadcaster_module
    import src.audit_db as audit_module

    monkeypatch.setattr(broadcaster_module, "broadcaster_instance", broadcaster)
    monkeypatch.setattr(audit_module, "append_audit_event_async", audit)

    # Label-based intervals are deployment-local now (UNITARES_CLASS_CALIBRATION
    # overlay); inject representative test values so the silence detector has a
    # label map to exercise, without depending on shipped per-resident config.
    monkeypatch.setattr(background_tasks, "_PERSISTENT_AGENT_INTERVALS",
                        {"Vigil": 1800, "Lumen": 300, "Sentinel": 600, "Watcher": 21600})

    yield broadcaster, audit

    agent_metadata.clear()
    background_tasks._silence_alerted.clear()
    background_tasks._silence_critical_alerted.clear()
    background_tasks._silence_duplicate_warned.clear()
    background_tasks._silence_server_start = None


def _make_meta(agent_id: str, label: str, last_update: str) -> AgentMetadata:
    now_iso = datetime.now(timezone.utc).isoformat()
    return AgentMetadata(
        agent_id=agent_id,
        status="active",
        created_at=now_iso,
        last_update=last_update,
        label=label,
    )


@pytest.mark.asyncio
async def test_tz_aware_last_update_fires_critical(isolated_silence_state):
    """Agent hydrated from postgres (tz-aware ISO) must still be flagged.

    This is the regression: before the fix, subtracting a naive ``now``
    from a tz-aware ``last`` raised ``TypeError`` and was swallowed, so
    the agent was silently skipped.
    """
    broadcaster, _ = isolated_silence_state

    # Sentinel interval is 600s; 5× = 3000s ⇒ CRITICAL.
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    aware_iso = stale.isoformat()  # keeps the +00:00 suffix

    agent_metadata["sentinel-uuid"] = _make_meta(
        "sentinel-uuid", "Sentinel", aware_iso
    )

    await background_tasks._silence_check_iteration()

    broadcaster.broadcast_event.assert_awaited_once()
    call = broadcaster.broadcast_event.await_args
    assert call.args[0] == "lifecycle_silent_critical"
    assert call.kwargs["agent_id"] == "sentinel-uuid"
    assert "sentinel-uuid" in background_tasks._silence_critical_alerted


@pytest.mark.asyncio
async def test_tz_naive_last_update_still_fires(isolated_silence_state):
    """Runtime-written naive timestamps continue to work (backwards compat)."""
    broadcaster, _ = isolated_silence_state

    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    # Strip tzinfo to produce the naive ISO form that
    # ``datetime.now().isoformat()`` emits at runtime.
    naive_iso = stale.replace(tzinfo=None).isoformat()

    agent_metadata["sentinel-uuid"] = _make_meta(
        "sentinel-uuid", "Sentinel", naive_iso
    )

    await background_tasks._silence_check_iteration()

    broadcaster.broadcast_event.assert_awaited_once()
    assert (
        broadcaster.broadcast_event.await_args.args[0]
        == "lifecycle_silent_critical"
    )


@pytest.mark.asyncio
async def test_fresh_checkin_is_not_flagged(isolated_silence_state):
    """Recent check-ins (tz-aware or naive) must not trigger an alert."""
    broadcaster, _ = isolated_silence_state

    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)
    agent_metadata["sentinel-uuid"] = _make_meta(
        "sentinel-uuid", "Sentinel", fresh.isoformat()
    )

    await background_tasks._silence_check_iteration()

    broadcaster.broadcast_event.assert_not_awaited()
    assert "sentinel-uuid" not in background_tasks._silence_alerted
    assert "sentinel-uuid" not in background_tasks._silence_critical_alerted


@pytest.mark.asyncio
async def test_mixed_tz_naive_and_aware(isolated_silence_state):
    """Neither form should poison the loop for the other.

    Before the fix, a raised TypeError from one agent would skip the
    rest of the iteration only for that agent via the inner ``except``,
    but the bug was that tz-aware agents were all individually skipped.
    This test makes the invariant explicit: both forms in the same pass
    must be detected independently.
    """
    broadcaster, _ = isolated_silence_state

    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    aware_iso = stale.isoformat()
    naive_iso = stale.replace(tzinfo=None).isoformat()

    agent_metadata["lumen"] = _make_meta("lumen", "Lumen", naive_iso)
    agent_metadata["sentinel"] = _make_meta("sentinel", "Sentinel", aware_iso)

    await background_tasks._silence_check_iteration()

    assert broadcaster.broadcast_event.await_count == 2
    fired_agents = {
        call.kwargs["agent_id"]
        for call in broadcaster.broadcast_event.await_args_list
    }
    assert fired_agents == {"lumen", "sentinel"}


@pytest.mark.asyncio
async def test_parse_last_update_aware_returns_utc():
    """Helper normalises both forms into tz-aware UTC datetimes."""
    aware = background_tasks._parse_last_update_aware(
        "2026-04-08T23:27:36.861374-06:00"
    )
    naive = background_tasks._parse_last_update_aware(
        "2026-04-08T23:27:36.861374"
    )

    assert aware is not None and aware.tzinfo is not None
    assert naive is not None and naive.tzinfo is not None
    # The aware form is -06:00, so its UTC equivalent is 05:27 on the 9th.
    assert aware == datetime(2026, 4, 9, 5, 27, 36, 861374, tzinfo=timezone.utc)

    assert background_tasks._parse_last_update_aware("not-a-date") is None
    assert background_tasks._parse_last_update_aware("") is None


@pytest.mark.asyncio
async def test_non_persistent_agent_skipped(isolated_silence_state):
    """Ephemeral agents (no entry in _PERSISTENT_AGENT_INTERVALS) are skipped."""
    broadcaster, _ = isolated_silence_state

    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["ephemeral"] = _make_meta(
        "ephemeral", "claude_cirwel_20260410", stale.isoformat()
    )

    await background_tasks._silence_check_iteration()

    broadcaster.broadcast_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_driven_resident_skips_silence_monitor(isolated_silence_state):
    """Watcher is event-driven; absence of check-ins between edits is not silence."""
    broadcaster, _ = isolated_silence_state

    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    meta = _make_meta("watcher-uuid", "Watcher", stale.isoformat())
    meta.tags = ["persistent", "autonomous"]
    agent_metadata["watcher-uuid"] = meta

    await background_tasks._silence_check_iteration()

    broadcaster.broadcast_event.assert_not_awaited()
    assert "watcher-uuid" not in background_tasks._silence_alerted
    assert "watcher-uuid" not in background_tasks._silence_critical_alerted


@pytest.mark.asyncio
async def test_repeat_iteration_does_not_refire(isolated_silence_state):
    """Once an agent has fired CRITICAL, subsequent iterations stay quiet."""
    broadcaster, _ = isolated_silence_state

    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta(
        "sentinel-uuid", "Sentinel", stale.isoformat()
    )

    await background_tasks._silence_check_iteration()
    await background_tasks._silence_check_iteration()

    # Only one broadcast, despite two iterations.
    broadcaster.broadcast_event.assert_awaited_once()


@pytest.mark.asyncio
async def test_pre_existing_staleness_ignored_on_fresh_start(isolated_silence_state):
    """Agent stale from before server started should not trigger alerts.

    Regression test: when Mac sleeps and governance restarts, agents
    appear 6+ hours stale from the loaded last_activity_at.  The silence
    detector should only count time since the server process started.
    """
    broadcaster, _ = isolated_silence_state

    # Simulate fresh server start (just now)
    background_tasks._silence_server_start = datetime.now(timezone.utc)

    # Agent last checked in 6 hours ago (before this server started)
    stale = datetime.now(timezone.utc) - timedelta(hours=6)
    agent_metadata["lumen-uuid"] = _make_meta(
        "lumen-uuid", "Lumen", stale.isoformat()
    )

    await background_tasks._silence_check_iteration()

    # Should NOT fire — the staleness predates this server process
    broadcaster.broadcast_event.assert_not_awaited()
    assert "lumen-uuid" not in background_tasks._silence_critical_alerted


# ---------------------------------------------------------------------------
# Proxy-alive suppression tests
# ---------------------------------------------------------------------------
# eisv-sync-task proxy tests removed — the proxy mapping is now empty after
# de-agentification. The proxy mechanism stays for future use but has no
# active entries.


@pytest.mark.asyncio
async def test_no_proxy_agent_fires_critical_normally(isolated_silence_state):
    """Sentinel has no proxy — CRITICAL fires as before."""
    broadcaster, _ = isolated_silence_state

    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    agent_metadata["sentinel-uuid"] = _make_meta(
        "sentinel-uuid", "Sentinel", stale.isoformat()
    )

    await background_tasks._silence_check_iteration()

    broadcaster.broadcast_event.assert_awaited_once()
    call = broadcaster.broadcast_event.await_args
    assert call.args[0] == "lifecycle_silent_critical"


@pytest.mark.asyncio
async def test_duplicate_resident_row_does_not_fire_silence(isolated_silence_state):
    """A 0-update resident fork should not page as if the canonical resident died."""
    broadcaster, _ = isolated_silence_state

    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)
    stale = datetime.now(timezone.utc) - timedelta(hours=2)

    canonical = _make_meta("sentinel-main", "Sentinel", fresh.isoformat())
    canonical.total_updates = 100
    duplicate = _make_meta("sentinel-fork", "Sentinel", stale.isoformat())
    duplicate.total_updates = 0

    agent_metadata["sentinel-main"] = canonical
    agent_metadata["sentinel-fork"] = duplicate

    await background_tasks._silence_check_iteration()

    broadcaster.broadcast_event.assert_not_awaited()
    assert "sentinel-fork" not in background_tasks._silence_critical_alerted
    assert "sentinel-fork" in background_tasks._silence_duplicate_warned


@pytest.mark.asyncio
async def test_duplicate_resident_prefers_fresh_real_update(isolated_silence_state):
    """A restarted resident with real activity beats an older high-count row."""
    broadcaster, _ = isolated_silence_state

    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)

    old_row = _make_meta("sentinel-old", "Sentinel", stale.isoformat())
    old_row.total_updates = 5000
    new_row = _make_meta("sentinel-new", "Sentinel", fresh.isoformat())
    new_row.total_updates = 1

    agent_metadata["sentinel-old"] = old_row
    agent_metadata["sentinel-new"] = new_row

    await background_tasks._silence_check_iteration()

    broadcaster.broadcast_event.assert_not_awaited()
    assert "sentinel-old" in background_tasks._silence_duplicate_warned
    assert "sentinel-new" not in background_tasks._silence_duplicate_warned


@pytest.mark.asyncio
async def test_duplicate_resident_malformed_update_count_does_not_break_pass(isolated_silence_state):
    """Corrupt hydrated metadata should not disable the silence detector."""
    broadcaster, _ = isolated_silence_state

    stale = datetime.now(timezone.utc) - timedelta(hours=2)
    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)

    duplicate = _make_meta("sentinel-corrupt", "Sentinel", stale.isoformat())
    duplicate.total_updates = "not-an-int"
    canonical = _make_meta("sentinel-main", "Sentinel", fresh.isoformat())
    canonical.total_updates = 1

    agent_metadata["sentinel-corrupt"] = duplicate
    agent_metadata["sentinel-main"] = canonical

    await background_tasks._silence_check_iteration()

    broadcaster.broadcast_event.assert_not_awaited()
    assert "sentinel-corrupt" in background_tasks._silence_duplicate_warned


# test_proxy_alive_recovery_clears_alert removed — depended on eisv-sync-task proxy

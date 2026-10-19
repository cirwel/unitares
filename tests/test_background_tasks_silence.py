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
    background_tasks._silence_close_checked.clear()
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
    # Durable "already alerted this episode" lookup: default to an empty audit
    # store so existing tests behave as a first-ever alert and never touch a DB.
    monkeypatch.setattr(audit_module, "query_audit_events_async", AsyncMock(return_value=[]))

    # Label-based intervals are deployment-local now (UNITARES_CLASS_CALIBRATION
    # overlay); inject representative test values so the silence detector has a
    # label map to exercise, without depending on shipped per-resident config.
    monkeypatch.setattr(background_tasks, "_PERSISTENT_AGENT_INTERVALS",
                        {"Vigil": 1800, "Lumen": 300, "Sentinel": 600, "Watcher": 21600})

    # Default the KG-finding write path (_log_resident_silence_finding) to a
    # safe no-op mock so existing tests never touch a real DB. Tests that
    # exercise the KG-write behavior itself override this via monkeypatch.
    import src.knowledge_graph as kg_module
    kg_default = AsyncMock()
    kg_default.query = AsyncMock(return_value=[])
    kg_default.add_discovery = AsyncMock()
    monkeypatch.setattr(kg_module, "get_knowledge_graph", AsyncMock(return_value=kg_default))

    yield broadcaster, audit

    agent_metadata.clear()
    background_tasks._silence_alerted.clear()
    background_tasks._silence_critical_alerted.clear()
    background_tasks._silence_duplicate_warned.clear()
    background_tasks._silence_close_checked.clear()
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
        "ephemeral", "claude_example_20260410", stale.isoformat()
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


# ---------------------------------------------------------------------------
# CRITICAL silence -> durable KG finding (_log_resident_silence_finding)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_critical_silence_files_kg_finding(isolated_silence_state, monkeypatch):
    """A CRITICAL silence escalation must be filed as a durable, open KG finding."""
    import src.knowledge_graph as kg_module

    kg = AsyncMock()
    kg.query = AsyncMock(return_value=[])
    kg.add_discovery = AsyncMock()
    monkeypatch.setattr(kg_module, "get_knowledge_graph", AsyncMock(return_value=kg))

    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())

    await background_tasks._silence_check_iteration()

    kg.add_discovery.assert_awaited_once()
    note = kg.add_discovery.await_args.args[0]
    assert note.agent_id == "sentinel-uuid"
    assert note.type == "bug_found"
    assert note.severity == "critical"
    assert note.status == "open"
    assert "resident-silence" in note.tags
    assert "sentinel" in note.tags  # lowercased label tag
    assert "Sentinel" in note.summary


@pytest.mark.asyncio
async def test_critical_silence_kg_finding_deduped_when_already_open(
    isolated_silence_state, monkeypatch
):
    """An already-open resident-silence finding for this agent must not be refiled.

    This is what makes the write safe across a server restart mid-outage: the
    in-memory ``_silence_critical_alerted`` set resets on restart, but the
    DB-backed dedup check survives it.
    """
    import src.knowledge_graph as kg_module

    existing_note = type("FakeDiscovery", (), {"tags": ["resident-silence", "auto-filed"]})()
    kg = AsyncMock()
    kg.query = AsyncMock(return_value=[existing_note])
    kg.add_discovery = AsyncMock()
    monkeypatch.setattr(kg_module, "get_knowledge_graph", AsyncMock(return_value=kg))

    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())

    await background_tasks._silence_check_iteration()

    # CRITICAL still logs/broadcasts as before — only the KG write is deduped.
    kg.add_discovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_critical_silence_kg_write_failure_does_not_break_iteration(
    isolated_silence_state, monkeypatch
):
    """A broken KG/DB path must degrade silently, not take down silence detection."""
    import src.knowledge_graph as kg_module

    monkeypatch.setattr(
        kg_module, "get_knowledge_graph", AsyncMock(side_effect=RuntimeError("db down"))
    )
    broadcaster, _ = isolated_silence_state

    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())

    await background_tasks._silence_check_iteration()

    # The existing CRITICAL path (log + broadcast + alert-set bookkeeping)
    # must still complete even though the KG write raised.
    broadcaster.broadcast_event.assert_awaited_once()
    assert "sentinel-uuid" in background_tasks._silence_critical_alerted


@pytest.mark.asyncio
async def test_non_critical_silence_does_not_file_kg_finding(isolated_silence_state, monkeypatch):
    """The WARNING tier (2x-5x interval) must not file a KG finding — CRITICAL only."""
    import src.knowledge_graph as kg_module

    kg = AsyncMock()
    kg.query = AsyncMock(return_value=[])
    kg.add_discovery = AsyncMock()
    monkeypatch.setattr(kg_module, "get_knowledge_graph", AsyncMock(return_value=kg))

    # Sentinel interval is 600s; 2x-5x is the WARNING band, not CRITICAL.
    stale = datetime.now(timezone.utc) - timedelta(seconds=600 * 3)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())

    await background_tasks._silence_check_iteration()

    kg.add_discovery.assert_not_awaited()


# --- closing the auto-filed row once the resident is healthy again ----------

def _silence_row(agent_id, tags=("resident-silence", "auto-filed", "lumen"), rid="row-1"):
    from src.knowledge_graph import DiscoveryNode

    return DiscoveryNode(
        id=rid, agent_id=agent_id, type="bug_found", summary="Lumen silent",
        details="auto-filed", tags=list(tags), severity="critical", status="open",
    )


def _kg_with(monkeypatch, rows):
    import src.knowledge_graph as kg_module

    kg = AsyncMock()
    kg.query = AsyncMock(return_value=rows)
    kg.update_discovery = AsyncMock(return_value=True)
    monkeypatch.setattr(kg_module, "get_knowledge_graph", AsyncMock(return_value=kg))
    return kg


@pytest.mark.asyncio
async def test_healthy_resident_closes_its_auto_filed_silence_row(
    isolated_silence_state, monkeypatch
):
    kg = _kg_with(monkeypatch, [
        _silence_row("lumen-uuid"),
        _silence_row("lumen-uuid", tags=("resident-silence",), rid="human-filed"),
        _silence_row("lumen-uuid", tags=("auto-filed",), rid="other-auto"),
        _silence_row("someone-else", rid="other-agent"),
    ])
    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)
    agent_metadata["lumen-uuid"] = _make_meta("lumen-uuid", "Lumen", fresh.isoformat())

    await background_tasks._silence_check_iteration()

    kg.update_discovery.assert_awaited_once()
    did, updates = kg.update_discovery.await_args.args
    assert did == "row-1"
    assert updates["status"] == "resolved"
    assert updates["closure_class"] == "unobserved"
    assert set(updates["closure_evidence"]) >= {"window", "instrument_check"}
    assert fresh.isoformat() in updates["details"]
    assert "Resolution notes" in updates["details"]


@pytest.mark.asyncio
async def test_silence_row_close_is_bounded_and_idempotent(
    isolated_silence_state, monkeypatch
):
    kg = _kg_with(monkeypatch, [])
    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)
    agent_metadata["lumen-uuid"] = _make_meta("lumen-uuid", "Lumen", fresh.isoformat())

    await background_tasks._silence_check_iteration()
    await background_tasks._silence_check_iteration()

    # One lookup for the process, not one per iteration; nothing to write.
    assert kg.query.await_count == 1
    kg.update_discovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_after_critical_rechecks_even_if_already_checked(
    isolated_silence_state, monkeypatch
):
    kg = _kg_with(monkeypatch, [_silence_row("lumen-uuid")])
    background_tasks._silence_close_checked.add("lumen-uuid")
    background_tasks._silence_critical_alerted.add("lumen-uuid")
    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)
    agent_metadata["lumen-uuid"] = _make_meta("lumen-uuid", "Lumen", fresh.isoformat())

    await background_tasks._silence_check_iteration()

    kg.update_discovery.assert_awaited_once()


@pytest.mark.asyncio
async def test_still_silent_resident_keeps_its_row(isolated_silence_state, monkeypatch):
    """Stale since before a restart looks 'recovered' on the start-capped clock."""
    kg = _kg_with(monkeypatch, [_silence_row("lumen-uuid")])
    background_tasks._silence_server_start = datetime.now(timezone.utc)
    stale = datetime.now(timezone.utc) - timedelta(hours=6)
    agent_metadata["lumen-uuid"] = _make_meta("lumen-uuid", "Lumen", stale.isoformat())

    await background_tasks._silence_check_iteration()

    kg.update_discovery.assert_not_awaited()


@pytest.mark.asyncio
async def test_kg_failure_on_close_is_swallowed_and_retried(
    isolated_silence_state, monkeypatch
):
    kg = _kg_with(monkeypatch, [_silence_row("lumen-uuid")])
    kg.update_discovery.side_effect = RuntimeError("db down")
    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)
    agent_metadata["lumen-uuid"] = _make_meta("lumen-uuid", "Lumen", fresh.isoformat())

    await background_tasks._silence_check_iteration()
    assert "lumen-uuid" not in background_tasks._silence_close_checked

    kg.update_discovery.side_effect = None
    await background_tasks._silence_check_iteration()
    assert "lumen-uuid" in background_tasks._silence_close_checked


@pytest.mark.asyncio
async def test_rejected_update_is_not_marked_checked_and_is_retried(
    isolated_silence_state, monkeypatch
):
    kg = _kg_with(monkeypatch, [_silence_row("lumen-uuid")])
    kg.update_discovery.return_value = False
    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)
    agent_metadata["lumen-uuid"] = _make_meta("lumen-uuid", "Lumen", fresh.isoformat())

    await background_tasks._silence_check_iteration()
    assert "lumen-uuid" not in background_tasks._silence_close_checked

    kg.update_discovery.return_value = True
    await background_tasks._silence_check_iteration()
    assert "lumen-uuid" in background_tasks._silence_close_checked
    assert kg.update_discovery.await_count == 2


@pytest.mark.asyncio
async def test_old_silence_row_behind_many_open_rows_is_still_closed(
    isolated_silence_state, monkeypatch
):
    """The lookup filters by tag, so >50 newer unrelated open rows cannot hide it."""
    import src.knowledge_graph as kg_module

    noise = [_silence_row("lumen-uuid", tags=("note",), rid=f"n{i}") for i in range(60)]
    target = _silence_row("lumen-uuid", rid="old-silence")

    async def fake_query(**kw):
        rows = noise + [target]  # newest first: the silence row is oldest
        if kw.get("tags"):
            rows = [r for r in rows if set(r.tags) & set(kw["tags"])]
        return rows[: kw.get("limit", 100)]

    kg = AsyncMock()
    kg.query = fake_query
    kg.update_discovery = AsyncMock(return_value=True)
    monkeypatch.setattr(kg_module, "get_knowledge_graph", AsyncMock(return_value=kg))
    fresh = datetime.now(timezone.utc) - timedelta(seconds=30)
    agent_metadata["lumen-uuid"] = _make_meta("lumen-uuid", "Lumen", fresh.isoformat())

    await background_tasks._silence_check_iteration()

    kg.update_discovery.assert_awaited_once()
    assert kg.update_discovery.await_args.args[0] == "old-silence"


# --- de-duplication that survives a governance restart -----------------------
#
# The in-memory alert sets are empty in every new process, and the silence clock
# restarts at server start, so before this the same outage was re-announced on
# every restart. "Already alerted" now comes from the alert events audit.events
# already holds, compared against the agent's last real activity.


class _FakeAuditStore:
    """In-memory stand-in for audit.events with the query filters the probe uses."""

    def __init__(self):
        self.events: list[dict] = []

    def record(self, agent_id, event_type, ts):
        self.events.append({"agent_id": agent_id, "event_type": event_type, "ts": ts})

    async def query(self, agent_id=None, event_types=None, start_time=None,
                    limit=1000, order="asc", **_kw):
        start = datetime.fromisoformat(start_time) if start_time else None
        rows = [
            e for e in self.events
            if (agent_id is None or e["agent_id"] == agent_id)
            and (not event_types or e["event_type"] in event_types)
            and (start is None or e["ts"] >= start)
        ]
        rows.sort(key=lambda e: e["ts"], reverse=(order == "desc"))
        return [{"timestamp": e["ts"].isoformat(), "agent_id": e["agent_id"],
                 "event_type": e["event_type"], "details": {}} for e in rows[:limit]]


@pytest.fixture
def audit_store(isolated_silence_state, monkeypatch):
    """Wire the broadcaster and the audit query to one fake durable store."""
    import src.audit_db as audit_module

    broadcaster, audit = isolated_silence_state
    store = _FakeAuditStore()

    async def _broadcast(event_type, agent_id=None, payload=None):
        # The real broadcaster persists every typed event to audit.events.
        store.record(agent_id, event_type, datetime.now(timezone.utc))

    async def _append(entry):
        store.record(entry.get("agent_id"), entry.get("event_type"),
                     datetime.fromisoformat(entry["timestamp"]))
        return True

    broadcaster.broadcast_event.side_effect = _broadcast
    audit.side_effect = _append
    monkeypatch.setattr(audit_module, "query_audit_events_async", store.query)
    return broadcaster, store


def _simulate_restart(started_ago: timedelta) -> None:
    """A fresh process: empty alert sets, silence clock restarted."""
    background_tasks._silence_alerted.clear()
    background_tasks._silence_critical_alerted.clear()
    background_tasks._silence_duplicate_warned.clear()
    background_tasks._silence_close_checked.clear()
    background_tasks._silence_server_start = datetime.now(timezone.utc) - started_ago


def _critical_calls(broadcaster):
    return [c for c in broadcaster.broadcast_event.await_args_list
            if c.args[0] == "lifecycle_silent_critical"]


class _FakeKG:
    """Stateful KG stand-in: filed rows come back from later open-row queries."""

    def __init__(self):
        self.rows: list = []
        self.add_calls = 0
        self.fail_adds = False

    async def query(self, status=None, agent_id=None, tags=None, limit=100, **_kw):
        rows = [
            r for r in self.rows
            if (status is None or r.status == status)
            and (agent_id is None or r.agent_id == agent_id)
            and (not tags or set(r.tags or []) & set(tags))
        ]
        return rows[:limit]

    async def add_discovery(self, note):
        self.add_calls += 1
        if self.fail_adds:
            raise RuntimeError("kg down")
        self.rows.append(note)


@pytest.fixture
def fake_kg(monkeypatch):
    import src.knowledge_graph as kg_module

    kg = _FakeKG()
    monkeypatch.setattr(kg_module, "get_knowledge_graph", AsyncMock(return_value=kg))
    return kg


def _silence_rows(kg, agent_id):
    return [r for r in kg.rows if r.agent_id == agent_id and "resident-silence" in r.tags]


@pytest.mark.asyncio
async def test_restart_does_not_realert_same_silence_episode(audit_store, fake_kg):
    """The 2026-10-18 shape: one outage, several restarts, one CRITICAL, one finding."""
    broadcaster, store = audit_store
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())

    await background_tasks._silence_check_iteration()
    assert len(_critical_calls(broadcaster)) == 1
    assert len(_silence_rows(fake_kg, "sentinel-uuid")) == 1

    # Three restarts, each running long enough to cross the 5x threshold again.
    for _ in range(3):
        _simulate_restart(started_ago=timedelta(hours=2))
        await background_tasks._silence_check_iteration()
        await background_tasks._silence_check_iteration()

    assert len(_critical_calls(broadcaster)) == 1
    # Adopted into the new process, so no downgrade WARNING follows either.
    assert broadcaster.broadcast_event.await_count == 1
    assert "sentinel-uuid" in background_tasks._silence_critical_alerted
    assert "sentinel-uuid" in background_tasks._silence_alerted
    # Each adopting restart re-ensures the finding; the open row dedupes it.
    assert len(_silence_rows(fake_kg, "sentinel-uuid")) == 1
    assert fake_kg.add_calls == 1


@pytest.mark.asyncio
async def test_adopted_alert_retries_failed_kg_finding(audit_store, fake_kg):
    """CRITICAL persisted but its KG write failed: a restart files the finding.

    Only the broadcast is suppressed on adoption. The finding is the durable
    record a later reader sees, so losing it to one failed write would leave
    the outage unrecorded for good.
    """
    broadcaster, store = audit_store
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())

    fake_kg.fail_adds = True
    await background_tasks._silence_check_iteration()
    assert len(_critical_calls(broadcaster)) == 1
    assert fake_kg.add_calls == 1
    assert _silence_rows(fake_kg, "sentinel-uuid") == []

    # KG recovers; governance restarts.
    fake_kg.fail_adds = False
    _simulate_restart(started_ago=timedelta(hours=2))
    await background_tasks._silence_check_iteration()

    assert len(_critical_calls(broadcaster)) == 1  # no second broadcast
    assert fake_kg.add_calls == 2  # the write was attempted again
    assert len(_silence_rows(fake_kg, "sentinel-uuid")) == 1  # and landed

    # A further restart finds the open row and files nothing new.
    _simulate_restart(started_ago=timedelta(hours=2))
    await background_tasks._silence_check_iteration()
    assert len(_critical_calls(broadcaster)) == 1
    assert fake_kg.add_calls == 2  # open row found, no write attempted
    assert len(_silence_rows(fake_kg, "sentinel-uuid")) == 1


@pytest.mark.asyncio
async def test_silence_finding_dedupe_sees_row_behind_many_open_rows(
    isolated_silence_state, fake_kg
):
    """The open-row check filters by tag, so >50 newer open notes cannot hide it."""
    from src.knowledge_graph import DiscoveryNode

    fake_kg.rows.append(_silence_row("sentinel-uuid", rid="old-silence"))
    for i in range(60):
        fake_kg.rows.append(DiscoveryNode(
            id=f"n{i}", agent_id="sentinel-uuid", type="note", summary="unrelated",
            details="", tags=["note"], severity="low", status="open",
        ))
    # Newest first, as the real query orders: the silence row is oldest.
    fake_kg.rows.reverse()

    meta = _make_meta("sentinel-uuid", "Sentinel", datetime.now(timezone.utc).isoformat())
    await background_tasks._log_resident_silence_finding("sentinel-uuid", meta, 300.0, 600)

    assert fake_kg.add_calls == 0


@pytest.mark.asyncio
async def test_new_episode_after_activity_alerts_again(audit_store):
    """Activity after the last alert ends the episode; the next silence is new."""
    broadcaster, store = audit_store
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())

    await background_tasks._silence_check_iteration()
    assert len(_critical_calls(broadcaster)) == 1

    # Backdate the recorded alert so the agent's later check-in falls after it.
    store.events[-1]["ts"] = datetime.now(timezone.utc) - timedelta(hours=10)
    checked_in = datetime.now(timezone.utc) - timedelta(hours=5)
    agent_metadata["sentinel-uuid"].last_update = checked_in.isoformat()

    _simulate_restart(started_ago=timedelta(hours=2))
    await background_tasks._silence_check_iteration()

    assert len(_critical_calls(broadcaster)) == 2


@pytest.mark.asyncio
async def test_prior_warning_does_not_hide_critical_after_restart(audit_store):
    """A WARNING-only episode still escalates to CRITICAL in the next process."""
    broadcaster, store = audit_store
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())
    store.record("sentinel-uuid", "lifecycle_silent", datetime.now(timezone.utc) - timedelta(hours=3))
    store.record("sentinel-uuid", "agent_silent", datetime.now(timezone.utc) - timedelta(hours=3))

    _simulate_restart(started_ago=timedelta(hours=2))
    await background_tasks._silence_check_iteration()

    assert len(_critical_calls(broadcaster)) == 1


@pytest.mark.asyncio
async def test_restart_does_not_repeat_warning_for_same_episode(audit_store):
    """The WARNING tier is de-duplicated against durable state too."""
    broadcaster, store = audit_store
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())
    store.record("sentinel-uuid", "lifecycle_silent", datetime.now(timezone.utc) - timedelta(hours=3))

    # Up 30 minutes: inside the 2x-5x WARNING band for a 600s cadence.
    _simulate_restart(started_ago=timedelta(minutes=30))
    await background_tasks._silence_check_iteration()

    broadcaster.broadcast_event.assert_not_awaited()
    assert "sentinel-uuid" in background_tasks._silence_alerted


@pytest.mark.asyncio
async def test_restart_does_not_repeat_proxy_alive_warning(audit_store, monkeypatch):
    """Past 5x with a live proxy, the WARNING is de-duplicated across restarts too."""
    broadcaster, store = audit_store
    monkeypatch.setattr(background_tasks, "_proxy_alive", lambda *_a, **_kw: True)
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())

    await background_tasks._silence_check_iteration()
    assert broadcaster.broadcast_event.await_count == 1
    assert broadcaster.broadcast_event.await_args.kwargs["payload"]["proxy_alive"] is True

    for _ in range(3):
        _simulate_restart(started_ago=timedelta(hours=2))
        await background_tasks._silence_check_iteration()

    assert broadcaster.broadcast_event.await_count == 1
    assert _critical_calls(broadcaster) == []


@pytest.mark.asyncio
async def test_alert_lookup_failure_fails_open(isolated_silence_state, monkeypatch):
    """An unreadable audit store must not suppress a CRITICAL."""
    import src.audit_db as audit_module

    broadcaster, _ = isolated_silence_state
    monkeypatch.setattr(
        audit_module, "query_audit_events_async", AsyncMock(side_effect=RuntimeError("db down"))
    )
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())

    await background_tasks._silence_check_iteration()

    assert len(_critical_calls(broadcaster)) == 1


@pytest.mark.asyncio
async def test_alert_lookup_is_scoped_to_episode(isolated_silence_state, monkeypatch):
    """The lookup asks for critical alerts for this agent since its last activity."""
    import src.audit_db as audit_module

    query = AsyncMock(return_value=[])
    monkeypatch.setattr(audit_module, "query_audit_events_async", query)
    stale = datetime.now(timezone.utc) - timedelta(hours=30)
    agent_metadata["sentinel-uuid"] = _make_meta("sentinel-uuid", "Sentinel", stale.isoformat())

    await background_tasks._silence_check_iteration()

    query.assert_awaited_once()
    kwargs = query.await_args.kwargs
    assert kwargs["agent_id"] == "sentinel-uuid"
    assert kwargs["event_types"] == ["lifecycle_silent_critical"]
    assert datetime.fromisoformat(kwargs["start_time"]) == stale
    assert kwargs["limit"] == 1

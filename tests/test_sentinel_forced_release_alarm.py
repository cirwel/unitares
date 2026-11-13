"""
Phase A PR 3b — Sentinel forced-release alarm rule (RFC v0.8 §7.10 + §7.11.5).

Tests the forced-release alarm wiring in `agents/sentinel/forced_release_alarm.py`:

  - Per-event alarm for ad-hoc `event_type='forced'` (RFC §7.10 alarm-on-every-event).
  - Batched alarm for `event_type='lease.deprecation_swept'`: groups by
    `deprecation_id`, emits one summary alarm per completed batch
    (RFC §7.11.5 batch suppression).
  - Cursor state so successive polls don't re-emit alarms for already-seen events.

 PR 3b
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    import asyncpg
except ImportError:
    pytest.skip("asyncpg not installed", allow_module_level=True)

from tests.test_db_utils import (
    TEST_DB_URL,
    can_connect_to_test_db,
    ensure_test_database_schema,
)

if not can_connect_to_test_db():
    pytest.skip("governance_test database not available", allow_module_level=True)


async def _cleanup(conn):
    """Reset sweep / event / deprecated_schemes rows from prior tests."""
    await conn.execute(
        "DELETE FROM lease_plane.lease_plane_events WHERE surface_id LIKE 'td:/pr3b%' "
        "OR payload->>'deprecation_id' IN (SELECT deprecation_id::text FROM lease_plane.deprecated_schemes "
        "WHERE marked_by_session_id LIKE 'test-pr3b-%')"
    )
    await conn.execute(
        "DELETE FROM lease_plane.deprecated_schemes WHERE marked_by_session_id LIKE 'test-pr3b-%'"
    )
    await conn.execute(
        "DELETE FROM lease_plane.surface_leases WHERE intent LIKE 'pr3b-test%'"
    )
    # Reserved + legacy test-namespace events (force-release contract fixtures).
    await conn.execute(
        "DELETE FROM lease_plane.lease_plane_events "
        "WHERE surface_id LIKE 'td:/test/%' "
        "OR surface_id LIKE 'td:/force-release-contract-test-%'"
    )


async def _emit_event(
    conn, *, event_type: str, surface_id: str,
    surface_kind: str | None = None, payload: dict | None = None,
) -> uuid.UUID:
    """Insert a synthetic lease_plane_events row, return event_id."""
    if surface_kind is None:
        surface_kind = surface_id.split(":", 1)[0]
    event_id = await conn.fetchval(
        """
        INSERT INTO lease_plane.lease_plane_events
          (event_type, surface_id, surface_kind, advisory_mode, payload)
        VALUES ($1, $2, $3, false, $4::jsonb)
        RETURNING event_id
        """,
        event_type, surface_id, surface_kind, json.dumps(payload or {}),
    )
    return event_id


_STRICT_BEARER = "test-pr3b-strict-bearer"


class _DirectTestDb:
    """Minimal `get_db()` stand-in backed directly by `TEST_DB_URL` on the caller loop."""

    @asynccontextmanager
    async def acquire(self):
        c = await asyncpg.connect(TEST_DB_URL)
        try:
            yield c
        finally:
            await c.close()


async def _poll_over_http(*, last_event_ts: datetime | None):
    """Exercise the full Core `/v1/lease-plane/alarm-events` ASGI route + strict
    bearer auth + `poll_forced_release_alarms` client against `TEST_DB_URL`."""
    from unittest.mock import patch

    from agents.sentinel.forced_release_alarm import poll_forced_release_alarms
    from src import agent_storage
    from src.http_routes.lease_plane_telemetry import http_get_lease_alarm_events

    app = Starlette(
        routes=[
            Route(
                "/v1/lease-plane/alarm-events",
                http_get_lease_alarm_events,
                methods=["GET"],
            )
        ]
    )
    env_overrides = {
        "UNITARES_REST_STRICT": "1",
        "UNITARES_MCP_BEARER_TOKENS": _STRICT_BEARER,
    }
    transport = httpx.ASGITransport(app=app)
    with (
        patch.dict(os.environ, env_overrides, clear=False),
        patch.object(agent_storage, "get_db", return_value=_DirectTestDb()),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await poll_forced_release_alarms(
                last_event_ts=last_event_ts,
                api_url="http://testserver",
                api_token=_STRICT_BEARER,
                http_client=client,
            )


@pytest.fixture
def live_alarm_http_server(monkeypatch):
    """Loopback HTTP server serving `/v1/lease-plane/alarm-events` for sync-thread tests."""
    monkeypatch.setenv("UNITARES_MCP_BEARER_TOKEN", _STRICT_BEARER)

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.headers.get("Authorization") != f"Bearer {_STRICT_BEARER}":
                self.send_response(401)
                self.end_headers()
                return
            payload = json.dumps(
                {
                    "success": True,
                    "polled_at": "2026-11-12T00:00:00+00:00",
                    "since": None,
                    "ad_hoc": [],
                    "deprecation_batches": [],
                    "conflict_batches": [],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


# §9: test_sentinel_force_release_alarm_wired
@pytest.mark.asyncio
async def test_sentinel_force_release_alarm_per_event():
    """RFC §7.10: ad-hoc event_type='forced' produces one alarm per event.

    The fact that this test exists + passes IS the wiring evidence — the
    §9 alias above lets `audit_rfc_section_9_gates.py` recognize it as
    the RFC-named "wired" gate without a separate redundant test.
    """
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        # Seed two ad-hoc forced events.
        await _emit_event(conn, event_type="forced", surface_id="td:/pr3b_a")
        await _emit_event(conn, event_type="forced", surface_id="td:/pr3b_b")

        alarms, new_cursor = await _poll_over_http(last_event_ts=None)
        # One alarm per event; both should appear.
        per_event = [a for a in alarms if a.kind == "ad_hoc"]
        assert len(per_event) == 2, f"expected 2 ad-hoc alarms, got {len(per_event)}"
        surface_ids = {a.extra["surface_id"] for a in per_event}
        assert surface_ids == {"td:/pr3b_a", "td:/pr3b_b"}
        assert new_cursor is not None
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_sentinel_force_release_alarm_dedupes_via_cursor():
    """Re-polling with the cursor returned by a previous poll yields zero new alarms."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await _emit_event(conn, event_type="forced", surface_id="td:/pr3b_dedupe")

        first_alarms, cursor = await _poll_over_http(last_event_ts=None)
        assert len([a for a in first_alarms if a.kind == "ad_hoc"]) >= 1

        # Re-poll with the cursor; no new alarms.
        second_alarms, _ = await _poll_over_http(last_event_ts=cursor)
        ad_hoc_second = [a for a in second_alarms if a.kind == "ad_hoc"]
        assert ad_hoc_second == [], (
            f"cursor-based dedup failed; got {len(ad_hoc_second)} re-emitted alarms"
        )
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_sentinel_batch_alarm_for_deprecation_sweep():
    """RFC §7.11.5: N lease.deprecation_swept events from same deprecation_id
    produce ONE summary alarm, not N per-event alarms."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        # Mark a scheme deprecated and complete the sweep so sweep_completed_at IS NOT NULL.
        depr_id = await conn.fetchval(
            """
            INSERT INTO lease_plane.deprecated_schemes
              (surface_kind, marked_by_session_id, drain_window_days,
               sweep_started_at, sweep_completed_at)
            VALUES ('td', 'test-pr3b-batch', 30, now(), now())
            RETURNING deprecation_id
            """
        )
        # Seed 3 sweep events with the same deprecation_id.
        for sid in ("td:/pr3b_batch_a", "td:/pr3b_batch_b", "td:/pr3b_batch_c"):
            await _emit_event(
                conn, event_type="lease.deprecation_swept",
                surface_id=sid,
                payload={"deprecation_id": str(depr_id), "kind": "td"},
            )

        alarms, _ = await _poll_over_http(last_event_ts=None)
        batched = [a for a in alarms if a.kind == "deprecation_batch"]
        assert len(batched) == 1, (
            f"expected exactly 1 batched alarm for deprecation_id={depr_id}, got {len(batched)}"
        )
        assert batched[0].extra["count"] == 3
        assert batched[0].extra["deprecation_id"] == str(depr_id)
        assert batched[0].extra["kind"] == "td"
        # Importantly: NO per-event 'ad_hoc' alarms for the deprecation_swept events.
        per_event = [a for a in alarms if a.kind == "ad_hoc"]
        ad_hoc_for_swept = [a for a in per_event if "pr3b_batch" in a.extra.get("surface_id", "")]
        assert ad_hoc_for_swept == [], (
            f"deprecation_swept events must not produce per-event alarms; got {ad_hoc_for_swept}"
        )
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_sentinel_batch_alarm_only_after_sweep_completed_at():
    """Batched alarm waits for sweep_completed_at to be set on the
    deprecated_schemes row — partial sweeps don't fire premature alarms."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        # In-progress sweep: sweep_started_at set, sweep_completed_at NULL.
        depr_id = await conn.fetchval(
            """
            INSERT INTO lease_plane.deprecated_schemes
              (surface_kind, marked_by_session_id, drain_window_days, sweep_started_at)
            VALUES ('td', 'test-pr3b-partial', 30, now())
            RETURNING deprecation_id
            """
        )
        await _emit_event(
            conn, event_type="lease.deprecation_swept",
            surface_id="td:/pr3b_partial_a",
            payload={"deprecation_id": str(depr_id), "kind": "td"},
        )

        alarms, _ = await _poll_over_http(last_event_ts=None)
        # No batched alarm yet (sweep still in progress).
        batched = [
            a for a in alarms if a.kind == "deprecation_batch"
            and a.extra.get("deprecation_id") == str(depr_id)
        ]
        assert batched == [], "batched alarm fired before sweep_completed_at was set"

        # Now complete the sweep — alarm should fire on next poll.
        await conn.execute(
            "UPDATE lease_plane.deprecated_schemes SET sweep_completed_at = now() "
            "WHERE deprecation_id = $1",
            depr_id,
        )
        alarms2, _ = await _poll_over_http(last_event_ts=None)
        batched2 = [
            a for a in alarms2 if a.kind == "deprecation_batch"
            and a.extra.get("deprecation_id") == str(depr_id)
        ]
        assert len(batched2) == 1, (
            f"batched alarm should fire after sweep_completed_at; got {len(batched2)}"
        )
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_phase_zero_acquire_race_blocked():
    """RFC §7.11.7: serializable transaction + advisory lock during deprecate_cmd
    blocks concurrent acquires from racing the Phase 0 mark.

    The CLI's deprecate_cmd holds pg_advisory_xact_lock(_lock_key_for_kind(kind)).
    A concurrent pg_advisory_xact_lock attempt on the same key blocks until the
    deprecate transaction commits.

    Note (PR 5): test now imports _lock_key_for_kind() — the SHA-256-derived
    deterministic key. The pre-PR-5 implementation used `abs(hash(kind))` which
    is salted per-process by PYTHONHASHSEED, so the test passed only by accident
    (both holder and deprecate_cmd ran in the same process). PR 5 fixes the CLI;
    this test now uses the same helper as production.
    """
    import asyncio
    from scripts.dev import lease_plane_deprecate as cli
    from scripts.dev.lease_plane_deprecate import _lock_key_for_kind

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)

        kind = "td"
        lock_key = _lock_key_for_kind(kind)

        # Open a separate connection that holds the SAME advisory lock.
        # While it holds, deprecate_cmd's serializable transaction must wait.
        holder_conn = await asyncpg.connect(TEST_DB_URL)
        await holder_conn.execute("BEGIN")
        await holder_conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)

        # Run deprecate_cmd with a tight asyncio timeout — should NOT complete
        # while holder_conn still holds the lock.
        deprecate_task = asyncio.create_task(
            cli.deprecate_cmd(
                kind=kind, session_id="test-pr3b-race",
                drain_window_days=30, db_url=TEST_DB_URL,
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(deprecate_task), timeout=0.5)
            assert False, "deprecate_cmd completed despite advisory lock being held by another tx"
        except asyncio.TimeoutError:
            pass  # expected — lock contention blocks the deprecate

        # Release the holder lock; deprecate_cmd should now complete.
        await holder_conn.execute("ROLLBACK")
        await holder_conn.close()

        rc = await asyncio.wait_for(deprecate_task, timeout=5.0)
        assert rc == 0, f"deprecate_cmd should succeed once lock is released; rc={rc}"
        # Verify the row landed.
        count = await conn.fetchval(
            "SELECT count(*) FROM lease_plane.deprecated_schemes "
            "WHERE marked_by_session_id = 'test-pr3b-race'"
        )
        assert count == 1
    finally:
        await _cleanup(conn)
        await conn.close()


# ---------- reserved test-surface exclusion ----------


@pytest.mark.asyncio
async def test_sentinel_reserved_test_surface_excluded_from_alarms():
    """Forced + conflict events on the reserved test namespace
    (RESERVED_TEST_SURFACE_PREFIX) advance the cursor but emit NO alarms.

    The force-release contract test does a REAL force-release against the live
    router on every run; without this exclusion Sentinel paged a fresh
    high-severity alarm each `pytest` invocation. The cursor must still advance
    past the event so it isn't re-scanned indefinitely.
    """
    from agents.sentinel.forced_release_alarm import RESERVED_TEST_SURFACE_PREFIX

    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        forced_surface = f"{RESERVED_TEST_SURFACE_PREFIX}force-release-contract-{uuid.uuid4()}"
        conflict_surface = f"{RESERVED_TEST_SURFACE_PREFIX}conflict-{uuid.uuid4()}"
        await _emit_event(conn, event_type="forced", surface_id=forced_surface)
        await _emit_event(
            conn, event_type="conflict_held_by_other", surface_id=conflict_surface,
        )

        alarms, new_cursor = await _poll_over_http(last_event_ts=None)
        reserved = [
            a for a in alarms
            if str(a.extra.get("surface_id", "")).startswith(RESERVED_TEST_SURFACE_PREFIX)
        ]
        assert reserved == [], (
            f"reserved test-surface events must not alarm; got {reserved}"
        )
        # Cursor advanced past the events so they aren't re-scanned forever.
        assert new_cursor is not None
    finally:
        await _cleanup(conn)
        await conn.close()


# ---------- conflict_held_by_other: per-cycle batched alarms (RFC §7.11.5) ----------


@pytest.mark.asyncio
async def test_sentinel_conflict_alarm_batched_per_surface():
    """N conflict_held_by_other events on the same surface in one cycle
    produce ONE summary alarm with count=N — not N per-event alarms."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        for _ in range(4):
            await _emit_event(
                conn, event_type="conflict_held_by_other",
                surface_id="td:/pr3b_conflict_same",
            )

        alarms, _ = await _poll_over_http(last_event_ts=None)
        conflict = [a for a in alarms if a.kind == "conflict_batch"]
        assert len(conflict) == 1, (
            f"expected 1 conflict_batch alarm, got {len(conflict)}"
        )
        assert conflict[0].extra["count"] == 4
        assert conflict[0].extra["surface_id"] == "td:/pr3b_conflict_same"
        # And no ad_hoc alarms for these events.
        ad_hoc_for_conflict = [
            a for a in alarms if a.kind == "ad_hoc"
            and a.extra.get("surface_id") == "td:/pr3b_conflict_same"
        ]
        assert ad_hoc_for_conflict == [], (
            f"conflict_held_by_other must not produce per-event alarms; got {ad_hoc_for_conflict}"
        )
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_sentinel_conflict_alarm_separate_per_surface():
    """Conflicts on different surfaces produce one alarm per surface."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        for sid in ("td:/pr3b_conflict_x", "td:/pr3b_conflict_y", "td:/pr3b_conflict_z"):
            await _emit_event(
                conn, event_type="conflict_held_by_other", surface_id=sid,
            )

        alarms, _ = await _poll_over_http(last_event_ts=None)
        conflict = [a for a in alarms if a.kind == "conflict_batch"]
        surface_ids = {a.extra["surface_id"] for a in conflict}
        assert surface_ids == {
            "td:/pr3b_conflict_x", "td:/pr3b_conflict_y", "td:/pr3b_conflict_z",
        }, f"expected one alarm per surface; got {surface_ids}"
        assert all(a.extra["count"] == 1 for a in conflict)
    finally:
        await _cleanup(conn)
        await conn.close()


@pytest.mark.asyncio
async def test_sentinel_conflict_alarm_dedupes_via_cursor():
    """Re-polling with the cursor returned by a previous poll yields zero new
    conflict_batch alarms — and a fresh conflict on the same surface AFTER
    the cursor produces a new alarm with a distinct fingerprint."""
    await ensure_test_database_schema()
    conn = await asyncpg.connect(TEST_DB_URL)
    try:
        await _cleanup(conn)
        await _emit_event(
            conn, event_type="conflict_held_by_other",
            surface_id="td:/pr3b_conflict_dedupe",
        )

        first_alarms, cursor = await _poll_over_http(last_event_ts=None)
        first_conflict = [a for a in first_alarms if a.kind == "conflict_batch"]
        assert len(first_conflict) == 1
        first_fp = first_conflict[0].fingerprint

        # Re-poll with cursor: no new alarms.
        second_alarms, cursor2 = await _poll_over_http(last_event_ts=cursor)
        second_conflict = [a for a in second_alarms if a.kind == "conflict_batch"]
        assert second_conflict == [], (
            f"cursor dedup failed; got {len(second_conflict)} re-emitted conflict alarms"
        )

        # New conflict on the same surface AFTER the cursor → new alarm with
        # a distinct fingerprint (fingerprint includes max_ts so successive
        # bursts on the same surface stay distinguishable downstream).
        await _emit_event(
            conn, event_type="conflict_held_by_other",
            surface_id="td:/pr3b_conflict_dedupe",
        )
        third_alarms, _ = await _poll_over_http(last_event_ts=cursor2)
        third_conflict = [a for a in third_alarms if a.kind == "conflict_batch"]
        assert len(third_conflict) == 1
        assert third_conflict[0].fingerprint != first_fp, (
            "second-cycle conflict on same surface must have a distinct fingerprint"
        )
    finally:
        await _cleanup(conn)
        await conn.close()


# ---------- _poll_sync_forced_release: executor isolation ----------


def test_poll_sync_forced_release_returns_same_tuple_shape_when_no_events(
    live_alarm_http_server: str,
):
    """`_poll_sync_forced_release` must return the same `(alarms, new_cursor)`
    tuple shape as the async `poll_forced_release_alarms` it wraps.
    """
    from agents.sentinel.agent import _poll_sync_forced_release

    alarms, new_cursor = _poll_sync_forced_release(live_alarm_http_server, None)
    assert isinstance(alarms, list)
    from datetime import datetime as _dt
    assert new_cursor is None or isinstance(new_cursor, _dt)


def test_poll_sync_forced_release_loop_free_thread_succeeds_twice(
    live_alarm_http_server: str,
):
    """Sanity: two consecutive calls from a loop-free thread succeed without
    leaving a running event loop or leaked client between them.
    """
    from agents.sentinel.agent import _poll_sync_forced_release

    alarms_1, _ = _poll_sync_forced_release(live_alarm_http_server, None)
    alarms_2, _ = _poll_sync_forced_release(live_alarm_http_server, None)

    assert isinstance(alarms_1, list)
    assert isinstance(alarms_2, list)


def test_poll_sync_forced_release_callable_via_run_in_executor(
    live_alarm_http_server: str,
):
    """Exercise the production dispatch shape: outer asyncio loop -> default
    ThreadPoolExecutor -> two consecutive `_poll_sync_forced_release` calls.
    """
    import asyncio
    from agents.sentinel.agent import _poll_sync_forced_release

    async def driver():
        loop = asyncio.get_running_loop()
        alarms_1, _ = await loop.run_in_executor(
            None, _poll_sync_forced_release, live_alarm_http_server, None,
        )
        alarms_2, _ = await loop.run_in_executor(
            None, _poll_sync_forced_release, live_alarm_http_server, None,
        )
        return alarms_1, alarms_2

    alarms_1, alarms_2 = asyncio.run(driver())
    assert isinstance(alarms_1, list)
    assert isinstance(alarms_2, list)


def test_poll_sync_forced_release_timeout_propagates(monkeypatch):
    """The wrapper bounds executor-thread lifetime via `asyncio.wait_for` so a
    wedged HTTP call doesn't accumulate threads in the default ThreadPoolExecutor.
    """
    import asyncio
    from agents.sentinel import agent as agent_mod

    monkeypatch.setattr(agent_mod, "_POLL_TIMEOUT_S", 0.2)

    async def _wedged_poll(*, api_url=None, last_event_ts=None, **_kw):
        await asyncio.sleep(5.0)
        return [], None

    from agents.sentinel import forced_release_alarm
    monkeypatch.setattr(forced_release_alarm, "poll_forced_release_alarms", _wedged_poll)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        agent_mod._poll_sync_forced_release("http://127.0.0.1:8767", None)

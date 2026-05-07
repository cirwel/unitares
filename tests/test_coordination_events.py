"""Tests for src/coordination_events.py — Wave 0 emitter.

Pins the migration-035 envelope contract:
  - Service enum (6 names; rejection of unknowns)
  - event_type namespace (regex coordination_failure.<lowercase_subtype>)
  - payload + context MUST be JSON objects
  - context auto-populated by emitter (caller doesn't pass)
  - event_id returned for replay/dedup
  - agent_id optional, indexed for per-agent attribution

Live DB integration tests against governance_test (mirrors the
test_lease_plane_substrate_state.py pattern).
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

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

from src.coordination_events import (  # noqa: E402
    COORDINATION_FAILURE_ANYIO_CANCELLATION,
    COORDINATION_FAILURE_ASYNCPG_CONNECT_ERROR,
    COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_BEAM_TO_PYTHON_REQUEST_FAILED,
    COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_PYTHON_TO_BEAM_REQUEST_FAILED,
    COORDINATION_FAILURE_EXECUTOR_POOL_EXHAUSTION,
    COORDINATION_FAILURE_MCP_HANDLER_TIMEOUT,
    WAVE_0_EVENT_TYPES,
    _validate_event_type,
    emit_event,
    reset_context_cache_for_tests,
)


_TEST_AGENT_ID = "00000000-0000-4000-8000-000000000001"


@pytest_asyncio.fixture
async def pool():
    await ensure_test_database_schema()
    pool = await asyncpg.create_pool(TEST_DB_URL, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


@pytest.fixture(autouse=True)
def _reset_context_cache():
    reset_context_cache_for_tests()
    yield
    reset_context_cache_for_tests()


async def _cleanup(pool, agent_id: str = _TEST_AGENT_ID) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM audit.coordination_events WHERE agent_id = $1 OR "
            "context->>'host' = 'pytest-test-host'",
            agent_id,
        )


# ---------- event_type validation (client-side mirror of CHECK regex) ----------


def test_validate_event_type_accepts_all_wave_0_constants():
    """Every documented Wave 0 event_type passes client-side validation."""
    for et in WAVE_0_EVENT_TYPES:
        _validate_event_type(et)  # must not raise


def test_event_type_constants_match_documented_set():
    """Drift guard: WAVE_0_EVENT_TYPES MUST equal the documented values.
    If you add a new event_type to the module, also extend this set, the
    migration's regex CHECK (when needed), and the dashboard panel — drift
    between code and DB CHECK becomes silent rejection.

    The Wave 2 schema extension (RFC roadmap) added the
    `coordination_failure.beam_python_boundary.*` namespace. The migration's
    existing regex `^(coordination_failure)(\\.[a-z_]+)+$` already accepts
    multi-segment subtypes — no migration follow-up was needed for that
    extension. Future families (e.g., `coordination_recovery.*`) WILL
    require a migration.
    """
    expected = {
        "coordination_failure.asyncpg_connect_error",
        "coordination_failure.anyio_cancellation",
        "coordination_failure.executor_pool_exhaustion",
        "coordination_failure.mcp_handler_timeout",
        # Wave 2 extension — directional cross-runtime request failures.
        "coordination_failure.beam_python_boundary.python_to_beam_request_failed",
        "coordination_failure.beam_python_boundary.beam_to_python_request_failed",
    }
    assert WAVE_0_EVENT_TYPES == expected


def test_beam_python_boundary_constants_pass_validation():
    """Both Wave 2 boundary subtypes pass the client-side regex validator
    (which mirrors the migration's CHECK). Wire-up sites land in Wave 3,
    but the constants are stable referents from this PR forward."""
    _validate_event_type(
        COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_PYTHON_TO_BEAM_REQUEST_FAILED
    )
    _validate_event_type(
        COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_BEAM_TO_PYTHON_REQUEST_FAILED
    )


def test_beam_python_boundary_constants_have_canonical_dotted_form():
    """The strings themselves carry the structure: family . namespace . direction.
    Pin the literal forms here so a future rename can't slip through silently —
    Wave 3 wire-up sites will look up these constants by name, but downstream
    audit consumers (Chronicler projection, dashboard) match on the literal
    event_type column. Renaming would silently break replay."""
    assert (
        COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_PYTHON_TO_BEAM_REQUEST_FAILED
        == "coordination_failure.beam_python_boundary.python_to_beam_request_failed"
    )
    assert (
        COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_BEAM_TO_PYTHON_REQUEST_FAILED
        == "coordination_failure.beam_python_boundary.beam_to_python_request_failed"
    )


def test_validate_event_type_rejects_unknown_family():
    """Family must be 'coordination_failure' (Wave 0). Wave 1+ extend the
    regex via migration AND register the new family here in the same PR."""
    with pytest.raises(ValueError, match="not in Wave 0 set"):
        _validate_event_type("coordination_recovery.something")


def test_validate_event_type_accepts_sub_namespace():
    """Sub-namespaces extend the event_type contract (council C5): subtype
    discrimination lives in event_type, NOT in payload.subtype.

    Examples that MUST pass:
      coordination_failure.mcp_handler_timeout                    (one segment)
      coordination_failure.mcp_handler_timeout.identity_step      (two segments)
      coordination_failure.mcp_handler_timeout.resident_progress  (alt subtype)
    """
    _validate_event_type("coordination_failure.mcp_handler_timeout.identity_step")
    _validate_event_type("coordination_failure.mcp_handler_timeout.resident_progress")
    _validate_event_type("coordination_failure.anyio_cancellation.background_task")


def test_validate_event_type_rejects_missing_subtype():
    with pytest.raises(ValueError, match="family.subtype"):
        _validate_event_type("coordination_failure")


def test_validate_event_type_rejects_uppercase_subtype():
    """Migration regex pins lowercase + underscores. Validate client-side too
    so callers get a clear error before the DB rejects."""
    with pytest.raises(ValueError, match="lowercase"):
        _validate_event_type("coordination_failure.AsyncpgConnectError")


def test_validate_event_type_rejects_empty_string():
    with pytest.raises(ValueError):
        _validate_event_type("")


# ---------- live DB emit + read-back ----------


@pytest.mark.asyncio
async def test_emit_event_writes_row_with_envelope_fields(pool):
    """The full envelope contract: emit returns event_id; row in DB has all
    seven required columns populated correctly; context is auto-set."""
    await _cleanup(pool)

    payload = {"error_class": "ConnectionRefused", "retries": 3}
    event_id = await emit_event(
        pool,
        service="lease_plane",
        event_type=COORDINATION_FAILURE_ASYNCPG_CONNECT_ERROR,
        payload=payload,
        agent_id=_TEST_AGENT_ID,
    )
    assert isinstance(event_id, uuid.UUID)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ts, event_id, service, event_type, agent_id, "
            "       payload, context "
            "FROM audit.coordination_events WHERE event_id = $1",
            event_id,
        )
    assert row is not None
    assert row["service"] == "lease_plane"
    assert row["event_type"] == COORDINATION_FAILURE_ASYNCPG_CONNECT_ERROR
    assert row["agent_id"] == _TEST_AGENT_ID

    payload_back = json.loads(row["payload"])
    assert payload_back["error_class"] == "ConnectionRefused"
    assert payload_back["retries"] == 3

    context_back = json.loads(row["context"])
    # Context auto-populated by emitter — caller did NOT pass these.
    assert "git_commit" in context_back
    assert "service_pid" in context_back
    assert "running_since" in context_back
    assert "host" in context_back

    await _cleanup(pool)


@pytest.mark.asyncio
async def test_emit_event_omits_agent_id_when_none(pool):
    """agent_id is optional. When None, the row stores NULL (the partial
    index idx_coord_events_agent_ts WHERE agent_id IS NOT NULL skips it)."""
    await _cleanup(pool)

    event_id = await emit_event(
        pool,
        service="governance_mcp",
        event_type=COORDINATION_FAILURE_MCP_HANDLER_TIMEOUT,
        payload={"tool": "process_agent_update", "timeout_ms": 45000},
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT agent_id FROM audit.coordination_events WHERE event_id = $1",
            event_id,
        )
    assert row["agent_id"] is None

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM audit.coordination_events WHERE event_id = $1", event_id)


@pytest.mark.asyncio
async def test_emit_event_empty_payload_uses_default_object(pool):
    """Caller may pass payload=None; emitter substitutes empty dict, which
    satisfies the jsonb_typeof = 'object' CHECK."""
    await _cleanup(pool)

    event_id = await emit_event(
        pool,
        service="vigil",
        event_type=COORDINATION_FAILURE_ANYIO_CANCELLATION,
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT payload FROM audit.coordination_events WHERE event_id = $1",
            event_id,
        )
    assert json.loads(row["payload"]) == {}
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM audit.coordination_events WHERE event_id = $1", event_id)


@pytest.mark.asyncio
async def test_emit_event_rejects_non_dict_payload(pool):
    """payload MUST be a dict — emitter rejects client-side. Mirrors the
    jsonb_typeof = 'object' CHECK so the error message names the field
    rather than surfacing as a Postgres CHECK violation."""
    with pytest.raises(ValueError, match="payload must be a dict"):
        await emit_event(
            pool,
            service="watcher",
            event_type=COORDINATION_FAILURE_EXECUTOR_POOL_EXHAUSTION,
            payload="not a dict",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_emit_event_rejects_unknown_service_via_db(pool):
    """Service enum is enforced server-side by CHECK. The Service Literal
    type catches it at the type-checker level; here we exercise the DB
    rejection path (the type system isn't enforced at runtime)."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError) as exc_info:
        await emit_event(
            pool,
            service="not_a_real_service",  # type: ignore[arg-type]
            event_type=COORDINATION_FAILURE_ASYNCPG_CONNECT_ERROR,
        )
    assert "service" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_emit_event_rejects_unknown_event_type_namespace_via_validator(pool):
    """Client-side validator catches unknown families before the DB does.
    Faster failure + clearer error message than the regex CHECK."""
    with pytest.raises(ValueError, match="not in Wave 0 set"):
        await emit_event(
            pool,
            service="sentinel",
            event_type="coordination_recovery.healing",
        )


@pytest.mark.asyncio
async def test_emit_event_returns_distinct_event_ids(pool):
    """Each emit gets a fresh UUID — no event_id collision under rapid emit."""
    await _cleanup(pool)

    ids = []
    for _ in range(5):
        eid = await emit_event(
            pool,
            service="sentinel",
            event_type=COORDINATION_FAILURE_ANYIO_CANCELLATION,
            payload={"task_name": "fleet_cycle"},
            agent_id=_TEST_AGENT_ID,
        )
        ids.append(eid)
    assert len(set(ids)) == 5  # all distinct

    await _cleanup(pool)


@pytest.mark.asyncio
async def test_emit_event_explicit_ts_passes_through(pool):
    """Caller may pass ts override (for events captured asynchronously where
    emit happens later than the event itself)."""
    await _cleanup(pool)
    event_ts = datetime.now(UTC) - timedelta(seconds=120)

    event_id = await emit_event(
        pool,
        service="chronicler",
        event_type=COORDINATION_FAILURE_ASYNCPG_CONNECT_ERROR,
        payload={},
        ts=event_ts,
    )
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT ts FROM audit.coordination_events WHERE event_id = $1",
            event_id,
        )
    # tolerance for timestamptz round-trip jitter
    assert abs((row["ts"] - event_ts).total_seconds()) < 0.001
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM audit.coordination_events WHERE event_id = $1", event_id)


@pytest.mark.asyncio
async def test_beam_python_boundary_types_pass_db_check_constraint(pool):
    """Live-DB regression for the Wave 2 schema extension: both directional
    boundary subtypes pass migration 035's `event_type` regex CHECK and
    write a row with the dotted three-segment shape preserved.

    The migration's existing regex `^(coordination_failure)(\\.[a-z_]+)+$`
    repeats the `\\.[a-z_]+` group, so multi-segment subtypes like
    `coordination_failure.beam_python_boundary.python_to_beam_request_failed`
    pass without a new migration. This test pins that — if a future
    migration tightens the regex (e.g., to `\\.[a-z_]+\\.[a-z_]+` flat),
    the CHECK would silently start rejecting these and the test catches it
    before Wave 3 wire-up sites land in production."""
    await _cleanup(pool)

    payload = {
        "endpoint": "http://127.0.0.1:8788/v1/lease/acquire",
        "method": "POST",
        "error_class": "timeout",
        "status_code": None,
        "elapsed_ms": 2050,
    }
    e1 = await emit_event(
        pool,
        service="governance_mcp",
        event_type=COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_PYTHON_TO_BEAM_REQUEST_FAILED,
        payload=payload,
        agent_id=_TEST_AGENT_ID,
    )
    e2 = await emit_event(
        pool,
        service="sentinel",
        event_type=COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_BEAM_TO_PYTHON_REQUEST_FAILED,
        payload={
            "endpoint": "/api/findings",
            "method": "POST",
            "error_class": "non_200",
            "status_code": 503,
            "elapsed_ms": 142,
        },
    )

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT event_type, service FROM audit.coordination_events "
            "WHERE event_id IN ($1, $2) ORDER BY event_type",
            e1, e2,
        )
    by_type = {r["event_type"]: r["service"] for r in rows}
    assert by_type[
        COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_BEAM_TO_PYTHON_REQUEST_FAILED
    ] == "sentinel"
    assert by_type[
        COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_PYTHON_TO_BEAM_REQUEST_FAILED
    ] == "governance_mcp"

    await _cleanup(pool)


@pytest.mark.asyncio
async def test_context_cache_returns_same_values_across_emits(pool):
    """Per the module docstring: context is captured once at first emit and
    cached. Two emits in the same process must report the same git_commit,
    pid, host, running_since (the running process IS that triple)."""
    await _cleanup(pool)
    e1 = await emit_event(pool, service="vigil", event_type=COORDINATION_FAILURE_ANYIO_CANCELLATION)
    e2 = await emit_event(pool, service="vigil", event_type=COORDINATION_FAILURE_ANYIO_CANCELLATION)
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT context FROM audit.coordination_events WHERE event_id IN ($1, $2)",
            e1, e2,
        )
    contexts = [json.loads(r["context"]) for r in rows]
    assert contexts[0] == contexts[1]
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM audit.coordination_events WHERE event_id IN ($1, $2)", e1, e2
        )

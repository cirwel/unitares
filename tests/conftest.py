"""
Pytest configuration and fixtures for unitares tests.
"""
import os
import pytest
import pytest_asyncio
import tempfile
import warnings
import sys
import asyncio
from collections import defaultdict, deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Redirect Vigil's log file to a temp path BEFORE any test imports the agent
# module. The agent's LOG_FILE is resolved at module-import time from the
# VIGIL_LOG_FILE env var, so this must happen before the first
# `from agents.vigil.agent import …` anywhere in the test suite.
#
# Without this, tests that mock client.audit_knowledge to raise
# (e.g. RuntimeError("boom")) and call _run_stale_opens_sweep through a real
# VigilAgent log "stale-opens sweep failed (boom); continuing cycle" to the
# operator's production Vigil log at ~/Library/Logs/unitares-vigil.log —
# observed in the wild as a steady stream of test-injected noise.
os.environ.setdefault(
    "VIGIL_LOG_FILE",
    str(Path(tempfile.gettempdir()) / "unitares-vigil-test.log"),
)

# The resident roster is now deployment config (UNITARES_RESIDENTS), empty by
# default for user-agnostic installs. The test suite validates the resident
# machinery against the canonical fleet, so configure it here BEFORE any test
# imports src.grounding.class_indicator (KNOWN_RESIDENT_LABELS is resolved at
# module-import time from this env var). Tests that need a different roster set
# the env var and call class_indicator.load_resident_labels() directly.
os.environ.setdefault(
    "UNITARES_RESIDENTS",
    "Lumen,Vigil,Sentinel,Watcher,Steward,Chronicler",
)

# The resident-progress registry is likewise deployment config, loaded from a
# JSON manifest (UNITARES_RESIDENT_PROGRESS_MANIFEST), empty by default. Point
# the suite at the canonical fleet manifest BEFORE any test imports
# src.resident_progress.registry (RESIDENT_PROGRESS_REGISTRY is built at
# module-import time). Tests that vary it call load_resident_progress_registry()
# directly or patch the registry.
os.environ.setdefault(
    "UNITARES_RESIDENT_PROGRESS_MANIFEST",
    str(Path(__file__).resolve().parent.parent / "config" / "resident_progress.example.json"),
)

# Agent-state lock backend. Production defaults to the PostgreSQL advisory lock
# (src/state_locking.py), but most tests exercise the lock with a mocked/absent
# DB and assert file-lock semantics — so the suite defaults to the fcntl backend.
# Tests covering the advisory path opt in explicitly via monkeypatch.setenv.
os.environ.setdefault("UNITARES_AGENT_LOCK_BACKEND", "fcntl")

# Filter ResourceWarnings globally before any imports
warnings.filterwarnings("ignore", category=ResourceWarning)


def pytest_configure(config):
    """Configure pytest to filter noisy ResourceWarnings from DB drivers."""
    warnings.filterwarnings(
        "ignore",
        message="unclosed database",
        category=ResourceWarning
    )


# Track AsyncMock coroutine leaks across the session. We cannot catch them
# via `filterwarnings = error:...` in pyproject.toml because pytest wraps
# every test in `warnings.catch_warnings(record=True)`, which captures
# warnings into a log instead of letting an "error" filter promote them to
# exceptions. The `pytest_warning_recorded` hook fires for each recorded
# warning — we inspect each one, stash the unawaited-coroutine ones, and
# fail the session at teardown.
#
# Why fail at session teardown instead of per-test:
#   - The warning often fires after the test body has returned but before
#     pytest's per-test catch_warnings context exits (GC between items).
#     Failing the already-completed test would require mutating its outcome,
#     which pytest doesn't expose cleanly.
#   - Session-level exit-status signal is enough to make CI fail, which is
#     the actual regression gate we want.
_ASYNCMOCK_LEAKS: list = []


def pytest_warning_recorded(warning_message, nodeid, when, location):
    """Capture unawaited-coroutine warnings for session-level enforcement."""
    msg = str(warning_message.message)
    if (
        "coroutine" in msg
        and "was never awaited" in msg
        and warning_message.category is RuntimeWarning
    ):
        _ASYNCMOCK_LEAKS.append((nodeid, when, msg.splitlines()[0]))


def pytest_sessionfinish(session, exitstatus):
    """Fail the session if any AsyncMock coroutine leaks were observed."""
    if not _ASYNCMOCK_LEAKS:
        return
    import sys
    uniq = sorted({(nid, summary) for nid, _when, summary in _ASYNCMOCK_LEAKS})
    print(
        f"\n\nFAILED: {len(uniq)} unawaited AsyncMock coroutine leak(s):",
        file=sys.stderr,
    )
    for nid, summary in uniq[:15]:
        print(f"  - {nid}\n      {summary}", file=sys.stderr)
    if len(uniq) > 15:
        print(f"  ... and {len(uniq) - 15} more", file=sys.stderr)
    print(
        "\nHint: add an explicit AsyncMock stub to tests/conftest.py "
        "_isolate_db_backend for the leaking db method, or fix the test\n"
        "to ensure all mocked coroutines are awaited.\n",
        file=sys.stderr,
    )
    if exitstatus == 0:
        session.exitstatus = 1


@pytest.fixture(autouse=True)
def _isolate_db_backend(monkeypatch):
    """
    Prevent tests from accidentally connecting to production PostgreSQL.

    Sets a mock DB backend as the get_db() singleton, so any code path that
    reaches get_db() without explicit mocking gets a safe no-op mock instead
    of a real database connection. This prevents ghost agents from being
    created in the production database during test runs.

    Tests that need real DB access (e.g. test_postgres_backend_integration.py)
    create their own backend instances directly, bypassing get_db().

    Tests that already mock at higher levels (agent_storage, get_db patches)
    are unaffected — their mocks intercept before reaching the singleton.
    """
    import src.db as db_module
    import src.agent_storage as storage_module

    mock_backend = AsyncMock()
    # Identity operations
    mock_backend.get_identity.return_value = None
    mock_backend.get_identity_by_id.return_value = None
    mock_backend.upsert_identity.return_value = 1
    mock_backend.upsert_agent.return_value = True
    mock_backend.update_agent_fields.return_value = True
    mock_backend.list_identities.return_value = []
    mock_backend.list_recently_active_identities.return_value = []
    mock_backend.update_identity_status.return_value = True
    mock_backend.update_identity_metadata.return_value = True
    mock_backend.verify_api_key.return_value = True
    mock_backend.get_agent_label.return_value = None
    mock_backend.find_agent_by_label.return_value = None
    # Session operations
    mock_backend.create_session.return_value = True
    mock_backend.get_session.return_value = None
    mock_backend.update_session_activity.return_value = True
    mock_backend.end_session.return_value = True
    mock_backend.get_active_sessions_for_identity.return_value = []
    mock_backend.cleanup_expired_sessions.return_value = 0
    # State operations
    mock_backend.record_agent_state.return_value = 1
    mock_backend.get_latest_agent_state.return_value = None
    mock_backend.get_agent_state_history.return_value = []
    mock_backend.reconstruct_eisv_series.return_value = {
        "E": [], "I": [], "S": [], "V": [],
    }
    # R1 v3.3-D provisional helpers + v3.3-C calibration_state singleton
    mock_backend.mark_lineage_provisional.return_value = True
    mock_backend.confirm_lineage.return_value = True
    mock_backend.is_lineage_provisional.return_value = False
    # R2 lineage lifecycle helpers (PR 1 — migration 036 + storage helpers).
    # Per the R2 plan §"Test 10 (meta)" — explicit stubs avoid the
    # AsyncMock auto-child coroutine-leak pattern noted in R1 v3.2-E.
    mock_backend.declare_lineage.return_value = True
    mock_backend.demote_lineage.return_value = True
    mock_backend.archive_lineage.return_value = True
    mock_backend.increment_chain_obs_count.return_value = 0
    mock_backend.stamp_lineage_eval.return_value = None
    mock_backend.are_lineages_provisional.return_value = {}
    # R2 PR 3 council fixes — re-declaration reset and symmetric
    # rejection clear. Default False so unit tests that don't seed a
    # terminal-state row exercise the no-op branch (matches the live
    # helper contract for active rows).
    mock_backend.reset_lineage_for_redeclaration.return_value = False
    mock_backend.clear_lineage_declaration.return_value = False
    # R2 PR 2 — lineage FSM single-query read. Default to None so tests
    # that don't seed a provisional/confirmed row exercise the
    # `no_parent` skip path. Per the R2 plan §"Test 10 (meta)" this
    # explicit stub avoids the AsyncMock auto-child coroutine-leak
    # pattern noted in R1 v3.2-E.
    mock_backend.read_lineage_state.return_value = None
    # R2 PR 4 — lineage-eval sweeper candidate selector. Default to an
    # empty list so tests that don't seed candidates exercise the
    # zero-work cycle path. Explicit stub avoids the AsyncMock auto-child
    # coroutine-leak pattern.
    mock_backend.select_lineage_eval_candidates.return_value = []
    # R2 PR 3 — cross-role pre-check reads `metadata.tags[0]`. Default
    # to None so the charitable orphan branch fires by default in unit
    # tests that don't seed a class tag (per the helper's contract:
    # missing class on either side → accept). Explicit stub avoids the
    # AsyncMock auto-child coroutine-leak pattern.
    mock_backend.read_class_tag.return_value = None
    mock_backend.read_r1_calibration_state.return_value = {
        "calibration_status": "seeded",
        "seeded_since": None,
        "earned_at": None,
        "failed_at": None,
        "updated_at": None,
    }
    mock_backend.transition_r1_calibration_state.return_value = {
        "calibration_status": "seeded",
        "seeded_since": None,
        "earned_at": None,
        "failed_at": None,
        "updated_at": None,
    }
    mock_backend.record_r1_score_audit.return_value = True
    # R1 v3.3-A public KG emission target — `kg_add_discovery` is fired
    # at the end of every score_trajectory_continuity call to publish the
    # redacted public node. Returns None on the real path; AsyncMock
    # leak-prevention requires an explicit stub.
    mock_backend.kg_add_discovery.return_value = None
    # Audit/tool operations
    mock_backend.append_audit_event.return_value = True
    mock_backend.query_audit_events.return_value = []
    mock_backend.search_audit_events.return_value = []
    mock_backend.append_tool_usage.return_value = True
    mock_backend.query_tool_usage.return_value = []
    # Calibration
    mock_backend.get_calibration.return_value = {}
    mock_backend.update_calibration.return_value = True
    # Graph
    mock_backend.graph_query.return_value = []
    mock_backend.graph_available.return_value = False
    # Thread operations
    mock_backend.get_agent_thread_info = AsyncMock(return_value=None)
    mock_backend.get_thread_nodes = AsyncMock(return_value=[])
    # Identity batch lookup (used by list_agents full-mode trust_tier fallback
    # in src/mcp_handlers/lifecycle/query.py:394). Without an explicit stub,
    # the auto-generated AsyncMock child leaked unawaited _execute_mock_call
    # coroutines whenever handle_list_agents walked trust_tier=None agents —
    # observed as 2 surviving RuntimeWarnings after the _lm/_lo rebind fix
    # (KG bug 2026-04-10T06:27:12.501426 follow-up).
    mock_backend.get_identities_batch = AsyncMock(return_value={})
    # Baseline preload (src/mcp_handlers/updates/phases.py:667). Must return
    # None so the AgentBaseline.from_dict(AsyncMock) path doesn't fire — that
    # path calls `data.get('last_updated')` which on an AsyncMock returns a
    # coroutine that is never awaited. Latent bug surfaced after
    # governance_core was folded into this repo in 2026-04-24; before the
    # fold, `from governance_core import ...` in phases.py could silently
    # ImportError in CI and the entire preload block was skipped, masking
    # this leak.
    mock_backend.load_agent_baseline = AsyncMock(return_value=None)
    # Health
    mock_backend.init.return_value = None
    mock_backend.close.return_value = None
    mock_backend.health_check.return_value = {"status": "ok", "backend": "test_mock"}
    # acquire() — must be a regular (non-async) call returning an async context manager
    from unittest.mock import MagicMock
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = []
    mock_conn.fetchval.return_value = None
    mock_conn.fetchrow.return_value = None
    mock_conn.execute.return_value = "SELECT 0"
    mock_backend.acquire = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_conn),
        __aexit__=AsyncMock(return_value=False),
    ))
    # transaction() — same as acquire() for tests (returns same mock conn)
    mock_backend.transaction = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=mock_conn),
        __aexit__=AsyncMock(return_value=False),
    ))

    # Set mock as the singleton — ALL get_db() calls return this
    monkeypatch.setattr(db_module, "_db_instance", mock_backend)
    # Clear the db-ready cache so _ensure_db_ready() doesn't skip init
    storage_module._db_ready_cache.clear()

    # Also neutralize src.dialectic_db, which keeps its OWN singleton
    # (DialecticDB instance) that would otherwise open a real asyncpg pool
    # during tests and leak AsyncMock coroutines when handlers call
    # pg_update_phase / resolve_session_async. Observed failures:
    #   - test_dialectic_session_handlers::TestSaveSession::test_save_session_error_logged_not_raised
    #   - test_lifecycle_recovery::TestDetectStuckAgentsAutoRecover::test_auto_recover_*_triggers_dialectic
    # Every dialectic_db.*_async wrapper does `db = await get_dialectic_db();
    # return await db.<method>(...)` — so we need a mock with those methods
    # as AsyncMock returning safe defaults.
    try:
        import src.dialectic_db as dialectic_db_module
        mock_dialectic_db = AsyncMock()
        mock_dialectic_db.update_session_phase.return_value = True
        mock_dialectic_db.update_session_reviewer.return_value = True
        mock_dialectic_db.update_session_status.return_value = True
        mock_dialectic_db.resolve_session.return_value = True
        mock_dialectic_db.create_session.return_value = {"session_id": "mock-session"}
        mock_dialectic_db.get_session.return_value = None
        mock_dialectic_db.get_session_by_agent.return_value = None
        mock_dialectic_db.get_all_sessions_by_agent.return_value = []
        mock_dialectic_db.is_agent_in_active_session.return_value = False
        mock_dialectic_db.has_recently_reviewed.return_value = False
        mock_dialectic_db.add_message.return_value = 1
        mock_dialectic_db.get_active_sessions.return_value = []
        mock_dialectic_db.get_sessions_awaiting_reviewer.return_value = []
        mock_dialectic_db.init.return_value = None
        mock_dialectic_db.close.return_value = None
        # _ensure_pool / _pool: make _ensure_pool raise so the fast-path in
        # load_session_as_dict (src/mcp_handlers/dialectic/session.py:242)
        # hits its try/except and returns None cleanly, BEFORE it touches
        # compatible_acquire(db._pool) with a mock pool whose .fetchrow is a
        # plain MagicMock — that chain produced unawaited AsyncMock coroutines.
        mock_dialectic_db._ensure_pool = AsyncMock(
            side_effect=RuntimeError("test: dialectic DB pool not available")
        )
        mock_dialectic_db._pool = None
        monkeypatch.setattr(dialectic_db_module, "_db_instance", mock_dialectic_db)
    except ImportError:
        pass  # dialectic_db not available in some minimal test configs

    yield mock_backend

    # monkeypatch auto-restores _db_instance on teardown
    storage_module._db_ready_cache.clear()


@pytest.fixture(scope="session", autouse=True)
def _isolate_tool_usage_tracker(tmp_path_factory):
    """
    Redirect the tool_usage_tracker singleton to a session-scoped tmp file.

    governance_monitor.process_update() calls get_tool_usage_tracker().get_usage_stats()
    every cycle as part of dual-log complexity grounding. The default singleton
    points at <repo>/data/tool_usage.jsonl, which on developer machines accumulates
    to hundreds of MB / millions of entries — get_usage_stats() reads and json.loads()
    every line on each call.

    Measured cost (2026-05-06 dev box, 176MB / 1.09M-line tool_usage.jsonl):
      - Default tracker: 2779 ms / process_update
      - Tmp tracker:        0.3 ms / process_update  (~9000x speedup)

    Tests that legitimately exercise the tracker construct ToolUsageTracker
    directly with their own log_file (test_tool_usage_tracker.py), or patch
    get_tool_usage_tracker via unittest.mock.patch — both override this default.

    Session scope is safe because the tracker has no cross-test state we read;
    the file is only ever appended to and we never assert on its contents here.
    """
    import src.tool_usage_tracker as tut
    tmp_log = tmp_path_factory.mktemp("tool_usage") / "tool_usage.jsonl"
    original = tut._tool_usage_tracker
    tut._tool_usage_tracker = tut.ToolUsageTracker(log_file=tmp_log)
    try:
        yield tut._tool_usage_tracker
    finally:
        tut._tool_usage_tracker = original


@pytest.fixture(autouse=True)
def _isolate_recall_telemetry(monkeypatch, tmp_path):
    """Keep recall-miss telemetry out of the real data/telemetry file."""
    try:
        import src.recall_telemetry as recall_telemetry

        monkeypatch.setattr(
            recall_telemetry,
            "_telemetry_file",
            lambda: tmp_path / "recall_misses.jsonl",
        )
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _neutralize_metadata_loading(monkeypatch):
    """
    Prevent ensure_metadata_loaded() from trying to connect to PostgreSQL.

    The canonical state lives on ``src.agent_metadata_model`` (the
    ``_model`` reference inside ``agent_metadata_persistence``). Patching
    ``agent_state._metadata_loaded`` alone is a no-op because that name is
    only an import-time copy of the bool — assigning to it does not
    propagate back to ``agent_metadata_model``. Pre-2026-05-06 the fixture
    only patched ``agent_state``, so every code path that hit
    ``require_registered_agent`` (e.g. all dialectic submit handlers) blocked
    for the full 5.0s ``_metadata_loaded_event.wait(timeout=5.0)`` ceiling
    inside ``ensure_metadata_loaded()``. Three suite tests sat at exactly
    5.01s for that reason.

    Patch the canonical module *and* pre-set the threading event so any
    handler that reaches ``ensure_metadata_loaded`` returns on the fast
    path. Tests that need to exercise the loader explicitly clear these
    in-place (see ``_isolate_identity_state`` teardown).
    """
    try:
        import src.agent_metadata_model as amm
        monkeypatch.setattr(amm, '_metadata_loaded', True, raising=False)
        monkeypatch.setattr(amm, '_metadata_loading', False, raising=False)
        amm._metadata_loaded_event.set()

        # Keep the legacy patch on agent_state for any code that introspects
        # via that re-exported name (defensive — not currently the hot path).
        try:
            import src.agent_state as agent_state
            monkeypatch.setattr(agent_state, '_metadata_loaded', True, raising=False)
        except Exception:
            pass
    except Exception as exc:
        import warnings
        warnings.warn(f"test cleanup failed: {exc}", stacklevel=2)


@pytest.fixture(autouse=True)
def _safe_background_task_spawns(monkeypatch):
    """
    Close fire-and-forget coroutines spawned from sync tests with no event loop.

    Some plugin registration paths opportunistically call background task
    helpers during plain synchronous tests. When that happens there is no
    running event loop, asyncio.create_task() raises RuntimeError, and the
    freshly-created coroutine would otherwise leak a "was never awaited"
    warning before the caller swallows the exception. In async tests or code
    paths with a live loop, preserve the real task creation behavior.
    """
    import src.background_tasks as background_tasks

    original_supervised_create_task = background_tasks._supervised_create_task

    def _safe_create_task(coro, *, name=None):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            if hasattr(coro, "close"):
                coro.close()
            return MagicMock(name=f"closed_task_{name or 'background'}")
        return original_supervised_create_task(coro, name=name)

    monkeypatch.setattr(background_tasks, "_supervised_create_task", _safe_create_task)
    monkeypatch.setattr(background_tasks, "create_tracked_task", _safe_create_task)


@pytest.fixture(autouse=True)
def _isolate_identity_state():
    """
    Reset all in-memory identity and session state between tests.

    Without this, each test that triggers dispatch or identity resolution
    accumulates ghost agent entries in shared module-level dicts. These
    persist across the entire test session because Python module globals
    survive between test functions.

    Clears:
    - _session_identities: session -> agent binding cache
    - _uuid_prefix_index: UUID prefix -> full UUID lookup
    - agent_metadata / monitors: server-level agent registries
    - pattern tracker per-agent state
    - middleware _tool_call_history: rate-limit loop detection
    - contextvars: session_context, mcp_session_id, transport_client_hint,
      session_signals, trajectory_confidence
    """
    yield

    # --- identity_shared module-level caches ---
    try:
        from src.mcp_handlers.identity.shared import (
            _session_identities, _uuid_prefix_index,
        )
        _session_identities.clear()
        _uuid_prefix_index.clear()
    except Exception as exc:
        import warnings
        warnings.warn(f"test cleanup failed: {exc}", stacklevel=2)

    # --- agent_state (canonical) + mcp_server_std (re-exports) agent_metadata & monitors ---
    try:
        if 'src.agent_state' in sys.modules:
            mod = sys.modules['src.agent_state']
            if hasattr(mod, 'agent_metadata'):
                mod.agent_metadata.clear()
            if hasattr(mod, 'monitors'):
                mod.monitors.clear()
            # Reset metadata loading state so ensure_metadata_loaded doesn't carry over
            mod._metadata_loaded = False
            mod._metadata_loading = False
            mod._metadata_loaded_event.clear()
    except Exception as exc:
        import warnings
        warnings.warn(f"test cleanup failed: {exc}", stacklevel=2)
    try:
        if 'src.mcp_server_std' in sys.modules:
            mcp = sys.modules['src.mcp_server_std']
            if hasattr(mcp, 'agent_metadata'):
                mcp.agent_metadata.clear()
            if hasattr(mcp, 'monitors'):
                mcp.monitors.clear()
    except Exception as exc:
        import warnings
        warnings.warn(f"test cleanup failed: {exc}", stacklevel=2)

    # --- pattern tracker per-agent state ---
    try:
        from src.pattern_tracker import get_pattern_tracker
        tracker = get_pattern_tracker()
        if hasattr(tracker, 'pattern_history'):
            tracker.pattern_history.clear()
        if hasattr(tracker, 'investigations'):
            tracker.investigations.clear()
        if hasattr(tracker, 'hypotheses'):
            tracker.hypotheses.clear()
    except Exception as exc:
        import warnings
        warnings.warn(f"test cleanup failed: {exc}", stacklevel=2)

    # --- dialectic session in-memory state ---
    try:
        from src.mcp_handlers.dialectic.session import (
            ACTIVE_SESSIONS, _SESSION_METADATA_CACHE,
        )
        ACTIVE_SESSIONS.clear()
        _SESSION_METADATA_CACHE.clear()
    except Exception as exc:
        import warnings
        warnings.warn(f"test cleanup failed: {exc}", stacklevel=2)

    # --- middleware rate-limit loop history ---
    try:
        from src.mcp_handlers import middleware
        middleware._tool_call_history.clear()
    except Exception as exc:
        import warnings
        warnings.warn(f"test cleanup failed: {exc}", stacklevel=2)

    # --- contextvars (reset to defaults) ---
    try:
        from src.mcp_handlers.context import (
            _session_context,
            _mcp_session_id,
            _transport_client_hint,
            _session_signals,
            _trajectory_confidence,
            _session_resolution_source,
        )
        # Reset each contextvar to its default by setting then immediately
        # using the ContextVar default mechanism
        _session_context.set({})
        _mcp_session_id.set(None)
        _transport_client_hint.set(None)
        _session_signals.set(None)
        _trajectory_confidence.set(None)
        _session_resolution_source.set(None)
    except Exception as exc:
        import warnings
        warnings.warn(f"test cleanup failed: {exc}", stacklevel=2)


@pytest.fixture(autouse=True)
def _isolate_drift_telemetry(tmp_path, monkeypatch):
    """
    Redirect drift telemetry to a temp dir so tests don't pollute
    data/telemetry/drift_telemetry.jsonl (was generating ~17MB/day).
    """
    import src.drift_telemetry as dt_module
    old = dt_module._telemetry
    dt_module._telemetry = dt_module.DriftTelemetry(data_dir=tmp_path)
    yield
    dt_module._telemetry = old


@pytest.fixture(autouse=True, scope="session")
def _cleanup_stale_ghost_files():
    """Remove test agent files left over from previous test runs."""
    from pathlib import Path
    agents_dir = Path(__file__).parent.parent / "data" / "agents"
    if agents_dir.exists():
        for pattern in ["test_*_state.json", ".test_*_state.lock",
                        "mcp_*test*_state.json", ".mcp_*test*_state.lock"]:
            for f in agents_dir.glob(pattern):
                try:
                    f.unlink()
                except Exception:
                    pass
    yield


@pytest.fixture(autouse=True)
def _cleanup_ghost_agent_state_files():
    """
    Remove agent state files created during each test.

    Tests that call dispatch_tool("process_agent_update") or create
    UNITARESMonitor instances with load_state=True auto-save state to
    data/agents/{agent_id}_state.json. Without per-test cleanup, these
    accumulate and can cause cross-test contamination.
    """
    from pathlib import Path
    agents_dir = Path(__file__).parent.parent / "data" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    pre_existing = set(agents_dir.iterdir())

    yield

    # Remove files created during this test
    for f in agents_dir.iterdir():
        if f not in pre_existing:
            try:
                f.unlink()
            except Exception:
                pass


@pytest.fixture
def temp_db(tmp_path):
    """Provide a temporary database path for tests."""
    db_path = tmp_path / "test.db"
    yield db_path
    # Cleanup
    if db_path.exists():
        db_path.unlink()


@pytest_asyncio.fixture
async def live_postgres_backend():
    """
    Provide a real PostgresBackend connected to governance_test.

    Use for integration tests that need live DB. Skips if governance_test
    is unavailable. Schema is bootstrapped and tables truncated per test.
    See tests.test_db_utils for primitives.
    """
    from tests.test_db_utils import (
        TEST_DB_URL,
        can_connect_to_test_db,
        ensure_test_database_schema,
        TRUNCATE_SQL,
        CALIBRATION_RESET_SQL,
    )

    if not can_connect_to_test_db():
        pytest.skip("governance_test database not available")

    await ensure_test_database_schema()

    import os
    os.environ["DB_POSTGRES_URL"] = TEST_DB_URL
    os.environ["DB_POSTGRES_MIN_CONN"] = "1"
    os.environ["DB_POSTGRES_MAX_CONN"] = "3"
    os.environ["DB_AGE_GRAPH"] = "governance_graph"

    from src.db.postgres_backend import PostgresBackend

    be = PostgresBackend()
    await be.init()

    async with be.acquire() as conn:
        await conn.execute(TRUNCATE_SQL)
        await conn.execute(CALIBRATION_RESET_SQL)

    yield be
    await be.close()

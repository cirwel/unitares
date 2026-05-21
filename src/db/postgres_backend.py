"""
PostgreSQL + AGE Backend

Async PostgreSQL backend using asyncpg with Apache AGE for graph queries.
Methods are organized into mixin modules under src/db/mixins/.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

try:
    import asyncpg
except ImportError:
    asyncpg = None  # type: ignore

from .base import DatabaseBackend
from .executor_pool import ExecutorPool
from .mixins import (
    IdentityMixin,
    AgentMixin,
    SessionMixin,
    StateMixin,
    AuditMixin,
    CalibrationMixin,
    GraphMixin,
    ToolUsageMixin,
    KnowledgeGraphMixin,
    BaselineMixin,
    ThreadMixin,
)
from src.logging_utils import get_logger

logger = get_logger(__name__)

# Bounded timeout for the recovery-path failed_pool.close() call. close()
# can hang indefinitely against a wedged executor loop (asyncpg released
# connections but DISCARD ALL responses unread, observed 2026-04-27 →
# "Pool.close() is taking over 60 seconds"). Module-level so tests can
# patch.
POOL_CLOSE_TIMEOUT_SECONDS = 10.0


def _hash_db_url(db_url: str) -> str:
    """Short non-reversible tag for the connection target. Hashes the full
    URL — including any embedded credentials — so the emitted payload never
    leaks the password/user/host that observers could replay."""
    import hashlib
    return hashlib.sha256(db_url.encode("utf-8")).hexdigest()[:12]


def _emit_bootstrap_coord_failure(
    db_url: str,
    error_class: str,
    *,
    timeout_s: float,
) -> None:
    """Wave 0 step 2B (RFC roadmap §86): emit
    `coordination_failure.asyncpg_connect_error.bootstrap` when `_create_pool`
    fails to bring up the asyncpg pool.

    Failure-safe by contract — a raising emit must NOT mask the original
    ConnectionError that the caller is about to raise. `emit_coordination_
    failure_sync` swallows its own errors, but we wrap defensively so a
    surprise ImportError or environment hiccup at this site can never
    swap the user-facing exception."""
    try:
        from uuid import uuid4

        from src.coordination_failure_emit import emit_coordination_failure_sync

        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type="coordination_failure.asyncpg_connect_error.bootstrap",
            payload={
                "error_class": error_class,
                "db_url_hash": _hash_db_url(db_url),
                "timeout_s": timeout_s,
                "attempt": 1,
                "incident_id": str(uuid4()),
            },
            agent_id=None,
        )
    except Exception as exc:  # noqa: BLE001 — observability MUST NOT mask the real bug
        logger.warning(
            "[coord-events] bootstrap emit raised — original ConnectionError "
            "will still propagate: %r",
            exc,
        )


def _emit_runtime_coord_failure(
    *,
    error_class: str,
    pool_size: int,
    pool_max: int,
    pool_idle: int,
    timeout_s: float,
) -> None:
    """Wave 0 steps 2B + 2C-2 (RFC roadmap §86): emit a coordination_failure
    when an established pool fails to deliver a connection. Picks the
    event_type by the saturation discriminator (post-council reshape):

      pool_size == pool_max AND pool_idle == 0
        → `coordination_failure.executor_pool_exhaustion.acquire_timeout`
          (the pool is full and nothing's coming back — true saturation)

      otherwise
        → `coordination_failure.asyncpg_connect_error.runtime`
          (idle capacity exists OR pool can grow — the timeout is
          substrate-side, not saturation; reserves the asyncpg_connect_error
          family for substrate-unreachable semantics per architect F10)

    Same failure-safety contract as the bootstrap helper above."""
    saturated = pool_size == pool_max and pool_idle == 0
    event_type = (
        "coordination_failure.executor_pool_exhaustion.acquire_timeout"
        if saturated
        else "coordination_failure.asyncpg_connect_error.runtime"
    )
    try:
        from uuid import uuid4

        from src.coordination_failure_emit import emit_coordination_failure_sync
        from src.mcp_handlers.context import (
            get_context_agent_id,
            get_context_session_key,
        )

        emit_coordination_failure_sync(
            service="governance_mcp",
            event_type=event_type,
            payload={
                "error_class": error_class,
                "pool_size": pool_size,
                "pool_max": pool_max,
                "pool_idle": pool_idle,
                "timeout_s": timeout_s,
                "incident_id": str(uuid4()),
            },
            agent_id=get_context_agent_id(),
            session_id=get_context_session_key(),
        )
    except Exception as exc:  # noqa: BLE001 — observability MUST NOT mask the real bug
        logger.warning(
            "[coord-events] runtime emit raised — original ConnectionError "
            "will still propagate: %r",
            exc,
        )


class PostgresBackend(
    IdentityMixin,
    AgentMixin,
    SessionMixin,
    StateMixin,
    AuditMixin,
    CalibrationMixin,
    GraphMixin,
    ToolUsageMixin,
    KnowledgeGraphMixin,
    BaselineMixin,
    ThreadMixin,
    DatabaseBackend,
):
    """
    PostgreSQL + AGE backend.

    Requires:
        pip install asyncpg

    Environment:
        DB_POSTGRES_URL=postgresql://user:pass@host:port/dbname
        DB_POSTGRES_MIN_CONN=2
        DB_POSTGRES_MAX_CONN=10
        DB_AGE_GRAPH=governance
    """

    def __init__(self):
        if asyncpg is None:
            raise ImportError("asyncpg is required for PostgreSQL backend. pip install asyncpg")

        self._pool: Optional[asyncpg.Pool] = None
        self._db_url = os.environ.get("DB_POSTGRES_URL", "postgresql://postgres:postgres@localhost:5432/governance")
        # Increased default pool size to handle concurrent requests
        # Can be overridden with DB_POSTGRES_MIN_CONN and DB_POSTGRES_MAX_CONN
        self._min_conn = int(os.environ.get("DB_POSTGRES_MIN_CONN", "5"))
        self._max_conn = int(os.environ.get("DB_POSTGRES_MAX_CONN", "25"))
        self._age_graph = os.environ.get("DB_AGE_GRAPH", "governance_graph")
        self._init_lock = asyncio.Lock()
        self._last_pool_check = time.time()  # Avoid immediate health check on first request

    async def _ensure_pool(self) -> asyncpg.Pool:
        """
        Ensure connection pool is available, recreating if necessary.

        This provides automatic recovery from:
        - Pool becoming None after close()
        - Connection timeouts
        - PostgreSQL restarts
        """
        import asyncio
        import time

        # Fast path: pool exists and is healthy
        if self._pool is not None:
            # Periodic health check (every 60s)
            now = time.time()
            if now - self._last_pool_check > 60:
                try:
                    async with self._pool.acquire(timeout=5) as conn:
                        await conn.fetchval("SELECT 1")
                    self._last_pool_check = now

                    # Check pool size and warn if getting full
                    pool_size = self._pool.get_size()
                    pool_max = self._pool.get_max_size()
                    if pool_size >= pool_max * 0.9:  # 90% full
                        logger.warning(
                            f"Connection pool nearly full: {pool_size}/{pool_max}. "
                            f"Consider increasing DB_POSTGRES_MAX_CONN or checking for connection leaks."
                        )
                except Exception as e:
                    # Health check failed — acquire lock, re-check pool identity,
                    # then log+destroy. The log MUST be inside the lock + after
                    # the identity re-check; otherwise N concurrent failing
                    # health-check tasks all log "destroying pool" before
                    # queueing on the lock, but only one actually destroys.
                    # That fan-in inflates the apparent destroy count by the
                    # concurrency factor and makes the recovery path look
                    # deadlocked when it isn't.
                    failed_pool = self._pool
                    async with self._init_lock:
                        # Only destroy if still the same pool (another task may have already replaced it)
                        if self._pool is not None and self._pool is failed_pool:
                            logger.warning(f"Pool health check failed, destroying pool (backend={id(self)}): {e}")
                            # Null-before-close: drop the reference *before*
                            # awaiting close() so concurrent acquires take
                            # the slow-path create branch instead of blocking
                            # on _init_lock for the close()'s lifetime.
                            # close() can hang indefinitely if the executor
                            # loop is wedged (asyncpg released connections
                            # but DISCARD ALL responses unread, observed
                            # 2026-04-27 → "Pool.close() is taking over 60
                            # seconds"). Without this, _init_lock is held
                            # forever and every subsequent acquire 5s-times
                            # out → cascading wedge.
                            self._pool = None
                            try:
                                await asyncio.wait_for(
                                    failed_pool.close(),
                                    timeout=POOL_CLOSE_TIMEOUT_SECONDS,
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    f"Pool close timed out after 10s "
                                    f"(backend={id(self)}); orphaning pool "
                                    f"and proceeding to recreate. The "
                                    f"executor loop is likely wedged; the "
                                    f"daemon thread will die with the "
                                    f"process."
                                )
                            except Exception as close_err:
                                logger.debug(
                                    f"Pool close raised (non-fatal): "
                                    f"{close_err}"
                                )

        if self._pool is not None:
            return self._pool

        # Slow path: need to create pool
        # Use lock to prevent multiple concurrent pool creations
        async with self._init_lock:
            # Double-check after acquiring lock
            if self._pool is not None:
                return self._pool

            self._pool = await self._create_pool()
            self._last_pool_check = time.time()
            logger.info("PostgreSQL connection pool created")
            return self._pool

    async def _create_pool(self):
        """Create a new connection pool. Caller must hold _init_lock.

        Wraps the asyncpg pool in ExecutorPool so all DB operations route
        through a dedicated background thread+loop, isolating asyncpg from
        the MCP SDK's anyio task group. See src/db/executor_pool.py.
        """
        logger.info("Creating PostgreSQL connection pool...")
        try:
            def _create_factory():
                return asyncio.wait_for(
                    asyncpg.create_pool(
                        self._db_url,
                        min_size=self._min_conn,
                        max_size=self._max_conn,
                        command_timeout=30,
                        max_inactive_connection_lifetime=300,  # Close idle connections after 5 minutes
                        max_queries=50000,  # Recycle connections after 50k queries
                    ),
                    timeout=5.0  # Fail fast if PostgreSQL isn't available
                )
            return await ExecutorPool.create(_create_factory)
        except asyncio.TimeoutError:
            _emit_bootstrap_coord_failure(self._db_url, "TimeoutError", timeout_s=5.0)
            raise ConnectionError(
                f"PostgreSQL connection timeout after 5s. "
                f"Is PostgreSQL running on {self._db_url}? "
                f"Check: psql -d {self._db_url.split('/')[-1]}"
            )
        except Exception as e:
            _emit_bootstrap_coord_failure(self._db_url, type(e).__name__, timeout_s=5.0)
            raise ConnectionError(
                f"Failed to connect to PostgreSQL at {self._db_url}: {e}. "
                f"Is PostgreSQL running?"
            ) from e

    def acquire(self, timeout: float = None):
        """
        Get a connection from the pool with automatic recovery.

        Usage:
            async with self.acquire() as conn:
                await conn.fetchval("SELECT 1")

        This wraps pool.acquire() and ensures the pool exists.
        """
        class _AcquireContext:
            def __init__(ctx_self, backend, timeout):
                ctx_self.backend = backend
                ctx_self.timeout = timeout
                ctx_self.conn = None
                ctx_self.acquired_pool = None  # Track which pool we acquired from

            async def __aenter__(ctx_self):
                pool = await ctx_self.backend._ensure_pool()
                ctx_self.acquired_pool = pool  # Store reference to THIS pool
                acquire_timeout = ctx_self.timeout or 10.0
                try:
                    # Use timeout to prevent hanging (default 10s)
                    ctx_self.conn = await pool.acquire(timeout=acquire_timeout)
                    return ctx_self.conn
                except asyncio.TimeoutError:
                    logger.error(f"Connection pool timeout after {acquire_timeout}s. Pool size: {pool.get_size()}, free: {pool.get_idle_size()}")
                    _emit_runtime_coord_failure(
                        error_class="TimeoutError",
                        pool_size=pool.get_size(),
                        pool_max=pool.get_max_size(),
                        pool_idle=pool.get_idle_size(),
                        timeout_s=acquire_timeout,
                    )
                    raise ConnectionError(
                        f"PostgreSQL connection pool exhausted. "
                        f"Current pool: {pool.get_size()}/{pool.get_max_size()}. "
                        f"Try increasing DB_POSTGRES_MAX_CONN or check for connection leaks."
                    )
                except BaseException:
                    # Cancellation (anyio task-group teardown) or any other
                    # exception after asyncpg has internally registered a
                    # connection but before we return it would leak that
                    # connection — the pool's checked-out set keeps a
                    # reference, PG sees it idle, the app sees free=0. Watcher
                    # fingerprint #3df34c78 flags this exact line range. Match
                    # the pattern used in _TransactionContext.__aenter__.
                    if ctx_self.conn is not None:
                        try:
                            await pool.release(ctx_self.conn)
                        except Exception:
                            pass
                        ctx_self.conn = None
                    ctx_self.acquired_pool = None
                    raise

            async def __aexit__(ctx_self, exc_type, exc_val, exc_tb):
                if ctx_self.conn and ctx_self.acquired_pool:
                    # Only release to the SAME pool we acquired from
                    # If pool was recreated, current_pool will differ from acquired_pool
                    current_pool = ctx_self.backend._pool
                    if current_pool is ctx_self.acquired_pool:
                        try:
                            await ctx_self.acquired_pool.release(ctx_self.conn)
                        except Exception as e:
                            logger.warning(f"Error releasing connection: {e}")
                    else:
                        # Pool was recreated - connection is orphaned, just close it
                        logger.debug("Pool was recreated during operation, closing orphan connection")
                        try:
                            await ctx_self.conn.close()
                        except Exception:
                            pass  # Connection may already be closed
                ctx_self.conn = None
                ctx_self.acquired_pool = None
                return False

        return _AcquireContext(self, timeout)

    def transaction(self, timeout: float = None):
        """
        Get a connection from the pool wrapped in an explicit transaction.

        Usage:
            async with self.transaction() as conn:
                await conn.execute("INSERT ...")
                await conn.execute("UPDATE ...")
                # auto-commits on exit, auto-rollbacks on exception

        This provides atomicity for multi-statement operations. The
        connection is acquired via acquire() (preserving pool orphan
        protection) and wrapped in asyncpg's conn.transaction().
        """
        class _TransactionContext:
            def __init__(ctx_self, backend, timeout):
                ctx_self.backend = backend
                ctx_self.timeout = timeout
                ctx_self._acquire_ctx = None
                ctx_self._txn = None
                ctx_self.conn = None

            async def __aenter__(ctx_self):
                ctx_self._acquire_ctx = ctx_self.backend.acquire(timeout=ctx_self.timeout)
                ctx_self.conn = await ctx_self._acquire_ctx.__aenter__()
                # Connection acquired. If anything below raises before we
                # return, __aexit__ will NOT run (async-CM protocol), so we
                # must release the inner acquire ourselves. The realistic
                # trigger is CancelledError when the executor wedges
                # mid-txn-start (same wedge class this branch is fixing) —
                # CancelledError is a BaseException in 3.8+, so catch
                # broadly.
                try:
                    ctx_self._txn = ctx_self.conn.transaction()
                    await ctx_self._txn.start()
                except BaseException:
                    inner = ctx_self._acquire_ctx
                    ctx_self._acquire_ctx = None
                    ctx_self.conn = None
                    ctx_self._txn = None
                    try:
                        await inner.__aexit__(None, None, None)
                    except Exception as release_err:
                        logger.warning(
                            f"Connection release after failed txn-start "
                            f"raised (non-fatal): {release_err}"
                        )
                    raise
                return ctx_self.conn

            async def __aexit__(ctx_self, exc_type, exc_val, exc_tb):
                try:
                    if exc_type is not None:
                        await ctx_self._txn.rollback()
                    else:
                        await ctx_self._txn.commit()
                except Exception as e:
                    logger.warning(f"Transaction cleanup error: {e}")
                finally:
                    # Release connection back to pool
                    await ctx_self._acquire_ctx.__aexit__(exc_type, exc_val, exc_tb)
                return False

        return _TransactionContext(self, timeout)

    async def init(self) -> None:
        """Initialize connection pool and verify schema."""
        already_existed = self._pool is not None
        # Delegate pool creation to _ensure_pool (handles locking, dedup)
        await self._ensure_pool()

        # Skip schema verification if pool already existed (already verified)
        if already_existed:
            return

        # Verify schema exists
        async with self.acquire() as conn:
            # Check core schema
            result = await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = 'core')"
            )
            if not result:
                raise RuntimeError(
                    "PostgreSQL schema not initialized. Run db/postgres/schema.sql first."
                )

            # Check AGE extension
            try:
                await conn.execute("LOAD 'age'")
                await conn.execute(f"SET search_path = ag_catalog, core, audit, public")
            except Exception:
                # AGE not available, graph queries will be disabled
                pass

    async def close(self) -> None:
        """Close connection pool.

        Same null-before-close discipline as the recovery path: drop the
        reference *before* awaiting close() so a wedged executor loop
        doesn't strand any concurrent caller observing self._pool. The
        ExecutorPool.close() is internally idempotent + bounded by
        CLOSE_TIMEOUT_SECONDS, so this caller doesn't need its own
        wait_for — but null-before-close still matters for callers that
        check self._pool directly (test code, shutdown coordinators).
        """
        if self._pool:
            failed_pool = self._pool
            self._pool = None
            try:
                await failed_pool.close()
            except Exception as e:
                logger.debug(f"Pool close raised during shutdown (non-fatal): {e}")

    async def health_check(self) -> Dict[str, Any]:
        """Return health/status information."""
        if not self._pool:
            return {"status": "error", "error": "Pool not initialized"}

        async with self.acquire() as conn:
            # Single query for schema version and counts (also proves connectivity)
            row = await conn.fetchrow("""
                SELECT
                    (SELECT MAX(version) FROM core.schema_migrations) AS schema_version,
                    (SELECT COUNT(*) FROM core.identities) AS identity_count,
                    (SELECT COUNT(*) FROM core.sessions WHERE is_active = TRUE) AS active_session_count
            """)
            version = row["schema_version"]
            identity_count = row["identity_count"]
            session_count = row["active_session_count"]

            # AGE status: require the configured graph to be usable, not just the extension to load.
            try:
                age_available = await self.graph_available()
            except Exception:
                age_available = False

            return {
                "status": "healthy",
                "backend": "postgres",
                "db_url": self._db_url.split("@")[-1] if "@" in self._db_url else "***",  # Hide credentials
                "pool_size": self._pool.get_size(),
                "pool_idle": self._pool.get_idle_size(),
                "pool_free": self._pool.get_idle_size(),  # Alias for compatibility
                "pool_max": self._pool.get_max_size(),
                "schema_version": version,
                "identity_count": identity_count,
                "active_session_count": session_count,
                "age_available": age_available,
                "age_graph": self._age_graph if age_available else None,
            }

"""Backend-independent write budget for the knowledge graph.

The KG's only structural limit on propagation is a per-agent store budget
(default 20/hour). Until this module existed, that budget lived inside the AGE
storage driver and was enforced from exactly one call site
(``KnowledgeGraphAGE.add_discovery``). The PostgreSQL FTS driver — which
``src/knowledge_graph.py`` documents as the canonical/default backend — had no
budget at all, so selecting it (explicitly, or implicitly via
``UNITARES_KNOWLEDGE_BACKEND=auto`` + ``DB_BACKEND=postgres``) silently removed
the anti-poisoning limit. No error, no log line, no failing test.

A propagation limit that a config flip can remove is not an invariant, it is a
deployment coincidence. This module makes the budget a property of the write
contract rather than of whichever driver happens to be mounted: every backend
calls the same check, so adding a backend cannot silently drop it.

Scope, stated honestly:

- The budget covers **new discoveries only**. ``update_discovery`` is not
  budgeted, so mutating an entry that already carries standing and inbound
  ``related_to`` links remains free. That exemption is load-bearing for the
  hourly ``memory/scripts/sync_kg.py`` backlog drain, so closing it is a
  behavior change to a live automation, not a cleanup — left deliberately open.
- This is a budget, not a detector. It counts writes; it never inspects content
  or judges quality.
"""

from typing import Any, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

DEFAULT_STORES_PER_HOUR = 20
WINDOW_SECONDS = 3600


class WriteBudgetExceeded(ValueError):
    """Raised when an agent exceeds its per-hour store budget.

    Subclasses ``ValueError`` because that is what the AGE driver raised before
    the check was lifted here; existing ``except ValueError`` handlers upstream
    keep working unchanged.
    """


def _exceeded_message(agent_id: str, count: Any, limit: int) -> str:
    return (
        f"Rate limit exceeded: Agent '{agent_id}' has stored {count} "
        f"discoveries in the last hour (limit: {limit}/hour). "
        f"This prevents knowledge graph poisoning flood attacks. "
        f"Please wait before storing more discoveries."
    )


async def check_store_budget(
    agent_id: str,
    *,
    db: Any,
    limit: int = DEFAULT_STORES_PER_HOUR,
    conn: Optional[Any] = None,
) -> None:
    """Consume one unit of ``agent_id``'s hourly store budget.

    Raises ``WriteBudgetExceeded`` if the budget is spent. On success the write
    is recorded, so callers must treat this as a mutating operation.

    Args:
        agent_id: Agent whose budget is being charged.
        db: Database backend, used for the PostgreSQL fallback path.
        limit: Stores permitted per hour.
        conn: Optional connection to reuse, so the caller can run the check
            inside its own transaction and have a rollback un-count the write.
            Backends without a transaction around the write pass ``None`` and
            accept that a failure after this point leaks one budget unit.
    """
    # Fast path: Redis.
    try:
        from src.cache import get_rate_limiter

        limiter = get_rate_limiter()

        if not await limiter.check(
            agent_id,
            limit=limit,
            window=WINDOW_SECONDS,
            operation="kg_store",
        ):
            count = await limiter.get_count(agent_id, WINDOW_SECONDS, operation="kg_store")
            raise WriteBudgetExceeded(_exceeded_message(agent_id, count, limit))

        await limiter.record(agent_id, WINDOW_SECONDS, operation="kg_store")
        return
    except WriteBudgetExceeded:
        raise
    except Exception as exc:
        logger.debug(f"Redis rate limiting failed, falling back to PostgreSQL: {exc}")

    # Fallback: PostgreSQL, atomic check-and-insert to avoid a race between the
    # count and the insert.
    async def _do_rate_limit_check(c):
        from datetime import datetime, timedelta

        one_hour_ago = datetime.now() - timedelta(hours=1)

        inserted = await c.fetchval(
            """
            INSERT INTO audit.rate_limits (agent_id, timestamp)
            SELECT $1, $2
            WHERE (
                SELECT COUNT(*) FROM audit.rate_limits
                WHERE agent_id = $1 AND timestamp > $3
            ) < $4
            RETURNING agent_id
            """,
            agent_id,
            datetime.now(),
            one_hour_ago,
            limit,
        )

        if inserted is None:
            count = await c.fetchval(
                "SELECT COUNT(*) FROM audit.rate_limits WHERE agent_id = $1 AND timestamp > $2",
                agent_id,
                one_hour_ago,
            )
            raise WriteBudgetExceeded(_exceeded_message(agent_id, count or 0, limit))

        await c.execute(
            "DELETE FROM audit.rate_limits WHERE timestamp < $1",
            one_hour_ago,
        )

    if conn is not None:
        await _do_rate_limit_check(conn)
    else:
        async with db.acquire() as pooled_conn:
            await _do_rate_limit_check(pooled_conn)

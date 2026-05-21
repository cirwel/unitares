"""S8a Phase-2 promotion sweep: ephemeral → engaged_ephemeral.

Phase-1 (PR #121) stamps ``["ephemeral"]`` on freshly-minted identities so
the class-conditional calibration partition is populated from creation.
Phase-2 promotes ephemeral identities that show repeated activity into a
distinct ``engaged_ephemeral`` cohort so calibration can specialize against
agents that crossed the "did onboard + ≥2 substantive updates" threshold.

Naming + axis note (technical debt — see KG follow-up): ``engaged_ephemeral``
is a *behavior cohort* encoded in the same single-tag namespace that holds
*identity-class* tags (``ephemeral``, ``embodied``, ``persistent``). They
are not orthogonal axes today; a future schema change introduces a
separate ``behavior_cohort`` field, at which point this tag retires.
Until then the resolution order in ``classify_agent`` checks
``engaged_ephemeral`` before ``ephemeral`` so the more specific cohort wins.

Threshold rationale (see ````):
  - Day-7 distribution within the ephemeral cohort is roughly flat across
    1–25 update buckets (12% / 12% / 8% / 12%); the only honest break is
    "0 vs engaged."
  - ``≥1`` is too aggressive — single-shot probes (``cursor_binding_fix_probe``)
    promote on one stray check-in.
  - ``≥3`` means "agent did onboard + ≥2 substantive updates." Captures
    repeated work without trapping accidental probes. Promotes 44 of 126
    in-window ephemeral identities (35%) at ratification.

Idempotency: once promoted, the ``ephemeral`` tag is removed from the array,
so the WHERE clause never matches the same row twice. ``total_updates`` is a
monotone counter, so promotion is also monotone — there is no demotion path.

Concurrency: the sweep runs as a single CTE-scoped statement that uses
``FOR UPDATE SKIP LOCKED`` on the candidate set, then re-checks the
``ephemeral`` tag on the UPDATE side. This guards against:
  - Two MCP processes running the sweep simultaneously (skip locked rows).
  - A handler-side write (``set_agent_label``, ``update_agent_metadata``)
    racing with the sweep's SELECT — the row lock + re-check ensures we
    only update rows that are still ephemeral at the moment of the write.
PG's READ COMMITTED isolation ensures that any concurrent
``increment_update_count`` UPDATE on the same row already serializes
correctly with the sweep's UPDATE; the re-check is for the gap between
SELECT and UPDATE, which is closed by combining them in one statement.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PROMOTION_THRESHOLD = 3
PROMOTION_SOURCE_TAG = "ephemeral"
PROMOTION_TARGET_TAG = "engaged_ephemeral"
PROMOTION_SOURCE_JSONB = '["ephemeral"]'
PROMOTION_TARGET_JSONB = '["engaged_ephemeral"]'


async def promote_engaged_ephemeral(
    threshold: int = DEFAULT_PROMOTION_THRESHOLD,
    *,
    db=None,
    include_archived: bool = False,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """Promote ``ephemeral`` identities with ``total_updates >= threshold`` to ``engaged_ephemeral``.

    Args:
      threshold: Minimum ``total_updates`` for promotion. Default 3 per
        the Phase-2 audit; CLI/operator may override for backfill experiments.
      db: Optional DB backend with an ``acquire()`` async context manager
        yielding a connection. Defaults to ``src.db.get_db()``. Tests pass
        a live ``PostgresBackend`` here to bypass the ``_isolate_db_backend``
        autouse mock.
      include_archived: If True, also sweep ``status='archived'`` rows.
        Used by the decision (d) backfill (~3180 archived rows). Default
        False — live sweep only touches active rows.
      dry_run: If True, return the list of agents that *would* promote
        without writing. Useful before a backfill run.
      limit: Optional cap on number of promotions per call. Useful when
        the backfill set is large and operators want to chunk.

    Returns:
      Dict with keys:
        - ``promoted``: int — number of rows updated (0 in dry_run)
        - ``would_promote``: int — number that would update (only in dry_run)
        - ``threshold``: int — the threshold used
        - ``sample``: list[dict] — up to 10 (agent_id, label, total_updates) tuples
        - ``include_archived``: bool — echoed for audit
        - ``dry_run``: bool — echoed for audit
    """
    if db is None:
        from src.db import get_db
        db = get_db()

    status_clause = "" if include_archived else "AND status = 'active'"
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""

    # NULLIF guards against an empty-string ``total_updates`` (which would
    # crash ``::int``). The cast still raises on a non-numeric value, but
    # ``total_updates`` is initialized at agent_storage.create_agent and
    # only ever bumped by a counter — non-numeric there indicates corruption
    # we want to surface, not silently exclude.
    total_updates_expr = "COALESCE(NULLIF(metadata->>'total_updates', '')::int, 0)"

    if dry_run:
        select_sql = f"""
            SELECT agent_id,
                   metadata->>'label' AS label,
                   {total_updates_expr} AS total_updates
            FROM core.identities
            WHERE metadata->'tags' @> $2::jsonb
              AND {total_updates_expr} >= $1
              {status_clause}
            ORDER BY {total_updates_expr} DESC
            {limit_clause}
        """
        async with db.acquire() as conn:
            candidates = await conn.fetch(
                select_sql, threshold, PROMOTION_SOURCE_JSONB
            )
        return {
            "promoted": 0,
            "would_promote": len(candidates),
            "threshold": threshold,
            "sample": [
                {
                    "agent_id": r["agent_id"],
                    "label": r["label"],
                    "total_updates": r["total_updates"],
                }
                for r in candidates[:10]
            ],
            "include_archived": include_archived,
            "dry_run": True,
        }

    # Wet path: single CTE so the SELECT, FOR UPDATE, UPDATE, and RETURN
    # all happen in one statement. ``FOR UPDATE SKIP LOCKED`` protects
    # against double-sweeper races; the re-check on the UPDATE side
    # (``i.metadata->'tags' @> ...``) protects against the (small)
    # window between candidate selection and update where another
    # transaction could drop the ephemeral tag.
    sweep_sql = f"""
        WITH candidates AS (
            SELECT agent_id,
                   metadata->>'label' AS label,
                   {total_updates_expr} AS total_updates
            FROM core.identities
            WHERE metadata->'tags' @> $2::jsonb
              AND {total_updates_expr} >= $1
              {status_clause}
            ORDER BY {total_updates_expr} DESC
            {limit_clause}
            FOR UPDATE SKIP LOCKED
        ),
        promoted AS (
            UPDATE core.identities i
            SET metadata = jsonb_set(
                    i.metadata,
                    ARRAY['tags'],
                    COALESCE((i.metadata->'tags') - $3::text, '[]'::jsonb) || $4::jsonb
                ),
                updated_at = NOW()
            FROM candidates c
            WHERE i.agent_id = c.agent_id
              AND i.metadata->'tags' @> $2::jsonb
            RETURNING i.agent_id
        )
        SELECT
            (SELECT COUNT(*) FROM promoted) AS promoted_count,
            COALESCE((SELECT array_agg(agent_id) FROM promoted), ARRAY[]::text[]) AS promoted_ids,
            COALESCE((
                SELECT json_agg(row_to_json(c))
                FROM (
                    SELECT agent_id, label, total_updates
                    FROM candidates
                    ORDER BY total_updates DESC
                    LIMIT 10
                ) c
            ), '[]'::json) AS sample
    """

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            sweep_sql,
            threshold,
            PROMOTION_SOURCE_JSONB,
            PROMOTION_SOURCE_TAG,
            PROMOTION_TARGET_JSONB,
        )

    if row is None:
        return {
            "promoted": 0,
            "threshold": threshold,
            "sample": [],
            "include_archived": include_archived,
            "dry_run": False,
        }

    promoted_count = int(row["promoted_count"] or 0)
    promoted_ids = list(row["promoted_ids"] or [])
    sample_raw = row["sample"]
    if isinstance(sample_raw, str):
        sample = json.loads(sample_raw)
    else:
        sample = list(sample_raw or [])

    # Sync in-memory metadata cache so already-loaded agents see the
    # new tag on their next request without waiting for a metadata
    # reload. Mirrors the dual-write pattern in
    # ``stamp_default_class_tags`` and ``set_agent_label``. The lazy
    # import keeps this module testable without bringing the full
    # MCP server surface into the test harness.
    if promoted_ids:
        try:
            from src.mcp_handlers.shared import lazy_mcp_server as _server
            for aid in promoted_ids:
                meta = _server.agent_metadata.get(aid)
                if meta is None:
                    continue
                current = list(getattr(meta, "tags", None) or [])
                new_tags = [t for t in current if t != PROMOTION_SOURCE_TAG]
                if PROMOTION_TARGET_TAG not in new_tags:
                    new_tags.append(PROMOTION_TARGET_TAG)
                meta.tags = new_tags
        except Exception as cache_err:
            # In-memory sync failure is recoverable: the next metadata
            # reload from PG will pick up the canonical tag set.
            logger.debug(
                "[CLASS_PROMOTION] in-memory cache sync skipped: %s", cache_err
            )

    return {
        "promoted": promoted_count,
        "threshold": threshold,
        "sample": sample,
        "include_archived": include_archived,
        "dry_run": False,
    }


# Phase-1 stamp-gap backfill: stamps default class tags on identities
# created before the Phase-2 wiring landed (or via a code path that still
# misses the stamp). Without this, those identities can never become
# promotion candidates because they're not tagged ``ephemeral``.

async def stamp_untagged_identities(
    *,
    db=None,
    include_archived: bool = False,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """Stamp default class tags on identities that have empty/missing tags.

    Uses ``default_tags_for_onboard(label)`` so a known resident label
    (Sentinel, Vigil, etc.) gets resident tags and everything else gets
    ``ephemeral`` — same rule the live stamp helper applies at creation.

    Args:
      db: Optional DB backend; defaults to ``get_db()``.
      include_archived: If True, also stamp ``status='archived'`` rows.
      dry_run: If True, return what would be stamped without writing.
      limit: Optional cap on number of stamps per call.

    Returns:
      Dict with ``stamped`` (or ``would_stamp``), ``sample``, ``dry_run``,
      ``include_archived``.
    """
    if db is None:
        from src.db import get_db
        db = get_db()

    from src.grounding.onboard_classifier import default_tags_for_onboard

    status_clause = "" if include_archived else "AND status = 'active'"
    limit_clause = f"LIMIT {int(limit)}" if limit and limit > 0 else ""

    select_sql = f"""
        SELECT agent_id, metadata->>'label' AS label
        FROM core.identities
        WHERE (metadata->'tags' IS NULL
               OR jsonb_array_length(COALESCE(metadata->'tags', '[]'::jsonb)) = 0)
          {status_clause}
        ORDER BY created_at DESC
        {limit_clause}
    """

    async with db.acquire() as conn:
        rows = await conn.fetch(select_sql)

        sample = []
        stamped_count = 0
        # Group by tag list so we can issue one UPDATE per (tag-list,
        # agent_id-array) combo. In practice almost everything ends up
        # ephemeral.
        plans: dict[tuple, list[str]] = {}
        for r in rows:
            tags = default_tags_for_onboard(r["label"], existing_tags=None)
            if tags is None:
                continue
            plans.setdefault(tuple(tags), []).append(r["agent_id"])
            if len(sample) < 10:
                sample.append(
                    {"agent_id": r["agent_id"], "label": r["label"], "tags": tags}
                )

        if dry_run:
            return {
                "stamped": 0,
                "would_stamp": sum(len(ids) for ids in plans.values()),
                "sample": sample,
                "include_archived": include_archived,
                "dry_run": True,
            }

        update_sql = """
            UPDATE core.identities
            SET metadata = jsonb_set(metadata, ARRAY['tags'], $2::jsonb),
                updated_at = NOW()
            WHERE agent_id = ANY($1::text[])
              AND (metadata->'tags' IS NULL
                   OR jsonb_array_length(COALESCE(metadata->'tags', '[]'::jsonb)) = 0)
        """
        for tag_tuple, agent_ids in plans.items():
            tags_jsonb = json.dumps(list(tag_tuple))
            await conn.execute(update_sql, agent_ids, tags_jsonb)
            stamped_count += len(agent_ids)

    return {
        "stamped": stamped_count,
        "sample": sample,
        "include_archived": include_archived,
        "dry_run": False,
    }

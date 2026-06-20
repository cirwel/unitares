"""
Knowledge Graph Data Lifecycle Management

PHILOSOPHY (2025-12-26):
Never delete memories. Archive forever. Forced amnesia is not governance.

LIFECYCLE TIERS:
- Tier 1: Permanent (never auto-archive)
    - type: architecture_decision, learning, pattern, root_cause_analysis
    - tags: ["permanent", "foundational"]

- Tier 2: Resolved → Archived (30 days after resolved)
    - Default for resolved items
    - Work items, bugs, tasks

- Tier 3: Conditional (archive when superseded)
    - Explicit supersession via superseded_by field
    - Old documentation when new version exists

- Ephemeral: Only if explicitly tagged
    - tags: ["ephemeral", "temp", "scratch"]
    - Archived after 7 days

Storage tiers:
- open/resolved: Hot (active queries)
- archived: Warm (recent history)
- cold: Cold (long-term memory, queryable with include_cold=true)
"""

import asyncio
from datetime import datetime, timedelta
import logging
from typing import List, Dict, Any, Optional, Set

logger = logging.getLogger(__name__)

# Best-effort in-process lifecycle health tracking for operator diagnostics.
_KG_LIFECYCLE_STATUS: Dict[str, Any] = {
    "status": "unknown",
    "last_run": None,
    "last_error": None,
}


def _record_kg_lifecycle_status(*, status: str, last_error: Optional[str] = None) -> None:
    """Record the most recent KG lifecycle outcome for health reporting."""
    _KG_LIFECYCLE_STATUS["status"] = status
    _KG_LIFECYCLE_STATUS["last_run"] = datetime.now().isoformat()
    _KG_LIFECYCLE_STATUS["last_error"] = last_error


def get_kg_lifecycle_health() -> Dict[str, Any]:
    """Return the latest KG lifecycle status for operator-facing health checks."""
    return dict(_KG_LIFECYCLE_STATUS)


# Lifecycle policy definitions
PERMANENT_TYPES: Set[str] = {
    "architecture_decision",
    "learning",
    "pattern",
    "root_cause_analysis",
    "migration",
}

PERMANENT_TAGS: Set[str] = {
    "permanent",
    "foundational",
    "architecture",
    "decision",
}

EPHEMERAL_TAGS: Set[str] = {
    "ephemeral",
    "temp",
    "scratch",
    "test",
    "demo",
}


class KnowledgeGraphLifecycle:
    """Manages knowledge graph data lifecycle - NEVER DELETES"""

    def __init__(self, graph=None):
        """
        Initialize lifecycle manager.

        Args:
            graph: Knowledge graph backend instance (optional, lazy-loaded)
        """
        self._graph = graph

        # Lifecycle thresholds (days)
        self.RESOLVED_TO_ARCHIVED_DAYS = 30   # Archive resolved after 30 days
        self.ARCHIVED_TO_COLD_DAYS = 90       # Move to cold after 90 days total
        self.EPHEMERAL_ARCHIVE_DAYS = 7       # Archive ephemeral after 7 days
        # NO DELETION - memories persist forever

    async def _get_graph(self):
        """Get knowledge graph instance (lazy initialization)."""
        if self._graph is None:
            from src.knowledge_graph import get_knowledge_graph
            self._graph = await get_knowledge_graph()
        return self._graph

    def get_lifecycle_policy(self, discovery) -> str:
        """
        Determine lifecycle policy for a discovery.

        Returns:
            "permanent" - Never auto-archive
            "standard" - Resolved → Archived after 30 days
            "ephemeral" - Archive after 7 days
        """
        # Check for permanent types
        if discovery.type in PERMANENT_TYPES:
            return "permanent"

        # Check for permanent tags
        discovery_tags = set(discovery.tags or [])
        if discovery_tags & PERMANENT_TAGS:
            return "permanent"

        # Check for ephemeral tags
        if discovery_tags & EPHEMERAL_TAGS:
            return "ephemeral"

        # Default to standard lifecycle
        return "standard"

    async def run_cleanup(self, dry_run: bool = False) -> Dict[str, Any]:
        """
        Run full lifecycle cleanup cycle.

        Returns summary of what was archived/moved to cold.
        Set dry_run=True to see what would happen without making changes.

        NOTE: This NEVER deletes. It only moves between tiers.
        """
        now = datetime.now()
        summary = {
            "timestamp": now.isoformat(),
            "dry_run": dry_run,
            "discoveries_archived": 0,
            "discoveries_to_cold": 0,
            "ephemeral_archived": 0,
            "tags_canonicalized": 0,
            "skipped_permanent": 0,
            "discoveries_deleted": 0,  # Always 0 - we don't delete
            "philosophy": "Never delete. Archive forever.",
            "errors": []
        }

        try:
            graph = await self._get_graph()

            # Step 0: Canonicalize tags via the curated semantic synonym map.
            # Formatting fragmentation is fixed at write time by normalize_tags;
            # this catches the semantic residue (db→database, auth→identity) on
            # the active corpus, where the rewrite is visible and auditable.
            canonicalized = await self._canonicalize_tags(now, dry_run)
            summary["tags_canonicalized"] = len(canonicalized)

            # Step 1: Archive ephemeral discoveries (fastest deprecation)
            ephemeral = await self._archive_ephemeral(now, dry_run)
            summary["ephemeral_archived"] = len(ephemeral)

            # Step 2: Auto-archive old resolved discoveries (respecting permanent policy)
            archived, skipped = await self._archive_old_resolved(now, dry_run)
            summary["discoveries_archived"] = len(archived)
            summary["skipped_permanent"] = skipped

            # Step 3: Move very old archived to cold storage
            cold = await self._move_to_cold(now, dry_run)
            summary["discoveries_to_cold"] = len(cold)

            # Step 4: NO DELETION - memories persist forever
            summary["discoveries_deleted"] = 0

        except Exception as e:
            summary["errors"].append(str(e))
            logger.error(f"Cleanup error: {e}")

        return summary

    async def _batch_update_status(
        self, graph, discovery_ids: List[str], new_status: str, now: datetime
    ):
        """Update status through the active KG backend and canonical PG table."""
        updated_at = now.isoformat()

        # Update the selected KG backend.
        for discovery_id in discovery_ids:
            await graph.update_discovery(discovery_id, {
                "status": new_status,
                "updated_at": updated_at,
            })

        # Keep the canonical PG table aligned when an alternate backend is active.
        try:
            from src.db.postgres_backend import get_postgres_backend
            db = await get_postgres_backend()
            async with db.acquire() as conn:
                await conn.execute(
                    """
                    UPDATE knowledge.discoveries
                    SET status = $1, updated_at = now()
                    WHERE id = ANY($2::text[])
                    """,
                    new_status,
                    discovery_ids,
                )
        except Exception as e:
            logger.debug(f"PG sync skipped for lifecycle update: {e}")

    async def _canonicalize_tags(self, now: datetime, dry_run: bool) -> List[str]:
        """Apply the curated semantic synonym map to the active corpus.

        Scans open + resolved discoveries and rewrites tags whose canonical
        form (``normalize_tags`` then ``apply_semantic_synonyms``) differs from
        what is stored. This is the lifecycle-only semantic layer — write-path
        ``normalize_tags`` deliberately does not merge synonyms. Never deletes;
        only rewrites the ``tags`` list in place.

        Returns the list of discovery IDs whose tags changed.
        """
        from src.knowledge_graph import normalize_tags
        from src.knowledge_ontology import apply_semantic_synonyms

        graph = await self._get_graph()
        changed: List[str] = []

        # Active corpus only — archived/cold rows are not re-queried.
        candidates = []
        for status in ("open", "resolved"):
            candidates.extend(await graph.query(status=status, limit=1000))

        for discovery in candidates:
            current = list(discovery.tags or [])
            if not current:
                continue
            canonical = apply_semantic_synonyms(normalize_tags(current))
            if canonical == current:
                continue
            changed.append(discovery.id)
            if not dry_run:
                await graph.update_discovery(discovery.id, {
                    "tags": canonical,
                    "updated_at": now.isoformat(),
                })

        logger.info(
            "%s tags on %d discoveries via semantic synonym map",
            "[DRY RUN] Would canonicalize" if dry_run else "Canonicalized",
            len(changed),
        )
        return changed

    async def _archive_ephemeral(self, now: datetime, dry_run: bool) -> List[str]:
        """Archive ephemeral discoveries older than threshold."""
        graph = await self._get_graph()
        cutoff = now - timedelta(days=self.EPHEMERAL_ARCHIVE_DAYS)
        cutoff_iso = cutoff.isoformat()

        # Query open discoveries
        open_discoveries = await graph.query(status="open", limit=1000)

        to_archive = []
        for discovery in open_discoveries:
            # Check if ephemeral
            policy = self.get_lifecycle_policy(discovery)
            if policy != "ephemeral":
                continue

            # Check age
            if discovery.timestamp and discovery.timestamp < cutoff_iso:
                to_archive.append(discovery.id)

        if not dry_run and to_archive:
            await self._batch_update_status(graph, to_archive, "archived", now)

        logger.info(f"{'[DRY RUN] Would archive' if dry_run else 'Archived'} {len(to_archive)} ephemeral discoveries")
        return to_archive

    async def _archive_old_resolved(self, now: datetime, dry_run: bool) -> tuple[List[str], int]:
        """Archive resolved discoveries older than threshold, respecting permanent policy."""
        graph = await self._get_graph()
        cutoff = now - timedelta(days=self.RESOLVED_TO_ARCHIVED_DAYS)
        cutoff_iso = cutoff.isoformat()

        # Query resolved discoveries
        resolved = await graph.query(status="resolved", limit=1000)

        to_archive = []
        skipped = 0

        for discovery in resolved:
            # Check lifecycle policy
            policy = self.get_lifecycle_policy(discovery)
            if policy == "permanent":
                skipped += 1
                continue

            # Check if resolved_at is old enough
            if discovery.resolved_at and discovery.resolved_at < cutoff_iso:
                to_archive.append(discovery.id)

        if not dry_run and to_archive:
            await self._batch_update_status(graph, to_archive, "archived", now)

        logger.info(f"{'[DRY RUN] Would archive' if dry_run else 'Archived'} {len(to_archive)} old resolved discoveries (skipped {skipped} permanent)")
        return to_archive, skipped

    async def _move_to_cold(self, now: datetime, dry_run: bool) -> List[str]:
        """Move very old archived discoveries to cold storage tier.

        Respects permanent policy for symmetry with ``_archive_old_resolved``:
        the stated philosophy is "permanent → never auto-archive", and moving a
        row to cold takes it out of default search scope (the deeper archival
        step), so a permanent entry that ever lands in ``archived`` — e.g. a
        manual archive, a re-type after archival, or a row stored before the
        resolved→archived permanent-skip existed — must not be buried by the
        cold sweep.
        """
        graph = await self._get_graph()
        cutoff = now - timedelta(days=self.ARCHIVED_TO_COLD_DAYS)
        cutoff_iso = cutoff.isoformat()

        # Query archived discoveries
        archived = await graph.query(status="archived", limit=1000)

        to_cold = []
        for discovery in archived:
            # Permanent entries are never auto-tiered, even out of archived.
            if self.get_lifecycle_policy(discovery) == "permanent":
                continue
            # Check if updated_at (when it was archived) is old enough
            if discovery.updated_at and discovery.updated_at < cutoff_iso:
                to_cold.append(discovery.id)

        if not dry_run and to_cold:
            await self._batch_update_status(graph, to_cold, "cold", now)

        logger.info(f"{'[DRY RUN] Would move to cold' if dry_run else 'Moved to cold'} {len(to_cold)} very old archived discoveries")
        return to_cold

    async def _embedding_coverage(self) -> Optional[Dict[str, Any]]:
        """Coverage of the active embeddings table over all discoveries.

        Lifecycle stats span all epochs and include cold rows, so the
        coverage here is the cross-corpus answer (compare with list which
        scopes to current epoch by default). Returns None on failure so the
        caller can decide whether to surface or omit.
        """
        try:
            from src.db import get_db
            from src.embeddings import get_active_table_name
            db = get_db()  # SYNC accessor — `await get_db()` raised TypeError
            table = get_active_table_name()
            # Use the ExecutorPool-wrapped acquire() path (canonical post-PR
            # #218). The old body did `db = await get_db()` (get_db is sync, so
            # this raised "object can't be awaited") then `db._pool.fetchval`;
            # both threw and the except-return-None swallowed it, so coverage
            # surfaced as null on the live backend (fixed 2026-06-20).
            async with db.acquire() as conn:
                total = await conn.fetchval(
                    "SELECT COUNT(*) FROM knowledge.discoveries"
                ) or 0
                with_embeddings = await conn.fetchval(
                    f"SELECT COUNT(*) FROM knowledge.discoveries d "
                    f"WHERE d.id IN (SELECT discovery_id FROM {table})"
                ) or 0
            without = max(0, total - with_embeddings)
            ratio = round(with_embeddings / total, 4) if total else 0.0
            return {
                "with_embeddings": with_embeddings,
                "without_embeddings": without,
                "ratio": ratio,
                "active_table": table,
            }
        except Exception as exc:
            logger.debug(f"lifecycle embedding coverage probe failed: {exc}")
            return None

    async def get_lifecycle_stats(self) -> Dict[str, Any]:
        """Get statistics about discovery lifecycle."""
        graph = await self._get_graph()
        now = datetime.now()

        # Get all discoveries by status
        open_discoveries = await graph.query(status="open", limit=10000)
        resolved_discoveries = await graph.query(status="resolved", limit=10000)
        archived_discoveries = await graph.query(status="archived", limit=10000)
        cold_discoveries = await graph.query(status="cold", limit=10000)

        open_count = len(open_discoveries)
        resolved_count = len(resolved_discoveries)
        archived_count = len(archived_discoveries)
        cold_count = len(cold_discoveries)

        # Count by policy
        policy_counts = {"permanent": 0, "standard": 0, "ephemeral": 0}
        for d in open_discoveries + resolved_discoveries:
            policy = self.get_lifecycle_policy(d)
            policy_counts[policy] += 1

        # Count old resolved (candidates for archival)
        cutoff_resolved = (now - timedelta(days=self.RESOLVED_TO_ARCHIVED_DAYS)).isoformat()
        old_resolved = sum(
            1 for d in resolved_discoveries
            if d.resolved_at and d.resolved_at < cutoff_resolved
            and self.get_lifecycle_policy(d) != "permanent"
        )

        # Count old archived (candidates for cold)
        cutoff_archived = (now - timedelta(days=self.ARCHIVED_TO_COLD_DAYS)).isoformat()
        old_archived = sum(
            1 for d in archived_discoveries
            if d.updated_at and d.updated_at < cutoff_archived
            and self.get_lifecycle_policy(d) != "permanent"
        )

        # Count ephemeral ready to archive
        cutoff_ephemeral = (now - timedelta(days=self.EPHEMERAL_ARCHIVE_DAYS)).isoformat()
        old_ephemeral = sum(
            1 for d in open_discoveries
            if d.timestamp and d.timestamp < cutoff_ephemeral
            and self.get_lifecycle_policy(d) == "ephemeral"
        )

        return {
            "total_discoveries": open_count + resolved_count + archived_count + cold_count,
            "by_status": {
                "open": open_count,
                "resolved": resolved_count,
                "archived": archived_count,
                "cold": cold_count,
            },
            "by_policy": policy_counts,
            "lifecycle_candidates": {
                "ephemeral_ready_to_archive": old_ephemeral,
                "resolved_ready_to_archive": old_resolved,
                "archived_ready_for_cold": old_archived,
                "ready_to_delete": 0,  # NEVER - we don't delete memories
            },
            "thresholds_days": {
                "ephemeral_to_archived": self.EPHEMERAL_ARCHIVE_DAYS,
                "resolved_to_archived": self.RESOLVED_TO_ARCHIVED_DAYS,
                "archived_to_cold": self.ARCHIVED_TO_COLD_DAYS,
                "deletion": "NEVER - memories persist forever",
            },
            "policy_definitions": {
                "permanent_types": list(PERMANENT_TYPES),
                "permanent_tags": list(PERMANENT_TAGS),
                "ephemeral_tags": list(EPHEMERAL_TAGS),
            },
            "philosophy": "Never delete. Archive to cold. Query with include_cold=true.",
            # Scope marker (#165) — same-name fields on list_knowledge_graph
            # report a different scope (raw status aggregate, epoch-current).
            # Surface here so callers comparing the two know which is which.
            "scope": {
                "kind": "lifecycle_buckets",
                "epoch_scope": "all",
                "including_cold": True,
                "note": (
                    "Sums {open, resolved, archived, cold} from per-status "
                    "queries across all epochs. Does not surface 'superseded' "
                    "as a top-level bucket (those rows are absorbed by the "
                    "supersede_chain). Compare with knowledge action=list."
                ),
            },
            "embedding_coverage": await self._embedding_coverage(),
        }


# Convenience function for MCP handler
async def run_kg_lifecycle_cleanup(dry_run: bool = False) -> Dict[str, Any]:
    """Run knowledge graph lifecycle cleanup."""
    lifecycle = KnowledgeGraphLifecycle()
    result = await lifecycle.run_cleanup(dry_run=dry_run)
    errors = result.get("errors") or []
    if errors:
        _record_kg_lifecycle_status(status="error", last_error=str(errors[0]))
    else:
        _record_kg_lifecycle_status(status="healthy")
    return result


async def get_kg_lifecycle_stats() -> Dict[str, Any]:
    """Get knowledge graph lifecycle statistics."""
    lifecycle = KnowledgeGraphLifecycle()
    return await lifecycle.get_lifecycle_stats()


# Staleness thresholds (days)
AUDIT_HEALTHY_DAYS = 7
AUDIT_AGING_DAYS = 14
AUDIT_STALE_DAYS = 30


def _score_discovery(discovery, lifecycle: KnowledgeGraphLifecycle) -> Dict[str, Any]:
    """Score a single discovery for staleness."""
    now = datetime.now()

    age_days = 0
    try:
        created = datetime.fromisoformat(discovery.timestamp.replace("Z", "+00:00")) if discovery.timestamp else now
        age_days = (now - created.replace(tzinfo=None)).days
    except (ValueError, TypeError):
        pass

    last_activity_days = age_days
    if getattr(discovery, "updated_at", None):
        try:
            updated = datetime.fromisoformat(discovery.updated_at.replace("Z", "+00:00"))
            last_activity_days = (now - updated.replace(tzinfo=None)).days
        except (ValueError, TypeError):
            pass

    responses = getattr(discovery, "responses_from", []) or []
    related = getattr(discovery, "related_to", []) or []
    activity_score = len(responses) + len(related)

    # Bucket classification.
    #
    # Both link kinds count as "alive in the system":
    #   - responses_from = active conversation (someone replied)
    #   - related_to     = structural anchor (someone cross-referenced)
    # An entry that's heavily cited via related_to but never replied to is
    # still load-bearing — keeping the healthy guard symmetric in both link
    # types prevents foundational notes from sliding into candidate_for_archive
    # just because nobody responded to them.
    if last_activity_days <= AUDIT_HEALTHY_DAYS or activity_score > 0:
        bucket = "healthy"
    elif last_activity_days <= AUDIT_AGING_DAYS:
        bucket = "aging"
    elif last_activity_days <= AUDIT_STALE_DAYS:
        bucket = "stale"
    else:
        bucket = "candidate_for_archive"

    # Permanent entries are always healthy
    if lifecycle.get_lifecycle_policy(discovery) == "permanent":
        bucket = "healthy"

    return {
        "id": discovery.id,
        "summary": getattr(discovery, "summary", ""),
        "type": getattr(discovery, "type", ""),
        "agent_id": getattr(discovery, "agent_id", None),
        "age_days": age_days,
        "last_activity_days": last_activity_days,
        "activity_score": activity_score,
        "bucket": bucket,
        "tags": getattr(discovery, "tags", []) or [],
    }


async def run_kg_audit(
    scope: str = "open",
    top_n: int = 10,
    use_model: bool = False,
    agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Audit the knowledge graph for staleness.

    Read-only: scores entries by age/activity, groups into health buckets.
    Optionally uses call_model for relevance assessment of stale entries.

    Args:
        scope: "open" (default), "all", "by_agent"
        top_n: Number of stale entries to return in detail
        use_model: If True, feed stale entries to call_model for relevance check
        agent_id: Filter by agent (used when scope="by_agent")

    Returns:
        Structured audit report with bucket counts and top stale entries.
    """
    from src.knowledge_graph import get_knowledge_graph
    graph = await get_knowledge_graph()
    lifecycle = KnowledgeGraphLifecycle()
    now = datetime.now()

    # Query based on scope
    discoveries = []
    if scope == "open":
        discoveries = await graph.query(status="open", limit=10000)
    elif scope == "all":
        for status in ("open", "resolved", "archived"):
            discoveries.extend(await graph.query(status=status, limit=5000))
    elif scope == "by_agent" and agent_id:
        all_open = await graph.query(status="open", limit=10000)
        discoveries = [d for d in all_open if getattr(d, "agent_id", None) == agent_id]
    else:
        discoveries = await graph.query(status="open", limit=10000)

    # Score each discovery
    scored = [_score_discovery(d, lifecycle) for d in discoveries]

    # Aggregate into buckets
    buckets: Dict[str, int] = {"healthy": 0, "aging": 0, "stale": 0, "candidate_for_archive": 0}
    for s in scored:
        buckets[s["bucket"]] = buckets.get(s["bucket"], 0) + 1

    # Top stale entries (stale + candidate_for_archive, sorted by last_activity_days desc)
    stale_entries = [s for s in scored if s["bucket"] in ("stale", "candidate_for_archive")]
    stale_entries.sort(key=lambda x: x["last_activity_days"], reverse=True)
    top_stale = stale_entries[:top_n]

    # Optional model assessment
    model_assessment = None
    if use_model and top_stale:
        try:
            from src.mcp_handlers.support.model_inference import handle_call_model
            import json as _json

            prompt_lines = ["Given these knowledge graph entries, which are still relevant and which should be archived?\n"]
            for entry in top_stale:
                prompt_lines.append(
                    f"- [{entry['id'][:8]}] {entry['summary']} "
                    f"(age: {entry['age_days']}d, type: {entry['type']}, "
                    f"last activity: {entry['last_activity_days']}d ago)"
                )
            prompt_lines.append("\nFor each, reply: KEEP or ARCHIVE with a brief reason.")

            result = await handle_call_model({
                "prompt": "\n".join(prompt_lines),
                "model": "auto",
                "task_type": "analysis",
                "max_tokens": 1000,
            })
            # Parse response
            if isinstance(result, list) and hasattr(result[0], "text"):
                data = _json.loads(result[0].text)
                if data.get("success"):
                    model_assessment = data.get("response")
        except Exception as e:
            logger.warning(f"Model assessment failed during audit: {e}")
            model_assessment = f"(model unavailable: {e})"

    return {
        "timestamp": now.isoformat(),
        "scope": scope,
        "total_audited": len(scored),
        "buckets": buckets,
        "top_stale": top_stale,
        "model_assessment": model_assessment,
        "thresholds": {
            "healthy_days": AUDIT_HEALTHY_DAYS,
            "aging_days": AUDIT_AGING_DAYS,
            "stale_days": AUDIT_STALE_DAYS,
        },
    }


async def kg_lifecycle_background_task(interval_hours: float = 24.0):
    """
    Background task that periodically runs lifecycle cleanup.

    Archives ephemeral notes older than 7 days, resolved entries older
    than 30 days, and moves old archived entries to cold storage.

    Args:
        interval_hours: How often to run cleanup (default: 24 hours)
    """
    logger.info(f"KG lifecycle background task started (interval: {interval_hours}h)")

    while True:
        try:
            await asyncio.sleep(interval_hours * 3600)

            logger.info("Running KG lifecycle cleanup...")
            result = await run_kg_lifecycle_cleanup(dry_run=False)

            archived = result.get("ephemeral_archived", 0) + result.get("discoveries_archived", 0)
            cold = result.get("discoveries_to_cold", 0)
            if archived > 0 or cold > 0:
                logger.info(
                    f"KG lifecycle: archived {archived} entries, "
                    f"moved {cold} to cold"
                )
            else:
                logger.debug("KG lifecycle: nothing to clean up")

        except asyncio.CancelledError:
            logger.info("KG lifecycle background task cancelled")
            break
        except Exception as e:
            logger.error(f"KG lifecycle error: {e}", exc_info=True)
            _record_kg_lifecycle_status(status="error", last_error=str(e))
            # Don't crash the background task on errors
            await asyncio.sleep(60)

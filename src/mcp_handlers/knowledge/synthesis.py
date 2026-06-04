"""On-demand knowledge-graph synthesis (Issue #1: closing the loop).

The knowledge() store/note path persists discrete discovery rows + related_to
edges, and synthesis only happens on read (``synthesize=true`` on search). The
knowledge-graph skill itself notes the graph "does not close loops
automatically" (surfaced by the LLM-wiki comparison, #44 / PR #45).

This module compounds those discrete rows into rolled-up *topic summaries* so a
cross-referenced, compounded narrative exists before query time — the way
Microsoft GraphRAG maintains hierarchical community summaries over base nodes.

Why on-demand, not on-write
---------------------------
The original proposal said "post-write synthesis pass". That framing is a trap
for a multi-agent fleet: running an LLM synthesis pass on *every* store/note is
the auto-checkin-on-every-trivial-write anti-pattern UNITARES explicitly lists
as a non-goal — per-write latency, LLM cost, and a fresh noise source (stale
auto-generated rollups racing live writes). So synthesis runs like lint or
cleanup: periodically or on demand via ``knowledge(action='synthesize')``,
reusing the existing store + LLM-delegation machinery. No per-write cost.

Why no schema change
--------------------
Rollups are persisted as ordinary discovery rows: a deterministic id
``rollup::<topic>`` and ``type='topic_rollup'``. That means they upsert in place
(compounding across runs, never duplicating), are queryable through normal
search/get, and are lifecycle-managed like any other discovery — with zero
migration. Re-running refreshes ``summary``/``details`` via the existing
``ON CONFLICT (id) DO UPDATE`` write path.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.knowledge_graph import DiscoveryNode, normalize_tags
from src.logging_utils import get_logger
from ..support.llm_delegation import call_local_llm

logger = get_logger(__name__)

# Rollup rows are first-class discoveries, distinguished by a reserved type and
# a deterministic id so successive passes upsert the same row.
ROLLUP_TYPE = "topic_rollup"
ROLLUP_ID_PREFIX = "rollup::"
# Stable synthetic writer for system-generated rollups (attribution, not auth).
SYNTHESIS_WRITER_ID = "system:kg-synthesis"

# Tuning. A topic needs a few members before a rollup carries more signal than
# the raw rows. Per-run topic cap bounds cost the way cleanup bounds its batch.
MIN_TOPIC_MEMBERS = 3
DEFAULT_TOPIC_LIMIT = 20
# Cap members fed to the narrative so the LLM prompt (and details blob) stays
# small; GraphRAG likewise summarizes a bounded slice, not the whole community.
MAX_MEMBERS_PER_ROLLUP = 12
ROLLUP_SUMMARY_TOKENS = 256


def rollup_id(topic: str) -> str:
    """Deterministic discovery id for a topic's rollup row."""
    return f"{ROLLUP_ID_PREFIX}{topic}"


def is_rollup(discovery: Any) -> bool:
    """True if a discovery dict / node is a synthesis rollup row."""
    dtype = discovery.get("type") if isinstance(discovery, dict) else getattr(discovery, "type", None)
    did = discovery.get("id") if isinstance(discovery, dict) else getattr(discovery, "id", "")
    return dtype == ROLLUP_TYPE or (isinstance(did, str) and did.startswith(ROLLUP_ID_PREFIX))


def extract_related_topics(members: List[Dict[str, Any]], topic: str, limit: int = 8) -> List[str]:
    """Co-occurring tags across the members — the precomputed cross-reference set.

    These let a reader hop topic -> topic without a query-time join. Ordered by
    co-occurrence frequency so the densest neighbours come first.
    """
    counts: Dict[str, int] = {}
    for m in members:
        for tag in m.get("tags", []) or []:
            if tag and tag != topic:
                counts[tag] = counts.get(tag, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [tag for tag, _ in ranked[:limit]]


def _member_lines(members: List[Dict[str, Any]]) -> List[str]:
    """One ``[type] summary (status)`` line per member, capped for prompt size."""
    lines = []
    for m in members[:MAX_MEMBERS_PER_ROLLUP]:
        summary = (m.get("summary") or "").strip().replace("\n", " ")[:140]
        dtype = m.get("type") or "note"
        status = m.get("status") or "open"
        lines.append(f"[{dtype}] {summary} ({status})")
    return lines


def build_deterministic_summary(
    topic: str,
    members: List[Dict[str, Any]],
    related_topics: List[str],
) -> str:
    """Narrative used when no LLM is reachable.

    Not a degraded placeholder — a faithful, query-time-free rollup assembled
    from the member summaries. Deterministic so re-runs are stable.
    """
    open_count = sum(1 for m in members if (m.get("status") or "open") == "open")
    header = (
        f"Topic '{topic}': {len(members)} discoveries "
        f"({open_count} open). "
    )
    if related_topics:
        header += "Related topics: " + ", ".join(related_topics) + ". "
    body = "\n".join(f"- {line}" for line in _member_lines(members))
    return header + "\nKey discoveries:\n" + body


async def _generate_narrative(
    topic: str,
    members: List[Dict[str, Any]],
    related_topics: List[str],
    *,
    use_llm: bool,
) -> tuple[str, str]:
    """Return ``(narrative, source)`` where source is 'llm' or 'deterministic'.

    Always returns a usable narrative — falls back to the deterministic rollup
    if the LLM is disabled, unavailable, or times out (graceful, never raises).
    """
    deterministic = build_deterministic_summary(topic, members, related_topics)
    if not use_llm:
        return deterministic, "deterministic"

    member_block = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(_member_lines(members)))
    related_hint = (
        f"\nCross-references (co-occurring topics): {', '.join(related_topics)}"
        if related_topics else ""
    )
    prompt = (
        f"You are maintaining a rolled-up summary page for the topic '{topic}' "
        f"in a shared knowledge graph. Compound these discoveries into a single "
        f"coherent narrative: the current state, recurring patterns, and what is "
        f"still open. 4-6 sentences, no preamble, no bullet list.\n\n"
        f"Discoveries:\n{member_block}{related_hint}"
    )
    try:
        result = await call_local_llm(
            prompt=prompt, max_tokens=ROLLUP_SUMMARY_TOKENS, temperature=0.5, timeout=30.0
        )
    except Exception as exc:  # pragma: no cover - call_local_llm already guards
        logger.warning("rollup narrative LLM call raised: %s", exc)
        result = None

    if result and result.strip():
        return result.strip(), "llm"
    return deterministic, "deterministic"


def _make_rollup_node(
    topic: str,
    members: List[Dict[str, Any]],
    narrative: str,
    source: str,
    related_topics: List[str],
    *,
    writer_id: str,
) -> DiscoveryNode:
    """Assemble the rollup discovery row for a topic."""
    member_ids = [m["id"] for m in members[:MAX_MEMBERS_PER_ROLLUP] if m.get("id")]
    open_count = sum(1 for m in members if (m.get("status") or "open") == "open")

    # Staleness watermark. A rollup's id is deterministic (rollup::<topic>), so
    # re-runs upsert in place and there is otherwise NO record of how current the
    # row is. `newest_member_id` is the lexicographically-max member id across
    # ALL considered members (discovery ids are UTC-ISO timestamps, so max == the
    # newest discovery rolled up); a reader or a periodic pass can compare it
    # against the topic's current newest discovery to know whether the rollup is
    # behind. `synthesized_at` is wall-clock recency for the same purpose.
    member_id_pool = [m.get("id") for m in members if m.get("id")]
    newest_member_id = max(member_id_pool) if member_id_pool else None
    synthesized_at = datetime.now(timezone.utc).isoformat()
    headline = narrative.strip().split("\n", 1)[0][:200]
    summary = f"[rollup] {topic}: {len(members)} discoveries ({open_count} open) — {headline}"

    details_parts = [narrative.strip(), ""]
    if related_topics:
        details_parts.append("Related topics: " + ", ".join(related_topics))
    details_parts.append(f"Members ({len(member_ids)} of {len(members)} shown):")
    details_parts.extend(f"- {mid}" for mid in member_ids)
    details = "\n".join(details_parts)

    # Tag with the topic plus a marker so rollups are filterable in normal search
    # (knowledge(action='search', tags=['rollup'])).
    tags = normalize_tags([topic, "rollup"])

    return DiscoveryNode(
        id=rollup_id(topic),
        agent_id=writer_id,
        type=ROLLUP_TYPE,
        summary=summary,
        details=details,
        tags=tags,
        related_to=member_ids,
        status="open",
        provenance={
            "synthesis": {
                "topic": topic,
                "member_count": len(members),
                "open_count": open_count,
                "summary_source": source,
                "related_topics": related_topics,
                "synthesized_at": synthesized_at,
                "newest_member_id": newest_member_id,
            },
            "source": "kg_synthesis",
        },
    )


async def synthesize_topic(
    graph,
    topic: str,
    *,
    use_llm: bool = True,
    dry_run: bool = False,
    writer_id: str = SYNTHESIS_WRITER_ID,
    min_members: int = MIN_TOPIC_MEMBERS,
) -> Optional[Dict[str, Any]]:
    """Build (and unless ``dry_run``, persist) the rollup for one topic.

    Returns a small report dict, or None if the topic has too few members to be
    worth a rollup.
    """
    members = await graph.query(tags=[topic], limit=MAX_MEMBERS_PER_ROLLUP * 3, exclude_archived=True)
    member_dicts = [m.to_dict(include_details=False) for m in members]
    # Never let an existing rollup row become a member of itself.
    member_dicts = [m for m in member_dicts if not is_rollup(m)]

    if len(member_dicts) < min_members:
        return {"topic": topic, "member_count": len(member_dicts), "action": "skipped", "reason": "below_min_members"}

    related_topics = extract_related_topics(member_dicts, topic)
    narrative, source = await _generate_narrative(topic, member_dicts, related_topics, use_llm=use_llm)
    node = _make_rollup_node(topic, member_dicts, narrative, source, related_topics, writer_id=writer_id)

    if not dry_run:
        await graph.add_discovery(node)

    return {
        "topic": topic,
        "rollup_id": node.id,
        "member_count": len(member_dicts),
        "related_topics": related_topics,
        "summary_source": source,
        "summary": node.summary,
        "action": "previewed" if dry_run else "synthesized",
    }


async def synthesize_topics(
    graph,
    *,
    topic: Optional[str] = None,
    limit: int = DEFAULT_TOPIC_LIMIT,
    min_members: int = MIN_TOPIC_MEMBERS,
    use_llm: bool = True,
    dry_run: bool = False,
    writer_id: str = SYNTHESIS_WRITER_ID,
) -> Dict[str, Any]:
    """Run a synthesis pass over the densest topics (or one named topic).

    Bounded by ``limit`` (topics per run) the way cleanup bounds its batch.
    Each topic is synthesized independently and failures are isolated so one bad
    topic never aborts the pass.
    """
    if topic:
        candidates = [{"topic": normalize_tags([topic])[0] if normalize_tags([topic]) else topic}]
    else:
        from src.db import get_db
        db = get_db()
        candidates = await db.kg_topic_candidates(
            min_members=min_members, limit=limit, exclude_types=[ROLLUP_TYPE]
        )

    rollups: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for cand in candidates:
        t = cand["topic"]
        try:
            report = await synthesize_topic(
                graph, t, use_llm=use_llm, dry_run=dry_run,
                writer_id=writer_id, min_members=min_members,
            )
            if report:
                rollups.append(report)
        except Exception as exc:
            logger.warning("synthesis failed for topic %r: %s", t, exc)
            errors.append({"topic": t, "error": str(exc)})

    synthesized = [r for r in rollups if r.get("action") in ("synthesized", "previewed")]
    return {
        "topics_considered": len(candidates),
        "rollups_written": 0 if dry_run else len(synthesized),
        "dry_run": dry_run,
        "rollups": rollups,
        "errors": errors,
    }

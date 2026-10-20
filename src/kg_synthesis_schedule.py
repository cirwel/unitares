"""Scheduled knowledge-graph synthesis, fed by store-time consolidation hints.

``knowledge(action='synthesize')`` (``mcp_handlers/knowledge/synthesis.py``)
builds topic rollups, and the store path computes a ``consolidation_hint`` when
a new finding has three or more open near-duplicates. Until this module, both
existed and nothing connected them: no caller scheduled the action, and the
hint went back to the writer and was discarded (dormant-capability registry,
Theme 2, rows "knowledge action=synthesize" and "consolidation_hint").

This module is the wire:

* ``record_consolidation_hint`` puts the topics behind a fired hint on a small
  in-process queue. It is bounded and deduplicating, and it is lost on restart
  on purpose: the next densest-topic sweep covers whatever a restart dropped,
  so a durable queue would buy nothing a migration should pay for.
* ``kg_synthesis_background_task`` drains that queue on a fixed cadence, then
  runs a bounded sweep over the densest topics.

Mode (``UNITARES_KG_SYNTHESIS_SCHEDULE``):

* ``shadow`` (default) previews rollups with ``dry_run=True`` and the
  deterministic narrative only, and logs what it would have written. Rollups
  are ordinary discovery rows that every agent's search can return, so the
  schedule wakes the eyes before the hands (registry Theme 6): the operator
  reads the shadow log, then sets ``apply``.
* ``apply`` persists the rollups through the same path as the manual action,
  with the local-LLM narrative and its deterministic fallback.
* ``off`` does not schedule anything.
"""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from src.logging_utils import get_logger

logger = get_logger(__name__)

MODE_ENV = "UNITARES_KG_SYNTHESIS_SCHEDULE"
INTERVAL_ENV = "UNITARES_KG_SYNTHESIS_INTERVAL_HOURS"
MODES = ("off", "shadow", "apply")
DEFAULT_MODE = "shadow"
DEFAULT_INTERVAL_HOURS = 24.0
STARTUP_DELAY_SECONDS = 900.0

# Bounds on one cycle. Hinted topics go first because a writer was just told
# its finding is the third open copy; the sweep then fills the remainder.
MAX_HINT_QUEUE = 50
MAX_HINTED_TOPICS_PER_RUN = 10
SWEEP_TOPIC_LIMIT = 10
# Per-topic ceiling: the narrative LLM call has its own 30s timeout, so this
# only trips on a stuck graph query.
TOPIC_TIMEOUT_SECONDS = 90.0

# topic -> discovery ids whose store raised the hint (insertion-ordered so the
# oldest hint is drained, and evicted, first).
_hint_queue: "OrderedDict[str, List[str]]" = OrderedDict()
_last_run: Optional[Dict[str, Any]] = None


def schedule_mode() -> str:
    """Scheduled synthesis mode (off, shadow or apply); an unrecognised value falls back to shadow."""
    raw = os.environ.get(MODE_ENV, DEFAULT_MODE).strip().lower() or DEFAULT_MODE
    if raw in MODES:
        return raw
    logger.warning(
        "[KG_SYNTHESIS] %s=%r is not one of %s; using %r",
        MODE_ENV, raw, "/".join(MODES), DEFAULT_MODE,
    )
    return DEFAULT_MODE


def interval_hours() -> float:
    """Hours between scheduled synthesis cycles; a non-positive value falls back to the default."""
    raw = os.environ.get(INTERVAL_ENV, str(DEFAULT_INTERVAL_HOURS)).strip()
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        logger.warning(
            "[KG_SYNTHESIS] %s=%r is not a positive number; using %s",
            INTERVAL_ENV, raw, DEFAULT_INTERVAL_HOURS,
        )
        return DEFAULT_INTERVAL_HOURS
    return value


def hint_topics(discovery_tags: Iterable[Any], similar_tags: Iterable[Iterable[Any]]) -> List[str]:
    """Topics to synthesize for a fired hint.

    The tags the new finding shares with its open near-duplicates name what
    they have in common; when they share none, the finding's own semantic tags
    are the best available name. Storage-lane tags never qualify.
    """
    from src.knowledge_graph import normalize_tags
    from src.mcp_handlers.knowledge.synthesis import is_semantic_topic_tag

    own = [t for t in normalize_tags(list(discovery_tags or [])) if is_semantic_topic_tag(t)]
    shared_counts: Dict[str, int] = {}
    for tags in similar_tags:
        for tag in set(normalize_tags(list(tags or []))):
            shared_counts[tag] = shared_counts.get(tag, 0) + 1
    shared = [t for t in own if shared_counts.get(t)]
    shared.sort(key=lambda t: -shared_counts[t])
    return shared or own


def record_consolidation_hint(topics: Iterable[str], discovery_id: Optional[str] = None) -> List[str]:
    """Queue topics for the next scheduled synthesis pass; returns what was queued.

    Never raises: it runs on the store path, after the write has landed.
    """
    queued: List[str] = []
    try:
        if schedule_mode() == "off":
            return queued
        for topic in topics:
            if not topic:
                continue
            ids = _hint_queue.pop(topic, [])
            if discovery_id and discovery_id not in ids:
                ids.append(discovery_id)
            _hint_queue[topic] = ids[-5:]
            queued.append(topic)
        while len(_hint_queue) > MAX_HINT_QUEUE:
            _hint_queue.popitem(last=False)
    except Exception as exc:  # pragma: no cover - defensive, store path
        logger.warning("[KG_SYNTHESIS] could not queue consolidation hint: %s", exc)
    return queued


def pending_hints() -> Dict[str, List[str]]:
    return {topic: list(ids) for topic, ids in _hint_queue.items()}


def last_run_report() -> Optional[Dict[str, Any]]:
    return dict(_last_run) if _last_run else None


def _drain_hints(limit: int) -> List[str]:
    topics: List[str] = []
    while _hint_queue and len(topics) < limit:
        topic, _ids = _hint_queue.popitem(last=False)
        topics.append(topic)
    return topics


async def run_synthesis_cycle(mode: Optional[str] = None, graph: Any = None) -> Dict[str, Any]:
    """One bounded pass: hinted topics first, then the densest-topic sweep."""
    global _last_run
    from src.mcp_handlers.knowledge.synthesis import synthesize_topic, synthesize_topics

    mode = mode or schedule_mode()
    dry_run = mode != "apply"
    use_llm = mode == "apply"
    if graph is None:
        from src.knowledge_graph import get_knowledge_graph
        graph = await get_knowledge_graph()

    hinted = _drain_hints(MAX_HINTED_TOPICS_PER_RUN)
    rollups: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for topic in hinted:
        try:
            report = await asyncio.wait_for(
                synthesize_topic(graph, topic, use_llm=use_llm, dry_run=dry_run),
                timeout=TOPIC_TIMEOUT_SECONDS,
            )
            if report:
                rollups.append({**report, "trigger": "consolidation_hint"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[KG_SYNTHESIS] hinted topic %r failed: %s", topic, exc)
            errors.append({"topic": topic, "error": str(exc) or type(exc).__name__})

    sweep: Dict[str, Any] = {}
    try:
        sweep = await asyncio.wait_for(
            synthesize_topics(graph, limit=SWEEP_TOPIC_LIMIT, use_llm=use_llm, dry_run=dry_run),
            timeout=TOPIC_TIMEOUT_SECONDS * SWEEP_TOPIC_LIMIT,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[KG_SYNTHESIS] densest-topic sweep failed: %s", exc)
        errors.append({"topic": "*sweep*", "error": str(exc) or type(exc).__name__})
    already = {r.get("topic") for r in rollups}
    for report in sweep.get("rollups", []):
        if report.get("topic") not in already:
            rollups.append({**report, "trigger": "sweep"})
    errors.extend(sweep.get("errors", []))

    done = [r for r in rollups if r.get("action") in ("synthesized", "previewed")]
    report = {
        "mode": mode,
        "dry_run": dry_run,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "hinted_topics": hinted,
        "topics_considered": len(hinted) + int(sweep.get("topics_considered", 0)),
        "rollups_previewed": len(done) if dry_run else 0,
        "rollups_written": 0 if dry_run else len(done),
        "rollups": rollups,
        "errors": errors,
    }
    _last_run = report
    return report


def _log_cycle(report: Dict[str, Any]) -> None:
    verb = "would write" if report["dry_run"] else "wrote"
    count = report["rollups_previewed"] if report["dry_run"] else report["rollups_written"]
    logger.info(
        "[KG_SYNTHESIS] mode=%s %s %d rollup(s) over %d topic(s) (%d from consolidation hints): %s",
        report["mode"], verb, count, report["topics_considered"],
        len(report["hinted_topics"]),
        ", ".join(
            r["rollup_id"] for r in report["rollups"]
            if r.get("rollup_id") and r.get("action") in ("synthesized", "previewed")
        ) or "none",
    )
    if report["errors"]:
        logger.warning("[KG_SYNTHESIS] %d topic error(s): %s", len(report["errors"]), report["errors"][:5])


async def kg_synthesis_background_task(startup_delay: float = STARTUP_DELAY_SECONDS) -> None:
    """Periodic synthesis; returns at once when the schedule is off."""
    mode = schedule_mode()
    if mode == "off":
        logger.info("[KG_SYNTHESIS] %s=off; scheduled synthesis not started", MODE_ENV)
        return
    hours = interval_hours()
    logger.info("[KG_SYNTHESIS] scheduled synthesis started (mode=%s, every %sh)", mode, hours)
    try:
        await asyncio.sleep(startup_delay)
    except asyncio.CancelledError:
        return
    while True:
        try:
            current = schedule_mode()
            if current != "off":
                _log_cycle(await run_synthesis_cycle(current))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("[KG_SYNTHESIS] cycle failed: %s", exc, exc_info=True)
        try:
            await asyncio.sleep(hours * 3600)
        except asyncio.CancelledError:
            break

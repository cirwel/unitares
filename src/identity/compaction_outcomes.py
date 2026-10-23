"""Pair each recorded context compaction with what happened to its identity.

A host reports a compaction through ``/v1/runtime/observe`` with
``observation_kind="context_compaction"``. The route requires the session's
own live binding, so the row names the identity that was bound when the host
replaced the context with a summary. What followed is read from records that
already exist:

- ``minted``: an identity declaring this one as parent with
  ``spawn_reason="compaction"`` was created at or after the compaction and
  before this identity's next recorded compaction.
- ``kept``: no such child, and this identity acted again after the
  compaction: a check-in, a knowledge write or a consult
  (``SELF_ACTIVITY_EVENT_TYPES``). Rows other callers or detectors write
  against an identity, such as ``agent_archived`` or
  ``identity_hijack_suspected``, do not count; nor does ``knowledge_read``,
  which a reader listing this identity's knowledge can log against it.
- ``no_later_activity``: neither. The session may have ended, or nothing it
  did was recorded. This is not evidence for either choice.

The outcome describes the record, not the agent's view of itself. Hook-driven
check-ins land on whatever identity the session cache holds, so ``kept`` means
the identity continued, not that the agent judged it should. A compaction with
no usable binding is never recorded, because the route needs the session's
proof, so those compactions do not appear here at all.

The agent's view is read separately, where an adapter asked for it: a
``context_compaction_probe`` row carries the agent's own answer (same process,
new process, cannot tell) or the hook's note that it gave none, keyed to the
compaction it answers. Each row then shows the answer beside the outcome. Read
the pair with two limits in mind. The answer was given with the operator's
compaction guidance in the same context (``guidance_present``), which says the
process continues. And ``kept`` is mostly the hook's doing: the Stop hook that
records the answer then checks in on the cached identity, which counts as the
identity acting again. So ``same_process`` beside ``kept`` is what guidance and
hook produce together. The informative cells are answers beside ``minted`` and
any answer other than ``same_process``.

The identity ontology (``docs/ontology/identity.md``, *Compaction*) says why
the two outcomes are worth comparing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from src.runtime_observations import (
    COMPACTION_EVENT_TYPE,
    PROBE_ANSWERS,
    PROBE_EVENT_TYPE,
)

OUTCOMES = ("minted", "kept", "no_later_activity")
# The answer consistent with each outcome. ``cannot_tell``, ``no_answer`` and
# ``no_later_activity`` match nothing and are counted, not scored. Consistency
# is not accuracy: see the module docstring on what produces ``kept``.
CONSISTENT_ANSWER = {"kept": "same_process", "minted": "new_process"}
# Audit rows written for an identity's own action: one ``auto_attest`` per
# check-in (the agent checking in), ``knowledge_write`` (the author) and
# ``consultation`` (the requester).
SELF_ACTIVITY_EVENT_TYPES = ("auto_attest", "knowledge_write", "consultation")
PAGE_SIZE = 5000
MAX_PAGES = 20


def _utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _probes_by_compaction(
    probes: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, datetime], list[dict[str, Any]]]:
    """Group probe answers by (compacted identity, compaction time), oldest first.

    Which of them counts is decided per compaction, once its successors are
    known (``_select_probe``).
    """
    keyed: dict[tuple[str, datetime], list[dict[str, Any]]] = {}
    ordered = sorted(
        probes, key=lambda row: _utc(row.get("timestamp")) or datetime.max.replace(tzinfo=timezone.utc)
    )
    for row in ordered:
        details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
        compacted = str(details.get("compaction_agent_uuid") or "").strip()
        at = _utc(details.get("compaction_observed_at"))
        answer = str(details.get("probe_answer") or "")
        if not compacted or at is None or answer not in PROBE_ANSWERS:
            continue
        keyed.setdefault((compacted, at), []).append(
            {
                "answer": answer,
                "probe_version": str(details.get("probe_version") or ""),
                "guidance_present": details.get("guidance_present") is True,
                "answered_by": str(row.get("agent_id") or ""),
            }
        )
    return keyed


def _select_probe(
    candidates: list[dict[str, Any]], eligible: set[str]
) -> tuple[dict[str, Any] | None, int, int]:
    """Pick the first answer from the compacted identity or a successor it minted.

    The route lets any bound identity name any compaction, so an answer from
    anyone else is ignored rather than allowed to displace the agent's own.
    Returns the answer, the later eligible answers ignored as duplicates, and
    the answers ignored as unrelated.
    """
    chosen = None
    duplicates = unrelated = 0
    for candidate in candidates:
        if candidate["answered_by"] not in eligible:
            unrelated += 1
        elif chosen is None:
            chosen = candidate
        else:
            duplicates += 1
    return chosen, duplicates, unrelated


def _probe_summary(
    rows: list[dict[str, Any]], duplicates: int, unrelated: int, unmatched: int
) -> dict[str, Any]:
    asked = [row for row in rows if row["probe"] is not None]
    by_outcome = {
        answer: {name: 0 for name in OUTCOMES} for answer in PROBE_ANSWERS
    }
    consistent = inconsistent = 0
    for row in asked:
        answer = row["probe"]["answer"]
        by_outcome[answer][row["outcome"]] += 1
        expected = CONSISTENT_ANSWER.get(row["outcome"])
        if expected is None or answer not in CONSISTENT_ANSWER.values():
            continue
        if answer == expected:
            consistent += 1
        else:
            inconsistent += 1
    return {
        "asked": len(asked),
        "answers": {
            answer: sum(1 for row in asked if row["probe"]["answer"] == answer)
            for answer in PROBE_ANSWERS
        },
        "by_answer_and_outcome": by_outcome,
        "decisive_pairs": consistent + inconsistent,
        "consistent": consistent,
        "inconsistent": inconsistent,
        "answered_by_successor": sum(
            1 for row in asked if row["probe"]["answered_by"] != row["agent_id"]
        ),
        "duplicates_ignored": duplicates,
        "unrelated_ignored": unrelated,
        "unmatched_ignored": unmatched,
        "scope": (
            "Answers given with the compaction guidance in context. kept is "
            "mostly produced by the Stop hook's own check-in, so same_process "
            "beside kept is expected; answers beside minted, and answers other "
            "than same_process, carry the information. Consistency pairs "
            "same_process with kept and new_process with minted; it is not "
            "accuracy."
        ),
    }


def classify_compaction_outcomes(
    compactions: Iterable[Mapping[str, Any]],
    children: Iterable[Mapping[str, Any]],
    last_activity: Mapping[str, Any],
    *,
    through: datetime | None = None,
    probes: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Classify compactions against minted children and later activity.

    ``through`` bounds the window when the compaction read was incomplete: an
    identity's last compaction read then ends there, so a child minted after a
    compaction that was never read is not credited to an earlier one.
    Compactions at or after ``through`` may sit beside unread ones, so they are
    left out and counted in ``excluded_at_cutoff`` rather than classified.

    ``compactions`` are audit rows (``agent_id``, ``timestamp``, ``details``,
    ``event_id``). ``children`` are identities with
    ``spawn_reason="compaction"`` (``agent_id``, ``parent_agent_id``,
    ``created_at``). ``last_activity`` maps an agent id to the time of its
    latest audit event that is not a runtime observation. ``probes`` are
    ``context_compaction_probe`` audit rows, matched to a compaction by the
    identity and time it names, and counted only when posted by that identity
    or a child it minted after the compaction.
    """
    probe_index = _probes_by_compaction(probes)
    probe_duplicates = probe_unrelated = 0
    matched_keys: set[tuple[str, datetime]] = set()
    by_agent: dict[str, list[tuple[datetime, Mapping[str, Any]]]] = {}
    excluded_at_cutoff = 0
    for row in compactions:
        agent_id = str(row.get("agent_id") or "").strip()
        at = _utc(row.get("timestamp"))
        if not agent_id or at is None:
            continue
        if through is not None and at >= through:
            excluded_at_cutoff += 1
            continue
        by_agent.setdefault(agent_id, []).append((at, row))

    children_by_parent: dict[str, list[tuple[datetime, str]]] = {}
    for child in children:
        parent = str(child.get("parent_agent_id") or "").strip()
        created = _utc(child.get("created_at"))
        if parent and created is not None:
            children_by_parent.setdefault(parent, []).append(
                (created, str(child.get("agent_id") or ""))
            )

    rows: list[dict[str, Any]] = []
    for agent_id, events in by_agent.items():
        events.sort(key=lambda item: item[0])
        latest = _utc(last_activity.get(agent_id))
        for index, (at, row) in enumerate(events):
            until = events[index + 1][0] if index + 1 < len(events) else through
            minted = sorted(
                child_id
                for created, child_id in children_by_parent.get(agent_id, [])
                if created >= at and (until is None or created < until)
            )
            if minted:
                outcome = "minted"
            elif latest is not None and latest > at:
                outcome = "kept"
            else:
                outcome = "no_later_activity"
            details = row.get("details") if isinstance(row.get("details"), Mapping) else {}
            reported_at = _utc(details.get("observed_at")) or at
            matched_keys.add((agent_id, reported_at))
            probe, dupes, unrelated = _select_probe(
                probe_index.get((agent_id, reported_at), []), {agent_id, *minted}
            )
            probe_duplicates += dupes
            probe_unrelated += unrelated
            rows.append(
                {
                    "agent_id": agent_id,
                    "compacted_at": at.isoformat(),
                    "event_id": row.get("event_id"),
                    "host_family": str(details.get("host_family") or "unknown"),
                    "outcome": outcome,
                    "minted_children": minted,
                    "probe": probe,
                }
            )

    rows.sort(key=lambda item: item["compacted_at"])
    by_host: dict[str, Counter[str]] = {}
    for row in rows:
        by_host.setdefault(row["host_family"], Counter())[row["outcome"]] += 1
    totals = Counter(row["outcome"] for row in rows)
    return {
        "compactions": len(rows),
        "outcomes": {name: totals.get(name, 0) for name in OUTCOMES},
        "by_host": {
            host: {name: counts.get(name, 0) for name in OUTCOMES}
            for host, counts in sorted(by_host.items())
        },
        "rows": rows,
        "probe": _probe_summary(
            rows,
            probe_duplicates,
            probe_unrelated,
            # Answers naming no classified compaction: before the window, past
            # the cutoff, or a compaction whose stored time the adapter did
            # not send back. Counted so none disappears silently.
            sum(len(v) for k, v in probe_index.items() if k not in matched_keys),
        ),
        "complete": through is None,
        "excluded_at_cutoff": excluded_at_cutoff,
        "scope": (
            "Compactions reported with a usable session binding only; "
            "outcomes describe the identity record, not the agent's view."
        ),
    }


async def _read_event_type(
    event_type: str, *, since: datetime, page_size: int
) -> tuple[list[dict[str, Any]], datetime | None]:
    """Read every row of one audit event type since ``since``.

    Returns the rows and, when the read stopped early, the time it reached.
    """
    from src.audit_db import query_audit_events_async

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    start = since
    through: datetime | None = None
    for _ in range(MAX_PAGES):
        page = await query_audit_events_async(
            event_types=[event_type],
            start_time=start.isoformat(),
            limit=page_size,
            order="asc",
        )
        for row in page:
            key = str(row.get("event_id") or "")
            if key and key in seen:
                continue
            seen.add(key)
            rows.append(row)
        last = _utc(page[-1].get("timestamp")) if page else None
        if len(page) < page_size or last is None:
            break
        if last <= start:
            # A full page sharing one timestamp cannot advance; stop and
            # disclose rather than loop.
            through = last
            break
        start = last
    else:
        through = start
    return rows, through


async def collect_compaction_outcomes(
    *, since: datetime, page_size: int = PAGE_SIZE
) -> dict[str, Any]:
    """Read compactions since ``since`` and classify their outcomes."""
    from src.audit_db import aggregate_audit_events_async
    from src.db import get_db

    compactions, through = await _read_event_type(
        COMPACTION_EVENT_TYPE, since=since, page_size=page_size
    )
    probes, probes_through = await _read_event_type(
        PROBE_EVENT_TYPE, since=since, page_size=page_size
    )
    agent_ids = sorted(
        {str(row.get("agent_id")) for row in compactions if row.get("agent_id")}
    )
    children: list[Any] = []
    activity: dict[str, Any] = {}
    if agent_ids:
        db = get_db()
        async with db.acquire() as conn:
            children = await conn.fetch(
                """
                SELECT agent_id, parent_agent_id, created_at
                FROM core.identities
                WHERE spawn_reason = 'compaction'
                  AND parent_agent_id = ANY($1::text[])
                  AND created_at >= $2
                """,
                agent_ids,
                since,
            )
        # The audit layer owns audit.events; its window-wide aggregate gives
        # one (agent, event type) row with last_ts, so no row limit truncates it.
        for agent_id in agent_ids:
            groups = await aggregate_audit_events_async(
                agent_id=agent_id,
                event_types=list(SELF_ACTIVITY_EVENT_TYPES),
                start_time=since.isoformat(),
            )
            latest = [
                _utc(group.get("last_ts"))
                for group in groups
                if group.get("event_type") in SELF_ACTIVITY_EVENT_TYPES
            ]
            latest = [value for value in latest if value is not None]
            if latest:
                activity[agent_id] = max(latest)
    report = classify_compaction_outcomes(
        compactions,
        [dict(row) for row in children],
        activity,
        through=through,
        probes=probes,
    )
    report["probe"]["complete"] = probes_through is None
    report["since"] = since.isoformat()
    return report

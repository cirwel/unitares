"""Pair each recorded context compaction with what happened to its identity.

A host reports a compaction through ``/v1/runtime/observe`` with
``observation_kind="context_compaction"``. The route requires the session's
own live binding, so the row names the identity that was bound when the host
replaced the context with a summary. What followed is read from records that
already exist:

- ``minted``: an identity declaring this one as parent with
  ``spawn_reason="compaction"`` was created at or after the compaction and
  before this identity's next recorded compaction.
- ``kept``: no such child, and this identity has at least one audit event
  after the compaction other than a runtime observation.
- ``no_later_activity``: neither. The session may have ended, or nothing it
  did was recorded. This is not evidence for either choice.

The outcome describes the record, not the agent's view of itself. Hook-driven
check-ins land on whatever identity the session cache holds, so ``kept`` means
the identity continued, not that the agent judged it should. A compaction with
no usable binding is never recorded, because the route needs the session's
proof, so those compactions do not appear here at all.

The identity ontology (``docs/ontology/identity.md``, *Compaction*) says why
the two outcomes are worth comparing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from src.runtime_observations import COMPACTION_EVENT_TYPE, RUNTIME_EVENT_PREFIX

OUTCOMES = ("minted", "kept", "no_later_activity")


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


def classify_compaction_outcomes(
    compactions: Iterable[Mapping[str, Any]],
    children: Iterable[Mapping[str, Any]],
    last_activity: Mapping[str, Any],
) -> dict[str, Any]:
    """Classify compactions against minted children and later activity.

    ``compactions`` are audit rows (``agent_id``, ``timestamp``, ``details``,
    ``event_id``). ``children`` are identities with
    ``spawn_reason="compaction"`` (``agent_id``, ``parent_agent_id``,
    ``created_at``). ``last_activity`` maps an agent id to the time of its
    latest audit event that is not a runtime observation.
    """
    by_agent: dict[str, list[tuple[datetime, Mapping[str, Any]]]] = {}
    for row in compactions:
        agent_id = str(row.get("agent_id") or "").strip()
        at = _utc(row.get("timestamp"))
        if agent_id and at is not None:
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
            until = events[index + 1][0] if index + 1 < len(events) else None
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
            rows.append(
                {
                    "agent_id": agent_id,
                    "compacted_at": at.isoformat(),
                    "event_id": row.get("event_id"),
                    "host_family": str(details.get("host_family") or "unknown"),
                    "outcome": outcome,
                    "minted_children": minted,
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
        "scope": (
            "Compactions reported with a usable session binding only; "
            "outcomes describe the identity record, not the agent's view."
        ),
    }


async def collect_compaction_outcomes(
    *, since: datetime, limit: int = 5000
) -> dict[str, Any]:
    """Read compactions since ``since`` and classify their outcomes."""
    from src.audit_db import aggregate_audit_events_async, query_audit_events_async
    from src.db import get_db

    compactions = await query_audit_events_async(
        event_types=[COMPACTION_EVENT_TYPE],
        start_time=since.isoformat(),
        limit=limit,
        order="asc",
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
                agent_id=agent_id, start_time=since.isoformat()
            )
            latest = [
                _utc(group.get("last_ts"))
                for group in groups
                if not str(group.get("event_type") or "").startswith(RUNTIME_EVENT_PREFIX)
            ]
            latest = [value for value in latest if value is not None]
            if latest:
                activity[agent_id] = max(latest)
    report = classify_compaction_outcomes(
        compactions, [dict(row) for row in children], activity
    )
    report["since"] = since.isoformat()
    return report

"""Sentinel surfaces: summary, finding intake, backlog, and adjudication.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from starlette.responses import JSONResponse


from src.logging_utils import get_logger
from src.broadcaster import broadcaster_instance
from src.dashboard_auth import (
    dashboard_session_write_authorized,
)

from src.http_routes import access

logger = get_logger(__name__)


# Allowed severity values for externally posted findings
_FINDING_SEVERITIES = frozenset({"info", "low", "medium", "warning", "high", "critical"})
# Only accept *_finding event types via this endpoint (prevents spoofing
# reserved dashboard event types like verdict_change / risk_threshold)
_FINDING_TYPE_SUFFIX = "_finding"
# Required top-level fields on the posted JSON
_FINDING_REQUIRED_FIELDS = ("type", "severity", "message", "agent_id", "agent_name", "fingerprint")
# Sentinel finding event types as persisted in audit.events (the durable store
# behind the transient ring buffer). The backlog endpoint reads these.
# Families eligible for the adjudication queue. Widened ONE family at a time,
# and only after that producer is verified to write a real governance UUID into
# audit.events.agent_id -- a slug cannot be resolved by _finding_producer_uuid
# and would just yield 422s. doctor_check_finding qualified 2026-08-26, when the
# doctor layer's shared identity was first provisioned; the other six slug
# producers do NOT yet.
#
# ⛔After adding a family, watch that it still produces DISMISSALS. A family
# that only ever confirms has become the all-positive generator Invariant 4
# forbids, and it poisons the anchor channel rather than feeding it.
_SENTINEL_FINDING_EVENT_TYPES = (
    "sentinel_finding", "sentinel_alarm_finding", "doctor_check_finding",
)

# event_type -> outcome_type prefix. Identities may pool; LABELS MUST NOT.
# These detectors have very different precision and very different volume, so a
# shared label would move with the volume mix rather than with any detector's
# quality -- the confound that made the pooled dialectic-reviewer number
# describe neither instrument. Keeping doctor_check_finding_* distinct is what
# lets a structurally-broken check (immortal_lease is a known false positive for
# every resident:/dispatch/<thread> lease) show up as one bad detector instead
# of quietly dragging Sentinel's precision down.
#
# Sentinel's two families deliberately BOTH map to "sentinel_finding": that is
# today's behaviour, and remapping them would orphan every historical
# sentinel_finding_* outcome row from its own dedup set.
_FINDING_KIND_BY_EVENT_TYPE = {
    "sentinel_finding": "sentinel_finding",
    "sentinel_alarm_finding": "sentinel_finding",
    "doctor_check_finding": "doctor_check_finding",
}
# Fallback for a fingerprint whose event_type is not a queue family. This
# preserves today's behaviour exactly rather than changing it in passing, but
# it is a KNOWN GAP, not a design: a watcher_finding adjudicated through this
# endpoint is booked as sentinel_finding_confirmed. Watcher has its own local
# resolution path, so this is reachable only by adjudicating a Watcher
# fingerprint here directly. Giving Watcher its own mapping is a separate
# change with its own blast radius (audit.outcome_events already carries
# watcher_finding_% rows from that other path) and wants its own review.
_DEFAULT_FINDING_KIND = "sentinel_finding"
# Default severities the operator cares about when reviewing "did I miss
# something across restarts?" — the load-bearing findings.
_SENTINEL_BACKLOG_DEFAULT_SEVERITIES = frozenset({"high", "critical"})

# ⛔The SECOND gate. Eligibility is (event_type AT severity), and adding a
# family to _SENTINEL_FINDING_EVENT_TYPES without checking the severity half
# admits it and then shows zero of it -- an inert wire that reads like a
# working one.
#
# Severity vocabularies are NOT shared across producers. Sentinel emits
# medium/high/info and is held at {high, critical} for a reason: its `medium`
# alone is 834 distinct fingerprints over 30d, and the queue is deliberately
# small because outcomes join to the last prior state snapshot, so a batch
# sweep collapses into ONE statistical cluster.
#
# The doctor layer emits `warning` and nothing else. Measured 2026-08-26: 204
# doctor_check_finding rows over 30d but only **7 distinct fingerprints** --
# the rest are cooldown re-alerts of the same still-open conditions. So
# admitting `warning` for this family adds ~7 items per 30d against the 22 the
# queue sees today. That is inside "a few per day", which is the constraint
# that matters.
_ADJUDICABLE_SEVERITIES_BY_EVENT_TYPE = {
    "doctor_check_finding": frozenset({"warning", "high", "critical"}),
}


def _adjudicable_severities(event_type):
    """Severities eligible for the queue, for this finding family."""
    return _ADJUDICABLE_SEVERITIES_BY_EVENT_TYPE.get(
        event_type, _SENTINEL_BACKLOG_DEFAULT_SEVERITIES
    )


# Marker for "no explicit ?severity= was given, so apply the per-family
# default". Distinct from None, which means "all severities".
_PER_FAMILY_SEVERITY_DEFAULT = object()


_SENTINEL_DEFAULT_WINDOW_HOURS = 24
_SENTINEL_DEFAULT_RECENT_LIMIT = 50


def _sentinel_summary_from_events(
    events, now=None, window_hours=_SENTINEL_DEFAULT_WINDOW_HOURS,
    recent_limit=_SENTINEL_DEFAULT_RECENT_LIMIT,
):
    """Aggregate sentinel_finding and sentinel_alarm_finding events into
    dashboard-ready shape.

    Pure function so tests can feed parsed-dict events and assert on the
    output without standing up Starlette or the event_detector singleton.

    Two event shapes are accepted: fleet-analysis findings (carry
    `finding_type` + `violation_class`) and forced-release alarms (carry
    `alarm_kind`, no violation class assigned in taxonomy yet). Stream
    entries fall back `finding_type` to `alarm_kind` so the dashboard panel
    has a non-null finding_type column for alarm rows. Sentinel findings
    have no open/closed lifecycle — they're transient fleet-state signals.
    """
    from collections import Counter, defaultdict
    from datetime import datetime, timedelta, timezone

    if now is None:
        now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)

    def _parse_ts(value):
        if not value:
            return None
        try:
            if isinstance(value, str) and value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value)
        except Exception:
            return None

    windowed = []
    for e in events:
        ts = _parse_ts(e.get("timestamp"))
        if ts is None:
            # Malformed timestamp — count toward totals but skip window check
            windowed.append((None, e))
            continue
        if ts >= window_start:
            windowed.append((ts, e))

    by_severity = Counter()
    by_class_counts = Counter()
    by_class_severity = defaultdict(Counter)

    for _ts, e in windowed:
        severity = str(e.get("severity") or "?")
        vclass = str(e.get("violation_class") or "?")
        by_severity[severity] += 1
        by_class_counts[vclass] += 1
        by_class_severity[vclass][severity] += 1

    by_violation_class = [
        {
            "violation_class": vc,
            "count": by_class_counts[vc],
            "by_severity": dict(by_class_severity[vc]),
        }
        for vc in sorted(by_class_counts, key=lambda v: (-by_class_counts[v], v))
    ]

    # Recent stream — newest first. Events with bad timestamps sort last but
    # are still included so operators can see they exist.
    def _sort_key(pair):
        ts, _ = pair
        return ts or datetime.min.replace(tzinfo=timezone.utc)

    recent_sorted = sorted(windowed, key=_sort_key, reverse=True)
    recent = [
        {
            "timestamp": e.get("timestamp"),
            "severity": e.get("severity"),
            "violation_class": e.get("violation_class"),
            # Alarm events don't carry finding_type — fall back to alarm_kind
            # so the dashboard panel doesn't show a blank cell.
            "finding_type": e.get("finding_type") or e.get("alarm_kind"),
            "message": e.get("message"),
            "agent_id": e.get("agent_id"),
            "event_id": e.get("event_id"),
        }
        for _ts, e in recent_sorted[:recent_limit]
    ]

    return {
        "total": len(windowed),
        "by_severity": dict(by_severity),
        "by_violation_class": by_violation_class,
        "recent": recent,
        "window_hours": window_hours,
        "generated_at": now.isoformat(),
    }


def _sentinel_event_from_audit(row):
    """Flatten an audit.events row into the flat event shape
    ``_sentinel_summary_from_events`` consumes.

    Sentinel findings are persisted to audit.events with the finding fields
    nested under ``details`` (see broadcaster._persist_event); the aggregator
    expects them at the top level. ``timestamp``/``agent_id``/``event_id`` are
    already top-level on the audit row.
    """
    details = row.get("details") or {}
    return {
        "timestamp": row.get("timestamp"),
        "severity": details.get("severity"),
        "violation_class": details.get("violation_class"),
        "finding_type": details.get("finding_type"),
        # Alarm rows carry alarm_kind instead of finding_type — the aggregator
        # falls one back to the other for the recent stream.
        "alarm_kind": details.get("alarm_kind"),
        "message": details.get("message"),
        "agent_id": row.get("agent_id"),
        "event_id": row.get("event_id"),
    }


async def _sentinel_events_durable(window_hours, recent_limit):
    """Read sentinel findings from the durable audit.events store.

    Returns flat events (newest-first from the DB) for the aggregator. Raises
    on DB failure so the caller can fall back to the in-memory ring.
    """
    from src.audit_db import query_audit_events_async
    start_time = (
        datetime.now(timezone.utc) - timedelta(hours=window_hours)
    ).isoformat()
    rows = await query_audit_events_async(
        event_types=list(_SENTINEL_FINDING_EVENT_TYPES),
        start_time=start_time,
        order="desc",
        limit=max(recent_limit * 4, 500),
    )
    return [_sentinel_event_from_audit(r) for r in rows]


async def http_sentinel_summary(request):
    """GET /v1/sentinel/summary — aggregate recent sentinel_finding and
    sentinel_alarm_finding events for the dashboard panel.

    Reads from the durable audit.events store (broadcaster._persist_event
    writes every finding there), so the panel survives governance-mcp
    restarts — a HIGH finding that fired before the last restart still shows
    instead of an empty 0/0/0 panel. Falls back to event_detector's in-memory
    ring buffer if the durable read fails, so the panel degrades rather than
    500s. Both fleet-analysis findings and forced-release alarms are surfaced
    together so the panel reflects the full Sentinel output stream (Surface 2
    + Surface 3 + Surface 4). The ``source`` field reports which path served
    the response."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    try:
        window_hours = int(request.query_params.get("window_hours", _SENTINEL_DEFAULT_WINDOW_HOURS))
    except ValueError:
        window_hours = _SENTINEL_DEFAULT_WINDOW_HOURS
    window_hours = max(1, min(window_hours, 24 * 30))

    try:
        recent_limit = int(request.query_params.get("limit", _SENTINEL_DEFAULT_RECENT_LIMIT))
    except ValueError:
        recent_limit = _SENTINEL_DEFAULT_RECENT_LIMIT
    recent_limit = max(1, min(recent_limit, 500))

    source = "audit_durable"
    try:
        events = await _sentinel_events_durable(window_hours, recent_limit)
    except Exception as e:
        # Durable read failed — degrade to the transient ring so the panel
        # still shows whatever's in memory rather than erroring out.
        logger.warning(f"sentinel summary: durable read failed ({e}); falling back to in-memory ring")
        source = "memory_ring"
        from src.event_detector import event_detector
        # Pre-2026-05-06 the alarm path's `type` was
        # `sentinel_forced_release_alarm` and got 400'd at the gate (#398);
        # now that it lands as `sentinel_alarm_finding`, look it up here too.
        events = list(event_detector.get_recent_events(
            event_type="sentinel_finding", limit=500,
        ))
        events.extend(event_detector.get_recent_events(
            event_type="sentinel_alarm_finding", limit=500,
        ))

    summary = _sentinel_summary_from_events(
        events, window_hours=window_hours, recent_limit=recent_limit,
    )
    summary["success"] = True
    summary["source"] = source
    return JSONResponse(summary)


async def http_record_finding(request):
    """POST /api/findings — ingest an external finding into the event ring buffer."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

        if not isinstance(payload, dict):
            return JSONResponse({"success": False, "error": "Body must be a JSON object"}, status_code=400)

        missing = [f for f in _FINDING_REQUIRED_FIELDS if not payload.get(f)]
        if missing:
            return JSONResponse(
                {"success": False, "error": f"Missing required fields: {missing}"},
                status_code=400,
            )

        if not str(payload["type"]).endswith(_FINDING_TYPE_SUFFIX):
            return JSONResponse(
                {"success": False, "error": f"type must end in {_FINDING_TYPE_SUFFIX}"},
                status_code=400,
            )

        if payload["severity"] not in _FINDING_SEVERITIES:
            return JSONResponse(
                {"success": False, "error": f"severity must be one of {sorted(_FINDING_SEVERITIES)}"},
                status_code=400,
            )

        # Evidence at ingest (bridge-dispatch proposal §4, PR #1450): forced-
        # release sentinel findings get their event check attached BEFORE
        # storage, so the durable audit record, the /api/events feed (Discord
        # bridge), and the dashboard all carry it. Client-supplied evidence is
        # stripped first — this endpoint is not operator-gated, so the check
        # must always be server-computed. Additive: failure never blocks ingest.
        payload.pop("evidence", None)
        try:
            if (str(payload["type"]).startswith("sentinel_")
                    and str(payload.get("message") or "").startswith(_FORCED_RELEASE_MESSAGE_PREFIX)):
                await _attach_forced_release_evidence([(payload, payload)])
        except Exception as ev_err:
            logger.warning(f"finding ingest event-check failed (ingest unaffected): {ev_err}")

        from src.event_detector import event_detector
        stored = event_detector.record_event(payload)
        if stored is not None:
            await broadcaster_instance.broadcast_event(
                event_type=stored["type"],
                agent_id=stored.get("agent_id"),
                payload=stored,
            )
        return JSONResponse({
            "success": True,
            "deduped": stored is None,
            "event": stored,
        })
    except Exception as e:
        logger.error(f"Error recording finding: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def http_sentinel_backlog(request):
    """GET /v1/sentinel/backlog?window_hours=168&limit=200&severity=high — durable backlog.

    Sentinel findings are durably persisted to audit.events by the broadcast
    path (broadcaster._persist_event), so the underlying record already
    survives governance-mcp restarts. What was missing is a read surface:
    /v1/sentinel/summary reads only the in-memory ring buffer (wiped on every
    restart), and /v1/incidents queries anomaly/stuck events, not findings. This
    endpoint reads the persisted finding rows so the operator can answer "did a
    HIGH finding fire that I missed across a deploy?" by query, not memory.

    Defaults to high/critical (the load-bearing findings). Pass severity=all to
    include every severity, or severity=<value> to pin one. Read-only.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        try:
            window_hours = float(request.query_params.get("window_hours", "168"))
        except (TypeError, ValueError):
            window_hours = 168.0
        window_hours = max(1.0, min(window_hours, 24 * 90))
        try:
            limit = int(request.query_params.get("limit", "200"))
        except (TypeError, ValueError):
            limit = 200
        limit = max(1, min(limit, 1000))

        severity_param = (request.query_params.get("severity") or "").strip().lower()
        if severity_param == "all":
            severity_filter = None  # no filter — every severity
        elif severity_param in _FINDING_SEVERITIES:
            severity_filter = {severity_param}
        else:
            severity_filter = _PER_FAMILY_SEVERITY_DEFAULT

        from src.audit_db import query_audit_events_async
        start_time = (
            datetime.now(timezone.utc) - timedelta(hours=window_hours)
        ).isoformat()
        # Over-fetch before the in-Python severity filter so the cap still
        # yields up to `limit` matching rows.
        events = await query_audit_events_async(
            event_types=list(_SENTINEL_FINDING_EVENT_TYPES),
            start_time=start_time,
            order="desc",
            limit=max(limit * 4, limit),
        )

        findings = []
        for e in events:
            details = e.get("details") or {}
            severity = details.get("severity")
            if severity_filter is _PER_FAMILY_SEVERITY_DEFAULT:
                allowed = _adjudicable_severities(e.get("event_type"))
            else:
                allowed = severity_filter
            if allowed is not None and severity not in allowed:
                continue
            findings.append({
                "timestamp": e.get("timestamp"),
                "severity": severity,
                "finding_type": details.get("finding_type") or details.get("alarm_kind"),
                "violation_class": details.get("violation_class"),
                "message": details.get("message"),
                "agent_id": e.get("agent_id"),
                "agent_name": details.get("agent_name"),
                "fingerprint": details.get("fingerprint"),
                "event_id": e.get("event_id"),
            })
            if len(findings) >= limit:
                break

        return JSONResponse({
            "success": True,
            "window_hours": window_hours,
            "severity": (
                "all" if severity_filter is None
                else sorted(set().union(*_ADJUDICABLE_SEVERITIES_BY_EVENT_TYPE.values(),
                                        _SENTINEL_BACKLOG_DEFAULT_SEVERITIES))
                if severity_filter is _PER_FAMILY_SEVERITY_DEFAULT
                else sorted(severity_filter)
            ),
            "count": len(findings),
            "findings": findings,
        })
    except Exception as e:
        logger.error(f"Error reading sentinel backlog: {e}")
        return JSONResponse({"success": False, "error": str(e), "findings": []}, status_code=500)


# --- Sentinel finding adjudication (dashboard widget backend) ----------------
#
# The exogenous-anchor channel (#1214) added operator adjudication of Sentinel
# findings as ONE label stratum for the EISV §6.3 falsifier. It is not "the
# ground-truth feed", which is what this comment used to say: measured
# 2026-08-28, operator adjudications are 73 of 3,090 rows on the external_signal
# channel — 2.4%. The other 97.6% is machine-checked harness outcome
# (test_passed / test_failed via the harness outcome endpoint). Stating the
# share matters because the independence question is answerable per-stratum and
# unanswerable if a 2.4% minority is described as the feed.
#
# Sensitivity, measured 2026-08-28 rather than assumed. The operator reported
# that dashboard adjudication had been performative. Recomputing the channel
# with those rows excluded (detail->>'adjudicated_via' = 'dashboard', stamped
# since #1343) moves nothing that the falsifier reads:
#
#     cohort                       n      bad   bad_days
#     all rows                     3095   735   37
#     excluding via=dashboard      3078   735   37
#
# Every dashboard row is is_bad=false, and both bad-count and bad-day are the
# gated quantities — so the delta is exactly zero and only n falls (0.55%).
# Recorded as a RESULT, not as a retraction: labels are not invalidated on a
# later report of the producer's state of mind, because a standard applied
# after the fact is not a standard. The operator's report stands as a stated
# limitation on this stratum; the measurement stands on its own.
#
# These two endpoints give the dashboard a daily queue + one-click verdicts. Cadence matters more than volume: outcomes join
# to the last prior state snapshot, so a batch sweep collapses into ONE
# statistical cluster — the queue is deliberately small (a few per day).

# Every outcome_type any queue family can produce. ⛔Must stay in sync with
# _FINDING_KIND_BY_EVENT_TYPE: a family whose outcome_type is missing here is
# adjudicated, recorded, and then handed straight back to the operator on the
# next page load, because the dedup lookup never sees its row.
_SENTINEL_ADJUDICATION_OUTCOME_TYPES = tuple(
    f"{kind}_{suffix}"
    for kind in dict.fromkeys(_FINDING_KIND_BY_EVENT_TYPE.values())
    for suffix in ("confirmed", "dismissed")
)
# Mirrors agents/common/resolution_outcome.py semantics: only "fp" is a bad
# label; the other reasons drop a finding that was still analytically right.
_ADJUDICATION_DISMISS_REASONS = ("fp", "out_of_scope", "wont_fix", "dup", "unclear", "stale")
_SENTINEL_SUBSTRATE_LABEL_PREFIX = "com.unitares.sentinel"

# --- Abstention -------------------------------------------------------------
#
# "I cannot determine this" is not a verdict, and until now there was no way to
# say it. Every path out of the queue wrote an outcome_event, and
# audit.outcome_events declares `is_bad BOOLEAN NOT NULL` (migration 004) — so
# the table is structurally incapable of recording an absence of judgement. That
# constraint is why every non-`fp` reason silently resolves to is_bad=false: not
# a policy choice, a schema floor.
#
# Note this is NOT the same thing as dismiss reason `unclear`. That reason is
# Watcher's taxonomy (agents/watcher/findings.py) and means the FINDING is
# unclear — a statement about the finding, which downstream calibration already
# excludes deliberately. Abstention is a statement about the OPERATOR: no
# judgement was formed. Collapsing the two would put "I don't know" into the
# exogenous-anchor channel wearing an external_signal label.
#
# So abstention lands in audit.events instead, and is kept out of BOTH:
#   * _SENTINEL_FINDING_EVENT_TYPES  — or the queue would re-ingest it as a finding
#   * _SENTINEL_ADJUDICATION_OUTCOME_TYPES — so _adjudication_progress() and the
#     409 dedup never see it. The anchor-day count stays exactly as it was.
# It can therefore never reach is_bad, the falsifier, or the ablation matrix.
_ADJUDICATION_ABSTAIN_EVENT_TYPE = "sentinel_adjudication_abstained"

# Suppression is a COOLDOWN, never permanent. Permanent suppression with no
# label would be an outcome_event through a side door: the finding vanishes with
# nothing on record saying a judgement was declined, which is a worse epistemic
# state than today, not a better one. A bounded window means an abstained
# finding returns for a second look while it is still inside the queue's own
# lookback (default 336h), and ages out naturally if it is never judged.
_ABSTAIN_COOLDOWN_HOURS = float(os.getenv("UNITARES_ADJUDICATION_ABSTAIN_COOLDOWN_H", "168"))


async def _abstained_sentinel_fingerprints() -> set:
    """Fingerprints an operator declined to judge, within the cooldown window.

    Read from audit.events, NOT audit.outcome_events — abstention carries no
    truth value and must never occupy a row in the anchor channel.
    """
    from src.db import get_db
    db = get_db()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT payload->>'fingerprint' AS fp
                 FROM audit.events
                WHERE event_type = $1
                  AND ts > now() - ($2 || ' hours')::interval
                  AND payload->>'fingerprint' IS NOT NULL""",
            _ADJUDICATION_ABSTAIN_EVENT_TYPE, str(_ABSTAIN_COOLDOWN_HOURS),
        )
    return {r["fp"] for r in rows if r["fp"]}


async def _adjudicated_sentinel_fingerprints() -> set:
    """Fingerprints already carrying a durable adjudication outcome (option A:
    the outcome_event IS the adjudication record; backlog rows are immutable)."""
    from src.db import get_db
    db = get_db()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT detail->>'fingerprint' AS fp
               FROM audit.outcome_events
               WHERE outcome_type = ANY($1::text[])
                 AND detail->>'fingerprint' IS NOT NULL""",
            list(_SENTINEL_ADJUDICATION_OUTCOME_TYPES),
        )
    return {r["fp"] for r in rows if r["fp"]}


def event_type_is_sentinel_family(producer_ref: Optional[str]) -> bool:
    """True for Sentinel's own producer refs.

    Sentinel writes the bare slug ``sentinel`` on its alarm/build findings and
    its UUID on ``sentinel_finding``, so the slug case still needs the
    substrate-claim lookup. Scoped to Sentinel on purpose: this is the one
    producer for which "fall back to Sentinel's UUID" is the *correct*
    attribution rather than a convenient one.
    """
    return producer_ref == "sentinel"


async def _finding_producer_uuid(
    fingerprint: str,
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """``(producer_uuid, producer_ref, event_type)`` for the newest finding with this fingerprint.

    The event_type rides along from the SAME row on purpose. It selects the
    outcome_type family, and a second query for it could read a DIFFERENT row
    if a finding lands in between -- booking one producer's outcome under
    another producer's label, which is precisely the pooling this split exists
    to prevent.

    ``build_resolution_outcome_args`` states the contract: *"agent_uuid must be
    the resident's own UUID so the handler snapshots that resident's EISV."*
    The adjudication endpoint passed Sentinel's UUID unconditionally, which is
    correct only because the queue is Sentinel-only. It is the landmine under
    any widening: the first doctor finding adjudicated would book its outcome
    against **Sentinel's** trajectory.

    Producers do not agree on what they write into ``audit.events.agent_id``.
    Measured 2026-08-15 over 14d: Sentinel's ``sentinel_finding`` (150 rows)
    and Watcher's resolution/capability findings carry real governance UUIDs,
    while ``doctor-findings`` (96), ``sentinel_alarm_finding`` (249),
    ``deploy-drift-doctor`` (20), ``lumen-checkin-doctor`` (13) and
    ``cron-unitares-dogfood-pulse`` (9) carry a stable slug instead. So this
    returns the UUID only when the row resolves to a real agent, and hands the
    raw ref back either way — the caller decides, rather than guessing.
    """
    from src.db import get_db
    db = get_db()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT e.agent_id AS ref, a.id AS resolved, e.event_type AS event_type
               FROM audit.events e
               LEFT JOIN core.agents a ON a.id = e.agent_id
               WHERE e.event_type LIKE '%\\_finding' ESCAPE '\\'
                 AND e.payload->>'fingerprint' = $1
               ORDER BY e.ts DESC
               LIMIT 1""",
            fingerprint,
        )
    if row is None:
        return None, None, None
    return row["resolved"], row["ref"], row["event_type"]


async def _sentinel_substrate_uuid() -> Optional[str]:
    """Sentinel's UUID from the operator-enrolled substrate-claims registry.

    This is deliberately NOT lookup-by-label identity resolution: the row is a
    kernel-attested, operator-enrolled claim (single writer = the operator),
    used here as configuration for which resident adjudication outcomes are
    attributed to — the same UUID the Sentinel CLI path resolves to.
    """
    from src.db import get_db
    db = get_db()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT agent_id FROM core.substrate_claims
               WHERE expected_launchd_label LIKE $1
               ORDER BY enrolled_at DESC LIMIT 1""",
            _SENTINEL_SUBSTRATE_LABEL_PREFIX + "%",
        )
    return row["agent_id"] if row else None


async def _adjudication_progress() -> dict:
    """Falsifier-progress readout: independent adjudication DAYS are what buy
    statistical power (rows sharing a prior-state snapshot are one cluster —
    day granularity is the operational proxy the widget can act on)."""
    from src.db import get_db
    db = get_db()
    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT count(*) AS outcomes,
                      count(*) FILTER (WHERE is_bad) AS bad,
                      count(DISTINCT date(ts)) AS days,
                      count(DISTINCT date(ts)) FILTER (WHERE is_bad) AS bad_days
               FROM audit.outcome_events
               WHERE verification_source = 'external_signal'
                 AND (outcome_type LIKE 'sentinel_finding_%'
                      OR outcome_type LIKE 'watcher_finding_%')""",
        )
    return {
        "outcomes": row["outcomes"], "bad": row["bad"],
        "days": row["days"], "bad_days": row["bad_days"],
        # ≥3 independent bad days among ~11 day-clusters puts the permutation
        # floor near p≈0.006 — the "quotable AUC" bar the widget tracks.
        "bad_days_target": 3,
    }


# --- Evidence at the point of verdict (bridge-dispatch proposal §4, PR #1450)
#
# Findings whose subject is a database fact get an EVENT CHECK attached to the
# queue item. Scope honesty: the lease row and the finding's source event are
# written by the SAME lease-plane transaction (Repo.release/2 updates
# surface_leases and inserts the lease_plane_events row together), so a match
# is an intra-pipeline consistency check, never independent corroboration —
# the assessment names say so. What the check genuinely adds: the lease id
# resolves, the pipeline copied fields faithfully, the hold-duration facts,
# and DETECTION LATENCY (finding emission vs event time) — the one judgment-
# relevant dimension the machine computes exactly, since late reporting is
# this poller's documented failure mode. Severity/novelty stay the operator's.
# Deterministic SQL only — the free path. Evidence is additive: it never gates
# whether a finding is shown, and enrichment failure is reported as its own
# state (``check_error``) rather than silently rendering like "no check".

_FORCED_RELEASE_MESSAGE_PREFIX = "forced release:"

# Strict UUID shape. Finding payloads are ingestible via /api/findings (bearer
# or trusted network, NOT operator-gated), so lease_id is not trustworthy: one
# malformed value in the batched ANY($1::uuid[]) cast would fail the whole
# query and cost every finding on the page its evidence. Validate per-finding.
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _assess_forced_release_row(row: Optional[dict], claimed_surface: str) -> dict:
    """Pure event-check of one forced-release claim against its lease row.

    States:
    - ``event_recorded`` — row present, surface and release_reason match the
      finding. Same-transaction provenance; not independent corroboration.
    - ``lookup_mismatch`` — row disagrees with the finding's copied fields.
      Both sides are written by one transaction, so this is almost certainly
      an evidence-side or pipeline fault, NOT proof the finding was wrong.
    - ``no_lease_row`` — no row for the claimed id. surface_leases has no
      retention and the governance DB forbids DELETE, so a forced event
      without its lease row is a lease-plane integrity fault — the one state
      here that is genuinely alarming.
    """
    if row is None:
        return {"kind": "forced_release", "assessment": "no_lease_row"}
    reason = row.get("release_reason")
    if row.get("surface_id") != claimed_surface or reason != "forced":
        return {
            "kind": "forced_release",
            "assessment": "lookup_mismatch",
            "surface_match": row.get("surface_id") == claimed_surface,
            "release_reason": reason,
        }
    ttl = row.get("original_ttl_s") or 0
    held_s = row.get("held_s")
    # held/TTL is a displayed fact, not a verdict: legitimate local_beam
    # renewers also push the ratio far past 1.0 (renew moves expires_at but
    # never acquired_at/original_ttl_s), so no threshold classifies here.
    return {
        "kind": "forced_release",
        "assessment": "event_recorded",
        "release_reason": reason,
        "held_x_ttl": round(float(held_s) / ttl, 1) if ttl and held_s is not None else None,
        "holder_pid_null": bool(row.get("holder_pid_null")),
    }


def _finding_report_latency_s(finding_ts: Optional[str], event_ts: Optional[str]) -> Optional[float]:
    """Seconds between the lease event and Sentinel reporting it, if computable."""
    try:
        emitted = datetime.fromisoformat(str(finding_ts).replace("Z", "+00:00"))
        occurred = datetime.fromisoformat(str(event_ts).replace("Z", "+00:00"))
        return max(0.0, (emitted - occurred).total_seconds())
    except (TypeError, ValueError):
        return None


async def _fetch_lease_rows(lease_ids: list) -> dict:
    """Lease rows for the event check, keyed by lease id text."""
    from src.db import get_db
    db = get_db()
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """SELECT lease_id::text AS lease_id, surface_id, release_reason,
                      holder_kind, holder_pid IS NULL AS holder_pid_null,
                      original_ttl_s,
                      EXTRACT(epoch FROM (released_at - acquired_at)) AS held_s
               FROM lease_plane.surface_leases
               WHERE lease_id = ANY($1::uuid[])""",
            lease_ids,
        )
    return {r["lease_id"]: dict(r) for r in rows}


async def _attach_forced_release_evidence(targets: list) -> None:
    """Attach event-check evidence to forced-release findings, in place.

    ``targets`` is a list of ``(queue_item, event_details)`` pairs. Findings
    carry ``lease_id`` / ``surface_id`` / ``ts`` as structured payload keys
    (both emitters set them), so no message parsing is involved.
    """
    resolvable = []
    for item, details in targets:
        raw = details.get("lease_id")
        lease_id = str(raw or "").lower()
        if _UUID_RE.match(lease_id):
            resolvable.append((item, details, lease_id))
        else:
            item["evidence"] = {
                "kind": "forced_release", "assessment": "lookup_mismatch",
                "note": "finding carries a malformed lease id" if raw else "finding carries no lease id",
            }
    if not resolvable:
        return
    try:
        rows = await _fetch_lease_rows(sorted({t[2] for t in resolvable}))
    except Exception as err:
        logger.warning(f"adjudication event-check failed (queue unaffected): {err}")
        for item, _details, _lid in resolvable:
            item["evidence"] = {"kind": "forced_release", "assessment": "check_error"}
        return
    for item, details, lease_id in resolvable:
        ev = _assess_forced_release_row(rows.get(lease_id), details.get("surface_id") or "")
        # Queue items carry the audit row's emission timestamp; at ingest the
        # finding IS being emitted now, so "now" is the honest emission time.
        latency = _finding_report_latency_s(
            item.get("timestamp") or datetime.now(timezone.utc).isoformat(),
            details.get("ts"),
        )
        if latency is not None:
            ev["report_latency_s"] = round(latency, 1)
        item["evidence"] = ev


async def http_sentinel_adjudication_queue(request):
    """GET /v1/sentinel/adjudication-queue?limit=5&window_hours=336 — the daily
    unadjudicated slice of the Sentinel backlog, plus falsifier progress."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        try:
            limit = int(request.query_params.get("limit", "5"))
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(limit, 25))
        try:
            window_hours = float(request.query_params.get("window_hours", "336"))
        except (TypeError, ValueError):
            window_hours = 336.0
        window_hours = max(1.0, min(window_hours, 24 * 90))

        from src.audit_db import query_audit_events_async
        start_time = (
            datetime.now(timezone.utc) - timedelta(hours=window_hours)
        ).isoformat()
        events = await query_audit_events_async(
            event_types=list(_SENTINEL_FINDING_EVENT_TYPES),
            start_time=start_time,
            order="desc",
            limit=1000,
        )
        adjudicated = await _adjudicated_sentinel_fingerprints()
        # Two different exclusions, deliberately not merged: `adjudicated` is
        # permanent and drives the 409; `abstained` expires and does not.
        abstained = await _abstained_sentinel_fingerprints()

        seen: set = set()
        queue = []
        pending_total = 0
        abstained_suppressed = 0
        evidence_targets = []
        for e in events:
            details = e.get("details") or {}
            severity = details.get("severity")
            if severity not in _adjudicable_severities(e.get("event_type")):
                continue
            fp = details.get("fingerprint")
            if not fp or fp in seen:
                continue
            seen.add(fp)
            if fp in adjudicated:
                continue
            # Suppressed, not resolved. Counted and reported rather than hidden:
            # an operator must be able to see that declined items exist, or the
            # cooldown becomes a silent backlog.
            if fp in abstained:
                abstained_suppressed += 1
                continue
            pending_total += 1
            if len(queue) < limit:
                item = {
                    "timestamp": e.get("timestamp"),
                    "severity": severity,
                    "finding_type": details.get("finding_type") or details.get("alarm_kind"),
                    "violation_class": details.get("violation_class"),
                    "message": details.get("message"),
                    "agent_name": details.get("agent_name"),
                    "fingerprint": fp,
                }
                queue.append(item)
                if str(details.get("message") or "").startswith(_FORCED_RELEASE_MESSAGE_PREFIX):
                    evidence_targets.append((item, details))

        try:
            await _attach_forced_release_evidence(evidence_targets)
        except Exception as ev_err:
            logger.warning(f"adjudication evidence enrichment failed (queue unaffected): {ev_err}")

        return JSONResponse({
            "success": True,
            "window_hours": window_hours,
            "queue": queue,
            "pending_total": pending_total,
            "dismiss_reasons": list(_ADJUDICATION_DISMISS_REASONS),
            # Declined items are reported, never silently dropped. Without this
            # the cooldown would read as "queue is clear" when it is not.
            "abstained_suppressed": abstained_suppressed,
            "abstain_cooldown_hours": _ABSTAIN_COOLDOWN_HOURS,
            "progress": await _adjudication_progress(),
        })
    except Exception as e:
        logger.error(f"Error building adjudication queue: {e}")
        return JSONResponse({"success": False, "error": str(e), "queue": []}, status_code=500)


async def http_sentinel_adjudicate(request):
    """POST /v1/sentinel/adjudicate {fingerprint, status, reason?} — operator-gated.

    Records the operator verdict as an external-truth outcome_event attributed
    to Sentinel's substrate UUID (same shared builder + semantics as the CLI
    path in agents/sentinel/agent.py::adjudicate_finding). Idempotent per
    fingerprint: a second verdict returns 409 rather than double-counting a
    label the falsifier would read twice.
    """
    signals = access._build_http_session_signals(request)
    from src.mcp_handlers.identity.operator import is_operator_caller
    if not is_operator_caller(signals) and not dashboard_session_write_authorized(request):
        return JSONResponse(
            {"success": False,
             "error": "operator credential or passkey session with X-Unitares-Csrf: 1 required"},
            status_code=403,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "error": "invalid JSON body"}, status_code=400)

    fingerprint = str(body.get("fingerprint") or "").strip()
    status = str(body.get("status") or "").strip().lower()
    reason = (str(body.get("reason") or "").strip().lower() or None)
    if not fingerprint:
        return JSONResponse({"success": False, "error": "fingerprint required"}, status_code=400)
    if status not in ("confirmed", "dismissed", "abstain"):
        return JSONResponse(
            {"success": False,
             "error": "status must be 'confirmed', 'dismissed' or 'abstain'"},
            status_code=400,
        )
    if status == "dismissed" and reason not in _ADJUDICATION_DISMISS_REASONS:
        return JSONResponse(
            {"success": False,
             "error": f"dismissal needs a reason: {', '.join(_ADJUDICATION_DISMISS_REASONS)}"},
            status_code=400,
        )

    # Abstention returns before any outcome_event is built. Declining to judge
    # must cost nothing and record nothing in the anchor channel; the only
    # durable trace is an audit event that suppresses the item for a cooldown.
    if status == "abstain":
        try:
            if fingerprint in await _adjudicated_sentinel_fingerprints():
                return JSONResponse(
                    {"success": False, "error": "already adjudicated",
                     "fingerprint": fingerprint},
                    status_code=409,
                )
            import uuid as _uuid
            from src.db import get_db
            from src.db.base import AuditEvent
            await get_db().append_audit_event(AuditEvent(
                ts=datetime.now(timezone.utc),
                event_id=str(_uuid.uuid4()),
                event_type=_ADJUDICATION_ABSTAIN_EVENT_TYPE,
                payload={
                    "fingerprint": fingerprint,
                    "reason": reason,
                    "adjudicated_via": "dashboard",
                    # Stated on the row so a later reader cannot mistake this
                    # for a verdict that merely lacks a label.
                    "note": ("operator declined to judge; NOT a verdict and NOT "
                             "an exogenous-truth label"),
                },
            ))
            return JSONResponse({
                "success": True,
                "fingerprint": fingerprint,
                "status": "abstain",
                "recorded_outcome": False,
                "suppressed_for_hours": _ABSTAIN_COOLDOWN_HOURS,
            })
        except Exception as e:
            logger.error(f"Error recording abstention for {fingerprint}: {e}")
            return JSONResponse({"success": False, "error": str(e)}, status_code=500)

    try:
        if fingerprint in await _adjudicated_sentinel_fingerprints():
            return JSONResponse(
                {"success": False, "error": "already adjudicated", "fingerprint": fingerprint},
                status_code=409,
            )
        # Attribute to the PRODUCER, not to Sentinel. Falling back to Sentinel
        # for its own families keeps today's behaviour byte-identical (its
        # alarm rows carry the slug 'sentinel', not a UUID) while removing the
        # mis-attribution that would fire on the first non-Sentinel finding.
        producer_uuid, producer_ref, event_type = await _finding_producer_uuid(fingerprint)
        if not producer_uuid and event_type_is_sentinel_family(producer_ref):
            producer_uuid = await _sentinel_substrate_uuid()
            if not producer_uuid:
                # Distinct from the 422 below: Sentinel IS the right producer
                # here and simply is not enrolled. That is a configuration
                # problem on this deployment, not a bad request.
                return JSONResponse(
                    {"success": False,
                     "error": "no enrolled Sentinel substrate claim — cannot attribute outcome"},
                    status_code=503,
                )
        if not producer_uuid:
            # Refuse rather than book it against the wrong resident. A silent
            # wrong attribution corrupts the anchor channel the falsifiability
            # test depends on; an honest 422 names the missing piece.
            return JSONResponse(
                {"success": False,
                 "error": (
                     "cannot attribute outcome: finding producer "
                     f"{producer_ref!r} has no governance identity. Adjudicating "
                     "it would book the outcome against another resident's EISV."
                 ),
                 "fingerprint": fingerprint,
                 "producer": producer_ref},
                status_code=422,
            )

        from agents.common.resolution_outcome import build_resolution_outcome_args
        # Per-family outcome_type. This lands together with the dedup set that
        # filters on these exact strings (_SENTINEL_ADJUDICATION_OUTCOME_TYPES
        # is now derived from the same map), which is the ordering the previous
        # hardcoded "sentinel_finding" was holding the line for.
        #
        # ⛔_adjudication_progress() is deliberately NOT widened here. It reads
        # the EISV falsifier's anchor-day count -- an externally quoted figure --
        # and whether a doctor adjudication is anchor evidence of the same grade
        # as a Sentinel one is an open operator call, not a side effect of
        # closing the doctor loop. Doctor findings become closable now; they do
        # not silently inflate that number.
        finding_kind = _FINDING_KIND_BY_EVENT_TYPE.get(event_type, _DEFAULT_FINDING_KIND)
        args = build_resolution_outcome_args(
            finding_kind, status, fingerprint, producer_uuid, reason
        )
        args["detail"]["producer_ref"] = producer_ref
        args["detail"]["adjudicated_via"] = "dashboard"

        from src.mcp_handlers.observability.outcome_events import _record_outcome_event_inline
        await _record_outcome_event_inline(args)
        return JSONResponse({
            "success": True,
            "fingerprint": fingerprint,
            "outcome_type": args["outcome_type"],
            "is_bad": args["is_bad"],
            "progress": await _adjudication_progress(),
        })
    except Exception as e:
        logger.error(f"Error recording adjudication for {fingerprint}: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

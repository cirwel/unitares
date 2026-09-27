"""The monitoring resident's read surfaces: GET /v1/sentinel/{summary,backlog}.

Reference-resident code, not core: these routes mount only when the
server's UNITARES_ROUTE_PACKS names the `reference-residents` pack
(src/http_routes/packs.py imports this module lazily). Finding INTAKE, which
every producer uses, stays in core at src/http_routes/findings.py. (Moved
from src/http_routes/sentinel.py on 2026-09-27.)
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from starlette.responses import JSONResponse

from src.http_routes import access
from src.http_routes.findings import _FINDING_SEVERITIES
from src.logging_utils import get_logger

logger = get_logger(__name__)


# Finding event types as persisted in audit.events (the durable store behind
# the transient ring buffer). The backlog endpoint reads these.
_SENTINEL_FINDING_EVENT_TYPES = (
    "sentinel_finding", "sentinel_alarm_finding", "doctor_check_finding",
)

# Default severities the operator cares about when reviewing "did I miss
# something across restarts?" — the load-bearing findings.
_SENTINEL_BACKLOG_DEFAULT_SEVERITIES = frozenset({"high", "critical"})

# Default backlog severities per family. Severity vocabularies are NOT shared
# across producers: Sentinel emits medium/high/info and defaults to
# {high, critical} (its `medium` alone was 834 distinct fingerprints over 30d),
# while the doctor layer emits only `warning`, so its family defaults wider.
_ADJUDICABLE_SEVERITIES_BY_EVENT_TYPE = {
    "doctor_check_finding": frozenset({"warning", "high", "critical"}),
}


def _adjudicable_severities(event_type):
    """Default backlog severities for this finding family.

    (Name kept from the retired adjudication queue, which used the same table;
    the doctor script mirrors it by this name.)"""
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

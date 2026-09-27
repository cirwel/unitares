"""Sentinel surfaces: summary, finding intake and backlog.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from starlette.responses import JSONResponse


from src.logging_utils import get_logger
from src.broadcaster import broadcaster_instance

from src.http_routes import access

logger = get_logger(__name__)


# Allowed severity values for externally posted findings
_FINDING_SEVERITIES = frozenset({"info", "low", "medium", "warning", "high", "critical"})
# Only accept *_finding event types via this endpoint (prevents spoofing
# reserved dashboard event types like verdict_change / risk_threshold)
_FINDING_TYPE_SUFFIX = "_finding"
# Required top-level fields on the posted JSON
_FINDING_REQUIRED_FIELDS = ("type", "severity", "message", "agent_id", "agent_name", "fingerprint")
# One bound for every stored fingerprint. An over-long fingerprint is
# NORMALIZED at ingest, never rejected: producers post findings best-effort and
# swallow errors, so a 400 would lose the finding silently — the failure the
# finding stream exists to catch. The digest is deterministic, so dedup and
# every later lookup by fingerprint still agree; the original is kept (capped).
_FINGERPRINT_MAX_CHARS = 256
_FINGERPRINT_ORIGINAL_MAX_CHARS = 4096


def _bounded_fingerprint(raw: str) -> tuple[str, Optional[str]]:
    """``(fingerprint to store, original if it had to be replaced)``."""
    if len(raw) <= _FINGERPRINT_MAX_CHARS:
        return raw, None
    digest = "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return digest, raw[:_FINGERPRINT_ORIGINAL_MAX_CHARS]


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

        fingerprint, original = _bounded_fingerprint(str(payload["fingerprint"]))
        payload["fingerprint"] = fingerprint
        if original is not None:
            payload["fingerprint_original"] = original
        else:
            # Only this route may set it; never trust a client-supplied one.
            payload.pop("fingerprint_original", None)

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



# --- Evidence on ingested findings (bridge-dispatch proposal §4, PR #1450)
#
# Findings whose subject is a database fact get an EVENT CHECK attached at
# ingest (/api/findings). Scope honesty: the lease row and the finding's source
# event are written by the SAME lease-plane transaction (Repo.release/2 updates
# surface_leases and inserts the lease_plane_events row together), so a match
# is an intra-pipeline consistency check, never independent corroboration —
# the assessment names say so. What the check genuinely adds: the lease id
# resolves, the pipeline copied fields faithfully, the hold-duration facts,
# and DETECTION LATENCY (finding emission vs event time) — the one judgment-
# relevant dimension the machine computes exactly, since late reporting is
# this poller's documented failure mode. Deterministic SQL only — the free
# path. Evidence is additive: it never gates whether a finding is recorded,
# and enrichment failure is reported as its own state (``check_error``) rather
# than silently rendering like "no check".
#
# (Operator and model adjudication of these findings were removed 2026-09-27:
# the queue, its verdict routes and the model adjudicator. Operator verdicts
# were 2.4% of the external_signal channel, all is_bad=false, and excluding
# them moved nothing the EISV falsifier reads. Historical outcome rows stay.)

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

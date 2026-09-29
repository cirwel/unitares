"""Finding intake: POST /api/findings, and the evidence attached at ingest.

Any producer (a resident, a doctor check, a host hook) posts findings here;
they are persisted to audit.events and broadcast. This is core: it does not
depend on which residents a deployment runs. The monitoring resident's own
read surfaces (summary, backlog) live with it in agents/sentinel/routes.py and
mount only with the reference-residents route pack. (Split out of
src/http_routes/sentinel.py on 2026-09-27; registered in src/http_api.py.)
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
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

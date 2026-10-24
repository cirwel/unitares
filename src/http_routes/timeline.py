"""GET /v1/timeline — every agent's governance events in one time window.

One read for a per-agent timeline view: check-ins, graded outcomes, lifecycle
changes (pauses, resumes, archives), knowledge writes, declared lineage and
dialectic sessions, merged and sorted oldest first, plus a lookup of the agents
they name. Read-only.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from starlette.responses import JSONResponse

from src.http_routes import access
from src.http_routes.telemetry import _LIFECYCLE_EVENT_TYPES
from src.logging_utils import get_logger

logger = get_logger(__name__)

TIMELINE_KINDS = ("checkin", "outcome", "lifecycle", "knowledge", "lineage", "dialectic")
_SUPPORTED_PARAMS = {
    "hours", "agent_id", "kinds", "limit", "include_substrate", "include_trajectory",
}
_DEFAULT_HOURS = 6
_MAX_HOURS = 168
_DEFAULT_LIMIT = 500
_MAX_LIMIT = 2000
_SUMMARY_CHARS = 280

# Check-ins split by who wrote them. `agent_report` is the agent's own
# sync_state; the substrate classes are hook-recorded observations and
# interpretations of the agent, not claims it made.
_SUBSTRATE_CLASSES = ("substrate_observation", "substrate_interpretation")

# `trajectory_validated` is the server re-scoring its own trajectory estimate
# at nearly every check-in (~100k rows a week on a busy deployment). It is
# not an outcome of the agent's work, so it is left out unless asked for.
_TRAJECTORY_OUTCOME = "trajectory_validated"


def _flag(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _clamped_int(value: str | None, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value) if value is not None else default
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _payload(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _float_or_none(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _iso(ts):
    return ts.isoformat() if ts is not None else None


def _authored_by(epistemic_class: str | None) -> str:
    if epistemic_class == "agent_report":
        return "agent"
    if epistemic_class in _SUBSTRATE_CLASSES:
        return "substrate"
    return "unknown"


async def http_timeline(request):
    """GET /v1/timeline — merged per-agent events for one time window.

    Query params:
      - hours: lookback window (default 6, 1–168)
      - agent_id: one agent's UUID; lineage rows naming it as parent and
        dialectic sessions naming it as reviewer are included too
      - kinds: comma list from checkin, outcome, lifecycle, knowledge,
        lineage, dialectic (default all)
      - limit: newest rows kept PER KIND (default 500, max 2000);
        `truncated` names each kind that hit it
      - include_substrate: hook-recorded check-ins as well as the agent's
        own (default true)
      - include_trajectory: the server's `trajectory_validated` outcomes
        (default false)

    Unknown parameters and kinds are a 400, never a silently wider read.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    params = request.query_params
    unknown = sorted(set(params.keys()) - _SUPPORTED_PARAMS)
    if unknown:
        return JSONResponse({
            "success": False,
            "error": f"Unsupported parameter(s): {', '.join(unknown)}",
            "safe_options": sorted(_SUPPORTED_PARAMS),
        }, status_code=400)

    raw_kinds = params.get("kinds")
    if raw_kinds:
        kinds = [k.strip() for k in raw_kinds.split(",") if k.strip()]
        bad = sorted(set(kinds) - set(TIMELINE_KINDS))
        if bad or not kinds:
            return JSONResponse({
                "success": False,
                "error": f"Unknown kind(s): {', '.join(bad) or '(none given)'}",
                "safe_options": list(TIMELINE_KINDS),
            }, status_code=400)
    else:
        kinds = list(TIMELINE_KINDS)

    hours = _clamped_int(params.get("hours"), _DEFAULT_HOURS, 1, _MAX_HOURS)
    limit = _clamped_int(params.get("limit"), _DEFAULT_LIMIT, 1, _MAX_LIMIT)
    include_substrate = _flag(params.get("include_substrate"), True)
    include_trajectory = _flag(params.get("include_trajectory"), False)
    agent_id = (params.get("agent_id") or "").strip() or None

    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=hours)
    events: list[dict] = []
    truncated: list[str] = []

    def _keep(rows, kind):
        # Each source is read with limit + 1 so a kind with exactly `limit`
        # rows is not reported as truncated.
        if len(rows) > limit:
            truncated.append(kind)
            return rows[:limit]
        return rows

    try:
        # Each table's read lives with its owner component
        # (scripts/dev/check_component_boundaries.py, rule B4).
        from src.audit_db import fetch_timeline_audit_events
        from src.db import get_db
        from src.db.mixins.identity import (
            fetch_identity_ids_for_agent,
            fetch_timeline_identities,
        )
        from src.db.mixins.state import fetch_timeline_checkins
        from src.db.mixins.tool_usage import fetch_timeline_outcomes
        from src.dialectic_db import fetch_timeline_dialectic_sessions
        db = get_db()
        async with db.acquire() as conn:
            # Check-ins are keyed by identity row; their agent ids are filled
            # in from the identity lookup at the end.
            checkin_events: list[tuple[int, dict]] = []
            if "checkin" in kinds:
                classes = ["agent_report"] + (list(_SUBSTRATE_CLASSES) if include_substrate else [])
                identity_ids = None
                if agent_id is not None:
                    identity_ids = await fetch_identity_ids_for_agent(conn, agent_id)
                rows = []
                if identity_ids is None or identity_ids:
                    rows = await fetch_timeline_checkins(
                        conn, start=start, identity_ids=identity_ids,
                        classes=classes, limit=limit + 1,
                    )
                rows = _keep(rows, "checkin")
                for r in rows:
                    event = {
                        "kind": "checkin",
                        "ts": _iso(r["recorded_at"]),
                        "agent_id": None,
                        "authored_by": _authored_by(r["epistemic_class"]),
                        "epistemic_class": r["epistemic_class"],
                        "action": r["action"],
                        "verdict": r["verdict"],
                        "risk_score": _float_or_none(r["risk_score"]),
                    }
                    checkin_events.append((r["identity_id"], event))
                    events.append(event)

            if "outcome" in kinds:
                rows = await fetch_timeline_outcomes(
                    conn, start=start, agent_id=agent_id,
                    exclude_type=None if include_trajectory else _TRAJECTORY_OUTCOME,
                    limit=limit + 1,
                )
                rows = _keep(rows, "outcome")
                for r in rows:
                    events.append({
                        "kind": "outcome",
                        "ts": _iso(r["ts"]),
                        "agent_id": r["agent_id"],
                        "outcome_id": str(r["outcome_id"]),
                        "outcome_type": r["outcome_type"],
                        "outcome_score": _float_or_none(r["outcome_score"]),
                        "is_bad": bool(r["is_bad"]),
                        "verification_source": r["verification_source"],
                        "reported_confidence": _float_or_none(r["reported_confidence"]),
                        "prediction_id": r["prediction_id"],
                        "decision_action": r["decision_action"],
                    })

            audit_kinds = {
                "lifecycle": list(_LIFECYCLE_EVENT_TYPES),
                "knowledge": ["knowledge_write"],
                "lineage": ["lineage_declared"],
            }
            for kind, event_types in audit_kinds.items():
                if kind not in kinds:
                    continue
                # A lineage row's subject is the child; the parent it names
                # belongs on the parent's timeline too. Other kinds match
                # their subject only.
                rows = await fetch_timeline_audit_events(
                    conn, start=start, agent_id=agent_id, event_types=event_types,
                    match_parent=kind == "lineage", limit=limit + 1,
                )
                rows = _keep(rows, kind)
                for r in rows:
                    payload = _payload(r["payload"])
                    event = {
                        "kind": kind,
                        "ts": _iso(r["ts"]),
                        "agent_id": r["agent_id"],
                        "event_id": str(r["event_id"]),
                    }
                    if kind == "lifecycle":
                        event["event_type"] = r["event_type"]
                        event["reason"] = payload.get("reason")
                    elif kind == "knowledge":
                        summary = payload.get("summary")
                        if isinstance(summary, str) and len(summary) > _SUMMARY_CHARS:
                            summary = summary[: _SUMMARY_CHARS - 1] + "…"
                        event["discovery_id"] = payload.get("discovery_id")
                        event["discovery_type"] = payload.get("discovery_type")
                        event["summary"] = summary
                    else:
                        event["parent_agent_id"] = payload.get("parent_id")
                        event["successor_class"] = payload.get("successor_class")
                    events.append(event)

            if "dialectic" in kinds:
                # A session active in the window can have opened before it,
                # so `ts` (its creation) may precede window.start; a view
                # clamps the band to its axis.
                rows = await fetch_timeline_dialectic_sessions(
                    conn, start=start, agent_id=agent_id, limit=limit + 1,
                )
                rows = _keep(rows, "dialectic")
                for r in rows:
                    open_session = r["status"] == "active"
                    events.append({
                        "kind": "dialectic",
                        "ts": _iso(r["created_at"]),
                        "end": None if open_session else _iso(r["updated_at"]),
                        "agent_id": r["paused_agent_id"],
                        "reviewer_agent_id": r["reviewer_agent_id"],
                        "session_id": r["session_id"],
                        "status": r["status"],
                        "phase": r["phase"],
                        "session_type": r["session_type"],
                    })

            named = set()
            for e in events:
                for key in ("agent_id", "parent_agent_id", "reviewer_agent_id"):
                    if e.get(key):
                        named.add(e[key])
            checkin_identities = sorted({identity_id for identity_id, _ in checkin_events})
            agents = []
            if named or checkin_identities:
                rows = await fetch_timeline_identities(
                    conn, identity_ids=checkin_identities, agent_ids=sorted(named),
                )
                by_identity = {r["identity_id"]: r["agent_id"] for r in rows}
                for identity_id, event in checkin_events:
                    event["agent_id"] = by_identity.get(identity_id)
                agents = [{
                    "agent_id": r["agent_id"],
                    "label": r["label"],
                    "model_type": r["model_type"],
                    "trust_tier": r["trust_tier"],
                    "status": r["status"],
                    "parent_agent_id": r["parent_agent_id"],
                    "spawn_reason": r["spawn_reason"],
                    "created_at": _iso(r["created_at"]),
                    "last_activity_at": _iso(r["last_activity_at"]),
                } for r in rows]
    except Exception as e:
        logger.error(f"timeline query failed: {e}")
        return JSONResponse({"success": False, "error": "query failed"}, status_code=500)

    events.sort(key=lambda e: e["ts"] or "")
    counts = {kind: 0 for kind in kinds}
    for e in events:
        counts[e["kind"]] += 1
    return JSONResponse({
        "success": True,
        "window": {"start": _iso(start), "end": _iso(end), "hours": hours},
        "kinds": kinds,
        "include_substrate": include_substrate,
        "include_trajectory": include_trajectory,
        "limit_per_kind": limit,
        "truncated": truncated,
        "counts": counts,
        "agents": sorted(agents, key=lambda a: a["agent_id"]),
        "events": events,
    })

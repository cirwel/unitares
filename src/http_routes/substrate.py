"""Substrate/runtime observation intake, bridge events, dark sessions,
and harness outcome recording.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import os

from starlette.responses import JSONResponse


from src.logging_utils import get_logger
from src.dashboard_auth import (
    dashboard_session_write_authorized,
)

from src.http_routes import access

logger = get_logger(__name__)


async def http_record_bridge_event(request):
    """POST /v1/bridge/events — ingest Discord bridge delivery/attention receipts."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"success": False, "error": "Invalid JSON"}, status_code=400)

        from src.bridge_events import BridgeEventError, record_bridge_event

        try:
            result = await record_bridge_event(payload)
        except BridgeEventError as exc:
            return JSONResponse({"success": False, "error": str(exc)}, status_code=400)

        if not result.get("persisted"):
            return JSONResponse(
                {
                    "success": False,
                    "error": "Bridge event could not be persisted",
                    "event": result.get("event"),
                },
                status_code=503,
            )
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Error recording bridge event: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def http_bridge_summary(request):
    """GET /v1/bridge/summary — summarize Discord delivery and attention state."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        from src.bridge_events import BridgeEventError, build_bridge_summary

        include_events_raw = request.query_params.get("include_events")
        include_events = True
        if include_events_raw is not None:
            include_events = include_events_raw.lower() not in {"0", "false", "no"}

        try:
            result = await build_bridge_summary(
                {
                    "since": request.query_params.get("since"),
                    "until": request.query_params.get("until"),
                    "limit": request.query_params.get("limit"),
                    "include_events": include_events,
                }
            )
        except BridgeEventError as exc:
            return JSONResponse({"success": False, "error": str(exc)}, status_code=400)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"Error building bridge summary: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def http_substrate_observe(request):
    """POST /v1/substrate/observe — the identity-free check-in FLOOR.

    A row here is a MEASUREMENT that a session ran but never onboarded — not a
    claim that an agent declared an identity. This endpoint deliberately does
    NOT go through the identity middleware: there is no sticky-cache resolution,
    no auto-mint, no agent row. The caller's `slot_key` (the Claude session id,
    sourced from the Stop-hook stdin) is stored verbatim as the disambiguator,
    so parallel localhost sessions never collapse onto one identity. The
    transport fingerprint is measured server-side for collision awareness only.

    Nothing in the trajectory/trust/calibration/EISV/similarity path reads
    core.substrate_observations. It exists to turn the silent 0 into a number.

    Body: {"slot_key": "...", "event"?, "tool_count"?, "summary_excerpt"?,
           "plugin_version"?}
    """
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

        slot_key = payload.get("slot_key")
        if not isinstance(slot_key, str) or not slot_key.strip():
            return JSONResponse(
                {"success": False, "error": "Missing or invalid 'slot_key'"},
                status_code=400,
            )
        slot_key = slot_key.strip()[:256]
        event = str(payload.get("event") or "turn_stop")[:64]
        try:
            tool_count = int(payload.get("tool_count") or 0)
        except (TypeError, ValueError):
            tool_count = 0
        tool_count = max(0, min(tool_count, 100000))
        summary = payload.get("summary_excerpt")
        summary = (str(summary)[:512] if summary is not None else None)
        plugin_version = payload.get("plugin_version")
        plugin_version = (str(plugin_version)[:64] if plugin_version is not None else None)

        # Measure the transport fingerprint server-side (collision awareness only).
        try:
            fingerprint = access._build_http_session_signals(request).ip_ua_fingerprint
        except Exception:
            fingerprint = None

        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO core.substrate_observations
                    (slot_key, fingerprint, event, tool_count, summary_excerpt, plugin_version)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                slot_key, fingerprint, event, tool_count, summary, plugin_version,
            )
        return JSONResponse({"success": True}, status_code=201)
    except Exception as e:
        logger.error(f"Error recording substrate observation: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def http_runtime_observe(request):
    """POST /v1/runtime/observe — identity-bound host evidence.

    Unlike ``/v1/substrate/observe`` (the identity-free dark-session floor),
    this route requires an active ``client_session_id`` whose durable binding
    matches ``agent_uuid``. The ``runtime`` path is retained for compatibility;
    accepted rows live in ``audit.events`` and never prove continuous agent
    runtime or create an EISV/state update.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(
                {"success": False, "error": "Invalid JSON"}, status_code=400
            )

        from src.runtime_observations import (
            RuntimeObservationError,
            record_runtime_observation,
        )

        try:
            result = await record_runtime_observation(payload)
        except RuntimeObservationError as exc:
            return JSONResponse(
                {"success": False, "error": str(exc), "code": exc.code},
                status_code=exc.status_code,
            )
        return JSONResponse(result, status_code=201)
    except Exception as e:
        logger.error(f"Error recording runtime observation: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def http_runtime_activity(request):
    """GET /v1/runtime/activity — host and state-update clocks, separated.

    The host side is derived only from identity-bound hook observations. The
    state side distinguishes ``agent_report`` rows from automatic substrate
    interpretations, synthetic initialization, and legacy unclassified rows.
    A hook-parent heartbeat never marks an agent or slot as active.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        try:
            window_hours = float(request.query_params.get("window_hours", "24"))
        except (TypeError, ValueError):
            return JSONResponse(
                {"success": False, "error": "'window_hours' must be numeric"},
                status_code=400,
            )
        try:
            limit = int(request.query_params.get("limit", "1000"))
        except (TypeError, ValueError):
            return JSONResponse(
                {"success": False, "error": "'limit' must be an integer"},
                status_code=400,
            )
        window_hours = max(0.1, min(window_hours, 24 * 90))
        limit = max(1, min(limit, 5000))

        from src.runtime_observations import read_runtime_activity

        return JSONResponse(
            await read_runtime_activity(window_hours=window_hours, limit=limit)
        )
    except Exception as e:
        logger.error(f"Error reading runtime activity: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


async def http_substrate_dark_sessions(request):
    """GET /v1/substrate/dark_sessions?window_hours=24 — the coverage-gap dial.

    Counts the floor: distinct session slots that produced substrate
    observations in the window (sessions that ran but never onboarded), plus
    the raw observation count. This is the measured form of the old silent 0 —
    a compliance-gap metric, NOT a coverage success. `adoption_kpi.py` must
    keep it separate from real check-in coverage.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        try:
            window_hours = float(request.query_params.get("window_hours", "24"))
        except (TypeError, ValueError):
            window_hours = 24.0
        window_hours = max(0.1, min(window_hours, 24 * 90))

        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT COUNT(DISTINCT slot_key) AS distinct_slots,
                       COUNT(*)                 AS total_observations,
                       COUNT(DISTINCT slot_key) FILTER (WHERE claimed_by_uuid IS NULL) AS unclaimed_slots
                FROM core.substrate_observations
                WHERE observed_at >= now() - make_interval(secs => $1)
                """,
                window_hours * 3600.0,
            )
        return JSONResponse({
            "success": True,
            "window_hours": window_hours,
            "dark_sessions": int(row["distinct_slots"]) if row else 0,
            "unclaimed_sessions": int(row["unclaimed_slots"]) if row else 0,
            "total_observations": int(row["total_observations"]) if row else 0,
        })
    except Exception as e:
        logger.error(f"Error reading substrate dark sessions: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

# Provenance values the harness outcome endpoint will accept — mirrors the
# outcome_event schema Literal (src/mcp_handlers/schemas/core.py).
_HARNESS_OUTCOME_VERIFICATION_SOURCES = frozenset({
    "agent_reported_tool_result",
    "server_observation",
    "external_signal",
})


async def http_harness_outcome(request):
    """POST /v1/harness/outcome {agent_uuid, outcome_type, ...} — operator-gated.

    Sanctioned delivery path for harness-side observers (e.g. the PostToolUse
    outcome-tracker hook) to record outcomes attributed to a governed agent
    (#1345). The REST tool-call path deliberately refuses a cross-fingerprint
    ``client_session_id`` echo (hijack-guard fail-closed, #1325), which
    orphaned hook delivery; this endpoint accepts explicit attribution under
    the operator credential instead — the same trust model as
    /v1/sentinel/adjudicate. Attribution is operator-asserted: the server
    records the row against ``agent_uuid`` as given and does not attempt
    session resolution, so the caller owns pointing at the right identity.

    Validation visibility (#1790): a producer with no prediction of its own,
    such as a test hook, sends no ``confidence``. The server then scrapes one
    and stamps the row ``calibration_excluded`` so calibration does not train
    on it. Since 2026-09-02 the non-protocol validation instruments keep such
    rows as evidence under their default ``corrected`` fixture rule; registered
    reads run the rule their protocol fixed, and the stop rule's own read drops
    them. The row is recorded either way, with its reason.
    Inventing a confidence to avoid the flag would be the scraped-confidence
    defect moved client-side. ``prediction_id`` is deliberately NOT accepted
    here: this endpoint takes an operator-asserted ``agent_uuid`` with no
    work or session correlation, so forwarding an id would let any open
    prediction of that agent be bound to an unrelated outcome and train
    calibration on it (the laundering PR #1445 removed). An agent with a
    registered prediction records its outcome through ``record_result`` on
    its own bound session. The response carries ``calibration_excluded`` and
    a ``validation_visibility`` note when the flag was stamped.
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

    import uuid as _uuid_mod

    from src.mcp_handlers.observability.outcome_events import VALID_OUTCOME_TYPES

    agent_uuid = str(body.get("agent_uuid") or "").strip()
    outcome_type = str(body.get("outcome_type") or "").strip()
    if not agent_uuid:
        return JSONResponse({"success": False, "error": "agent_uuid required"}, status_code=400)
    try:
        _uuid_mod.UUID(agent_uuid)
    except ValueError:
        return JSONResponse(
            {"success": False, "error": "agent_uuid must be a UUID"}, status_code=400
        )
    if outcome_type not in VALID_OUTCOME_TYPES:
        return JSONResponse(
            {"success": False,
             "error": f"outcome_type must be one of {sorted(VALID_OUTCOME_TYPES)}"},
            status_code=400,
        )

    verification_source = str(
        body.get("verification_source") or "agent_reported_tool_result"
    ).strip()
    if verification_source not in _HARNESS_OUTCOME_VERIFICATION_SOURCES:
        return JSONResponse(
            {"success": False,
             "error": "verification_source must be one of "
                      f"{sorted(_HARNESS_OUTCOME_VERIFICATION_SOURCES)}"},
            status_code=400,
        )

    detail = body.get("detail") or {}
    if not isinstance(detail, dict):
        return JSONResponse(
            {"success": False, "error": "detail must be an object"}, status_code=400
        )
    detail = dict(detail)
    detail["recorded_via"] = "harness_outcome_endpoint"

    args: dict = {
        "agent_id": agent_uuid,
        "outcome_type": outcome_type,
        "verification_source": verification_source,
        "detail": detail,
    }

    confidence = body.get("confidence")
    if confidence is not None:
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            return JSONResponse(
                {"success": False, "error": "confidence must be a number"}, status_code=400
            )
        if not 0.0 <= confidence <= 1.0:
            return JSONResponse(
                {"success": False, "error": "confidence must be in [0, 1]"}, status_code=400
            )
        args["confidence"] = confidence

    # `prediction_id` in the body is ignored on purpose; see the docstring.

    if body.get("is_bad") is not None:
        args["is_bad"] = bool(body["is_bad"])
    if body.get("outcome_score") is not None:
        try:
            args["outcome_score"] = float(body["outcome_score"])
        except (TypeError, ValueError):
            return JSONResponse(
                {"success": False, "error": "outcome_score must be a number"}, status_code=400
            )
    session_id = str(body.get("session_id") or "").strip()
    if session_id:
        args["session_id"] = session_id

    try:
        from src.mcp_handlers.observability.outcome_events import _record_outcome_event_inline
        payload = await _record_outcome_event_inline(args)
        if "error" in payload:
            return JSONResponse(
                {"success": False, "error": payload["error"]}, status_code=500
            )
        resp = {
            "success": True,
            "outcome_id": payload.get("outcome_id"),
            "outcome_type": outcome_type,
            "agent_uuid": agent_uuid,
            "is_bad": payload.get("is_bad"),
            "corroboration_grade": payload.get("corroboration_grade"),
            "evidence_weight": payload.get("evidence_weight"),
            "agent_state_found": payload.get("eisv_snapshot") is not None,
        }
        # #1790: recording succeeds either way, but a calibration_excluded row
        # is invisible to the validation inventory (the fixture classifiers
        # treat the flag as fixture traffic). A producer wired without a
        # caller-supplied confidence would silently accumulate rows that no
        # analysis surface can see — warn at the only moment the caller is
        # listening.
        if payload.get("calibration_excluded"):
            resp["calibration_excluded"] = True
            if payload.get("prediction_source") in (
                "prev_confidence_fallback", "audit_trail_fallback",
            ):
                resp["validation_visibility"] = (
                    "calibration_excluded: no caller-supplied confidence, so the "
                    f"server scraped one ({payload.get('prediction_source')}); "
                    "calibration will not train on this row. Validation instruments "
                    "keep it as evidence under their default corrected fixture rule; "
                    "registered reads run the rule their protocol fixed. The reason "
                    "is recorded. Do not "
                    "invent a confidence to avoid this flag; an agent with a "
                    "registered prediction records through record_result instead."
                )
            else:
                resp["validation_visibility"] = (
                    "excluded from validation inventory: the row carries a "
                    "fixture/shadow marker (see detail.calibration_excluded)."
                )
        return JSONResponse(resp)
    except Exception as e:
        logger.error(f"Error recording harness outcome for {agent_uuid}: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)

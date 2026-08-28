"""Registry and overview feeds for dashboards: agent history, tier
distribution, automations, activity, incidents,
violation taxonomy, and silent-bootstrap observability.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import os

from starlette.responses import JSONResponse


from src.logging_utils import get_logger
from src.broadcaster import broadcaster_instance

from src.http_routes import access

logger = get_logger(__name__)


async def http_agent_history(request):
    """GET /v1/agents/{agent_id}/history — an agent's EISV state trajectory.

    Reads core.agent_state (append-only measured observations, indexed by
    identity_id/recorded_at). E lives in state_json, the rest are columns.
    Returns oldest→newest points so the chart reads left-to-right. Synthetic
    bootstrap rows are excluded, and agent-authored reports stay explicitly
    separated from automatic substrate interpretations.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    agent_id = request.path_params.get("agent_id", "")
    try:
        limit = int(request.query_params.get("limit", "80"))
    except (TypeError, ValueError):
        limit = 80
    limit = max(2, min(limit, 400))
    # Context-aware: 'recent' = the last `limit` raw check-ins (event-by-event);
    # 'all' = ~`limit` real check-ins sampled evenly across the agent's whole
    # lifespan (decimation, not averaging — every point is a real check-in).
    mode = "all" if request.query_params.get("mode") == "all" else "recent"
    include_telemetry = str(
        request.query_params.get("include_telemetry", "")
    ).strip().lower() in ("1", "true", "yes")
    try:
        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            # Identity resolution: check-in history is keyed by the agent's UUID
            # identity, but the agent list may show the structured id (mcp_DATE_<8hex>)
            # whose suffix is that UUID's prefix. Match exact id OR the UUID identity
            # whose prefix == the structured id's trailing 8 hex. The numbered CTE
            # carries the total so the panel can tell how much history exists.
            rows = await conn.fetch(
                """
                WITH ids AS (
                    SELECT identity_id FROM core.identities WHERE agent_id = $1
                    UNION
                    SELECT identity_id FROM core.identities
                     WHERE substring($1 from '([0-9a-f]{8})$') IS NOT NULL
                       AND agent_id ~ '^[0-9a-f]{8}-'
                       AND agent_id LIKE substring($1 from '([0-9a-f]{8})$') || '%'
                ),
                numbered AS (
                    SELECT s.recorded_at,
                           (s.state_json->>'E')::real AS e,
                           s.integrity AS i, s.entropy AS s_entropy, s.volatility AS v,
                           s.coherence, s.risk_score, s.state_json,
                           coalesce(s.epistemic_class,
                                    s.state_json->>'epistemic_class') AS epistemic_class,
                           (jsonb_typeof(s.state_json->'eisv_telemetry') = 'object')
                               AS telemetry_available,
                           row_number() OVER (ORDER BY s.recorded_at) AS rn,
                           count(*) OVER () AS total,
                           count(*) FILTER (
                               WHERE coalesce(s.epistemic_class,
                                              s.state_json->>'epistemic_class') = 'agent_report'
                           ) OVER () AS agent_report_total,
                           count(*) FILTER (
                               WHERE coalesce(s.epistemic_class,
                                              s.state_json->>'epistemic_class')
                                     IN ('substrate_observation', 'substrate_interpretation')
                           ) OVER () AS substrate_total,
                           count(*) FILTER (
                               WHERE jsonb_typeof(s.state_json->'eisv_telemetry') = 'object'
                           ) OVER () AS telemetry_total
                    FROM core.agent_state s
                    WHERE s.identity_id IN (SELECT identity_id FROM ids) AND s.synthetic = false
                )
                SELECT recorded_at, e, i, s_entropy, v, coherence, risk_score,
                       state_json, epistemic_class, telemetry_available, total,
                       agent_report_total, substrate_total, telemetry_total
                FROM numbered
                WHERE CASE WHEN $3 = 'all'
                           THEN (rn % GREATEST(1, (total / $2)::int) = 0 OR rn = 1 OR rn = total)
                           ELSE rn > total - $2
                      END
                ORDER BY recorded_at
                """,
                agent_id, limit, mode,
            )
        total = rows[0]["total"] if rows else 0
        agent_report_total = rows[0]["agent_report_total"] if rows else 0
        substrate_total = rows[0]["substrate_total"] if rows else 0
        telemetry_total = rows[0]["telemetry_total"] if rows else 0
        from src.eisv_telemetry import summarize_state_eisv_telemetry
        points = []
        for r in rows:
            state_json = r["state_json"] if isinstance(r["state_json"], dict) else {}
            point = {
                "t": r["recorded_at"].isoformat(),
                "E": r["e"], "I": r["i"], "S": r["s_entropy"], "V": r["v"],
                "coherence": r["coherence"], "risk": r["risk_score"],
                # The governance action and EISV verdict tier paired with this
                # row's risk. Both are persisted into state_json by
                # record_agent_state, so no extra column or join is needed.
                # `action` is the decision vocabulary ('approve' | 'guide' |
                # 'cirs_block' | 'risk_pause' | 'reject'); `verdict` is the risk
                # tier ('safe' | 'caution' | 'high-risk'). Rows written before
                # the action-write landed carry neither, so both may be null —
                # consumers must not read a missing action as 'approve'.
                #
                # A hard action recorded here is a verdict the policy PRODUCED.
                # It is not evidence that an intervention was delivered:
                # gap-suppression downgrades pauses to proceed at any >150s
                # inter-check-in gap. See /v1/enforcement/divergence.
                "action": state_json.get("action"),
                "verdict": state_json.get("verdict"),
                "epistemic_class": r["epistemic_class"],
                "telemetry_available": bool(r["telemetry_available"]),
                "telemetry": summarize_state_eisv_telemetry(state_json),
            }
            if include_telemetry and isinstance(state_json.get("eisv_telemetry"), dict):
                point["telemetry_envelope"] = state_json["eisv_telemetry"]
            points.append(point)
        return JSONResponse({
            "success": True,
            "agent_id": agent_id,
            "mode": mode,
            "count": len(points),
            "total": total,
            "observation_summary": {
                "state_rows": total,
                "agent_reports": agent_report_total,
                "substrate_rows": substrate_total,
                "other_rows": max(0, total - agent_report_total - substrate_total),
                "telemetry_envelopes": telemetry_total,
            },
            "telemetry_included": include_telemetry,
            "points": points,
        })
    except Exception as exc:  # noqa: BLE001 — read-only panel endpoint, degrade gracefully
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)


async def http_automations(request):
    """GET /api/automations — automation census snapshot for the dashboard.

    Reads the snapshot written by `unitares-automations census --write` (path
    overridable via UNITARES_AUTOMATION_CENSUS_PATH). Read-only and does NOT
    shell out — freshness comes from the snapshot's mtime, surfaced as
    snapshot_age_seconds / stale so the panel can flag a stale census.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    import json as _json
    import time as _time
    default_path = os.path.expanduser("~/.local/state/unitares-automations/last.json")
    snapshot_path = os.getenv("UNITARES_AUTOMATION_CENSUS_PATH", default_path)
    if not os.path.exists(snapshot_path):
        return JSONResponse({
            "schema": "unitares.automation_census.v1",
            "summary": {"total": 0, "by_source": {}, "by_kind": {}, "needs_attention": [], "warnings": []},
            "automations": [],
            "snapshot_path": snapshot_path,
            "snapshot_age_seconds": None,
            "stale": True,
            "warnings": ["census snapshot missing — run `unitares-automations census --write`"],
        })
    try:
        with open(snapshot_path) as fh:
            data = _json.load(fh)
        age = int(_time.time() - os.path.getmtime(snapshot_path))
        data["snapshot_path"] = snapshot_path
        data["snapshot_age_seconds"] = age
        data["stale"] = age > 86400  # older than 24h

        # Opt-in summary view. The Overview card reads four things — the summary
        # block, `stale`, and a COUNT of ungated entries — while the full census
        # is ~206 KB of per-automation detail (228 items on 2026-08-28) that only
        # the Automations tab renders. Measured: 205,933 B down to ~641 B of
        # actually-consumed fields, 99.7% of that response discarded on the
        # DEFAULT page, on every load. Fast on loopback, not over a tunnel.
        #
        # The ungated count is computed HERE rather than shipping notes arrays,
        # because counting is the only thing the caller does with them. Default
        # response shape is unchanged for the Automations tab.
        # getattr: `view` is optional and absent on the normal path, so reading
        # it must not be able to fail the request. A minimal request object with
        # no query_params is a legitimate caller shape, and turning that into a
        # 500 would make an opt-in projection a liability for every existing
        # consumer of the default response.
        _qp = getattr(request, "query_params", None) or {}
        if str(_qp.get("view", "") or "").strip().lower() == "summary":
            items = data.get("automations") or []

            # Classify the SAME way sections/automations.js::gateClass does, so
            # the Overview card and the Automations tab cannot disagree about
            # how an automation is grounded. Explicit `gate:` note wins; else
            # github-actions and claude are machine-gated by construction; else
            # UNCLASSIFIED — meaning no determination exists, not that one was
            # made and came back clean.
            #
            # The previous version counted only explicit `gate:ungated` notes.
            # Nothing writes that marker: measured 2026-08-28, 0 of 228 carried
            # it while 221 carried no gate note at all. So the card reported
            # "0 ungated" permanently — an unfair zero, reassuring precisely
            # where its own comment says it exists to surface risk ("ungated =
            # nothing verifies it"). Counting an absent marker is not a
            # measurement of safety, it is a measurement of the marker.
            def _gate(it):
                for n in (it.get("notes") or []):
                    if isinstance(n, str) and n.startswith("gate:"):
                        return n[5:]
                if it.get("source") in ("github-actions", "claude"):
                    return "machine"
                return "unclassified"

            gates: dict[str, int] = {}
            for it in items:
                g = _gate(it)
                gates[g] = gates.get(g, 0) + 1
            ungated = gates.get("ungated", 0)
            unclassified = gates.get("unclassified", 0)
            return JSONResponse({
                "schema": data.get("schema"),
                "summary": data.get("summary"),
                "ungated": ungated,
                # The honest headline. `ungated` stays for continuity, but it is
                # an explicit-marker count and reads 0 on every real deployment.
                "unclassified": unclassified,
                "gates": gates,
                "generated_at": data.get("generated_at"),
                "snapshot_age_seconds": age,
                "stale": data["stale"],
                "view": "summary",
            })
        return JSONResponse(data)
    except Exception as exc:  # noqa: BLE001 — read-only panel endpoint, degrade gracefully
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)


async def http_tier_distribution(request):
    """GET /v1/agents/tier_distribution — full-fleet trust-tier counts.

    trust_tier is computed per agent (resolve_trust_tier), but the last-computed
    value is cached in core.identities.metadata->'trust_tier'. One GROUP BY gives
    the true distribution across all identities cheaply, without recomputing
    per agent. Identities with no cached tier (never earned one) fold to unknown.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT COALESCE(NULLIF(metadata->'trust_tier'->>'name', ''), 'unknown') AS tier,
                       count(*) AS n
                FROM core.identities
                GROUP BY 1
                """
            )
        order = ["verified", "established", "emerging", "provisional", "unknown"]
        tiers = {t: 0 for t in order}
        for r in rows:
            t = r["tier"] if r["tier"] in tiers else "unknown"
            tiers[t] += int(r["n"])
        total = sum(tiers.values())
        return JSONResponse({
            "success": True,
            "tiers": tiers,
            "total": total,
            "earned": total - tiers["unknown"],
        })
    except Exception as exc:  # noqa: BLE001 — read-only panel endpoint, degrade gracefully
        return JSONResponse({"success": False, "error": str(exc)}, status_code=500)


async def http_bootstrap_silent(request):
    """GET /v1/bootstrap/silent — agents bootstrapped past N hours with no real check-in.

    Validation surface for onboard-bootstrap-checkin §6 (population
    observability). The proposal exists to count exactly this population:
    agents with a synthetic t=0 anchor but no measured trajectory.

    Query params:
      min_age_hours (int, default 24): skip recently-bootstrapped agents
                                       that may genuinely be about to check in.
      limit (int, default 50, max 200): cap the returned list.

    Returns:
      {success, count, min_age_hours, agents: [{agent_id, identity_id,
        bootstrap_state_id, bootstrap_recorded_at, bootstrap_age_hours,
        display_name}]}
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    try:
        min_age_hours = int(request.query_params.get("min_age_hours", 24))
    except (TypeError, ValueError):
        min_age_hours = 24
    min_age_hours = max(0, min_age_hours)

    try:
        limit = int(request.query_params.get("limit", 50))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    try:
        from src.db import get_db
        db = get_db()
        count = await db.count_bootstrap_only_agents(min_age_hours=min_age_hours)
        rows = await db.list_bootstrap_only_agents(
            min_age_hours=min_age_hours, limit=limit,
        )
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": f"bootstrap_silent query failed: {e}"},
            status_code=500,
        )

    # Datetimes need to be JSON-serializable.
    def _norm(row):
        out = dict(row)
        ts = out.get("bootstrap_recorded_at")
        if ts is not None and hasattr(ts, "isoformat"):
            out["bootstrap_recorded_at"] = ts.isoformat()
        age = out.get("bootstrap_age_hours")
        if age is not None:
            out["bootstrap_age_hours"] = round(float(age), 3)
        return out

    return JSONResponse({
        "success": True,
        "count": count,
        "min_age_hours": min_age_hours,
        "limit": limit,
        "returned": len(rows),
        "agents": [_norm(r) for r in rows],
    })


# Incident history endpoint (anomalies + stuck agents from audit log)
async def http_incidents(request):
    """Return historical anomaly and stuck-agent incidents from the audit trail."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()
    try:
        from src.audit_db import query_audit_events_async

        event_type = request.query_params.get("type")  # "anomaly_detected" or "stuck_detected"
        limit = min(int(request.query_params.get("limit", 200)), 500)

        # Query both types if none specified
        types_to_query = [event_type] if event_type else ["anomaly_detected", "stuck_detected"]
        all_events = []
        for et in types_to_query:
            events = await query_audit_events_async(event_type=et, order="desc", limit=limit)
            all_events.extend(events)

        # Sort by timestamp descending, limit total
        all_events.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
        all_events = all_events[:limit]

        return JSONResponse({"success": True, "incidents": all_events, "count": len(all_events)})
    except Exception as e:
        logger.error(f"Error fetching incidents: {e}")
        return JSONResponse({"success": False, "error": str(e), "incidents": []}, status_code=500)


# Activity sparkline endpoint
async def http_activity(request):
    """Return check-in activity buckets for sparkline chart."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    try:
        window = int(request.query_params.get("window", 60))
        bucket = int(request.query_params.get("bucket", 5))
        # Clamp to reasonable limits
        window = max(10, min(window, 360))
        bucket = max(1, min(bucket, 30))
        buckets = broadcaster_instance.get_activity_buckets(
            window_minutes=window, bucket_minutes=bucket
        )
        return JSONResponse({
            "success": True,
            "buckets": buckets,
            "window_minutes": window,
            "bucket_minutes": bucket
        })
    except Exception as e:
        logger.error(f"Error fetching activity: {e}")
        return JSONResponse({
            "success": False,
            "error": str(e),
            "buckets": []
        }, status_code=500)


# ---------------------------------------------------------------------------
# Violation taxonomy endpoint — surface vocabulary for dashboards/bridges
# ---------------------------------------------------------------------------


async def http_taxonomy(request):
    """Return the violation taxonomy + reverse-lookup index as JSON.

    Lets the dashboard (and any other consumer) classify Watcher findings,
    Sentinel findings, and broadcast events into violation classes
    (CON / INT / ENT / REC / BEH / VOI) without having to ship its own copy
    of the YAML.

    Response shape::

        {
            "success": true,
            "version": 1,
            "classes": [{ "id": "INT", "name": "Integrity", ... }, ...],
            "reverse": {
                "watcher_patterns": {"P010": "INT", "P011": "INT", ...},
                "sentinel_findings": {"coordinated_degradation": "CON", ...},
                "broadcast_events": {"identity_drift": "CON", ...}
            }
        }

    Best-effort: if the taxonomy file is missing or malformed, returns a
    success=false response with an empty taxonomy rather than 500. The
    dashboard renders fine without classification — class badges just
    don't appear.
    """
    if not access._check_http_auth(request, http_api_token=os.getenv("UNITARES_HTTP_API_TOKEN")):
        return access._http_unauthorized()

    try:
        from src import violation_taxonomy as taxonomy_mod
        data = taxonomy_mod.load_taxonomy()
        # Build reverse index (taxonomy.py keeps it private; reconstruct here
        # so we don't depend on its internal _get_reverse implementation).
        reverse: dict = {
            "watcher_patterns": {},
            "sentinel_findings": {},
            "broadcast_events": {},
        }
        for cls in data.get("classes", []):
            cid = cls["id"]
            for kind in reverse:
                for sid in cls.get("surfaces", {}).get(kind, []):
                    reverse[kind][sid] = cid
        return JSONResponse({
            "success": True,
            "version": data.get("version"),
            "classes": data.get("classes", []),
            "reverse": reverse,
        })
    except Exception as exc:
        logger.warning("http_taxonomy failed: %s", exc)
        return JSONResponse({
            "success": False,
            "error": str(exc),
            "classes": [],
            "reverse": {
                "watcher_patterns": {},
                "sentinel_findings": {},
                "broadcast_events": {},
            },
        })

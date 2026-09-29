"""GET /api/automations — this deployment's automation census, as a route.

Reference-ops code, next to the `unitares-automations` census tool in this
directory: it serves the snapshot that tool writes. It is not part of the
installed server package. The governance server imports this file by path
only when UNITARES_ROUTE_PACKS names the `automation-census` pack
(src/http_routes/packs.py). (Moved from src/http_routes/overview.py on
2026-09-27.)
"""

from __future__ import annotations

import os

from starlette.responses import JSONResponse

from src.http_routes import access


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
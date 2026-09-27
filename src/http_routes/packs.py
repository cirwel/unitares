"""Opt-in route packs: HTTP routes that belong to one kind of deployment.

The core server mounts only routes any install can use. Some routes exist
because a deployment runs something specific: the reference residents that
ship under ``agents/`` (their summaries and finding backlog), or a local
automation census. An install that runs none
of those would otherwise carry endpoints that answer for things it does not
have. Those routes live here, grouped into packs, and are mounted only when
``UNITARES_ROUTE_PACKS`` names the pack (comma-separated). Unset, which is the
shipped default, mounts none.

Packs are named for what they serve, never for a resident: which residents a
deployment runs is configuration, and shipped source does not branch on a
resident name (see the fleet-neutrality rules in AGENTS.md).

The handlers themselves are unchanged and stay where they were; only their
registration moved. Every route here is credential-gated like the rest of
the REST surface (tests/test_http_route_auth_parity.py reads this module).
"""

from __future__ import annotations

import os

from starlette.routing import Route

from src.http_routes.overview import http_automations
from src.http_routes.sentinel import (
    http_sentinel_backlog,
    http_sentinel_summary,
)
from src.http_routes.vigil import http_vigil_summary
from src.http_routes.watcher import http_watcher_summary

ENV_VAR = "UNITARES_ROUTE_PACKS"


def pack_routes() -> dict[str, list[Route]]:
    """Every pack and the routes it mounts."""
    return {
        # The reference residents' read surfaces. Consumers are this
        # deployment's own tooling: the monitoring resident's backlog calls and
        # a dashboard extension.
        "reference-residents": [
            Route("/v1/sentinel/backlog", http_sentinel_backlog, methods=["GET"]),
            Route("/v1/sentinel/summary", http_sentinel_summary, methods=["GET"]),
            Route("/v1/watcher/summary", http_watcher_summary, methods=["GET"]),
            Route("/v1/vigil/summary", http_vigil_summary, methods=["GET"]),
        ],
        # A census of one machine's scheduled jobs, read from a snapshot file
        # the `unitares-automations` CLI writes (UNITARES_AUTOMATION_CENSUS_PATH).
        "automation-census": [
            Route("/api/automations", http_automations, methods=["GET"]),
        ],
    }


def enabled_packs(raw: str | None = None) -> list[str]:
    """Pack names from UNITARES_ROUTE_PACKS, in order, deduplicated.

    Unknown names are returned too, so the caller can report them: a typo
    should say so at startup, not silently mount nothing.
    """
    value = os.getenv(ENV_VAR, "") if raw is None else raw
    out: list[str] = []
    for part in value.split(","):
        name = part.strip()
        if name and name not in out:
            out.append(name)
    return out


def register_route_packs(app, *, logger=None, raw: str | None = None) -> list[str]:
    """Append the enabled packs' routes to ``app``; return the mounted names."""
    packs = pack_routes()
    mounted: list[str] = []
    for name in enabled_packs(raw):
        routes = packs.get(name)
        if routes is None:
            if logger is not None:
                logger.warning(
                    "%s names unknown route pack %r (known: %s)",
                    ENV_VAR, name, ", ".join(sorted(packs)),
                )
            continue
        app.routes.extend(routes)
        mounted.append(name)
    if logger is not None and mounted:
        logger.info("Route packs mounted: %s", ", ".join(mounted))
    return mounted

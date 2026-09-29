"""Opt-in route packs: HTTP routes that belong to one kind of deployment.

The core server mounts only routes any install can use. Some routes exist
because a deployment runs something specific: the reference residents that
ship under ``agents/`` (their summaries and finding backlog), or a local
automation census. Their code lives next to what they serve, not in ``src/``:
``agents/<resident>/routes.py`` and ``scripts/ops/automation_census_route.py``.
This module names each pack's routes and imports that code only when
``UNITARES_ROUTE_PACKS`` names the pack (comma-separated). Unset, which is the
shipped default, imports and mounts none.

Packs are named for what they serve, never for a resident: which residents a
deployment runs is configuration, and shipped source does not branch on a
resident name (see the fleet-neutrality rules in AGENTS.md).

Every pack route is credential-gated like the rest of the REST surface
(tests/test_http_route_auth_parity.py resolves this table and checks each
handler).
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
from typing import Callable

from starlette.routing import Route

ENV_VAR = "UNITARES_ROUTE_PACKS"
_REPO_ROOT = Path(__file__).resolve().parents[2]

# pack -> [(path, "module:attr" or "relative/file.py:attr", methods)]
PACK_SPECS: dict[str, list[tuple[str, str, list[str]]]] = {
    # The reference residents' read surfaces. Consumers are this deployment's
    # own tooling: the monitoring resident's backlog calls and a dashboard
    # extension.
    "reference-residents": [
        ("/v1/sentinel/backlog", "agents.sentinel.routes:http_sentinel_backlog", ["GET"]),
        ("/v1/sentinel/summary", "agents.sentinel.routes:http_sentinel_summary", ["GET"]),
        ("/v1/watcher/summary", "agents.watcher.routes:http_watcher_summary", ["GET"]),
        ("/v1/vigil/summary", "agents.vigil.routes:http_vigil_summary", ["GET"]),
    ],
    # A census of one machine's scheduled jobs, read from a snapshot file the
    # `unitares-automations` CLI writes (UNITARES_AUTOMATION_CENSUS_PATH). The
    # route file sits beside that tool in scripts/ops/, outside the package.
    "automation-census": [
        ("/api/automations", "scripts/ops/automation_census_route.py:http_automations", ["GET"]),
    ],
}


def resolve_handler(ref: str) -> Callable:
    """Import ``module:attr`` or ``relative/path.py:attr`` (from the repo root)."""
    target, attr = ref.rsplit(":", 1)
    if target.endswith(".py"):
        path = _REPO_ROOT / target
        spec = importlib.util.spec_from_file_location(
            "unitares_route_pack_" + path.stem, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    return getattr(module, attr)


def pack_routes(names=None) -> dict[str, list[Route]]:
    """Routes for the named packs (default: every pack), handlers imported."""
    wanted = PACK_SPECS if names is None else {n: PACK_SPECS[n] for n in names if n in PACK_SPECS}
    return {
        name: [Route(path, resolve_handler(ref), methods=methods) for path, ref, methods in specs]
        for name, specs in wanted.items()
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
    """Append the enabled packs' routes to ``app``; return the mounted names.

    A pack whose code cannot be imported (e.g. scripts/ops is absent from a
    container image) is logged and skipped; the server still starts.
    """
    mounted: list[str] = []
    for name in enabled_packs(raw):
        if name not in PACK_SPECS:
            if logger is not None:
                logger.warning(
                    "%s names unknown route pack %r (known: %s)",
                    ENV_VAR, name, ", ".join(sorted(PACK_SPECS)),
                )
            continue
        try:
            routes = pack_routes([name])[name]
        except Exception as exc:  # noqa: BLE001 — a missing pack must not stop the server
            if logger is not None:
                logger.error("route pack %r could not be loaded: %s", name, exc)
            continue
        app.routes.extend(routes)
        mounted.append(name)
    if logger is not None and mounted:
        logger.info("Route packs mounted: %s", ", ".join(mounted))
    return mounted

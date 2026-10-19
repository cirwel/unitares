"""Load explicitly configured HTTP extensions without a built-in fleet registry.

UNITARES_ROUTE_PACKS selects pack names. UNITARES_ROUTE_PACKS_FILE names a
deployment-owned JSON manifest describing those packs and their installed
handlers. Neither setting is enabled by default. A manifest is trusted local
deployment configuration, never request input: its handlers execute server code.
Every mounted handler passes the existing REST credential gate before execution.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import sys
from typing import Callable

from starlette.routing import Route

ENV_VAR = "UNITARES_ROUTE_PACKS"
MANIFEST_ENV = "UNITARES_ROUTE_PACKS_FILE"
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


def load_pack_specs(manifest: str | Path | None = None) -> dict:
    """Read and validate the complete manifest before importing any handler."""
    selected = manifest if manifest is not None else os.getenv(MANIFEST_ENV, "")
    if not selected or not str(selected).strip():
        return {}
    path = Path(selected).expanduser().resolve()
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or document.get("schema") != "unitares.route-packs.v1":
        raise ValueError("route-pack manifest needs schema unitares.route-packs.v1")
    packs = document.get("packs")
    if not isinstance(packs, dict):
        raise ValueError("route-pack manifest needs a packs object")
    result = {}
    for name, entries in packs.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError("invalid route-pack name")
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"route pack {name!r} needs a nonempty route list")
        routes = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "handler", "methods"}:
                raise ValueError(f"invalid route specification in {name!r}")
            route, ref, methods = entry["path"], entry["handler"], entry["methods"]
            if not isinstance(route, str) or not route.startswith("/") or any(c.isspace() for c in route):
                raise ValueError(f"invalid route path in {name!r}")
            if not isinstance(ref, str) or ":" not in ref:
                raise ValueError(f"invalid handler reference in {name!r}")
            target, attr = ref.rsplit(":", 1)
            if not target or not attr.isidentifier():
                raise ValueError(f"invalid handler reference in {name!r}")
            if target.endswith(".py"):
                target = str((path.parent / target).resolve())
            elif not all(part.isidentifier() for part in target.split(".")):
                raise ValueError(f"invalid handler module in {name!r}")
            if (not isinstance(methods, list) or not methods
                    or any(not isinstance(m, str) or m not in _METHODS for m in methods)):
                raise ValueError(f"invalid methods in {name!r}")
            routes.append((route, f"{target}:{attr}", list(dict.fromkeys(methods))))
        result[name] = routes
    return result


def resolve_handler(ref: str) -> Callable:
    """Import an installed module or a manifest-resolved Python file."""
    target, attr = ref.rsplit(":", 1)
    if target.endswith(".py"):
        path = Path(target)
        key = hashlib.sha256(str(path).encode()).hexdigest()[:16]
        name = f"unitares_route_pack_{key}"
        module = sys.modules.get(name)
        if module is None:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load {path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(name, None)
                raise
    else:
        module = importlib.import_module(target)
    handler = getattr(module, attr)
    if not inspect.iscoroutinefunction(handler):
        raise TypeError(f"route-pack handler must be an async function: {ref}")
    return handler


def _credential_gated(handler: Callable) -> Callable:
    async def endpoint(request):
        from src.http_routes import access

        if not access._check_http_auth(request, http_api_token=os.getenv("UNITARES_HTTP_API_TOKEN")):
            return access._http_unauthorized()
        return await handler(request)

    return endpoint


def pack_routes(names=None, *, specs=None) -> dict[str, list[Route]]:
    """Resolve selected packs from an explicit manifest, with the REST gate."""
    available = load_pack_specs() if specs is None else specs
    wanted = available if names is None else {n: available[n] for n in names if n in available}
    return {
        name: [Route(path, _credential_gated(resolve_handler(ref)), methods=methods)
               for path, ref, methods in entries]
        for name, entries in wanted.items()
    }


def enabled_packs(raw: str | None = None) -> list[str]:
    """Selected pack names in order, deduplicated; unset selects none."""
    value = os.getenv(ENV_VAR, "") if raw is None else raw
    return list(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))


def register_route_packs(app, *, logger=None, raw: str | None = None) -> list[str]:
    """Mount configured extensions; report unavailable packs without mounting them."""
    names = enabled_packs(raw)
    if not names:
        return []
    try:
        specs = load_pack_specs()
    except (OSError, ValueError) as exc:
        if logger is not None:
            logger.error("route-pack manifest could not be loaded: %s", exc)
        return []
    mounted = []
    occupied = {(r.path, method) for r in app.routes if isinstance(r, Route)
                for method in (r.methods or ())}
    for name in names:
        if name not in specs:
            if logger is not None:
                logger.warning("%s names unknown route pack %r; configure %s", ENV_VAR, name, MANIFEST_ENV)
            continue
        try:
            routes = pack_routes([name], specs=specs)[name]
            proposed = [(r.path, method) for r in routes for method in (r.methods or ())]
            if len(set(proposed)) != len(proposed) or occupied.intersection(proposed):
                raise ValueError("extension routes collide with an existing route")
        except Exception as exc:  # A missing optional extension does not stop core startup.
            if logger is not None:
                logger.error("route pack %r could not be loaded: %s", name, exc)
            continue
        app.routes.extend(routes)
        occupied.update(proposed)
        mounted.append(name)
    if logger is not None and mounted:
        logger.info("Route packs mounted: %s", ", ".join(mounted))
    return mounted

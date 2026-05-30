"""Wave 3a admin surface — PR #3 of v0.2 sequencing.

Spec: ``docs/proposals/beam-wave-3a-read-only-handlers.md`` v0.2 §3.1
(rollback shape). The script at ``scripts/ops/wave-3a-rollback.sh`` is the
operator-facing surface; this module is the HTTP backend the script calls.

Routes mounted under ``/v1/admin/wave3a/``:

- ``GET  /v1/admin/wave3a/routing-table``        — list current routes (op-gated)
- ``POST /v1/admin/wave3a/routing-table``        — add/update one route (op-gated)
- ``DELETE /v1/admin/wave3a/routing-table``      — clear ALL routes (op-gated)
- ``DELETE /v1/admin/wave3a/routing-table/<tool>`` — drop one route (op-gated)

Auth: ``X-Unitares-Operator`` header validated against
``UNITARES_OPERATOR_TOKENS`` per ``src/mcp_handlers/identity/operator.py``.
Missing/wrong → 401. The operator-token path is the established admin
gate; we reuse it rather than minting yet another token surface.

Fail-closed: if the MCP server is unreachable, the rollback script falls
back to writing the empty-routing-table state by exiting non-zero with a
diagnostic — the operator can manually restart the MCP, which itself
starts with an empty table per the §3.1 invariant.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Set

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src import wave3a_routing

logger = logging.getLogger(__name__)


WAVE3A_ADMIN_PREFIX = "/v1/admin/wave3a"
OPERATOR_HEADER = "x-unitares-operator"
OPERATOR_TOKENS_ENV = "UNITARES_OPERATOR_TOKENS"


# ---------------------------------------------------------------------------
# Operator gate (mirrors src/mcp_handlers/identity/operator.py)
# ---------------------------------------------------------------------------


def _allowlisted_operator_tokens() -> Set[str]:
    """Read fresh each request so operators can rotate without restart."""
    raw = os.environ.get(OPERATOR_TOKENS_ENV, "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def _is_operator(request: Request) -> bool:
    presented = request.headers.get(OPERATOR_HEADER) or request.headers.get(
        OPERATOR_HEADER.title()
    )
    if not presented:
        return False
    allowlist = _allowlisted_operator_tokens()
    if not allowlist:
        return False
    return presented in allowlist


def _denied_response() -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "permission_denied",
            "reason": "operator token missing or invalid",
        },
        status_code=401,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _list_routes(request: Request) -> JSONResponse:
    if not _is_operator(request):
        return _denied_response()
    routes = wave3a_routing.list_routes()
    return JSONResponse(
        {"ok": True, "routes": routes, "count": len(routes)}, status_code=200
    )


async def _set_route(request: Request) -> JSONResponse:
    if not _is_operator(request):
        return _denied_response()
    try:
        body: Dict[str, Any] = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "schema_invalid", "reason": "body not JSON"},
            status_code=400,
        )
    tool_name = body.get("tool_name")
    beam_url = body.get("beam_url")
    if not isinstance(tool_name, str) or not tool_name:
        return JSONResponse(
            {
                "ok": False,
                "error": "schema_invalid",
                "reason": "tool_name required",
            },
            status_code=400,
        )
    if not isinstance(beam_url, str) or not beam_url:
        return JSONResponse(
            {
                "ok": False,
                "error": "schema_invalid",
                "reason": "beam_url required",
            },
            status_code=400,
        )
    try:
        wave3a_routing.set_route(tool_name, beam_url)
    except ValueError as exc:
        return JSONResponse(
            {"ok": False, "error": "schema_invalid", "reason": str(exc)},
            status_code=400,
        )
    return JSONResponse(
        {"ok": True, "tool_name": tool_name, "beam_url": beam_url}, status_code=200
    )


async def _clear_routes(request: Request) -> JSONResponse:
    if not _is_operator(request):
        return _denied_response()
    removed = wave3a_routing.clear_routes()
    return JSONResponse(
        {"ok": True, "removed": removed, "mode": "all"}, status_code=200
    )


async def _delete_route(request: Request) -> JSONResponse:
    if not _is_operator(request):
        return _denied_response()
    tool_name = request.path_params.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        return JSONResponse(
            {
                "ok": False,
                "error": "schema_invalid",
                "reason": "tool_name path segment required",
            },
            status_code=400,
        )
    removed = wave3a_routing.remove_route(tool_name)
    return JSONResponse(
        {"ok": True, "tool_name": tool_name, "removed": removed}, status_code=200
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_wave3a_admin_routes(app) -> None:
    """Mount the Wave 3a admin routes on an existing Starlette app.

    Idempotent: re-registering does nothing.
    """
    existing_paths = {
        getattr(route, "path", None) for route in getattr(app, "routes", [])
    }
    routes = [
        Route(
            f"{WAVE3A_ADMIN_PREFIX}/routing-table",
            _list_routes,
            methods=["GET"],
        ),
        Route(
            f"{WAVE3A_ADMIN_PREFIX}/routing-table",
            _set_route,
            methods=["POST"],
        ),
        Route(
            f"{WAVE3A_ADMIN_PREFIX}/routing-table",
            _clear_routes,
            methods=["DELETE"],
        ),
        Route(
            f"{WAVE3A_ADMIN_PREFIX}/routing-table/{{tool_name}}",
            _delete_route,
            methods=["DELETE"],
        ),
    ]
    for route in routes:
        # Methods differ per route so dedupe on (path, methods) tuple.
        key = (route.path, tuple(sorted(route.methods or [])))
        existing_keys = {
            (getattr(r, "path", None), tuple(sorted(getattr(r, "methods", None) or [])))
            for r in getattr(app, "routes", [])
        }
        if key in existing_keys:
            continue
        app.routes.append(route)
        logger.debug("wave3a admin route registered: %s %s", route.methods, route.path)


__all__ = [
    "WAVE3A_ADMIN_PREFIX",
    "register_wave3a_admin_routes",
]

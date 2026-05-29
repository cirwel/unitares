"""
Wave 3a Python probe endpoint surface.

Internal HTTP surface at ``/v1/probe/*`` consumed only by the BEAM-side
Wave 3a handler listener (see ``docs/proposals/beam-wave-3a-read-only-handlers.md``
§2.3). This module is PR #1 of the v0.2 sequencing — scaffolding only; no
production traffic flows through it until PR #4/PR #5 land.

Contract surfaces:

- §2.2 envelope shape: top-level keys (``ok``, ``protocol_version``, ...).
  Matches the lease plane top-level-keys contract (no nested ``data`` wrapper).
- §2.5 bearer-auth via ``WAVE_3A_PROBE_TOKEN``; missing -> 503 (fail-closed);
  present-but-wrong -> 401.
- §2.6 timestamp masking applied to ``tool_registry`` so the response is
  byte-deterministic across calls (catches ``list_tools``-style
  "non-determinism added later" regressions at probe scope, not just at
  handler scope).

Council folds addressed in this module:

- FIND-R2 (``list_all_aliases`` lazy state). ``tool_stability.list_all_aliases``
  returns a copy of a module-level dict that is populated at import time;
  there is no first-call build. The byte-equality test on the masked
  ``tool_registry`` response is the structural guard against a future
  regression that introduces lazy state.
- FIND-R3 (``get_server_info`` PID semantics). The probe response describes
  the Python probe process. ``meta.probe_process: true`` makes the response
  self-identifying so the BEAM caller can override or annotate before
  returning to its own client.

This module mounts its routes on the existing MCP HTTP listener (the
Starlette ``app`` created in ``src/mcp_server.py``) via
``register_wave3a_probe_routes(app)``. It does NOT spawn a new HTTP server.
The route prefix carries its own bearer-auth — the public MCP bearer
(``UNITARES_HTTP_API_TOKEN``) is not consulted on this prefix.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import time
from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.logging_utils import get_logger

logger = get_logger(__name__)


PROTOCOL_VERSION = "wave3a.v1"
PROBE_TOKEN_ENV = "WAVE_3A_PROBE_TOKEN"
PROBE_PREFIX = "/v1/probe"

# Strong references for in-flight measurement writes. asyncio.create_task
# returns a weakly-held task; without storing the reference, GC can collect
# the task before the write completes and silently drop the row. Watcher
# P001. The pattern is set + discard-on-done.
_PENDING_WRITES: "set[asyncio.Task[None]]" = set()

# Wave 3a §4.3 measurement channel: every probe call records one row in
# audit.coordination_measurements with measurement_type='measurement.wave_3a.request'.
# §4.1 (HTTP transport p99 vs Python-in-process p99) and §4.2 (503 / fallback
# rate sliding window) both read from this surface; without it, neither stop
# sign can be evaluated.
MEASUREMENT_TYPE_WAVE_3A_REQUEST = "measurement.wave_3a.request"


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def _envelope_ok(payload: Dict[str, Any], *, meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a top-level-keys success envelope per §2.2.

    The handler-specific payload is flattened under ``data`` (NOT nested
    under ``data.data``); ``meta`` is optional and reserved for
    response-shape annotations like ``probe_process`` (FIND-R3 fold).
    """
    body: Dict[str, Any] = {
        "ok": True,
        "protocol_version": PROTOCOL_VERSION,
        "data": payload,
    }
    if meta is not None:
        body["meta"] = meta
    return body


def _envelope_err(error: str, reason: str, *, detail: Optional[Any] = None) -> Dict[str, Any]:
    """Build a top-level-keys error envelope per §2.2.

    ``error`` is machine-readable; ``reason`` is short human prose;
    ``detail`` is optional structured context.
    """
    body: Dict[str, Any] = {
        "ok": False,
        "protocol_version": PROTOCOL_VERSION,
        "error": error,
        "reason": reason,
    }
    if detail is not None:
        body["detail"] = detail
    return body


# ---------------------------------------------------------------------------
# Auth — §2.5 fail-closed
# ---------------------------------------------------------------------------


def _get_configured_token() -> Optional[str]:
    """Read the probe token from env each request — supports rotation."""
    tok = os.environ.get(PROBE_TOKEN_ENV)
    if tok is None or tok == "":
        return None
    return tok


def _service_unavailable_response() -> JSONResponse:
    """Fail-closed response when the probe token is not configured."""
    return JSONResponse(
        _envelope_err(
            "service_unavailable",
            f"{PROBE_TOKEN_ENV} not configured",
        ),
        status_code=503,
    )


def _unauthorized_response() -> JSONResponse:
    return JSONResponse(
        _envelope_err(
            "permission_denied",
            "bearer token missing or invalid",
        ),
        status_code=401,
    )


def _check_bearer(request: Request, configured_token: str) -> bool:
    """Constant-time bearer-token check."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not isinstance(auth, str):
        return False
    if not auth.lower().startswith("bearer "):
        return False
    token = auth.split(" ", 1)[1].strip()
    return secrets.compare_digest(token, configured_token)


def _auth_or_response(request: Request) -> Optional[JSONResponse]:
    """Returns an error JSONResponse if auth fails, else None.

    Order:
        1. Token unset in env -> 503 (fail-closed).
        2. Token set, request missing/wrong bearer -> 401.
        3. Token set, request authorized -> None.

    /v1/probe/health is exempted by the caller — see ``_health`` handler.
    """
    configured = _get_configured_token()
    if configured is None:
        return _service_unavailable_response()
    if not _check_bearer(request, configured):
        return _unauthorized_response()
    return None


# ---------------------------------------------------------------------------
# Timestamp masking — §2.6
# ---------------------------------------------------------------------------


# ISO-8601 datetime: YYYY-MM-DD[T ]HH:MM:SS[.ffffff][Z|+HH:MM]
_ISO8601_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)

# UUID (8-4-4-4-12 hex)
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# Fields whose values are always non-deterministic and should be masked
# regardless of value shape. Keep this conservative — false-positive masking
# is cheaper than letting jitter slip through and break golden parity.
_VOLATILE_FIELD_NAMES = frozenset(
    {
        # timestamps / time deltas
        "created_at",
        "updated_at",
        "started_at",
        "completed_at",
        "timestamp",
        "ts",
        "produced_at",
        "server_time",
        "build_date",
        # latency / cumulative counters
        "uptime",
        "uptime_seconds",
        "uptime_minutes",
        "uptime_hours",
        "uptime_formatted",
        "current_uptime_seconds",
        "current_uptime_formatted",
        "elapsed_ms",
        "processing_time_ms",
        "duration_ms",
        "age_seconds",
        "monotonic_ns",
        "create_time",
        # PIDs and process-identifying ints
        "pid",
        "current_pid",
        "ppid",
        # request/correlation ids
        "request_id",
        "correlation_id",
        "trace_id",
        "span_id",
    }
)

# Suffix matchers — anything ending with these is masked.
_VOLATILE_SUFFIXES = (
    "_at",
    "_ms",
    "_ns",
    "_uuid",
    "_pid",
    "_id_uuid",
)


def _is_volatile_key(key: str) -> bool:
    if key in _VOLATILE_FIELD_NAMES:
        return True
    for suffix in _VOLATILE_SUFFIXES:
        if key.endswith(suffix):
            # _id is too broad (tool_id, agent_id are legitimately stable in
            # tests); _id_uuid catches the explicitly volatile variant.
            if suffix == "_uuid" or suffix == "_pid":
                return True
            # _at, _ms, _ns, _id_uuid are unambiguous; safe to mask
            return True
    return False


def _mask_value(key: str, value: Any) -> Any:
    """Mask a single value by key and shape.

    Strategy:
      - Key-based: if key matches a volatile name/suffix, replace.
      - Shape-based: if the value LOOKS like an ISO-8601 timestamp or UUID,
        replace even if the key wasn't on the volatile list (catches
        timestamps stashed in generic ``meta`` blobs).
    """
    if _is_volatile_key(key):
        if isinstance(value, str):
            if _ISO8601_RE.match(value):
                return "<MASKED_TIMESTAMP>"
            if _UUID_RE.match(value):
                return "<MASKED_UUID>"
            # uptime_formatted ("Xh Ym"), build_date strings, etc.
            return "<MASKED_TIMESTAMP>"
        if isinstance(value, bool):
            # bools subclass int — preserve them
            return value
        if isinstance(value, int):
            # PIDs and counters
            if key.endswith("_pid") or key == "pid" or key == "current_pid" or key == "ppid":
                return "<MASKED_PID>"
            return "<MASKED_COUNTER>"
        if isinstance(value, float):
            return "<MASKED_COUNTER>"
        if value is None:
            return None
        return mask_timestamps(value) if isinstance(value, (dict, list)) else value

    # Shape-based fallback for strings under non-volatile keys
    if isinstance(value, str):
        if _ISO8601_RE.match(value):
            return "<MASKED_TIMESTAMP>"
        if _UUID_RE.match(value):
            return "<MASKED_UUID>"
        return value

    if isinstance(value, (dict, list)):
        return mask_timestamps(value)
    return value


def mask_timestamps(payload: Any) -> Any:
    """Recursively replace non-deterministic fields with stable placeholders.

    Applied to the ``tool_registry`` response to guarantee byte-deterministic
    output. The byte-equality test in
    ``tests/integration/test_wave_3a_probe.py`` calls this endpoint twice
    and diffs the masked bodies; any new non-deterministic field that this
    helper misses will break that test.

    Returns a new structure; does not mutate the input.
    """
    if isinstance(payload, dict):
        return {key: _mask_value(key, value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [mask_timestamps(item) for item in payload]
    return payload


# ---------------------------------------------------------------------------
# Measurement channel — §4.3
# ---------------------------------------------------------------------------


async def _write_measurement(
    *,
    endpoint: str,
    elapsed_ms: int,
    status: str,
    payload_bytes: int,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    """Insert one row into audit.coordination_measurements (Wave 3a §4.3).

    Uses ``get_db().acquire()`` (ExecutorPool-wrapped per CLAUDE.md "Substrate
    Tax") so the asyncpg await never lands on the anyio task group. Failures
    are logged at debug and swallowed — the measurement channel is observability
    infrastructure for stop signs §4.1/§4.2, and a write failure here MUST
    NOT propagate into the probe response (or contribute to the latency the
    response itself is being measured for).
    """
    try:
        # Lazy import so test patches at module scope work and so import-time
        # of this module doesn't pull in the DB backend.
        from src.db import get_db

        db = get_db()
        meta_json = json.dumps(meta) if meta is not None else None
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO audit.coordination_measurements
                    (measurement_type, endpoint, elapsed_ms, status,
                     payload_bytes, meta)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                """,
                MEASUREMENT_TYPE_WAVE_3A_REQUEST,
                endpoint,
                elapsed_ms,
                status,
                payload_bytes,
                meta_json,
            )
    except Exception as exc:  # noqa: BLE001 — observability infra; swallow
        logger.debug(
            "[wave3a-probe] measurement-channel write failed: %r (endpoint=%s "
            "status=%s elapsed_ms=%s)",
            exc,
            endpoint,
            status,
            elapsed_ms,
        )


def _record_and_return(
    request: Request,
    response: JSONResponse,
    *,
    endpoint: str,
    start_monotonic: float,
    meta_extra: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    """Fire-and-forget measurement write, then return the response.

    Per §4.3 the measurement IS the latency we're measuring — writing it
    inline would contribute to its own value. ``asyncio.create_task`` lets
    the write happen on the next event-loop tick after the response has
    already been sent.

    Wrapped in try/except: even task creation failures (running loop closed,
    etc.) must not break the response. The task itself swallows DB errors.
    """
    elapsed_ms = int((time.monotonic() - start_monotonic) * 1000)
    payload_bytes = len(response.body) if response.body is not None else 0
    status = str(response.status_code)

    meta: Dict[str, Any] = {
        "probe_token_set": _get_configured_token() is not None,
        "auth_header_present": bool(
            request.headers.get("authorization")
            or request.headers.get("Authorization")
        ),
    }
    if meta_extra:
        meta.update(meta_extra)

    try:
        task = asyncio.create_task(
            _write_measurement(
                endpoint=endpoint,
                elapsed_ms=elapsed_ms,
                status=status,
                payload_bytes=payload_bytes,
                meta=meta,
            )
        )
        _PENDING_WRITES.add(task)
        task.add_done_callback(_PENDING_WRITES.discard)
    except Exception as exc:  # noqa: BLE001 — observability infra
        logger.debug(
            "[wave3a-probe] create_task for measurement write failed: %r", exc
        )

    return response


# ---------------------------------------------------------------------------
# Endpoint implementations
# ---------------------------------------------------------------------------


async def _health(request: Request) -> JSONResponse:
    """Bare liveness probe. No auth, no data payload.

    Mirrors the lease plane's ``/health`` shape so the BEAM Finch client can
    verify connectivity before bothering with bearer headers.

    Per §4.3 this endpoint IS part of the contract surface and DOES record a
    measurement row — fallback-rate denominators (§4.2) need to count every
    probe call including liveness pings, otherwise the stop-sign denominator
    is wrong on the cheapest endpoint.
    """
    start = time.monotonic()
    response = JSONResponse(
        {
            "ok": True,
            "protocol_version": PROTOCOL_VERSION,
        },
        status_code=200,
    )
    return _record_and_return(
        request, response, endpoint=f"{PROBE_PREFIX}/health", start_monotonic=start
    )


async def _health_snapshot(request: Request) -> JSONResponse:
    start = time.monotonic()
    endpoint = f"{PROBE_PREFIX}/health_snapshot"
    auth_err = _auth_or_response(request)
    if auth_err is not None:
        return _record_and_return(
            request, auth_err, endpoint=endpoint, start_monotonic=start
        )

    # Import lazily so test patches at module scope work.
    from src.services.health_snapshot import (
        PROBE_INTERVAL_SECONDS,
        STALENESS_THRESHOLD_SECONDS,
        get_snapshot,
        is_stale,
    )

    snapshot, age_seconds, produced_at = get_snapshot()
    if snapshot is None:
        response = JSONResponse(
            _envelope_err(
                "snapshot_unavailable",
                "deep_health_probe has not run yet",
            ),
            status_code=503,
        )
        return _record_and_return(
            request, response, endpoint=endpoint, start_monotonic=start
        )

    # Full snapshot (no lite filter) per §2.3 — the BEAM-side handler decides
    # whether to apply the lite filter on its end.
    response_data = dict(snapshot)
    response_data["_cache"] = {
        "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
        "produced_at": produced_at,
        "stale": is_stale(age_seconds),
        "probe_interval_seconds": PROBE_INTERVAL_SECONDS,
        "staleness_threshold_seconds": STALENESS_THRESHOLD_SECONDS,
    }
    response = JSONResponse(_envelope_ok(response_data), status_code=200)
    return _record_and_return(
        request, response, endpoint=endpoint, start_monotonic=start
    )


async def _server_info(request: Request) -> JSONResponse:
    start = time.monotonic()
    endpoint = f"{PROBE_PREFIX}/server_info"
    auth_err = _auth_or_response(request)
    if auth_err is not None:
        return _record_and_return(
            request, auth_err, endpoint=endpoint, start_monotonic=start
        )

    # Reproduce the get_server_info data shape WITHOUT going through the MCP
    # handler (avoids TextContent wrapping). The fields here mirror the
    # success_response payload returned by handle_get_server_info — golden
    # parity is enforced in PR #6, not here.
    import sys

    from src.mcp_handlers.shared import lazy_mcp_server as mcp_server

    argv = [str(a) for a in getattr(sys, "argv", [])]
    is_http = any("mcp_server.py" in a for a in argv)
    is_stdio = any("mcp_server_std.py" in a for a in argv)
    transport = "HTTP" if is_http else ("STDIO" if is_stdio else "unknown")

    current_pid = os.getpid()
    server_version = getattr(mcp_server, "SERVER_VERSION", None) or "unknown"
    server_build_date = getattr(mcp_server, "SERVER_BUILD_DATE", None) or "unknown"

    server_processes = []
    current_uptime = 0.0
    if mcp_server.PSUTIL_AVAILABLE:
        import psutil

        target_script = (
            "mcp_server.py" if is_http else ("mcp_server_std.py" if is_stdio else None)
        )
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time", "status"]):
                try:
                    cmdline = proc.info.get("cmdline", [])
                    if not cmdline:
                        continue
                    if target_script:
                        if not any(target_script in str(arg) for arg in cmdline):
                            continue
                    else:
                        if not any(
                            ("mcp_server_std.py" in str(arg) or "mcp_server.py" in str(arg))
                            for arg in cmdline
                        ):
                            continue
                    pid = proc.info["pid"]
                    create_time = proc.info.get("create_time", 0)
                    uptime_seconds = time.time() - create_time
                    server_processes.append(
                        {
                            "pid": pid,
                            "is_current": pid == current_pid,
                            "uptime_seconds": int(uptime_seconds),
                            "status": proc.info.get("status", "unknown"),
                        }
                    )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception as exc:
            server_processes = [{"error": f"Could not enumerate processes: {exc}"}]

        try:
            current_proc = psutil.Process(current_pid)
            current_uptime = time.time() - current_proc.create_time()
            if not server_processes:
                server_processes.append(
                    {
                        "pid": current_pid,
                        "is_current": True,
                        "uptime_seconds": int(current_uptime),
                        "status": getattr(current_proc, "status", lambda: "unknown")(),
                    }
                )
        except Exception:
            current_uptime = 0.0

    from src.mcp_handlers import TOOL_HANDLERS

    payload = {
        "transport": transport,
        "server_version": server_version,
        "build_date": server_build_date,
        "tool_count": len(TOOL_HANDLERS),
        "current_pid": current_pid,
        "current_uptime_seconds": int(current_uptime),
        "server_processes": server_processes,
        "health": "healthy",
    }
    # FIND-R3 fold: BEAM caller knows this PID/transport describes the
    # Python probe process and may inject its own values before returning
    # to its caller.
    meta = {"probe_process": True}
    response = JSONResponse(_envelope_ok(payload, meta=meta), status_code=200)
    return _record_and_return(
        request, response, endpoint=endpoint, start_monotonic=start
    )


async def _tool_registry(request: Request) -> JSONResponse:
    start = time.monotonic()
    endpoint = f"{PROBE_PREFIX}/tool_registry"
    auth_err = _auth_or_response(request)
    if auth_err is not None:
        return _record_and_return(
            request, auth_err, endpoint=endpoint, start_monotonic=start
        )

    from src.mcp_handlers import TOOL_HANDLERS
    from src.mcp_handlers.tool_stability import list_all_aliases
    from src.tool_modes import TOOL_TIERS

    # Tools: names + (optional) tier classification.
    tool_names = sorted(TOOL_HANDLERS.keys())
    tools = [{"name": name} for name in tool_names]

    # Aliases: old_name -> {new_name, ...}. Datetime fields are coerced to
    # ISO-8601 strings so JSON serialization succeeds; they're then masked
    # by ``mask_timestamps`` below (deterministic byte-equality contract).
    from datetime import date, datetime

    aliases_raw = list_all_aliases()
    aliases: Dict[str, Dict[str, Any]] = {}
    for old_name, alias in aliases_raw.items():
        entry: Dict[str, Any] = {}
        for attr in ("new_name", "deprecated_since", "removal_target", "migration"):
            value = getattr(alias, attr, None)
            if value is None:
                continue
            if isinstance(value, (datetime, date)):
                value = value.isoformat()
            entry[attr] = value
        aliases[old_name] = entry

    # Tiers: tier_name -> [tools].
    tiers: Dict[str, list] = {}
    if isinstance(TOOL_TIERS, dict):
        for tier_name, tier_tools in TOOL_TIERS.items():
            if isinstance(tier_tools, (list, tuple, set)):
                tiers[str(tier_name)] = sorted(str(t) for t in tier_tools)

    deprecated_tools = sorted(aliases.keys())

    payload = {
        "tools": tools,
        "aliases": aliases,
        "tiers": tiers,
        "deprecated_tools": deprecated_tools,
    }
    masked = mask_timestamps(payload)
    response = JSONResponse(_envelope_ok(masked), status_code=200)
    return _record_and_return(
        request, response, endpoint=endpoint, start_monotonic=start
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def register_wave3a_probe_routes(app) -> None:
    """Mount the Wave 3a probe routes on an existing Starlette app.

    Idempotent: re-registering does nothing.
    """
    existing_paths = {
        getattr(route, "path", None) for route in getattr(app, "routes", [])
    }
    routes = [
        Route(f"{PROBE_PREFIX}/health", _health, methods=["GET"]),
        Route(f"{PROBE_PREFIX}/health_snapshot", _health_snapshot, methods=["GET"]),
        Route(f"{PROBE_PREFIX}/server_info", _server_info, methods=["GET"]),
        Route(f"{PROBE_PREFIX}/tool_registry", _tool_registry, methods=["GET"]),
    ]
    for route in routes:
        if route.path in existing_paths:
            continue
        app.routes.append(route)
        logger.debug("wave3a probe route registered: %s", route.path)

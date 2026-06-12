"""HTTP tool execution helpers.

Provides a narrow direct-call path for core tools whose handlers already accept
plain argument dicts. Everything else falls back to the MCP dispatch pipeline.

Wave 3a routing integration (cutover-discovered gap): the REST entry point
``execute_http_tool`` checks the per-tool routing table BEFORE the direct
handler short-circuit. Without this, REST callers (curl, loadgen, simple
HTTP clients) would bypass BEAM dispatch even when the operator has flipped
``WAVE_3A_*_ON_BEAM=true`` and the routing table is populated, because the
five core tools in ``_DIRECT_HTTP_TOOL_HANDLERS`` short-circuit MCP dispatch.

See ``docs/proposals/beam-wave-3a-read-only-handlers.md`` v0.2 §5 (Wave 3a
cutover sequence) + architect FIND-A4 (dispatch-path question).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

from src.mcp_handlers.identity.handlers import (
    handle_identity_adapter,
    handle_onboard_v2,
)
from src.mcp_handlers.core import handle_process_agent_update, unbound_metrics_payload
from src.mcp_handlers.utils import require_agent_id
from src.services.http_dispatch_fallback import execute_http_dispatch_fallback
from src.services.runtime_queries import get_governance_metrics_data, get_health_check_data
from src.services.tool_usage_recorder import (
    classify_tool_result,
    record_tool_usage,
    resolve_minted_agent_id,
)
from src.wave3a_beam_proxy import proxy_to_beam
from src.wave3a_routing import get_route as wave3a_get_route

ToolHandler = Callable[[Dict[str, Any]], Awaitable[Any]]


# Envelope keys that are transport metadata — stripped before handing the
# payload to ``_build_http_tool_response`` so REST callers see the same
# handler-shape they got pre-cutover. Per ``elixir/wave3a_handlers/lib/
# wave3a_handlers/http_router.ex`` §2.2 the envelope is flat (top-level
# keys, never nested under ``data``); ``ok`` + ``protocol_version`` are the
# only universal transport fields.
_WAVE3A_ENVELOPE_TRANSPORT_KEYS = frozenset({"ok", "protocol_version"})


def _unwrap_wave3a_envelope_for_http(envelope: Any) -> Any:
    """Strip Wave 3a envelope transport keys for REST wire output.

    The BEAM envelope is ``{"ok": true, "protocol_version": "wave3a.v1",
    ...handler_fields...}`` — handler payload is at the top level alongside
    the transport keys. REST callers expect the handler payload only (same
    shape the Python direct handler returns), so we strip ``ok`` and
    ``protocol_version`` and return the rest as the tool result.

    Defensive: if the envelope is unrecognised (not a dict, ``ok`` not
    True, missing keys) we return it untouched. ``proxy_to_beam`` only
    returns ``ok=True`` results with a validated envelope per
    ``_validate_success_envelope``, so this branch shouldn't fire in
    production — but if it ever does, returning the raw envelope is more
    diagnostic than raising.
    """
    if not isinstance(envelope, dict):
        return envelope
    if envelope.get("ok") is not True:
        return envelope
    return {
        k: v for k, v in envelope.items()
        if k not in _WAVE3A_ENVELOPE_TRANSPORT_KEYS
    }


def _normalize_direct_http_result(result: Any) -> Any:
    """Convert direct-handler MCP text output into plain data for HTTP callers."""
    if isinstance(result, (list, tuple)) and len(result) == 1 and hasattr(result[0], "text"):
        try:
            return json.loads(result[0].text)
        except (json.JSONDecodeError, TypeError):
            return result
    return result

async def _execute_http_get_governance_metrics(arguments: Dict[str, Any]) -> Any:
    # Read-purity (trust contract §3.5), REST half: this direct handler
    # bypasses handle_get_governance_metrics, so without its own guard an
    # unbound REST caller reached require_agent_id FALLBACK 2 and minted a
    # fresh in-memory auto_* identity + monitor per call — the cold-probe
    # transport from the 2026-06-10 incident, caught by PR #608 review
    # after the first fix only covered the MCP handler. Same shared
    # ignorance payload, guard carried per-transport.
    if not arguments.get("agent_id"):
        try:
            from src.mcp_handlers.context import get_context_agent_id
            bound_agent_id = get_context_agent_id()
        except Exception:
            bound_agent_id = None
        if not bound_agent_id:
            return unbound_metrics_payload()
    agent_id, error = require_agent_id(arguments)
    if error:
        return [error]
    return await get_governance_metrics_data(agent_id, arguments)


async def _execute_http_health_check(arguments: Dict[str, Any]) -> Any:
    return await get_health_check_data(arguments)


_DIRECT_HTTP_TOOL_HANDLERS: Dict[str, ToolHandler] = {
    "get_governance_metrics": _execute_http_get_governance_metrics,
    "health_check": _execute_http_health_check,
    "identity": handle_identity_adapter,
    "onboard": handle_onboard_v2,
    "process_agent_update": handle_process_agent_update,
}


def get_direct_http_tool_handler(tool_name: str) -> Optional[ToolHandler]:
    """Return a direct handler for HTTP-safe core tools, if any."""
    return _DIRECT_HTTP_TOOL_HANDLERS.get(tool_name)


def _strict_identity_refusal_or_none(
    tool_name: str, arguments: Dict[str, Any]
) -> Optional[dict]:
    """#425 REST parity gate (stage-1 burn-in fold, 2026-06-11).

    The MCP dispatch middleware's typed refusal never ran on this
    surface — under STRICT_IDENTITY_REQUIRED, unbound REST reads
    succeeded and unbound writes failed with an off-contract generic
    SESSION_ERROR.

    The pass-decision keys on the RESOLVED BINDING, never on credential
    presence: ``http_call_tool`` transport-injects a synthetic
    ``client_session_id`` into every request before this gate runs (its
    own comment: "DO NOT TRUST client_session_id FOR AUTH —
    TRANSPORT-INJECTED HERE, NOT CLIENT-ASSERTED"), so a
    presence-based bypass would never fire on real traffic — the
    council's live battery proved exactly that, the same
    argument-presence trap PR #608's review caught one layer down. A
    caller whose credential RESOLVED has a context binding by the time
    this runs (``_resolve_http_bound_agent`` precedes
    ``execute_http_tool`` and calls ``update_context_agent_id`` on
    success — valid session ids, valid continuity tokens, and explicit
    UUIDs all land there); a garbage or synthetic credential resolves
    to nothing and is treated as what it is: unbound.

    - flag off → None (inert; today's default everywhere)
    - ``requires_identity="pre_onboard"`` → None (the tool serves its
      own unbound shape; unknown tools fail closed to "required"; the
      reserved third tier ``scoped`` deliberately refuses here until a
      first scoped tool defines its semantics)
    - resolved context binding → None
    - explicit non-UUID ``agent_id`` argument → None (legacy-name
      reference; require_agent_id + downstream ownership checks own it)
    - otherwise → the single-sourced typed refusal (same payload the
      MCP middleware wraps; transports cannot drift)
    """
    from src.mcp_handlers.identity_bootstrap import (
        is_strict_identity_required,
        strict_identity_refusal_payload,
    )

    if not is_strict_identity_required():
        return None
    # Call-level resolution (alias-aware): legacy names like
    # detect_anomalies canonicalize to observe(anomalies) at dispatch,
    # and mixed tools split read/write by action — judge the canonical
    # CALL, not the tool string (#425 action-level fold).
    from src.mcp_handlers.decorators import get_call_identity_requirement
    if get_call_identity_requirement(tool_name, arguments) == "pre_onboard":
        return None
    try:
        from src.mcp_handlers.context import get_context_agent_id
        if get_context_agent_id():
            return None
    except Exception:
        pass
    if isinstance(arguments, dict) and arguments.get("agent_id"):
        return None
    logger.info(
        "[HTTP] %s unbound under STRICT_IDENTITY_REQUIRED — returning "
        "typed refusal (no auto-mint)",
        tool_name,
    )
    return strict_identity_refusal_payload(tool_name)


async def execute_http_tool(tool_name: str, arguments: Dict[str, Any]) -> Any:
    """Execute a tool for the HTTP API.

    Core governance tools use direct handlers so HTTP does not always depend on
    the full MCP dispatch path. All other tools use an HTTP-specific fallback
    that skips identity-resolution middleware because HTTP already set context.

    Wave 3a routing — HTTP path must also honor the per-tool routing table.
    Cutover-discovered gap: REST callers bypass MCP dispatch via
    ``_DIRECT_HTTP_TOOL_HANDLERS``, so without this check the routing only
    fires for MCP-protocol clients. The check is symmetric with
    ``src/mcp_server.py::get_tool_wrapper``: routing-table-hit → BEAM proxy
    → on success, return the unwrapped envelope payload; on any BEAM failure
    mode, fall through to the existing direct-handler / fallback path.
    Routing-table-miss → unchanged (single dict lookup is hot-path cheap).

    Records tool_usage telemetry (JSONL + audit.tool_usage) at every exit point.
    """
    agent_id = arguments.get("agent_id") if isinstance(arguments, dict) else None
    t0 = time.monotonic()
    try:
        # #425 strict-identity gate — REST parity with the MCP dispatch
        # middleware (stage-1 burn-in fold). Sits ahead of Wave-3a routing:
        # the BEAM listener deliberately implements no identity middleware
        # (Wave-3a RFC — middleware is the 3b port), so this gate is the
        # ONLY enforcement on a proxied call. Forward-protection today
        # (every 3a-routed tool is pre_onboard and passes anyway); load-
        # bearing the day a `required` tool routes to BEAM.
        refusal = _strict_identity_refusal_or_none(tool_name, arguments)
        if refusal is not None:
            latency_ms = int((time.monotonic() - t0) * 1000)
            # A typed refusal is NOT a tool success. Recording success=True
            # here made every strict-gate refusal look like a SUCCEEDING
            # anonymous call in audit.tool_usage — poisoning exactly the
            # burn-in question "did any unbound write get through?" (#543
            # honesty class; found day 1 of the 2026-06-12 stage 2-4
            # burn-in). error_type mirrors the refusal payload's status so
            # triage queries can subtract refusals without log archaeology.
            record_tool_usage(tool_name=tool_name, agent_id=None,
                              success=False, error_type="identity_required",
                              latency_ms=latency_ms)
            return refusal

        # Wave 3a routing — HTTP path symmetric with MCP-protocol wrapper.
        # On BEAM success we return the unwrapped envelope payload. On any
        # BEAM failure (timeout, connect_error, envelope_invalid, etc.) the
        # proxy itself emits the §4.2 fallback event and we fall through to
        # the existing Python path — same fallback semantics as the MCP
        # wrapper at ``src/mcp_server.py::get_tool_wrapper``.
        beam_url = wave3a_get_route(tool_name)
        if beam_url is not None:
            proxy_result = await proxy_to_beam(
                tool_name=tool_name,
                beam_url=beam_url,
                kwargs=arguments,
            )
            if proxy_result.ok:
                latency_ms = int((time.monotonic() - t0) * 1000)
                record_tool_usage(tool_name=tool_name, agent_id=agent_id,
                                  success=True, latency_ms=latency_ms)
                # The proxy already wrote the success-row measurement
                # (FIND-A5 fold in ``wave3a_beam_proxy.py``); do not
                # duplicate the write here.
                return _unwrap_wave3a_envelope_for_http(proxy_result.response)
            # Proxy failed — fall through to Python path. The proxy already
            # emitted the §4.2 fallback event; nothing to do here.

        handler = get_direct_http_tool_handler(tool_name)
        if handler is not None:
            result = await handler(arguments)
            latency_ms = int((time.monotonic() - t0) * 1000)
            success, error_type = classify_tool_result(result)
            record_tool_usage(tool_name=tool_name,
                              agent_id=resolve_minted_agent_id(tool_name, agent_id, result),
                              success=success, error_type=error_type, latency_ms=latency_ms)
            return _normalize_direct_http_result(result)
        result = await execute_http_dispatch_fallback(tool_name, arguments)
        latency_ms = int((time.monotonic() - t0) * 1000)
        success, error_type = classify_tool_result(result)
        record_tool_usage(tool_name=tool_name,
                          agent_id=resolve_minted_agent_id(tool_name, agent_id, result),
                          success=success, error_type=error_type, latency_ms=latency_ms)
        return result
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        record_tool_usage(tool_name=tool_name, agent_id=agent_id,
                          success=False, error_type=type(e).__name__,
                          latency_ms=latency_ms)
        raise

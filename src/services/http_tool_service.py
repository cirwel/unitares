"""HTTP tool execution helpers.

Provides a narrow direct-call path for core tools whose handlers already accept
plain argument dicts. Everything else falls back to the MCP dispatch pipeline.

Wave 3a routing integration (cutover-discovered gap): the REST entry point
``execute_http_tool`` checks the per-tool routing table BEFORE the direct
handler short-circuit. Without this, REST callers (curl, loadgen, simple
HTTP clients) would bypass BEAM dispatch even when the operator has flipped
``WAVE_3A_*_ON_BEAM=true`` and the routing table is populated, because the
five core tools in ``_DIRECT_HTTP_TOOL_HANDLERS`` short-circuit MCP dispatch.

See ``docs/proposals/archive/beam-wave-3a-read-only-handlers.md`` v0.2 §5 (Wave 3a
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
from src.mcp_handlers.core import (
    handle_process_agent_update,
    metrics_agent_is_known,
    unbound_metrics_payload,
    unknown_agent_error,
)
from src.mcp_handlers.utils import require_agent_id
from src.services.http_dispatch_fallback import execute_http_dispatch_fallback
from src.services.runtime_queries import get_governance_metrics_data, get_health_check_data
from src.services.tool_usage_recorder import (
    build_tool_usage_payload,
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


async def execute_nested_http_tool(
    tool_name: str, arguments: Dict[str, Any]
) -> Any:
    """Execute a gateway target through the REST prebind boundary again."""
    from src.http_routes import access
    from src.mcp_handlers.context import (
        get_context_client_session_id,
        get_csid_injected_source,
        get_csid_transport_injected,
        get_session_signals,
        reset_csid_injected_source,
        reset_csid_transport_injected,
        reset_session_context,
        set_csid_injected_source,
        set_csid_transport_injected,
        set_session_context,
    )

    nested = dict(arguments or {})
    explicit_session = "client_session_id" in nested
    session_id = (
        nested.get("client_session_id")
        if explicit_session
        else get_context_client_session_id()
    )
    if session_id and not explicit_session:
        nested["client_session_id"] = session_id

    # A nested explicit session is caller input just like a direct REST body.
    # An inherited session retains the outer route's injected/proven status,
    # and the header it was derived from, if any.
    inherited_injected = get_csid_transport_injected()
    csid_token = set_csid_transport_injected(
        False if explicit_session else inherited_injected
    )
    source_token = set_csid_injected_source(
        None if explicit_session else get_csid_injected_source()
    )
    context_token = set_session_context(
        session_key=session_id,
        client_session_id=session_id,
    )
    try:
        signals = get_session_signals()
        if signals is not None:
            await access._resolve_http_bound_agent(tool_name, nested, signals)
        # Non-core targets re-enter the HTTP fallback dispatch pipeline, whose
        # post-validation steps charge the target. Core REST targets use the
        # direct-handler shortcut instead, so charge them here after target-
        # specific prebinding. The outer use_tool call deliberately deferred
        # its charge to this target and must not turn that shortcut into a
        # zero-charge path.
        if get_direct_http_tool_handler(tool_name) is not None:
            from src.mcp_handlers.context import get_context_agent_id
            from src.mcp_handlers.middleware import DispatchContext, check_rate_limit

            rate_started = time.monotonic()
            rate_result = await check_rate_limit(
                tool_name,
                nested,
                DispatchContext(
                    bound_agent_id=get_context_agent_id(),
                    client_session_id=session_id,
                ),
            )
            if isinstance(rate_result, list):
                success, error_type = classify_tool_result(rate_result)
                record_tool_usage(
                    tool_name=tool_name,
                    agent_id=nested.get("agent_id") or get_context_agent_id(),
                    success=success,
                    error_type=error_type,
                    latency_ms=int((time.monotonic() - rate_started) * 1000),
                    session_id=nested.get("client_session_id"),
                    payload=build_tool_usage_payload(tool_name, nested),
                )
                return rate_result
        return await execute_http_tool(tool_name, nested)
    finally:
        reset_session_context(context_token)
        reset_csid_injected_source(source_token)
        reset_csid_transport_injected(csid_token)

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
            from src.mcp_handlers.context import (
                get_context_resolved_agent_id,
                get_csid_transport_injected,
            )

            bound_agent_id = get_context_resolved_agent_id()
            transport_injected = get_csid_transport_injected()
        except Exception:
            bound_agent_id = None
            transport_injected = False
        if not bound_agent_id:
            sent = bool(arguments.get("client_session_id")) and not transport_injected
            return unbound_metrics_payload(caller_sent_session_id=sent)
    agent_id, error = require_agent_id(arguments)
    if error:
        return [error]
    # Same per-transport split as the unbound guard above: the refusal is
    # defined once in core.py, but each transport has to invoke it or the
    # REST shortcut answers seeded metrics for an id that names no agent.
    if not await metrics_agent_is_known(agent_id):
        return [unknown_agent_error(agent_id)]
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
    success — valid session ids, valid continuity tokens, and CORROBORATED
    explicit UUIDs all land there, since 2026-08-24; an uncorroborated one
    no longer does — see ``access._bind_explicit_http_agent``); a garbage,
    synthetic, or uncorroborated credential resolves to nothing and is
    treated as what it is: unbound.

    - flag off → None (inert; today's default everywhere)
    - ``requires_identity="pre_onboard"`` → None (the tool serves its
      own unbound shape; unknown tools fail closed to "required"; the
      reserved third tier ``scoped`` deliberately refuses here until a
      first scoped tool defines its semantics)
    - resolved context binding → None
    - explicit non-UUID ``agent_id`` argument → None for legacy-compatible
      tools (legacy-name reference; require_agent_id + downstream ownership
      checks own it). ``consult`` is deliberately excluded because inference
      attribution requires a resolved caller binding, never a name reference.
    - a UUID-shaped ``agent_id`` that did NOT resolve to a context binding
      → the typed refusal, same as no agent_id at all. Before 2026-08-24
      every UUID-shaped claim resolved unconditionally, so this branch was
      unreachable for that shape and the presence check below was
      (accidentally) safe; it is reachable now that
      ``_bind_explicit_http_agent`` can refuse an uncorroborated claim, and
      exempting it here would silently undo that refusal one layer up.
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
        from src.mcp_handlers.context import get_context_resolved_agent_id

        if get_context_resolved_agent_id():
            return None
    except Exception:
        pass
    if (
        tool_name != "consult"
        and isinstance(arguments, dict)
        and arguments.get("agent_id")
    ):
        from src.http_routes.access import _looks_like_uuid

        if not _looks_like_uuid(arguments["agent_id"]):
            return None
    logger.info(
        "[HTTP] %s unbound under STRICT_IDENTITY_REQUIRED — returning "
        "typed refusal (no auto-mint)",
        tool_name,
    )
    # The recovery depends on why nothing bound, and the prebind recorded the
    # resolver's result (access._record_unbound_resolution). The builder is the
    # one the MCP middleware uses for the same result, so a session miss, a
    # hijack-guard rejection, a substrate resident over HTTP and a server-side
    # failure each get the same recovery on both transports. A session miss's
    # recovery keys on whether the caller itself sent a client_session_id. On
    # REST every call carries one, so an id the transport put there (from the
    # fingerprint, a pin, or a header it derived) does not count.
    from src.mcp_handlers.context import (
        get_csid_injected_source,
        get_csid_transport_injected,
        get_http_prebind_resolution,
    )
    from src.mcp_handlers.identity_bootstrap import unbound_call_refusal

    resolution = get_http_prebind_resolution()
    if resolution is not None and "caller_sent_session_id" in resolution:
        # Judged by the prebind before its derivation dropped an invalid id.
        caller_sent_session_id = bool(resolution["caller_sent_session_id"])
    else:
        caller_sent_session_id = bool(
            isinstance(arguments, dict)
            and arguments.get("client_session_id")
            and not get_csid_transport_injected()
            and get_csid_injected_source() is None
        )
    options, surface_extra = unbound_call_refusal(
        tool_name,
        resolution,
        caller_sent_session_id=caller_sent_session_id,
        token_failed_verification=bool(
            resolution and resolution.get("token_failed_verification")
        ),
    )
    return strict_identity_refusal_payload(
        tool_name,
        **options,
        surface_context={
            "transport_surface": "rest_tool_call",
            "lifecycle_automation": "not_confirmed",
            "note": (
                "REST /v1/tools/call refusal; direct tool reachability does "
                "not prove client lifecycle-hook automation."
            ),
            **surface_extra,
        },
    )


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

    Records tool_usage telemetry (JSONL + audit.tool_usage) at every exit point,
    including the strict-identity refusal.
    """
    # Caller-supplied middleware handoff keys never reach the BEAM proxy, a
    # direct handler, or the fallback: only dispatch middleware writes them.
    # The MCP pipeline and the fallback strip in their own first step; the
    # direct and proxied paths start here.
    from src.mcp_handlers.middleware.params_step import remove_reserved_dispatch_keys

    remove_reserved_dispatch_keys(arguments)
    agent_id = arguments.get("agent_id") if isinstance(arguments, dict) else None
    session_id = arguments.get("client_session_id") if isinstance(arguments, dict) else None
    t0 = time.monotonic()
    nested_delegated = False
    # #1387 action discriminator. Built HERE, alongside the latency clock and
    # BEFORE any dispatch, because the pipeline mutates `arguments` in place:
    # `params_step.resolve_alias` writes the alias's implied action into the
    # caller's own dict, so a post-dispatch build would record every
    # `request_review` as action_source="explicit". Held in a local and reused
    # by whichever exit point fires.
    usage_payload = build_tool_usage_payload(tool_name, arguments)
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
                              latency_ms=latency_ms, session_id=session_id,
                              payload=usage_payload)
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
                                  success=True, latency_ms=latency_ms,
                                  session_id=session_id, payload=usage_payload)
                # The proxy already wrote the success-row measurement
                # (FIND-A5 fold in ``wave3a_beam_proxy.py``); do not
                # duplicate the write here.
                return _unwrap_wave3a_envelope_for_http(proxy_result.response)
            # Proxy failed — fall through to Python path. The proxy already
            # emitted the §4.2 fallback event; nothing to do here.

        handler = get_direct_http_tool_handler(tool_name)
        from src.mcp_handlers.context import (
            reset_nested_tool_invoker,
            set_nested_tool_invoker,
        )

        async def _nested_invoker(target_name, target_arguments):
            nonlocal nested_delegated
            nested_delegated = True
            return await execute_nested_http_tool(target_name, target_arguments)

        invoker_token = set_nested_tool_invoker(_nested_invoker)
        if handler is not None:
            try:
                result = await handler(arguments)
            finally:
                reset_nested_tool_invoker(invoker_token)
            latency_ms = int((time.monotonic() - t0) * 1000)
            success, error_type = classify_tool_result(result)
            if not nested_delegated:
                record_tool_usage(tool_name=tool_name,
                                  agent_id=resolve_minted_agent_id(tool_name, agent_id, result),
                                  success=success, error_type=error_type,
                                  latency_ms=latency_ms, session_id=session_id,
                                  payload=usage_payload)
            return _normalize_direct_http_result(result)
        try:
            result = await execute_http_dispatch_fallback(tool_name, arguments)
        finally:
            reset_nested_tool_invoker(invoker_token)
        latency_ms = int((time.monotonic() - t0) * 1000)
        success, error_type = classify_tool_result(result)
        if not nested_delegated:
            record_tool_usage(tool_name=tool_name,
                              agent_id=resolve_minted_agent_id(tool_name, agent_id, result),
                              success=success, error_type=error_type,
                              latency_ms=latency_ms, session_id=session_id,
                              payload=usage_payload)
        return result
    except Exception as e:
        latency_ms = int((time.monotonic() - t0) * 1000)
        if not nested_delegated:
            record_tool_usage(tool_name=tool_name, agent_id=agent_id,
                              success=False, error_type=type(e).__name__,
                              latency_ms=latency_ms, session_id=session_id,
                              payload=usage_payload)
        raise

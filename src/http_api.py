"""
HTTP REST API facade for non-MCP clients (Llama, Mistral, GPT, dashboards, etc.).

Declared re-export facade (see [tool.ruff.lint.per-file-ignores]): the
handlers live in src/http_routes/ domain modules; this module keeps the
public import surface (`from src.http_api import ...`) stable and owns
route registration.

Usage:
    from src.http_api import register_http_routes
    register_http_routes(app, ...)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.routing import Route, WebSocketRoute

from src.dashboard_auth import (
    attach_dashboard_session,
    http_auth_credential_revoke,
    http_auth_enroll,
    http_auth_logout,
    http_auth_sessions,
    http_auth_signin,
    http_webauthn_options,
    http_webauthn_register_options,
    http_webauthn_register_verify,
    http_webauthn_verify,
)
from src.broadcaster import broadcaster_instance  # noqa: F401  (singleton; tests reach it via this module)
from src.logging_utils import get_logger

from src.http_routes.access import (
    _build_http_session_signals,
    _TRUSTED_NETWORKS,
    _is_trusted_network,
    _http_unauthorized,
    _bearer_from_header,
    _check_ws_auth,
    _check_http_auth,
    _extract_client_session_id,
    _HTTP_PREBIND_SKIP_TOOLS,
    _explicit_bind_corroboration,
    _bind_explicit_http_agent,
    _preserve_explicit_target,
    _resolve_http_operator,
    _consult_http_sticky_binding,
    _cache_http_resolution,
    _touch_http_session_activity,
    _resolve_http_session_binding,
    _resolve_http_bound_agent,
    _http_bool,
)
from src.http_routes.tools import (
    _serialize_mcp_content_item,
    _build_http_tool_response,
    _normalize_http_tool_name,
    http_list_tools,
    _inject_http_client_session,
    _execute_http_tool_in_context,
    _http_tool_error_response,
    http_call_tool,
)
from src.http_routes.effects import (
    http_effect_grant,
    _BINDING_FLAG,
    _binding_enforced,
    _binding_mint_enabled,
    _check_effect_binding,
    http_effect_veto,
)
from src.http_routes.lease_identity import (
    http_attest_lease_holder,
    http_lease_attestation_keys,
    http_verify_lease_holder,
)
from src.http_routes.health import (
    http_health,
    http_health_live,
    http_health_ready,
    http_health_deep,
    http_metrics,
    http_debug_memory,
)
from src.http_routes.dashboard import (
    http_phase,
    http_dashboard_static,
    http_dashboard_redesign,
    http_dashboard_classic_redirect,
)
from src.http_routes.telemetry import (
    http_eisv_latest,
    http_eisv_recent,
    _EISV_TELEMETRY_HEALTH_CACHE_TTL_SECONDS,
    _eisv_telemetry_health_cache,
    http_eisv_telemetry_health,
    http_events,
    _LIFECYCLE_EVENT_TYPES,
    http_enforcement_divergence,
    http_lifecycle_recent,
    websocket_eisv_stream,
)
from src.http_routes.overview import (
    http_agent_history,
    http_automations,
    http_tier_distribution,
    http_bootstrap_silent,
    http_incidents,
    http_activity,
    http_taxonomy,
)
from src.http_routes.metrics_api import (
    http_post_metric,
    http_get_metrics,
    http_get_metrics_catalog,
    http_get_progress_flat_recent,
)
from src.http_routes.watcher import (
    _watcher_findings_path,
    _WATCHER_DAILY_WINDOW_DAYS,
    _watcher_summary_from_rows,
    http_watcher_summary,
)
from src.http_routes.sentinel import (
    _FINDING_SEVERITIES,
    _FINDING_TYPE_SUFFIX,
    _FINDING_REQUIRED_FIELDS,
    _SENTINEL_FINDING_EVENT_TYPES,
    _SENTINEL_BACKLOG_DEFAULT_SEVERITIES,
    _SENTINEL_DEFAULT_WINDOW_HOURS,
    _SENTINEL_DEFAULT_RECENT_LIMIT,
    _sentinel_summary_from_events,
    _sentinel_event_from_audit,
    _sentinel_events_durable,
    http_sentinel_summary,
    http_record_finding,
    http_sentinel_backlog,
    _SENTINEL_ADJUDICATION_OUTCOME_TYPES,
    _ADJUDICATION_DISMISS_REASONS,
    _SENTINEL_SUBSTRATE_LABEL_PREFIX,
    _adjudicated_sentinel_fingerprints,
    _sentinel_substrate_uuid,
    _adjudication_progress,
    _FORCED_RELEASE_MESSAGE_PREFIX,
    _UUID_RE,
    _assess_forced_release_row,
    _finding_report_latency_s,
    _fetch_lease_rows,
    _attach_forced_release_evidence,
    http_sentinel_adjudication_queue,
    http_sentinel_adjudicate,
)
from src.http_routes.vigil import (
    _VIGIL_DEFAULT_WINDOW_HOURS,
    _VIGIL_DEFAULT_RECENT_LIMIT,
    _VIGIL_CYCLE_HISTORY_LIMIT,
    _vigil_agent_id,
    _vigil_cycle_history,
    _vigil_stats,
    http_vigil_summary,
)
from src.http_routes.substrate import (
    http_record_bridge_event,
    http_bridge_summary,
    http_substrate_observe,
    http_runtime_observe,
    http_runtime_activity,
    http_substrate_dark_sessions,
    _HARNESS_OUTCOME_VERIFICATION_SOURCES,
    http_harness_outcome,
)
from src.http_routes.residents import (
    _DEFAULT_RESIDENT_SILENCE_SECONDS,
    _resolve_resident_labels,
    _latest_eisv_for_agent,
    _durable_latest_eisv_for_agent,
    _extract_eisv_fields,
    _parse_resident_timestamp,
    _safe_resident_total_updates,
    _resident_meta_preference_key,
    _latest_eisv_for_label,
    _coherence_history_for_agent,
    _recent_writes_for_agent,
    http_residents,
    RESIDENT_REQUIRED_TAGS,
    http_resident_tag_audit,
)

if TYPE_CHECKING:
    from starlette.applications import Starlette

logger = get_logger(__name__)



# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def register_http_routes(
    app: Starlette,
    *,
    server_ready_fn,
    server_start_time: float,
    server_version: str,
    has_streamable_http: bool,
    mcp_server_name: str = "governance-monitor-v1",
    server_build_sha: str = "unknown",
):
    """
    Register all HTTP REST endpoints on the given Starlette ``app``.

    Parameters that vary per-deployment (connection tracker, server readiness,
    version, etc.) are injected via a lightweight ASGI middleware that sets
    ``request.state`` attributes before each handler runs.  This avoids
    module-level globals while keeping handler signatures clean.
    """
    from starlette.requests import HTTPConnection
    from starlette.types import ASGIApp, Receive, Scope, Send

    # Tiny middleware that injects server context into request.state
    # so endpoint handlers can access server_ready_fn, server_version, etc.
    class _InjectContextMiddleware:
        def __init__(self, app: ASGIApp):
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send):
            if scope["type"] in ("http", "websocket"):
                state = scope.setdefault("state", {})
                state["_http_api_server_ready_fn"] = server_ready_fn
                state["_http_api_server_start_time"] = server_start_time
                state["_http_api_server_version"] = server_version
                state["_http_api_server_build_sha"] = server_build_sha
                state["_http_api_has_streamable_http"] = has_streamable_http
                state["_http_api_mcp_server_name"] = mcp_server_name
            await self.app(scope, receive, send)

    app.add_middleware(_InjectContextMiddleware)

    class _DashboardSessionMiddleware:
        """Attach a validated Postgres session before sync REST/WS auth gates."""

        def __init__(self, app: ASGIApp):
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send):
            path = scope.get("path", "")
            # The dashboard SHELL paths are session-aware too. They serve no
            # data, but they decide whether to hand the browser the API token,
            # and that decision reads dashboard_session_authenticated — which
            # is populated only here. Without these, a signed-in operator
            # arriving over a tunnel got the shell with no token, its data
            # calls 401'd, and /auth/signin bounced back to "/": a redirect
            # loop with no in-browser recovery.
            session_aware_path = (
                path.startswith(("/v1/", "/api/", "/auth/", "/debug/"))
                # /health/deep is gated like a data route and the dashboard
                # fetches it, so it needs the session attached or a signed-in
                # browser can never reach it in strict posture. The shallow
                # health routes stay out: they are public and take no session.
                or path in ("/metrics", "/", "/dashboard", "/phase", "/health/deep")
                or path.startswith("/dashboard/")
            )
            if scope["type"] == "websocket" or (
                scope["type"] == "http" and session_aware_path
            ):
                await attach_dashboard_session(HTTPConnection(scope))
            await self.app(scope, receive, send)

    app.add_middleware(_DashboardSessionMiddleware)

    # Browser passkey auth. These precede dashboard static routes and do not
    # alter /mcp, /sse, the public health endpoints, or Host allowlisting.
    app.routes.append(Route("/auth/signin", http_auth_signin, methods=["GET"]))
    app.routes.append(Route("/auth/enroll", http_auth_enroll, methods=["GET", "POST"]))
    app.routes.append(Route("/auth/webauthn/options", http_webauthn_options, methods=["POST"]))
    app.routes.append(Route("/auth/webauthn/verify", http_webauthn_verify, methods=["POST"]))
    app.routes.append(Route(
        "/auth/webauthn/register/options",
        http_webauthn_register_options,
        methods=["POST"],
    ))
    app.routes.append(Route(
        "/auth/webauthn/register/verify",
        http_webauthn_register_verify,
        methods=["POST"],
    ))
    app.routes.append(Route("/auth/logout", http_auth_logout, methods=["POST"]))
    app.routes.append(Route("/auth/sessions", http_auth_sessions, methods=["GET", "POST"]))
    app.routes.append(Route(
        "/auth/credentials/{credential_id}/revoke",
        http_auth_credential_revoke,
        methods=["POST"],
    ))

    # Redesign preview routes — must come BEFORE /dashboard/{file} so that
    # /dashboard/redesign resolves to the redesign handler, not the flat
    # static allowlist (which would 403 it). Additive and reversible.
    app.routes.append(Route("/dashboard/redesign", http_dashboard_redesign, methods=["GET"]))
    app.routes.append(Route("/dashboard/redesign/{file:path}", http_dashboard_redesign, methods=["GET"]))
    # CUTOVER (2026-06-19): /dashboard (and /) serve the redesign. The classic
    # dashboard was retired (see dashboard/README.md; recover from git history).
    # The static {file} route remains only to serve phase.js for the /phase view;
    # it stays BEFORE the /dashboard redesign route so /dashboard/phase.js resolves.
    # Retired classic → redirect home (must precede the /dashboard/{file} static
    # route, which would otherwise 403 the unknown "classic" path).
    app.routes.append(Route("/dashboard/classic", http_dashboard_classic_redirect, methods=["GET"]))
    app.routes.append(Route("/dashboard/{file}", http_dashboard_static, methods=["GET"]))
    app.routes.append(Route("/dashboard", http_dashboard_redesign, methods=["GET"]))
    app.routes.append(Route("/phase", http_phase, methods=["GET"]))
    app.routes.append(Route("/", http_dashboard_redesign, methods=["GET"]))  # Root also serves the redesign
    app.routes.append(Route("/v1/tools", http_list_tools, methods=["GET"]))
    app.routes.append(Route("/v1/tools/call", http_call_tool, methods=["POST"]))
    app.routes.append(Route("/v1/effect-grant", http_effect_grant, methods=["POST"]))
    app.routes.append(Route("/v1/effect-veto", http_effect_veto, methods=["POST"]))
    app.routes.append(Route("/v1/lease-holder/verify", http_verify_lease_holder, methods=["POST"]))
    app.routes.append(Route("/v1/lease-holder/attest", http_attest_lease_holder, methods=["POST"]))
    app.routes.append(Route("/v1/lease-holder/keys", http_lease_attestation_keys, methods=["GET"]))
    app.routes.append(Route("/health", http_health, methods=["GET"]))
    app.routes.append(Route("/health/live", http_health_live, methods=["GET"]))
    app.routes.append(Route("/health/ready", http_health_ready, methods=["GET"]))
    app.routes.append(Route("/health/deep", http_health_deep, methods=["GET"]))
    app.routes.append(Route("/metrics", http_metrics, methods=["GET"]))
    app.routes.append(Route("/v1/eisv/latest", http_eisv_latest, methods=["GET"]))
    app.routes.append(Route("/v1/eisv/recent", http_eisv_recent, methods=["GET"]))
    app.routes.append(Route("/v1/eisv/telemetry-health", http_eisv_telemetry_health, methods=["GET"]))
    app.routes.append(Route("/v1/lifecycle/recent", http_lifecycle_recent, methods=["GET"]))
    app.routes.append(Route("/v1/enforcement/divergence", http_enforcement_divergence, methods=["GET"]))
    app.routes.append(Route("/api/events", http_events, methods=["GET"]))
    app.routes.append(Route("/api/findings", http_record_finding, methods=["POST"]))
    app.routes.append(Route("/v1/bridge/events", http_record_bridge_event, methods=["POST"]))
    app.routes.append(Route("/v1/bridge/summary", http_bridge_summary, methods=["GET"]))
    app.routes.append(Route("/v1/substrate/observe", http_substrate_observe, methods=["POST"]))
    app.routes.append(Route("/v1/runtime/observe", http_runtime_observe, methods=["POST"]))
    app.routes.append(Route("/v1/runtime/activity", http_runtime_activity, methods=["GET"]))
    app.routes.append(Route("/v1/substrate/dark_sessions", http_substrate_dark_sessions, methods=["GET"]))
    app.routes.append(Route("/v1/sentinel/backlog", http_sentinel_backlog, methods=["GET"]))
    app.routes.append(Route("/v1/sentinel/adjudication-queue", http_sentinel_adjudication_queue, methods=["GET"]))
    app.routes.append(Route("/v1/sentinel/adjudicate", http_sentinel_adjudicate, methods=["POST"]))
    app.routes.append(Route("/v1/harness/outcome", http_harness_outcome, methods=["POST"]))
    app.routes.append(Route("/v1/metrics", http_post_metric, methods=["POST"]))
    app.routes.append(Route("/v1/metrics/series", http_get_metrics, methods=["GET"]))
    app.routes.append(Route("/v1/metrics/catalog", http_get_metrics_catalog, methods=["GET"]))
    app.routes.append(Route("/v1/progress_flat/recent", http_get_progress_flat_recent, methods=["GET"]))
    app.routes.append(Route("/v1/watcher/summary", http_watcher_summary, methods=["GET"]))
    app.routes.append(Route("/v1/bootstrap/silent", http_bootstrap_silent, methods=["GET"]))
    app.routes.append(Route("/v1/sentinel/summary", http_sentinel_summary, methods=["GET"]))
    app.routes.append(Route("/v1/vigil/summary", http_vigil_summary, methods=["GET"]))
    app.routes.append(Route("/v1/agents/tier_distribution", http_tier_distribution, methods=["GET"]))
    app.routes.append(Route("/v1/agents/{agent_id}/history", http_agent_history, methods=["GET"]))
    app.routes.append(Route("/api/automations", http_automations, methods=["GET"]))
    app.routes.append(Route("/api/activity", http_activity, methods=["GET"]))
    app.routes.append(Route("/api/incidents", http_incidents, methods=["GET"]))
    app.routes.append(Route("/v1/residents", http_residents, methods=["GET"]))
    app.routes.append(Route("/v1/residents/tag_audit", http_resident_tag_audit, methods=["GET"]))
    app.routes.append(Route("/v1/taxonomy", http_taxonomy, methods=["GET"]))
    app.routes.append(WebSocketRoute("/ws/eisv", websocket_eisv_stream))
    app.routes.append(Route("/debug/memory", http_debug_memory, methods=["GET"]))

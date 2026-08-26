"""ASGI application assembly and runtime for the streamable MCP transport."""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from starlette.datastructures import Headers
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount

from src.connection_tracker import ConnectionTrackingMiddleware
from src.logging_utils import get_logger
from src.mcp_handlers.context import (
    SessionSignals,
    detect_client_from_user_agent,
    note_ua_fingerprint,
    reset_mcp_session_id,
    reset_session_context,
    reset_session_signals,
    reset_transport_client_hint,
    set_mcp_session_id,
    set_session_context,
    set_session_signals,
    set_transport_client_hint,
)
from src.mcp_listen_config import (
    check_mcp_bearer,
    check_oauth_bearer,
    cors_extra_origins,
    mcp_bearer_tokens,
)
from src.mcp_transport import Transport503EmissionMiddleware

logger = get_logger(__name__)

AsgiCallable = Callable[
    [
        dict[str, Any],
        Callable[..., Awaitable[Any]],
        Callable[..., Awaitable[Any]],
    ],
    Awaitable[None],
]


@dataclass(frozen=True)
class McpAuthConfig:
    oauth_provider: Any = None
    auth_settings: Any = None
    required_scopes: Sequence[str] = ("mcp:tools",)


@dataclass(frozen=True)
class AuthDecision:
    allowed: bool
    oauth_client_id: str | None = None
    response: JSONResponse | None = None


@dataclass
class TransportContextTokens:
    session_context: Any = None
    mcp_session: Any = None
    client_hint: Any = None
    signals: Any = None


@dataclass
class McpTransportRuntime:
    app: Any
    session_manager: Any
    server: Any

    async def serve(self) -> None:
        uds_socket_path, uds_task = await _start_uds_listener(self.app)
        try:
            async with self.session_manager.run():
                logger.info("[STREAMABLE] Session manager started")
                await self.server.serve()
            logger.info("[STREAMABLE] Session manager shut down")
        finally:
            await _stop_uds_listener(uds_socket_path, uds_task)


def _www_authenticate_header(auth_settings: Any) -> str:
    if not auth_settings or not auth_settings.resource_server_url:
        return "Bearer"
    try:
        from mcp.server.auth.routes import build_resource_metadata_url

        resource_metadata_url = build_resource_metadata_url(
            auth_settings.resource_server_url
        )
        return f'Bearer resource_metadata="{resource_metadata_url}"'
    except Exception:
        return "Bearer"


async def authorize_mcp_request(
    scope: dict[str, Any],
    auth_config: McpAuthConfig,
) -> AuthDecision:
    """Evaluate the static/OAuth bearer gate against one allowlist snapshot."""
    bearer_allow = mcp_bearer_tokens()
    if not bearer_allow and auth_config.oauth_provider is None:
        return AuthDecision(allowed=True)

    authorization = Headers(scope=scope).get("authorization")
    static_bearer_ok = bool(bearer_allow) and check_mcp_bearer(
        authorization,
        bearer_allow,
    )
    oauth_bearer_ok = False
    oauth_client_id = None
    if auth_config.oauth_provider is not None:
        required_scopes = (
            auth_config.auth_settings.required_scopes
            if auth_config.auth_settings
            else auth_config.required_scopes
        )
        oauth_bearer_ok, client_id = await check_oauth_bearer(
            authorization,
            auth_config.oauth_provider,
            required_scopes=required_scopes,
        )
        if client_id:
            oauth_client_id = f"oauth:{client_id}"

    if static_bearer_ok or oauth_bearer_ok:
        return AuthDecision(allowed=True, oauth_client_id=oauth_client_id)

    detail = (
        "valid bearer token or OAuth access token required for /mcp"
        if auth_config.oauth_provider is not None
        else "valid bearer token required for /mcp"
    )
    return AuthDecision(
        allowed=False,
        response=JSONResponse(
            {"error": "unauthorized", "detail": detail},
            status_code=401,
            headers={
                "WWW-Authenticate": _www_authenticate_header(
                    auth_config.auth_settings
                )
            },
        ),
    )


def capture_transport_context(
    scope: dict[str, Any],
    *,
    oauth_client_id: str | None,
) -> TransportContextTokens:
    """Capture request headers into transport contextvars without resolving identity."""
    tokens = TransportContextTokens()
    try:
        headers = Headers(scope=scope)
        mcp_session_id = headers.get("mcp-session-id")
        client = scope.get("client")
        client_ip = client[0] if client and len(client) >= 1 else "unknown"
        user_agent = headers.get("user-agent", "unknown")
        ua_fingerprint = hashlib.md5(user_agent.encode()).hexdigest()[:6]
        note_ua_fingerprint(ua_fingerprint, user_agent)

        x_session_id = headers.get("x-session-id")
        x_client_id = headers.get("x-client-id") or headers.get("x-mcp-client-id")
        detected_client = detect_client_from_user_agent(user_agent)
        from src.model_harness_provenance import runtime_signal_fields_from_headers

        runtime_fields = runtime_signal_fields_from_headers(headers)
        ip_ua_fingerprint = f"{client_ip}:{ua_fingerprint}"
        peer_pid = scope.get("unitares_peer_pid")

        signals = SessionSignals(
            mcp_session_id=mcp_session_id,
            x_session_id=x_session_id,
            x_client_id=x_client_id,
            oauth_client_id=oauth_client_id,
            ip_ua_fingerprint=ip_ua_fingerprint,
            user_agent=user_agent,
            client_hint=detected_client,
            **runtime_fields,
            x_agent_name=headers.get("x-agent-name"),
            x_agent_id=headers.get("x-agent-id"),
            transport="uds" if peer_pid is not None else "mcp",
            peer_pid=peer_pid,
            unitares_operator_token=headers.get("x-unitares-operator"),
        )
        tokens.signals = set_session_signals(signals)
        if mcp_session_id:
            tokens.mcp_session = set_mcp_session_id(mcp_session_id)

        client_id = x_session_id or oauth_client_id or x_client_id or ip_ua_fingerprint
        scope.setdefault("state", {})["governance_client_id"] = client_id
        tokens.session_context = set_session_context(
            session_key=signals.ip_ua_fingerprint or "unknown",
            client_session_id=x_session_id or x_client_id,
            user_agent=user_agent,
        )
        if detected_client:
            tokens.client_hint = set_transport_client_hint(detected_client)
        return tokens
    except Exception:
        reset_transport_context(tokens)
        raise


def reset_transport_context(tokens: TransportContextTokens) -> None:
    """Best-effort reset of every contextvar set at request entry."""
    resets = (
        (tokens.session_context, reset_session_context),
        (tokens.mcp_session, reset_mcp_session_id),
        (tokens.client_hint, reset_transport_client_hint),
        (tokens.signals, reset_session_signals),
    )
    for token, reset in resets:
        if token is None:
            continue
        try:
            reset(token)
        except Exception:
            pass


def make_streamable_mcp_asgi(
    session_manager: Any,
    *,
    auth_config: McpAuthConfig,
) -> AsgiCallable:
    """Build the authenticated ASGI adapter around the SDK session manager."""

    async def streamable_mcp_asgi(scope, receive, send) -> None:
        if scope.get("type") != "http":
            return

        decision = await authorize_mcp_request(scope, auth_config)
        if not decision.allowed:
            assert decision.response is not None
            await decision.response(scope, receive, send)
            return

        tokens = TransportContextTokens()
        try:
            tokens = capture_transport_context(
                scope,
                oauth_client_id=decision.oauth_client_id,
            )
        except Exception as exc:
            logger.debug("[/mcp] Could not capture context: %s", exc)

        try:
            await session_manager.handle_request(scope, receive, send)
        except Exception as exc:
            logger.error("Streamable HTTP error: %s", exc, exc_info=True)
            await JSONResponse(
                {
                    "error": "Streamable HTTP transport error",
                    "details": str(exc),
                },
                status_code=500,
            )(scope, receive, send)
        finally:
            reset_transport_context(tokens)

    return streamable_mcp_asgi


def _log_transport_security(session_manager: Any, *, host: str) -> None:
    security = session_manager.security_settings
    if security is not None and security.enable_dns_rebinding_protection:
        logger.info(
            "/mcp Host/Origin validation ENFORCED — allowed_hosts=%s "
            "allowed_origins=%s (add more via UNITARES_MCP_ALLOWED_HOSTS / _ORIGINS)",
            security.allowed_hosts,
            security.allowed_origins,
        )
        return
    logger.warning(
        "/mcp Host/Origin validation DISABLED via "
        "UNITARES_MCP_DNS_REBIND_PROTECTION — any Host/Origin is accepted%s",
        "; server is bound to all interfaces" if host == "0.0.0.0" else "",
    )


def _create_base_application(mcp: Any) -> Any:
    """Create the body-reading-capable SDK app without its unused SSE bypass.

    The bare Starlette app historically broke REST POST body reads. The SDK's
    SSE app supplies the required plumbing, but its /sse and /messages routes
    reach the same tools without the /mcp bearer gate, so they must be pruned.
    """
    app = mcp.sse_app()
    sse_path = getattr(mcp.settings, "sse_path", "/sse")
    message_path = getattr(mcp.settings, "message_path", "/messages/").rstrip("/")
    pruned = [
        route
        for route in app.routes
        if getattr(route, "path", None) in (sse_path, message_path)
    ]
    app.routes[:] = [
        route
        for route in app.routes
        if getattr(route, "path", None) not in (sse_path, message_path)
    ]
    logger.info(
        "Pruned %d unused/ungated SSE route(s): %s, %s",
        len(pruned),
        sse_path,
        message_path,
    )
    return app


def _configure_middleware(
    app: Any,
    *,
    server_ready_fn: Callable[[], bool],
    server_version: str,
) -> None:
    cors_origins = [
        "http://localhost:8767",
        "http://127.0.0.1:8767",
    ]
    cors_allow_origin = os.getenv("UNITARES_HTTP_CORS_ALLOW_ORIGIN")
    if cors_allow_origin:
        cors_origins.append(cors_allow_origin)
    cors_origins.extend(cors_extra_origins())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["*"],
        # Passkey sessions stay same-origin; credentialed CORS would make the
        # cookie ambient cross-origin state and weaken the explicit CSRF header.
        allow_credentials=False,
    )
    app.add_middleware(
        ConnectionTrackingMiddleware,
        server_ready_fn=server_ready_fn,
        server_version=server_version,
    )
    # This is the single Wave 3 503 measurement point. Cutover proxies must not
    # emit a second numerator row for the same response.
    app.add_middleware(Transport503EmissionMiddleware)


def _register_application_routes(
    app: Any,
    *,
    mcp: Any,
    session_manager: Any,
    auth_config: McpAuthConfig,
    server_ready_fn: Callable[[], bool],
    server_start_time: float,
    server_version: str,
    server_build_sha: str,
) -> None:
    from src.http_api import register_http_routes
    from src.mcp_handlers.wave3a_admin import register_wave3a_admin_routes
    from src.mcp_handlers.wave3a_probe import register_wave3a_probe_routes
    from src.wave3a_routing import apply_env_flag_routes

    register_http_routes(
        app,
        server_ready_fn=server_ready_fn,
        server_start_time=server_start_time,
        server_version=server_version,
        has_streamable_http=True,
        mcp_server_name=mcp.name,
        server_build_sha=server_build_sha,
    )
    register_wave3a_probe_routes(app)
    register_wave3a_admin_routes(app)

    # The Wave3a routing table starts empty each boot; explicit environment
    # flags opt handlers into the BEAM path.
    wave3a_added = apply_env_flag_routes()
    if wave3a_added:
        logger.info(
            "[wave3a-routing] startup-hook added %d route(s): %s",
            len(wave3a_added),
            wave3a_added,
        )

    app.routes.append(
        Mount(
            "/mcp",
            app=make_streamable_mcp_asgi(
                session_manager,
                auth_config=auth_config,
            ),
        )
    )
    logger.info("Registered /mcp endpoint for Streamable HTTP transport")


def build_transport_runtime(
    mcp: Any,
    *,
    auth_config: McpAuthConfig,
    host: str,
    port: int,
    reload: bool,
    server_ready_fn: Callable[[], bool],
    set_server_ready: Callable[[], None],
    server_start_time: float,
    server_version: str,
    server_build_sha: str,
) -> McpTransportRuntime:
    """Assemble the ASGI app, streamable manager, and uvicorn server."""
    import uvicorn

    from src.background_tasks import start_all_background_tasks
    from src.mcp_compat import lowlevel_server
    from src.mcp_listen_config import build_streamable_session_manager

    # Attach DNS-rebinding settings to the manager that actually serves /mcp;
    # high-level FastMCP settings do not reach this manually mounted transport.
    session_manager = build_streamable_session_manager(lowlevel_server(mcp))
    logger.info("Streamable HTTP transport available at /mcp")
    _log_transport_security(session_manager, host=host)

    app = _create_base_application(mcp)
    _configure_middleware(
        app,
        server_ready_fn=server_ready_fn,
        server_version=server_version,
    )
    start_all_background_tasks(set_ready=set_server_ready)
    _register_application_routes(
        app,
        mcp=mcp,
        session_manager=session_manager,
        auth_config=auth_config,
        server_ready_fn=server_ready_fn,
        server_start_time=server_start_time,
        server_version=server_version,
        server_build_sha=server_build_sha,
    )

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        reload=reload,
        limit_concurrency=100,
        timeout_keep_alive=5,
        timeout_graceful_shutdown=10,
        forwarded_allow_ips="127.0.0.1",
        proxy_headers=True,
        ws="websockets-sansio",
    )
    return McpTransportRuntime(
        app=app,
        session_manager=session_manager,
        server=uvicorn.Server(config),
    )


async def _start_uds_listener(app: Any) -> tuple[str | None, asyncio.Task[None] | None]:
    """Start the optional kernel-attested resident listener."""
    socket_path = os.getenv("UNITARES_UDS_SOCKET")
    if not socket_path:
        return None, None
    try:
        from src.uds_listener import start_uds_listener

        task = await start_uds_listener(app, socket_path)
        logger.info("[UDS] substrate-attestation listener started at %s", socket_path)
        return socket_path, task
    except Exception as exc:
        logger.error(
            "[UDS] failed to start listener at %s: %s; "
            "HTTP-only mode (substrate residents will fall back)",
            socket_path,
            exc,
            exc_info=True,
        )
        return socket_path, None


async def _stop_uds_listener(
    socket_path: str | None,
    task: asyncio.Task[None] | None,
) -> None:
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    if socket_path and os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass

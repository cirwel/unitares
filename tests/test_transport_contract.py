"""The transport instrumentation contract — a zero must mean "unused".

A zero in ``audit.tool_usage`` cannot distinguish "nobody called this" from
"a whole transport never recorded" unless every surface that can execute a
tool is contractually wired to the shared recorder. #1424 was exactly that
failure: the MCP-protocol wrapper (the only path Claude Code / claude.ai
clients take) recorded nothing, so months of MCP traffic audited as silence.
The per-surface behavioural locks live next to their surfaces
(``test_tool_usage_payload.py`` for the MCP wrapper,
``test_http_tool_service_tool_usage.py`` for REST); this file pins the
CROSS-surface contract that no single-surface test states:

1. one tool call through the real dispatch pipeline binds an identity,
   round-trips a result, and lands one attributed audit row;
2. every dispatch entry point feeds ``record_tool_usage`` — adding a new
   transport without enrolling it here recreates the #1424 blind spot;
3. the SDK's ungated ``/sse`` + ``/messages`` bypass stays pruned, so no
   route reaches the tools without the ``/mcp`` bearer gate.
"""

import inspect
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.mcp_handlers  # noqa: F401 — registers every tool definition
from src.mcp_handlers.decorators import _TOOL_DEFINITIONS, ToolDefinition

AGENT_UUID = "9a41c6de-55f0-4c34-8e0e-3d2c6a1b7f42"
PROBE_TOOL = "transport_contract_probe"


def _consume_coro(coro, name=None):
    if hasattr(coro, "close"):
        coro.close()
    return MagicMock()


def _patch_recorder_io(monkeypatch):
    tracker = MagicMock()
    append = AsyncMock(return_value=True)
    monkeypatch.setattr("src.tool_usage_tracker.get_tool_usage_tracker", lambda: tracker)
    monkeypatch.setattr("src.audit_db.append_tool_usage_async", append)
    monkeypatch.setattr("src.background_tasks.create_tracked_task", _consume_coro)
    return tracker, append


# ---------------------------------------------------------------------------
# Fixtures — the same isolation set test_dispatch_tool_integration.py uses to
# run the full 10-step pipeline without live Postgres/Redis.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_registry():
    from src.mcp_handlers import TOOL_HANDLERS
    orig_defs = dict(_TOOL_DEFINITIONS)
    orig_handlers = dict(TOOL_HANDLERS)
    yield
    _TOOL_DEFINITIONS.clear()
    _TOOL_DEFINITIONS.update(orig_defs)
    TOOL_HANDLERS.clear()
    TOOL_HANDLERS.update(orig_handlers)


@pytest.fixture
def pipeline_mocks():
    with patch(
        "src.mcp_handlers.identity.handlers.resolve_session_identity",
        new_callable=AsyncMock,
    ) as identity, patch(
        "src.mcp_handlers.identity.handlers.lookup_onboard_pin",
        new_callable=AsyncMock,
    ) as onboard_pin, patch(
        "src.mcp_handlers.identity.handlers.derive_session_key",
        new_callable=AsyncMock,
    ) as session_key, patch(
        "src.mcp_handlers.middleware.rate_limit_step.get_rate_limiter"
    ) as rate_limiter, patch(
        "src.pattern_tracker.get_pattern_tracker"
    ) as pattern_tracker, patch(
        "src.tool_schemas.get_pydantic_schemas"
    ) as schemas, patch(
        "src.mcp_handlers.tool_stability.resolve_tool_alias"
    ) as alias:
        identity.return_value = {
            "agent_uuid": AGENT_UUID,
            "agent_name": "ContractProbe",
            "created": False,
            "persisted": True,
        }
        onboard_pin.return_value = None
        session_key.return_value = "contract-test-session-key"
        limiter = MagicMock()
        limiter.check_rate_limit.return_value = (True, None)
        limiter.get_stats.return_value = {}
        rate_limiter.return_value = limiter
        tracker = MagicMock()
        tracker.record_tool_call.return_value = None
        tracker.record_progress.return_value = None
        pattern_tracker.return_value = tracker
        schemas.return_value = {}
        alias.side_effect = lambda name: (name, None)
        yield identity


def _register_probe_tool():
    from mcp.types import TextContent
    from src.mcp_handlers import TOOL_HANDLERS

    async def handler(arguments):
        return [
            TextContent(
                type="text",
                text=json.dumps(
                    {"success": True, "bound_agent_id": arguments.get("agent_id")}
                ),
            )
        ]

    TOOL_HANDLERS[PROBE_TOOL] = handler
    _TOOL_DEFINITIONS[PROBE_TOOL] = ToolDefinition(
        name=PROBE_TOOL, handler=handler, timeout=30.0, description="contract probe"
    )


# ---------------------------------------------------------------------------
# 1. One call, all three contract properties, through the real pipeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_mcp_call_binds_identity_round_trips_and_audits(monkeypatch, pipeline_mocks):
    """The whole contract on one call: the MCP wrapper runs the REAL dispatch
    pipeline (identity resolution → alias → validation → handler), returns the
    handler's result, and exits through exactly one attributed audit row.

    ``dispatch_tool`` is deliberately NOT patched — a wrapper test that fakes
    dispatch can't notice a pipeline change that stops identity from binding.
    """
    import src.tool_registration as tr

    tracker, append = _patch_recorder_io(monkeypatch)
    scheduled = []
    monkeypatch.setattr(
        "src.mcp_handlers.identity.agent_presence_lease.schedule_agent_presence_heartbeat",
        lambda agent_id, client_session_id=None: scheduled.append(agent_id),
    )
    monkeypatch.setattr(tr, "_wave3a_get_route", lambda _n: None)

    _register_probe_tool()
    tr._tool_wrappers_cache.clear()
    try:
        result = await tr.get_tool_wrapper(PROBE_TOOL)(
            agent_id=AGENT_UUID, client_session_id="contract-csid"
        )
    finally:
        tr._tool_wrappers_cache.clear()

    # (c) round-trip: the handler's payload came back through the pipeline
    # (the wrapper parses the TextContent JSON into a dict for MCP clients).
    assert result["success"] is True
    assert result["bound_agent_id"] == AGENT_UUID

    # (a) identity: the middleware resolution step actually ran for the call.
    assert pipeline_mocks.await_count >= 1

    # (b) audit: exactly one attributed row, audit-only (no JSONL enrolment,
    # no presence-lease refresh — the #1424 fix's measured boundary).
    assert append.call_count == 1
    assert append.call_args.kwargs["tool_name"] == PROBE_TOOL
    assert append.call_args.kwargs["agent_id"] == AGENT_UUID
    assert append.call_args.kwargs["session_id"] == "contract-csid"
    assert tracker.log_tool_call.call_count == 0
    assert scheduled == []


# ---------------------------------------------------------------------------
# 2. The dispatch-surface census
# ---------------------------------------------------------------------------

def test_every_dispatch_entry_point_feeds_the_shared_recorder():
    """Census of every code path that can execute a tool for a caller. Each
    must call the shared ``record_tool_usage`` recorder; a surface missing
    from this table, or present but not recording, is the #1424 blind spot
    where a transport's entire history audits as "no traffic".

    Adding a NEW transport surface? Add its entry function here and give it a
    behavioural recording test beside its own module (see
    ``test_tool_usage_payload.py`` / ``test_http_tool_service_tool_usage.py``
    for the pattern).
    """
    import src.mcp_server_std as std
    import src.tool_registration as tr
    from src.services import http_tool_service

    entry_points = {
        "mcp_streamable_http_wrapper": tr.get_tool_wrapper,
        "rest_v1_tools_call": http_tool_service.execute_http_tool,
        "stdio_call_tool": std.call_tool,
    }
    unwired = [
        name
        for name, fn in entry_points.items()
        if "record_tool_usage(" not in inspect.getsource(fn)
    ]
    assert not unwired, (
        f"Dispatch surface(s) {unwired} no longer call record_tool_usage — "
        "their traffic would audit as silence (#1424 class)."
    )


# ---------------------------------------------------------------------------
# 3. The ungated SSE bypass stays pruned
# ---------------------------------------------------------------------------

class _StubSettings:
    def __init__(self, sse_path="/sse", message_path="/messages/"):
        self.sse_path = sse_path
        self.message_path = message_path


class _StubMcp:
    """Minimal stand-in for FastMCP: sse_app() + settings, nothing else."""

    def __init__(self, app, settings):
        self._app = app
        self.settings = settings

    def sse_app(self):
        return self._app


def _routes(paths):
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def ok(request):  # pragma: no cover - never invoked
        return PlainTextResponse("ok")

    return [Route(path, ok) for path in paths]


def test_sse_and_messages_routes_are_pruned():
    """`/sse` and `/messages` reach the same tools WITHOUT the /mcp bearer
    gate, so `_create_base_application` must strip them while keeping the
    SDK plumbing routes it actually wants. If this pruning ever stops
    matching, an unauthenticated transport silently reopens."""
    from starlette.applications import Starlette

    from src.services.mcp_transport_service import _create_base_application

    app = Starlette(routes=_routes(["/sse", "/messages", "/keepme"]))
    pruned = _create_base_application(_StubMcp(app, _StubSettings()))

    remaining = [route.path for route in pruned.routes]
    assert remaining == ["/keepme"]


def test_sse_pruning_follows_sdk_settings_not_hardcoded_paths():
    """The prune keys off ``mcp.settings`` so an SDK that relocates its SSE
    routes is still pruned — hardcoded "/sse" would rot silently."""
    from starlette.applications import Starlette

    from src.services.mcp_transport_service import _create_base_application

    app = Starlette(routes=_routes(["/events", "/msg", "/keepme"]))
    pruned = _create_base_application(
        _StubMcp(app, _StubSettings(sse_path="/events", message_path="/msg/"))
    )

    remaining = [route.path for route in pruned.routes]
    assert remaining == ["/keepme"]

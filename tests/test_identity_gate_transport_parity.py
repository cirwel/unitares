"""The MCP identity middleware and the REST gate judge every call name alike.

Both #425 gates ask ``get_call_identity_requirement`` whether a call may run
unbound. The resolver is alias-aware: for ``search_shared_memory`` it applies
the alias's ``inject_action`` and judges ``knowledge(action='search')``. That
only works when the gate hands it the name the caller INVOKED.

``resolve_identity`` runs before ``resolve_alias`` in the dispatch pipeline, so
at that step the action an alias implies has not been injected yet. Handing the
resolver the already-canonicalized tool name drops the action. The resolver then
falls back to the router's ``default_action``, and 22 of the 70 aliases got the
opposite answer from the one the REST gate gave for the same call. For example,
a proofless ``search_shared_memory`` was treated as an identity-requiring write,
and ``request_review`` as an unbound read.

These tests run the real middleware step over the whole call roster (every
registered tool and every alias). Each result is compared with the resolver the
REST gate uses, so a new alias or router action is covered without editing this
file.

REST prebind has the same shape of defect: its skip list names canonical tools,
but it is matched against the invoked name, so an alias of `onboard` was
pre-bound where `onboard` itself is not.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Settle handler imports so every tool and router is registered.
import src.mcp_handlers  # noqa: F401
import src.mcp_handlers.consolidated  # noqa: F401
import src.mcp_handlers.core  # noqa: F401
import src.mcp_handlers.support.model_inference  # noqa: F401

from src.mcp_handlers.decorators import get_call_identity_requirement, get_tool_registry
from src.mcp_handlers.middleware import DispatchContext, resolve_identity
from src.mcp_handlers.middleware.identity_step import _IDENTITY_LIFECYCLE_TOOLS
from src.mcp_handlers.tool_stability import _TOOL_ALIASES, resolve_tool_alias


def _call_roster() -> list[str]:
    return sorted(set(get_tool_registry()) | set(_TOOL_ALIASES))


CALL_ROSTER = _call_roster()


def _patches(resolve_mock, session_key: str):
    db = AsyncMock()
    db.update_session_activity = AsyncMock(return_value=True)
    return [
        patch("src.mcp_handlers.context.get_session_signals", return_value=None),
        patch(
            "src.mcp_handlers.identity.handlers.derive_session_key",
            new_callable=AsyncMock,
            return_value=session_key,
        ),
        patch("src.mcp_handlers.identity.handlers.resolve_session_identity", resolve_mock),
        patch("src.mcp_handlers.context.set_session_context", return_value=MagicMock()),
        patch("src.db.get_db", return_value=db),
    ]


async def _run_middleware(name: str, arguments: dict, resolve_mock, session_key: str):
    patches = _patches(resolve_mock, session_key)
    for p in patches:
        p.start()
    try:
        ctx = DispatchContext()
        return await resolve_identity(name, arguments, ctx)
    finally:
        for p in reversed(patches):
            p.stop()


def test_roster_includes_the_aliases_that_used_to_diverge():
    """Guard against a vacuous parametrization: the roster is the full table."""
    assert len(_TOOL_ALIASES) >= 70
    assert {"search_shared_memory", "request_review", "list_agents"} <= set(CALL_ROSTER)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CALL_ROSTER)
async def test_proofless_call_short_circuits_exactly_when_the_rest_resolver_says_pre_onboard(name):
    """No proof: the middleware skips resolution iff the call is a pre_onboard read.

    Lifecycle tools are deliberately excluded from the short-circuit (their
    handlers need the dispatch-threaded resolution), so they must resolve.
    """
    resolve_mock = AsyncMock(return_value={
        "resume_failed": True,
        "error": "session_resolve_miss",
        "session_key": "fp-session",
    })

    await _run_middleware(name, {}, resolve_mock, "fp-session")

    canonical, _ = resolve_tool_alias(name)
    expect_short_circuit = (
        get_call_identity_requirement(name, {}) == "pre_onboard"
        and canonical not in _IDENTITY_LIFECYCLE_TOOLS
    )
    resolved = resolve_mock.await_count > 0
    assert resolved is not expect_short_circuit, (
        f"{name} (-> {canonical}): middleware "
        f"{'resolved' if resolved else 'short-circuited'}, but the call-level "
        f"requirement is {get_call_identity_requirement(name, {})!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("name", CALL_ROSTER)
async def test_strict_session_miss_refuses_exactly_when_the_rest_gate_refuses(name, monkeypatch):
    """Stale proof under strict identity: both transports refuse the same calls."""
    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    resolve_mock = AsyncMock(return_value={
        "resume_failed": True,
        "error": "session_resolve_miss",
        "session_key": "stale-session",
    })

    result = await _run_middleware(
        name, {"client_session_id": "stale-session"}, resolve_mock, "stale-session",
    )
    mcp_refused = isinstance(result, list)

    from src.services.http_tool_service import _strict_identity_refusal_or_none

    with patch("src.mcp_handlers.context.get_context_resolved_agent_id", return_value=None):
        rest_refused = _strict_identity_refusal_or_none(
            name, {"client_session_id": "stale-session"},
        ) is not None

    assert mcp_refused is rest_refused, (
        f"{name} (-> {resolve_tool_alias(name)[0]}): MCP middleware "
        f"{'refused' if mcp_refused else 'passed'}, REST gate "
        f"{'refused' if rest_refused else 'passed'}"
    )


def _prebind_skipped_aliases() -> list[str]:
    from src.http_routes.access import _HTTP_PREBIND_SKIP_TOOLS

    return sorted(
        alias for alias, info in _TOOL_ALIASES.items()
        if info.new_name in _HTTP_PREBIND_SKIP_TOOLS
    )


def test_prebind_skip_roster_is_not_empty():
    """start_session is the alias whose prebind mattered; keep it in scope."""
    assert "start_session" in _prebind_skipped_aliases()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", _prebind_skipped_aliases())
async def test_rest_prebind_skips_every_alias_of_a_skipped_tool(name, monkeypatch):
    """REST prebind must treat an alias exactly as the tool it dispatches to.

    Prebind resolves an identity before dispatch. A skipped tool never does,
    so neither may its alias: `start_session(force_new=true)` was pre-bound to
    a sticky-cached agent while `onboard(force_new=true)` was left alone.
    """
    import src.http_routes.access as access

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError(f"prebind ran for {name}")

    monkeypatch.setattr(access, "_bind_explicit_http_agent", _must_not_run)
    monkeypatch.setattr(access, "_resolve_http_operator", _must_not_run)
    monkeypatch.setattr(access, "_consult_http_sticky_binding", _must_not_run)
    monkeypatch.setattr(access, "_resolve_http_session_binding", _must_not_run)

    assert await access._resolve_http_bound_agent(name, {"force_new": True}, None) is None


# ---------------------------------------------------------------------------
# The shared resolver names the action dispatch will route
# ---------------------------------------------------------------------------


def _router_aliases() -> list[str]:
    """Aliases whose target is an action_router, the routers whose rule is known."""
    from src.mcp_handlers.decorators import get_tool_definition

    names = []
    for alias, info in sorted(_TOOL_ALIASES.items()):
        td = get_tool_definition(info.new_name)
        if td is not None and td.known_actions and td.handler.__module__ == "src.mcp_handlers.decorators":
            names.append(alias)
    return names


def _argument_shapes(alias: str) -> list[dict]:
    from src.mcp_handlers.decorators import get_tool_definition

    info = _TOOL_ALIASES[alias]
    td = get_tool_definition(info.new_name)
    other = sorted(a for a in td.known_actions if a != info.inject_action)[0]
    return [
        {},
        {"action": other},
        {"op": other},
        {"action": other.upper()},
        {"action": "", "op": other},
        {"action": ""},
    ]


async def _dispatched_action(alias: str, arguments: dict):
    """Run the real alias step, then apply the action_router's own read."""
    from src.mcp_handlers.decorators import get_tool_definition
    from src.mcp_handlers.middleware.params_step import resolve_alias

    args = dict(arguments)
    name, args, _ctx = await resolve_alias(alias, args, DispatchContext())
    td = get_tool_definition(name)
    return name, (args.get("action") or args.get("op") or "").lower() or td.default_action


def test_router_alias_roster_is_not_empty():
    assert {"store_finding", "search_shared_memory", "request_review"} <= set(_router_aliases())


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", _router_aliases())
async def test_resolver_names_the_action_dispatch_routes(alias):
    """The gates judge (tool, action) through this resolver; it must be the call that runs.

    An alias injects its action only when the caller sent no `action` key,
    and the router reads `action` before `op`, so store_finding(op="search")
    runs the store. The resolver used to read `action or op` first and judged
    that call as a knowledge search, a pre_onboard read.
    """
    from src.mcp_handlers.decorators import resolve_canonical_action_and_source

    mismatches = []
    for arguments in _argument_shapes(alias):
        dispatched = await _dispatched_action(alias, arguments)
        canonical, action, _source = resolve_canonical_action_and_source(alias, dict(arguments))
        if (canonical, action) != dispatched:
            mismatches.append(f"{arguments}: resolver {(canonical, action)}, dispatch {dispatched}")
    assert not mismatches, f"{alias}:\n" + "\n".join(mismatches)


@pytest.mark.asyncio
async def test_a_write_alias_with_a_read_op_is_not_waved_through_unbound(monkeypatch):
    """store_finding(op="search") dispatches as knowledge(action="store").

    Judged as the search its `op` named, a proofless call short-circuited
    identity resolution and, under strict identity, skipped the typed refusal
    on the way to a write.
    """
    monkeypatch.setenv("STRICT_IDENTITY_REQUIRED", "true")
    resolve_mock = AsyncMock(return_value={
        "resume_failed": True,
        "error": "session_resolve_miss",
        "session_key": "fp-session",
    })

    result = await _run_middleware(
        "store_finding", {"op": "search", "summary": "x"}, resolve_mock, "fp-session",
    )

    assert resolve_mock.await_count == 1
    assert isinstance(result, list), "strict identity must refuse the unbound store"

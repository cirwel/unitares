"""What the server teaches about agent_id must pass the impersonation guard.

``inject_identity`` refuses a bound session that passes another agent's id as
``agent_id`` unless the call is an operator-target tool (``_OPERATOR_TARGET_CALLS``).
Three surfaces taught calls that guard refuses:
- the observe router's example ``observe(action='agent', agent_id=...)``;
- the ``observe_agent`` migration note;
- the guard's own recovery text, which promised ``knowledge(action='search',
  agent_id=X)`` was unrestricted.

``observe`` has taken the target as ``target_agent_id`` since 2.6.1, when this
same "Session mismatch" was fixed by that rename.

The council held on 2026-09-13 decided not to add name exemptions: in the
dialectic phases agent_id names the actor, so an exemption would let a bound
caller submit as another participant. The pins below keep that decision from
being undone quietly, and keep the teaching surfaces honest.

It also records two status-quo decisions from the same council:
- aliases of onboard/identity do not take middleware PATH 0 (keying stays on
  the invoked name; extending PATH 0 to aliases is an identity-posture change);
- session-injection membership stays at its eight tools, because injection
  changes which session key a call binds to, not only audit attribution.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.mcp_handlers  # noqa: F401
import src.mcp_handlers.consolidated  # noqa: F401

from src.mcp_handlers.middleware import (
    DispatchContext,
    inject_identity,
    resolve_alias,
    resolve_identity,
    validate_params,
)
from src.mcp_handlers.tool_stability import _TOOL_ALIASES

REPO_ROOT = Path(__file__).resolve().parents[1]
BOUND = "bound-uuid-1234"
OTHER = "other-agent-uuid-5678"
CALL = re.compile(r"\b([a-z_]+)\(([^()]*)\)")


def _call_kwargs(text: str) -> tuple[str, dict] | None:
    try:
        node = ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        return None
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return None
    kwargs = {}
    for kw in node.keywords:
        if kw.arg and isinstance(kw.value, ast.Constant):
            kwargs[kw.arg] = kw.value.value
        elif kw.arg and isinstance(kw.value, ast.Name):
            if kw.value.id in {"true", "false"}:
                kwargs[kw.arg] = kw.value.id == "true"
            elif kw.value.id == "null":
                kwargs[kw.arg] = None
    return node.func.id, kwargs


def _router_examples() -> list[str]:
    examples = []
    tree = ast.parse((REPO_ROOT / "src/mcp_handlers/consolidated.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "action_router":
            for kw in node.keywords:
                if kw.arg == "examples" and isinstance(kw.value, ast.List):
                    examples += [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
    return examples


def _migration_note_calls() -> list[str]:
    calls = []
    for info in _TOOL_ALIASES.values():
        for match in CALL.finditer(info.migration_note or ""):
            calls.append(match.group(0))
    return calls


def _taught_calls_with_agent_id() -> list[tuple[str, dict]]:
    taught = []
    for text in _router_examples() + _migration_note_calls():
        parsed = _call_kwargs(text)
        if parsed and "agent_id" in parsed[1]:
            taught.append(parsed)
    return taught


async def _guard(name: str, arguments: dict):
    ctx = DispatchContext(bound_agent_id=BOUND)
    server = MagicMock()
    server.agent_metadata = {}
    with patch("src.mcp_handlers.context.get_context_agent_id", return_value=BOUND), patch(
        "src.mcp_handlers.shared.get_mcp_server", return_value=server,
    ):
        return await inject_identity(name, dict(arguments), ctx)


async def _bound_alias_call(name: str, arguments: dict):
    """Run the public alias through the bound call's real parameter steps."""
    ctx = DispatchContext(bound_agent_id=BOUND)
    server = MagicMock()
    server.agent_metadata = {}
    with patch("src.mcp_handlers.context.get_context_agent_id", return_value=BOUND), patch(
        "src.mcp_handlers.shared.get_mcp_server", return_value=server,
    ):
        result = await resolve_alias(name, dict(arguments), ctx)
        if isinstance(result, list):
            return result
        result = await inject_identity(*result)
        if isinstance(result, list):
            return result
        return await validate_params(*result)


def test_the_example_and_note_scans_see_something():
    assert len(_router_examples()) > 10
    assert any("observe(" in call for call in _migration_note_calls())


@pytest.mark.asyncio
async def test_no_taught_call_is_refused_for_passing_another_agent_id():
    refused = []
    for name, kwargs in _taught_calls_with_agent_id():
        result = await _guard(name, {**kwargs, "agent_id": OTHER})
        if isinstance(result, list):
            refused.append(f"{name}({kwargs})")
    assert not refused, refused


@pytest.mark.asyncio
async def test_observe_target_agent_id_passes_the_guard():
    """The read path the refusal now recommends must itself pass."""
    result = await _guard("observe", {"action": "agent", "target_agent_id": OTHER})
    assert not isinstance(result, list)


@pytest.mark.asyncio
async def test_describe_tool_observe_examples_pass_the_bound_alias_path():
    """Live discovery examples must survive the same alias/guard path as calls."""
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool

    described = await handle_describe_tool({"tool_name": "observe_agent"})
    common_patterns = json.loads(described[0].text)["common_patterns"]
    assert len(common_patterns) == 3

    for example in common_patterns.values():
        parsed = _call_kwargs(example)
        assert parsed is not None
        name, kwargs = parsed
        assert name == "observe_agent"
        assert kwargs["target_agent_id"] == "my_agent"

        result = await _bound_alias_call(name, kwargs)
        assert not isinstance(result, list), example
        canonical_name, validated, _ctx = result
        assert canonical_name == "observe"
        assert validated["action"] == "agent"
        assert validated["target_agent_id"] == "my_agent"
        assert validated["agent_id"] == BOUND


@pytest.mark.asyncio
async def test_the_refusal_recommends_a_call_that_passes():
    refusal = await _guard("knowledge", {"action": "search", "agent_id": OTHER})
    assert isinstance(refusal, list)

    recovery = json.loads(refusal[0].text)["recovery"]
    name, kwargs = _call_kwargs(recovery["read_path"])
    assert not isinstance(await _guard(name, kwargs), list)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["thesis", "antithesis", "synthesis", "request"])
async def test_a_bound_caller_cannot_act_as_another_agent_in_a_dialectic(action):
    """No name exemption for dialectic phases (council, 2026-09-13)."""
    result = await _guard("dialectic", {"action": action, "agent_id": OTHER, "session_id": "s"})
    assert isinstance(result, list)
    assert "Session mismatch" in result[0].text


def test_session_injection_membership_is_the_decided_eight():
    """Widening changes identity resolution, not only attribution (council, 2026-09-13).

    An injected client_session_id is read before mcp_session_id, is scoped by
    model and is stamped server_inferred, so a new member binds differently.
    """
    from src.tool_registration import TOOLS_NEEDING_SESSION_INJECTION

    assert TOOLS_NEEDING_SESSION_INJECTION.tools == frozenset({
        "onboard", "identity", "process_agent_update", "get_governance_metrics",
        "search_knowledge_graph", "leave_note", "mark_response_complete", "dialectic",
    })
    assert not TOOLS_NEEDING_SESSION_INJECTION.actions


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["start_session", "authenticate", "bind_identity"])
async def test_aliases_of_onboard_and_identity_do_not_take_middleware_path0(alias, monkeypatch):
    """Keyed on the invoked name on purpose (council, 2026-09-13).

    Extending middleware PATH 0 to aliases changes identity posture on installs
    not running strict identity, so it needs its own decision.
    """
    monkeypatch.setenv("UNITARES_IDENTITY_STRICT", "log")
    victim = "99999999-9999-4999-8999-999999999999"
    db = AsyncMock()
    db.update_session_activity = AsyncMock(return_value=True)
    resolve_mock = AsyncMock(return_value={
        "resume_failed": True,
        "error": "session_resolve_miss",
        "session_key": "caller-session",
    })
    patches = [
        patch("src.mcp_handlers.context.get_session_signals", return_value=None),
        patch("src.mcp_handlers.identity.handlers.derive_session_key", new_callable=AsyncMock, return_value="caller-session"),
        patch("src.mcp_handlers.identity.handlers.resolve_session_identity", resolve_mock),
        patch("src.mcp_handlers.context.set_session_context", return_value=MagicMock()),
        patch("src.db.get_db", return_value=db),
    ]
    for p in patches:
        p.start()
    try:
        ctx = DispatchContext()
        await resolve_identity(alias, {"agent_uuid": victim}, ctx)
    finally:
        for p in reversed(patches):
            p.stop()

    assert ctx.bound_agent_id != victim
    assert (ctx.identity_result or {}).get("source") != "agent_uuid_passthrough"

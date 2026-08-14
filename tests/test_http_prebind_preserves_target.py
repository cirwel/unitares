"""A prebind path binds the CALLER; it must not retarget the CALL.

`_resolve_http_bound_agent` resolves two different things at once: who is
calling (context binding, which authorizes) and what the call is about (the
`agent_id` argument, which selects). All three prebind paths wrote the resolved
uuid into `arguments["agent_id"]` unconditionally. For the common self-read the
two coincide and nothing was wrong. When a caller named a target, the read came
back for the caller instead — under a `success` envelope, with the response's
own `agent_id` field reporting the substituted identity. That is the silent
substitution invariant 1 forbids.

It only bit a NON-uuid-shaped agent_id: `_bind_explicit_http_agent` returns
early on the uuid shape (36 chars / 4 hyphens) and never reaches the other
paths. A structured handle fell straight through — and since #1533 a read-state
response reports `agent_id` AS that handle, so the caller most likely to be
redirected was one round-tripping the field it had just been handed.

Observed live 2026-08-10: `get_governance_metrics(agent_id="zzz_not_an_agent")`
with an operator token returned metrics for `operator_20260810`.
"""

from __future__ import annotations

import pytest

import src.http_api as http_api


UUID_ID = "69a1a4f7-a30f-4f4a-bcf9-2de8606fb819"
OPERATOR_UUID = "5b1adba0-1111-2222-3333-444455556666"
HANDLE = "Claude_Code_20260805_33fcecfd"


# --- the helper itself ---

def test_stamps_when_no_target_named():
    """The self-read path: caller named nothing, so bind it."""
    args: dict = {}
    http_api._preserve_explicit_target(args, OPERATOR_UUID)
    assert args["agent_id"] == OPERATOR_UUID


def test_stamps_over_an_empty_target():
    args = {"agent_id": ""}
    http_api._preserve_explicit_target(args, OPERATOR_UUID)
    assert args["agent_id"] == OPERATOR_UUID


def test_preserves_a_named_handle_target():
    """The bug: a structured handle is a target, not an absence."""
    args = {"agent_id": HANDLE}
    http_api._preserve_explicit_target(args, OPERATOR_UUID)
    assert args["agent_id"] == HANDLE


def test_preserves_a_named_uuid_target():
    args = {"agent_id": UUID_ID}
    http_api._preserve_explicit_target(args, OPERATOR_UUID)
    assert args["agent_id"] == UUID_ID


# --- the operator path end to end ---

@pytest.mark.asyncio
async def test_operator_token_does_not_retarget_a_named_call(monkeypatch):
    """The exact live reproduction."""
    async def fake_resolve(_signals):
        return {"agent_uuid": OPERATOR_UUID}

    monkeypatch.setattr(
        "src.mcp_handlers.identity.operator.resolve_operator_identity",
        fake_resolve,
    )

    args = {"agent_id": HANDLE}
    resolved = await http_api._resolve_http_operator(args, object())

    # The caller is bound as the operator...
    assert resolved == OPERATOR_UUID
    # ...and the call still asks about what it asked about.
    assert args["agent_id"] == HANDLE


@pytest.mark.asyncio
async def test_operator_token_still_binds_an_unnamed_call(monkeypatch):
    """Regression guard: the self-read path must keep working."""
    async def fake_resolve(_signals):
        return {"agent_uuid": OPERATOR_UUID}

    monkeypatch.setattr(
        "src.mcp_handlers.identity.operator.resolve_operator_identity",
        fake_resolve,
    )

    args: dict = {}
    resolved = await http_api._resolve_http_operator(args, object())

    assert resolved == OPERATOR_UUID
    assert args["agent_id"] == OPERATOR_UUID


@pytest.mark.asyncio
async def test_operator_resolution_failure_leaves_target_alone(monkeypatch):
    async def boom(_signals):
        raise RuntimeError("operator lookup down")

    monkeypatch.setattr(
        "src.mcp_handlers.identity.operator.resolve_operator_identity", boom
    )

    args = {"agent_id": HANDLE}
    assert await http_api._resolve_http_operator(args, object()) is None
    assert args["agent_id"] == HANDLE


def test_all_prebind_paths_route_through_the_helper():
    """Structural lock. Three paths had the same unconditional write; a fourth
    added later must not reintroduce it. A bare
    `arguments["agent_id"] = <resolved>` outside the helper is the defect."""
    import importlib
    import inspect
    import pkgutil

    from src import http_routes

    sources = [inspect.getsource(http_api)]
    for mod_info in pkgutil.iter_modules(http_routes.__path__):
        module = importlib.import_module(f"src.http_routes.{mod_info.name}")
        sources.append(inspect.getsource(module))
    bare_writes = [
        line.strip()
        for source in sources
        for line in source.splitlines()
        if 'arguments["agent_id"] =' in line
    ]
    # The only permitted assignment is the one inside _preserve_explicit_target,
    # which writes its own parameter name.
    assert bare_writes == ['arguments["agent_id"] = resolved_uuid'], (
        f"unconditional agent_id overwrite outside the helper: {bare_writes}"
    )

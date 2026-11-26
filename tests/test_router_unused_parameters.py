"""A router names the parameters an action was sent and does not read.

A consolidated router advertises one flat schema for all of its actions, so
every parameter validates on every action. dialectic takes 31: a caller that
sends `proposed_conditions` with an antithesis (which reads `concerns`), or
`concerns` with a thesis, gets a success and a record with nothing from that
parameter in it. Observed 2026-11-26 as "a field you thought you filed can
vanish without an error".

The notice is opt-in per router (`action_router(report_unused_parameters=True)`)
because it is only as true as the router's ACTION_FIELDS, and two things make
that declaration less than the whole truth for a caller: an action that
forwards its arguments to another action's handler, and a parameter read by
the response envelope rather than the handler. The last section holds both
for every router that opts in.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import inspect
import json
import textwrap

import pytest

from src.mcp_handlers.decorators import get_tool_registry
from src.mcp_handlers.schemas.dialectic import DialecticParams
from src.mcp_handlers.schemas.router_actions import (
    declared_action_fields,
    supplied_fields_unused_by_action,
)
from src.tool_schemas import get_pydantic_schemas


def _validated(model, **arguments) -> dict:
    """Arguments as a handler receives them: every field present, defaults filled."""
    return model.model_validate(arguments).model_dump()


# ---------------------------------------------------------------------------
# Which parameters count
# ---------------------------------------------------------------------------


def test_a_parameter_declared_for_another_action_is_named():
    arguments = _validated(
        DialecticParams,
        action="antithesis",
        session_id="abc123",
        concerns=["the trajectory is not safe"],
        proposed_conditions=["re-read before each edit"],
    )
    assert supplied_fields_unused_by_action(
        DialecticParams, "antithesis", arguments
    ) == ("proposed_conditions",)


def test_parameters_are_named_in_schema_order():
    arguments = _validated(
        DialecticParams,
        action="thesis",
        session_id="abc123",
        root_cause="missed a checkpoint",
        concerns=["a reviewer's field"],
        position="quick's field",
        new_reviewer_id="reassign's field",
    )
    assert supplied_fields_unused_by_action(DialecticParams, "thesis", arguments) == (
        "position",
        "concerns",
        "new_reviewer_id",
    )


def test_a_call_that_sends_only_its_own_parameters_names_nothing():
    arguments = _validated(
        DialecticParams,
        action="synthesis",
        session_id="abc123",
        agrees=False,
        reasoning="the checkpoint concern stands",
        proposed_conditions=["gate on coherence"],
        observed_metrics={"coherence": 0.4},
        reviewer_provenance={"backend": "codex-cli"},
    )
    assert supplied_fields_unused_by_action(DialecticParams, "synthesis", arguments) == ()


def test_identity_and_selector_parameters_are_never_named():
    arguments = _validated(
        DialecticParams,
        action="quick",
        issue_description="ship it?",
        agent_id="11111111-2222-3333-4444-555555555555",
        client_session_id="agent-11111111-222",
        continuity_token="v1.token",
    )
    assert supplied_fields_unused_by_action(DialecticParams, "quick", arguments) == ()


def test_a_parameter_left_at_its_default_is_not_named():
    """Validation fills every absent field, so a default says nothing was sent.

    judgment_formed defaults to True and belongs to antithesis, synthesis and
    consult; it arrives as True on every thesis.
    """
    arguments = _validated(DialecticParams, action="thesis", session_id="abc123")
    assert arguments["judgment_formed"] is True
    assert supplied_fields_unused_by_action(DialecticParams, "thesis", arguments) == ()

    arguments = _validated(
        DialecticParams, action="thesis", session_id="abc123", judgment_formed=False
    )
    assert supplied_fields_unused_by_action(DialecticParams, "thesis", arguments) == (
        "judgment_formed",
    )


def test_empty_placeholders_are_not_named_and_false_is():
    """Some clients pad a call with empty values; that is not filing anything.

    False is different: agrees=False sent with an antithesis is a verdict the
    caller thinks it recorded.
    """
    arguments = _validated(
        DialecticParams,
        action="list",
        session_id="",
        concerns=[],
        observed_metrics={},
        check_timeout=False,
    )
    assert supplied_fields_unused_by_action(DialecticParams, "list", arguments) == (
        "check_timeout",
    )

    arguments = _validated(
        DialecticParams, action="antithesis", session_id="abc123", agrees=False
    )
    assert supplied_fields_unused_by_action(
        DialecticParams, "antithesis", arguments
    ) == ("agrees",)


def test_a_default_sent_as_a_string_is_not_named():
    assert supplied_fields_unused_by_action(
        DialecticParams, "list", {"action": "list", "judgment_formed": "true"}
    ) == ()
    assert supplied_fields_unused_by_action(
        DialecticParams, "list", {"action": "list", "judgment_formed": "false"}
    ) == ("judgment_formed",)


def test_a_default_the_alias_injected_is_not_named():
    """request_review sets use_brief_as_thesis=True when the caller omits it.

    Where the alias keeps the router's `action` (REST, stdio, use_tool),
    request_review(action='list') reached the router with that injected value
    and was told about a parameter it never sent. Found in review of #3402.
    """
    arguments = _validated(DialecticParams, action="list", use_brief_as_thesis=True)
    arguments["_param_coercions"] = {
        "use_brief_as_thesis": {
            "from": "omitted",
            "to": True,
            "interpretation": "alias_default",
        }
    }
    assert supplied_fields_unused_by_action(DialecticParams, "list", arguments) == ()

    # The same value sent by the caller is the caller's, and is named.
    arguments.pop("_param_coercions")
    assert supplied_fields_unused_by_action(DialecticParams, "list", arguments) == (
        "use_brief_as_thesis",
    )


def test_the_alias_injection_is_recorded_the_way_the_helper_reads_it():
    """Hold the helper to the params step that writes the record."""
    import inspect as _inspect

    from src.mcp_handlers.middleware import params_step

    source = _inspect.getsource(params_step)
    assert '"interpretation": "alias_default"' in source
    assert '"_param_coercions"' in source


def test_unvalidated_arguments_name_only_what_is_present():
    """The REST fallback can reach a handler with the caller's keys alone."""
    assert supplied_fields_unused_by_action(
        DialecticParams, "list", {"action": "list", "limit": 5, "concerns": ["x"]}
    ) == ("concerns",)


def test_an_undeclared_name_is_not_named():
    """Not this function's business: the handler may still read it.

    action=request reads reviewer_mode and session_type, which the schema
    does not declare.
    """
    assert supplied_fields_unused_by_action(
        DialecticParams,
        "request",
        {"action": "request", "issue_description": "x", "reviewer_mode": "llm"},
    ) == ()


@pytest.mark.parametrize("action", ["", "vote", None])
def test_an_action_the_model_does_not_know_names_nothing(action):
    assert supplied_fields_unused_by_action(
        DialecticParams, action, {"concerns": ["x"]}
    ) == ()


def test_a_model_with_no_action_map_names_nothing():
    class Plain:
        model_fields = DialecticParams.model_fields

    assert supplied_fields_unused_by_action(Plain, "thesis", {"concerns": ["x"]}) == ()


# ---------------------------------------------------------------------------
# What the router answers
# ---------------------------------------------------------------------------


def _router(name: str):
    return inspect.unwrap(get_tool_registry()[name])


def _call_dialectic(**arguments) -> dict:
    """Through the router itself. action=quick is a read that touches no
    session state, so it runs unpatched."""
    result = asyncio.run(_router("dialectic")(_validated(DialecticParams, **arguments)))
    return json.loads(result[0].text)


_QUICK = {
    "action": "quick",
    "issue_description": "ship the lite fix",
    "position": "ship it",
    "reasoning": "the focused tests pass",
}


def test_a_successful_response_names_the_unused_parameters():
    answered = _call_dialectic(
        **_QUICK, session_id="abc123", new_reviewer_id="someone"
    )
    assert answered.get("success") is not False, answered
    notice = answered["unused_parameters"]
    assert notice["action"] == "quick"
    assert notice["names"] == ["session_id", "new_reviewer_id"]
    assert "describe_tool(tool_name='dialectic', action='quick')" in notice["note"]


def test_a_call_with_nothing_unused_carries_no_notice():
    answered = _call_dialectic(**_QUICK)
    assert answered.get("success") is not False, answered
    assert "unused_parameters" not in answered


def test_a_refusal_is_returned_as_the_handler_wrote_it():
    """Nothing was recorded, and a refusal's shape is its own contract."""
    probe = {k: v for k, v in _QUICK.items() if k != "issue_description"}
    refused = _call_dialectic(**probe, session_id="abc123")
    assert refused.get("success") is False, refused
    assert "unused_parameters" not in refused


def test_the_default_action_is_the_one_the_notice_is_computed_for(monkeypatch):
    """dialectic({}) routes to list; the notice must describe list."""
    from mcp.types import TextContent

    router = _router("dialectic")
    actions = inspect.getclosurevars(router).nonlocals["actions"]

    async def listed(arguments):
        return [TextContent(type="text", text=json.dumps({"success": True, "sessions": []}))]

    monkeypatch.setitem(actions, "list", listed)
    arguments = DialecticParams.model_validate({"concerns": ["x"]}).model_dump()
    arguments.pop("action")
    answered = json.loads(asyncio.run(router(arguments))[0].text)
    assert answered["unused_parameters"]["action"] == "list"
    assert answered["unused_parameters"]["names"] == ["concerns"]


def test_a_response_that_is_not_a_json_object_is_left_alone():
    from mcp.types import TextContent
    from src.mcp_handlers.decorators import _attach_unused_parameters

    for text in ("plain text", "[1, 2]", ""):
        result = [TextContent(type="text", text=text)]
        assert _attach_unused_parameters(result, "dialectic", "quick", ("x",)) is result
    assert _attach_unused_parameters([], "dialectic", "quick", ("x",)) == []


# ---------------------------------------------------------------------------
# Which routers may opt in
# ---------------------------------------------------------------------------


def _routers() -> dict:
    return {
        name: _router(name)
        for name, model in get_pydantic_schemas().items()
        if declared_action_fields(model) and name in get_tool_registry()
    }


def _reports(router) -> bool:
    return bool(
        inspect.getclosurevars(router).nonlocals.get("report_unused_parameters")
    )


def _action_table(router) -> dict:
    return inspect.getclosurevars(router).nonlocals.get("actions") or {}


def _handoffs(router) -> set[tuple[str, str]]:
    """(action, other action) wherever an action's code path calls the
    handler of another action of the same router.

    It recognises a call to the function the routing table holds. It does not
    see a call to another action's implementation behind that function, or a
    handler passed along as a value. Those are covered from the other side:
    test_every_action_declares_the_parameters_its_handler_reads stops only at
    the routing-table functions too, so a path that reaches another action's
    implementation directly has that implementation's reads counted against
    the calling action, and an undeclared one fails there.
    """
    actions = _action_table(router)
    by_function = {inspect.unwrap(fn): action for action, fn in actions.items()}
    found = set()
    for action, handler in actions.items():
        start = inspect.unwrap(handler)
        package = start.__module__.rpartition(".")[0]
        pending, seen = [start], set()
        while pending:
            fn = pending.pop()
            if fn in seen:
                continue
            seen.add(fn)
            module = importlib.import_module(fn.__module__)
            try:
                tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            except (OSError, TypeError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                    continue
                helper = getattr(module, node.func.id, None)
                if not inspect.isfunction(helper):
                    continue
                helper = inspect.unwrap(helper)
                if helper in by_function and helper is not start:
                    found.add((action, by_function[helper]))
                elif helper.__module__ == package or helper.__module__.startswith(
                    package + "."
                ):
                    pending.append(helper)
    return found


# Reviewed hand-offs of a router that reports unused parameters. The value
# says why the notice stays true: the receiving handler must not be handed the
# caller's own arguments, or the caller's parameters for THAT action are read
# after all.
_REVIEWED_HANDOFFS = {
    ("dialectic", "request", "thesis"): (
        "request builds a new thesis_args dict from named request parameters "
        "(root_cause, reasoning, proposed_conditions) and never passes its own "
        "arguments on, so the caller's session_id or conditions do not reach "
        "the thesis handler"
    ),
}


def test_the_opted_in_routers_are_the_reviewed_ones():
    assert {name for name, router in _routers().items() if _reports(router)} == {
        "dialectic"
    }


def test_the_survey_finds_the_handoffs_that_keep_other_routers_out():
    """Guard the fixture: knowledge get forwards its arguments to details."""
    assert ("get", "details") in _handoffs(_router("knowledge"))


def test_every_handoff_of_a_reporting_router_has_been_reviewed():
    for name, router in _routers().items():
        if not _reports(router):
            continue
        found = {(name, a, b) for a, b in _handoffs(router)}
        assert found == {key for key in _REVIEWED_HANDOFFS if key[0] == name}, (
            f"{name} reports unused parameters and its action hand-offs changed. "
            "If the new one forwards the caller's arguments, the notice would "
            "name parameters the receiving handler reads."
        )


def test_the_reviewed_dialectic_handoff_still_builds_its_own_arguments():
    from src.mcp_handlers.dialectic import phase_submission

    source = inspect.getsource(phase_submission._handle_request_dialectic_review)
    assert "thesis_args: Dict[str, Any] = {" in source
    assert "_submit_thesis_fn(thesis_args)" in source
    assert "_submit_thesis_fn(arguments)" not in source


def test_a_reporting_router_declares_no_envelope_parameter():
    """response_mode is read by the response envelope on the workflow aliases,
    not by the handler, so no action's ACTION_FIELDS can speak for it."""
    models = get_pydantic_schemas()
    for name, router in _routers().items():
        if _reports(router):
            assert "response_mode" not in models[name].model_fields, name

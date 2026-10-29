"""A dry-run flag on a check-in is refused, not committed.

The sync_state description names simulate_update as the no-write path, and
the initial tools/list does not show simulate_update, so a reader of that
description alone could take it for a parameter and send
``sync_state(simulate_update=true)``. The check-in schema has no such field,
and the middleware deliberately passes undeclared keys through to the handler
for the S22 provenance callers (params_step.py), so the flag was ignored and
the write committed (dogfood pulse, 2026-10-29).

Pinned here:
  - ``simulate_update`` and ``dry_run`` are refused by name, at their own
    location, naming the separate tool, both on the model and through the
    dispatch steps a sync_state call runs before its handler;
  - the S22 pass-through those callers rely on is untouched, so this is not
    ``extra="forbid"`` by another route;
  - both served descriptions name simulate_update as a separate tool, and the
    advertised sync_state schema has no property by either refused name.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.mcp_handlers.schemas.core import (
    CHECKIN_NO_WRITE_INTENT_KEYS,
    ProcessAgentUpdateParams,
)

_S22_EXTRA_FIELDS = {"harness_type": "hermes", "locus": {"host": "probe"}}


@pytest.mark.parametrize("key", CHECKIN_NO_WRITE_INTENT_KEYS)
@pytest.mark.parametrize("value", [True, False, "true", None])
def test_model_refuses_the_key_at_its_own_location(key, value):
    """Any explicit presence is refused: an explicit ``false`` is still a
    caller who believes the flag exists."""
    with pytest.raises(ValidationError) as exc_info:
        ProcessAgentUpdateParams.model_validate({"response_text": "T", key: value})

    errors = exc_info.value.errors(include_url=False)
    assert [(e["loc"], e["type"]) for e in errors] == [((key,), "no_write_intent_key")]
    assert "call the separate simulate_update tool" in errors[0]["msg"]
    assert errors[0]["msg"].startswith(f"{key} is not a parameter of this check-in")


def test_model_still_ignores_the_s22_extra_fields_the_middleware_preserves():
    """Negative control: the refusal is by name, not ``extra="forbid"``."""
    params = ProcessAgentUpdateParams.model_validate(
        {"response_text": "T", **_S22_EXTRA_FIELDS}
    )
    assert params.response_text == "T"
    assert not any(field in params.model_dump() for field in _S22_EXTRA_FIELDS)


async def _through_dispatch(tool_name: str, arguments: dict):
    """The alias and validation steps dispatch runs before the handler."""
    from src.mcp_handlers.middleware import DispatchContext
    from src.mcp_handlers.middleware.params_step import resolve_alias, validate_params

    name, resolved, ctx = await resolve_alias(tool_name, dict(arguments), DispatchContext())
    return await validate_params(name, resolved, ctx)


@pytest.mark.asyncio
async def test_sync_state_dispatch_refuses_before_the_handler():
    outcome = await _through_dispatch(
        "sync_state",
        {"response_text": "T", "complexity": "low", "simulate_update": True},
    )

    assert isinstance(outcome, list), "a refusal is an error response, not a dispatch tuple"
    payload = json.loads(outcome[0].text)
    assert payload["success"] is False
    assert payload["error_category"] == "validation_error"
    assert [(e["field"], e["type"]) for e in payload["errors"]] == [
        ("simulate_update", "no_write_intent_key")
    ]
    assert "call the separate simulate_update tool" in payload["error"]


@pytest.mark.asyncio
async def test_dispatch_still_passes_s22_extra_fields_through():
    """The documented pass-through (schemas/core.py, params_step.py) survives."""
    outcome = await _through_dispatch(
        "process_agent_update", {"response_text": "T", **_S22_EXTRA_FIELDS}
    )

    assert isinstance(outcome, tuple)
    _, arguments, _ = outcome
    assert {field: arguments[field] for field in _S22_EXTRA_FIELDS} == _S22_EXTRA_FIELDS


@pytest.mark.asyncio
async def test_served_descriptions_name_simulate_update_as_a_separate_tool():
    from src.mcp_handlers.introspection.tool_introspection import handle_describe_tool
    from src.mcp_handlers.tool_stability import resolve_tool_alias
    from src.tool_descriptions import TOOL_DESCRIPTIONS

    _, alias = resolve_tool_alias("sync_state")
    assert "call the separate simulate_update tool" in alias.migration_note
    assert "the separate simulate_update tool" in TOOL_DESCRIPTIONS["process_agent_update"]

    served = json.loads(
        (await handle_describe_tool({"tool_name": "sync_state", "lite": False}))[0].text
    )["tool"]
    assert "call the separate simulate_update tool" in served["description"]
    advertised = served["inputSchema"]["properties"]
    assert not set(CHECKIN_NO_WRITE_INTENT_KEYS) & set(advertised)

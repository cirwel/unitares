"""Drift-guard: the list_tools catalog must keep advertising every
agent-experience alias, and each must resolve to a real tool.

The friendly task-verb names (start_session, sync_state, ...) are
surfaced for in-band discovery across the catalog's lite `signatures`
and full tool list. Those are hand-maintained strings; this guard pins
them to the registry (`experience_alias_map`) so a future alias rename
or removal fails here instead of silently dropping a name from
discovery while the registry still routes it.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import src.mcp_handlers.core  # noqa: F401  (settle handler registration)
import src.mcp_handlers.consolidated  # noqa: F401
import src.mcp_handlers.identity.handlers  # noqa: F401

from src.mcp_handlers.introspection.tool_introspection import handle_list_tools
from src.mcp_handlers.tool_stability import experience_alias_map, resolve_tool_alias


def _catalog(arguments: dict) -> dict:
    return json.loads(asyncio.run(handle_list_tools(arguments))[0].text)


def _tool_names(obj, acc: set) -> set:
    """Collect every advertised tool name in the (nested) full catalog."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "name" and isinstance(v, str):
                acc.add(v)
            _tool_names(v, acc)
    elif isinstance(obj, list):
        for x in obj:
            _tool_names(x, acc)
    return acc


def test_experience_aliases_discoverable_in_lite_catalog():
    friendly = set(experience_alias_map())
    signatures = set(_catalog({"lite": True}).get("signatures", {}))
    missing = friendly - signatures
    assert not missing, f"experience aliases absent from lite signatures: {sorted(missing)}"


def test_experience_aliases_discoverable_in_full_catalog():
    friendly = set(experience_alias_map())
    advertised = _tool_names(_catalog({"lite": False}), set())
    missing = friendly - advertised
    assert not missing, f"experience aliases absent from full catalog: {sorted(missing)}"


def test_every_advertised_alias_resolves_to_a_registered_tool():
    """No dead discovery entries: each friendly name the catalog advertises
    must resolve to a registered canonical handler."""
    from src.mcp_handlers import TOOL_HANDLERS

    for friendly, canonical in experience_alias_map().items():
        resolved, alias = resolve_tool_alias(friendly)
        assert resolved == canonical, f"{friendly} -> {resolved}, expected {canonical}"
        assert canonical in TOOL_HANDLERS, f"{friendly} -> {canonical} not registered"

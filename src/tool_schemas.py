"""
Tool Schema Definitions — Single source of truth for MCP tool schemas.

Dynamically built from Pydantic models (inputSchema) + description dict.
Descriptions live in tool_descriptions.py to keep this file compact.
"""

import os
import importlib
import inspect
from typing import Any

from mcp.types import Tool
from pydantic import BaseModel

from src.mcp_compat import get_tool_input_schema, set_tool_input_schema
from src.schema_brief import (
    BRIEF_BUDGET,
    apply_field_description_mode,
    apply_property_title_mode,
    resolve_brief_budget,
    resolve_field_description_mode,
    resolve_property_title_mode,
)
from src.tool_annotations import tool_annotations


_EXTRA_SCHEMA_MODULES: list[str] = []


def register_extra_schemas(module_path: str) -> None:
    """Append a plugin schema module to the loader.

    Called by ``governance_mcp.plugins`` entry-point plugins during
    ``plugin_loader.load_plugins()`` to make their Pydantic ``*Params``
    models discoverable by ``_load_pydantic_schemas``.
    """
    if module_path not in _EXTRA_SCHEMA_MODULES:
        _EXTRA_SCHEMA_MODULES.append(module_path)
    global _PYDANTIC_SCHEMAS_CACHE
    _PYDANTIC_SCHEMAS_CACHE = None  # invalidate so next lookup reloads


def _load_pydantic_schemas():
    """Discover all Pydantic *Params models from schema modules."""
    mods = [
        "src.mcp_handlers.schemas.core",
        "src.mcp_handlers.schemas.export",
        "src.mcp_handlers.schemas.lifecycle",
        "src.mcp_handlers.schemas.knowledge",
        "src.mcp_handlers.schemas.dialectic",
        "src.mcp_handlers.schemas.observability",
        "src.mcp_handlers.schemas.calibration",
        "src.mcp_handlers.schemas.identity",
        "src.mcp_handlers.schemas.admin",
        "src.mcp_handlers.schemas.dashboard",
        "src.mcp_handlers.schemas.skills",  # S15-a
        # progress_flat was missing from this list until 2026-08-29, so
        # record_progress_pulse fell through to the auto-discovery branch below
        # and advertised an empty stub schema even though the handler validates
        # against RecordProgressPulseParams on every call.
        "src.mcp_handlers.schemas.progress_flat",
        *_EXTRA_SCHEMA_MODULES,
    ]
    all_schemas = {}
    for mod_name in mods:
        mod = importlib.import_module(mod_name)
        for name, obj in inspect.getmembers(mod):
            if (
                inspect.isclass(obj)
                and issubclass(obj, BaseModel)
                and obj is not BaseModel
                and name.endswith("Params")
            ):
                tool_name = ""
                for char in getattr(obj, "__name__", ""):
                    if char.isupper():
                        tool_name += "_" + char.lower()
                    else:
                        tool_name += char
                tool_name = tool_name.lstrip("_").replace("_params", "")
                all_schemas[tool_name] = obj
    return all_schemas


_PYDANTIC_SCHEMAS_CACHE = None

def get_pydantic_schemas():
    """Get or load Pydantic schemas (cached)."""
    global _PYDANTIC_SCHEMAS_CACHE
    if _PYDANTIC_SCHEMAS_CACHE is None:
        _PYDANTIC_SCHEMAS_CACHE = _load_pydantic_schemas()
    return _PYDANTIC_SCHEMAS_CACHE

# Wire order of tools/list: the registered tools of src/tool_meta.py, in the
# order their records appear there. Until 2026-09-07 this was a hand-written
# list of 65 names, 23 of them register=False delegates that only exist to
# validate router actions and were filtered out again by every consumer that
# reaches a wire (the registrar, the interface contract); the advertised
# surface is the 42 registered tools plus the workflow aliases the registrar
# adds. The order is load-bearing: the interface contract's surface hash is
# computed over the capabilities in this order.
from src.tool_meta import WIRE_ORDER

TOOL_ORDER = list(WIRE_ORDER)


def _is_extra_schema_model(schema_model: type[BaseModel] | None) -> bool:
    """Return whether a schema was explicitly registered by a plugin."""
    if schema_model is None:
        return False
    model_module = getattr(schema_model, "__module__", "")
    return any(
        model_module == module or model_module.startswith(f"{module}.")
        for module in _EXTRA_SCHEMA_MODULES
    )


def _is_core_handler(definition) -> bool:
    """Whether a tool's handler ships in this repo rather than a plugin.

    Reads the declaring module the decorator recorded, NOT
    ``handler.__module__``. Every ``action_router`` handler is defined inside
    ``mcp_handlers/decorators.py``, so the latter reported "core" for a
    plugin's router too — and a plugin router that had not called
    ``register_extra_schemas()`` then hard-failed this check at startup with a
    message telling the operator to edit governance's own TOOL_ORDER.
    """
    from src.mcp_handlers.decorators import _is_first_party_module

    module = getattr(definition, "source_module", "") or ""
    return _is_first_party_module(module)


def _validate_consolidated_tool_order(
    schemas: dict[str, type[BaseModel]],
) -> None:
    """Fail when a core tool would be advertised with a stub schema.

    ``get_tool_definitions`` auto-discovers any registered tool missing from
    ``TOOL_ORDER`` (the registered records of src/tool_meta.py) and serves it ``{"properties": {}, "additionalProperties":
    true}``. That is a silent contract break rather than a graceful fallback:
    ``validate_params`` resolves the real ``*Params`` model by tool name
    regardless of ``TOOL_ORDER``, so the wire advertises "any parameters
    accepted" and the server then rejects the call against a schema the caller
    was never shown. Six core tools sat in that state until 2026-08-29.

    Scope is deliberately every registered core tool, not just action routers.
    The check used to cover ``known_actions is not None`` only, which is why
    single-purpose tools (get_trajectory_status, outcome_correlation,
    record_progress_pulse, ...) drifted out unnoticed.

    Plugin tools are exempt and keep the auto-discovery path: a plugin that
    wants a real advertised schema calls ``register_extra_schemas()``, and one
    that does not should not be able to hard-fail server startup.
    """
    from src.mcp_handlers.decorators import _TOOL_DEFINITIONS, get_tool_registry

    ordered = set(TOOL_ORDER)
    missing = sorted(
        name
        for name, definition in _TOOL_DEFINITIONS.items()
        if not definition.hidden
        and name not in ordered
        and _is_core_handler(definition)
        and not _is_extra_schema_model(schemas.get(name))
    )
    if missing:
        raise RuntimeError(
            "Registered core tools are missing from TOOL_ORDER and would be "
            f"advertised with empty stub schemas: {missing}. Add each to "
            "src/tool_meta.py with a matching *Params model (or set register=False "
            "if the tool is only reached through a router); plugins must call "
            "register_extra_schemas()."
        )

    # The reverse: a record for a name that is not a registered dispatch tool
    # would put a wire schema on a name nothing dispatches (the 23 delegates
    # sat in that state until 2026-09-07). Checked only once the registry has
    # been populated; before the handler package is imported it is empty.
    registry = get_tool_registry()
    if registry:
        stale = sorted(name for name in TOOL_ORDER if name not in registry)
        if stale:
            raise RuntimeError(
                "src/tool_meta.py has records for names that are not registered "
                f"dispatch tools: {stale}. A register=False delegate has no wire "
                "schema of its own; remove the record or register the tool."
            )


def first_line(s: str | None) -> str:
    """The first non-empty line of a description, stripped.

    This is the description ``tools/list`` serves under the default short
    verbosity, and since 2026-09-12 also the one ``list_tools`` and the compact
    ``describe_tool`` view serve for an advertised name, so every discovery
    surface derives its one-liner with the same rule.
    """
    if not s:
        return ""
    for line in s.splitlines():
        if line.strip():
            return line.strip()
    return ""


# Identity params hidden from the *advertised* check-in schema. They stay on the
# Pydantic model — the handler still accepts them as a same-process escape hatch
# (EXTRA_ARGUMENT_PASSTHROUGH + extra="allow") — but a check-in reads as an
# ambient binding rather than hand-threaded identifiers. Mirrors the existing
# `inject_action` strip in mcp_server._register_common_aliases.
#
# SCOPE IS DELIBERATELY agent_id + agent_name ONLY. client_session_id and
# continuity_token are NOT stripped, and that is load-bearing:
#   - A claude.ai remote-connector client only sends params present in the
#     advertised inputSchema. These two tools are in
#     TOOLS_NEEDING_SESSION_INJECTION, so when the client omits client_session_id
#     the server injects one from context — but for stateless streamable transport
#     (no Mcp-Session-Id) that injected value is the ip_ua_fingerprint, which
#     derive_session_key launders into `explicit_client_session_id` (strong/1.0).
#     The effect: multiple distinct agents behind one gateway IP+UA collapse onto
#     a single shared-fingerprint identity. Advertising client_session_id lets a
#     well-behaved agent send its unique agent-{uuid} key instead, keeping
#     attribution isolated. See tests/test_onboard_pin.py::TestToolSchemaClientSessionId.
#   - continuity_token has NO injection fallback; stripping it would break
#     claude.ai cross-instance PATH-0 resume outright.
# agent_id (structured handle, auto-resolved) and agent_name (cosmetic; the
# name-claim resolution path was removed 2026-04-17) carry no such dependency.
_AUTO_INJECTED_IDENTITY_PARAMS = (
    "agent_id",
    "agent_name",
)
# Canonical tools whose session is auto-injected (TOOLS_NEEDING_SESSION_INJECTION).
# Their aliases (sync_state, check_working_state) inherit this schema downstream,
# so stripping here at the canonical source covers the alias surfaces too.
_HIDE_IDENTITY_PARAMS_TOOLS = {
    "process_agent_update",
    "get_governance_metrics",
}


def _hide_auto_injected_identity(schema: Any) -> Any:
    """Drop session-auto-injected identity params from an advertised schema.

    Returns a copy; never mutates the (pydantic-cached) input dict.
    """
    if not isinstance(schema, dict):
        return schema
    import copy
    schema = copy.deepcopy(schema)
    props = schema.get("properties")
    if isinstance(props, dict):
        for name in _AUTO_INJECTED_IDENTITY_PARAMS:
            props.pop(name, None)
    req = schema.get("required")
    if isinstance(req, list):
        schema["required"] = [r for r in req if r not in _AUTO_INJECTED_IDENTITY_PARAMS]
    return schema


def advertised_input_schema(
    tool_name: str,
    schema: Any,
    *,
    field_descriptions: str = "full",
    budget: int = BRIEF_BUDGET,
    property_titles: str | None = None,
) -> Any:
    """The input schema a caller is told about, for a tool or its alias's canonical tool.

    One definition for the three surfaces that describe a tool's parameters:
    the wire catalog (``get_tool_definitions``), ``describe_tool``, and the
    tool-surface audit. Until 2026-09-07 only the wire applied the identity
    hiding, so ``describe_tool`` advertised ``agent_id`` / ``agent_name`` for
    ``process_agent_update`` / ``get_governance_metrics`` and their workflow
    aliases while the registered schema carried neither
    (DESCRIBE_SCHEMA_WIDER_THAN_WIRE in scripts/dev/tool_edge_index.py).

    ``field_descriptions`` is where the surfaces legitimately differ: the wire
    passes "brief" and describe_tool takes the "full" default. Either way the
    ``brief`` authoring key is removed, so no caller sees one parameter
    documented twice.

    ``property_titles`` does NOT differ between surfaces, and deliberately so:
    a Pydantic ``title`` is a titleized echo of the key (``client_session_id``
    -> "Client Session Id") or the model's class name at the root
    (``OnboardParams``). There is no surface on which repeating the key back to
    the caller in title case explains anything, so describe_tool drops it on
    the same rule the wire does. Defaults to
    ``UNITARES_TOOL_SCHEMA_PROPERTY_TITLES`` (``strip``).
    """
    if tool_name in _HIDE_IDENTITY_PARAMS_TOOLS:
        schema = _hide_auto_injected_identity(schema)
    schema = apply_field_description_mode(schema, field_descriptions, budget=budget)
    return apply_property_title_mode(
        schema, resolve_property_title_mode(property_titles)
    )


def get_tool_definitions(
    verbosity: str | None = None,
    field_descriptions: str | None = None,
) -> list[Tool]:
    """Build the list of MCP Tool objects from Pydantic schemas + descriptions.

    ``verbosity`` governs the tool's own description; ``field_descriptions``
    governs its parameters' (see src/schema_brief.py). Both default to the
    compact form, and both leave the authored text reachable through
    ``describe_tool``, which reads the Pydantic models rather than this
    catalog.
    """
    if verbosity is None:
        verbosity = os.getenv("UNITARES_TOOL_SCHEMA_VERBOSITY", "short").strip().lower()

    field_description_mode = resolve_field_description_mode(field_descriptions)
    brief_budget = resolve_brief_budget()

    from src.tool_descriptions import TOOL_DESCRIPTIONS

    schemas = get_pydantic_schemas()
    _validate_consolidated_tool_order(schemas)
    all_tools: list[Tool] = []

    for tool_name in TOOL_ORDER:
        schema_model = schemas.get(tool_name)
        if not schema_model:
            print(f"WARNING: Schema for {tool_name} not found in Pydantic models!")
            continue

        desc = TOOL_DESCRIPTIONS.get(tool_name)
        if not desc:
            # Fall back to Pydantic model docstring
            desc = schema_model.__doc__ or f"Tool: {tool_name}"

        raw_schema = schema_model.model_json_schema()

        all_tools.append(Tool(
            name=tool_name,
            description=desc,
            inputSchema=raw_schema,
        ))

    # Auto-discover decorator-defined tools not in TOOL_ORDER
    try:
        from src.mcp_handlers.decorators import _TOOL_DEFINITIONS

        hardcoded_names = {t.name for t in all_tools}

        for tn in sorted(_TOOL_DEFINITIONS.keys()):
            if tn in hardcoded_names:
                continue

            td = _TOOL_DEFINITIONS[tn]

            if getattr(td, "hidden", False):
                continue

            desc = getattr(td, "description", None) or f"Tool: {tn}"

            if getattr(td, "deprecated", False):
                superseded_by = getattr(td, "superseded_by", None)
                if superseded_by:
                    desc = f"[DEPRECATED - use {superseded_by}] {desc}"
                else:
                    desc = f"[DEPRECATED] {desc}"

            plugin_schema = schemas.get(tn)
            if _is_extra_schema_model(plugin_schema):
                input_schema = plugin_schema.model_json_schema()
            else:
                input_schema = {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                }

            all_tools.append(Tool(
                name=tn,
                description=desc,
                inputSchema=input_schema,
            ))
    except ImportError:
        pass

    # Apply verbosity and the advertised field-description mode. Both run for
    # every tool: advertised_input_schema is the one definition of "what a
    # caller is told", and it is what strips the `brief` authoring key.
    for t in all_tools:
        set_tool_input_schema(
            t,
            advertised_input_schema(
                t.name,
                get_tool_input_schema(t),
                field_descriptions=field_description_mode,
                budget=brief_budget,
            ),
        )
        if verbosity == "short":
            t.description = first_line(t.description)
        # The machine-readable half of the same statement the description
        # makes in prose (src/tool_annotations.py). Both Tool() sites above
        # funnel through this loop, so stdio, REST and the FastMCP registrar
        # all read one table. A tool with no record keeps annotations unset,
        # which the spec treats as "no hints", not as "no side effects".
        t.annotations = tool_annotations(t.name)

    return all_tools

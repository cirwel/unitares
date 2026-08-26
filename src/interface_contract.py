"""Versioned, transport-neutral contract for UNITARES tool discovery.

The three public dispatch surfaces -- streamable HTTP MCP, REST
``/v1/tools``, and local stdio -- must advertise the same callable names and
input schemas for a given tool mode.  Dispatch already resolves workflow
aliases on every surface; this module makes discovery use that same contract.

The contract describes tool reachability and the stable normalized lifecycle
envelope used by product-facing workflow aliases.  It does not claim that a
host installed lifecycle hooks, forwards host events, or honors returned
policy.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from mcp.types import Tool

from src.alias_schema import build_alias_input_schema
from src.mcp_compat import get_tool_input_schema


INTERFACE_CONTRACT_SCHEMA = "unitares.interface-contract.v1"
INTERFACE_CONTRACT_VERSION = "1.1.0"
LIFECYCLE_ENVELOPE_SCHEMA = "unitares.lifecycle-envelope.v1"
SUPPORTED_MCP_SPECIFIER = ">=1.26.0,<3.0.0"
FEDERATION_LIFECYCLE_CAPABILITIES = (
    "start_session",
    "sync_state",
    "check_working_state",
    "record_result",
)
LIFECYCLE_SUCCESS_REQUIRED_FIELDS = (
    "success",
    "tool",
    "next_action",
)
PUBLIC_TRANSPORTS = (
    "mcp_streamable_http",
    "rest_v1_tools",
    "stdio",
)


def workflow_alias_names_for_mode(mode: str) -> tuple[str, ...]:
    """Return primary workflow aliases advertised in ``mode``.

    Full mode includes every workflow alias in addition to every registered raw
    tool.  Filtered modes include only aliases explicitly named by that mode.
    """

    from src.mcp_handlers.tool_stability import AGENT_WORKFLOW_ALIASES
    from src.tool_modes import should_include_tool

    if mode == "full":
        return tuple(AGENT_WORKFLOW_ALIASES)
    return tuple(
        name for name in AGENT_WORKFLOW_ALIASES
        if should_include_tool(name, mode=mode)
    )


def build_alias_tool_definition(
    alias_name: str,
    *,
    definitions: Iterable[Tool] | None = None,
) -> Tool:
    """Build the public Tool definition for one workflow alias."""

    from src.mcp_handlers.tool_stability import resolve_tool_alias
    from src.tool_schemas import get_tool_definitions

    actual_name, alias = resolve_tool_alias(alias_name)
    if alias is None:
        raise KeyError(f"{alias_name!r} is not a registered workflow alias")

    raw_definitions = list(definitions or get_tool_definitions())
    actual = next(
        (tool for tool in raw_definitions if tool.name == actual_name),
        None,
    )
    if actual is None:
        raise KeyError(
            f"workflow alias {alias_name!r} targets missing tool {actual_name!r}"
        )

    schema = get_tool_input_schema(actual, {}) or {}
    schema = build_alias_input_schema(
        alias_name,
        schema,
        inject_action=bool(alias.inject_action),
    )
    description = alias.migration_note or f"Alias for {actual_name}"
    return Tool(
        name=alias_name,
        description=description,
        inputSchema=schema,
    )


def get_public_tool_definitions(
    mode: str,
    *,
    client_type: str | None = None,
) -> list[Tool]:
    """Return the exact advertised tool surface for a transport and mode."""

    # Importing the package settles every @mcp_tool decorator before the
    # registry is read.  This is idempotent in long-running servers.
    import src.mcp_handlers  # noqa: F401

    from src.mcp_handlers.decorators import get_tool_registry
    from src.tool_modes import should_include_tool
    from src.tool_schemas import get_tool_definitions

    definitions = list(get_tool_definitions())
    registered = set(get_tool_registry())
    public: dict[str, Tool] = {}

    for tool in definitions:
        if tool.name not in registered:
            continue
        if should_include_tool(tool.name, mode=mode, client_type=client_type):
            public[tool.name] = tool

    alias_names = workflow_alias_names_for_mode(mode)
    for alias_name in alias_names:
        try:
            public[alias_name] = build_alias_tool_definition(
                alias_name,
                definitions=definitions,
            )
        except KeyError:
            # Some introspection tests and embedded consumers deliberately
            # install a partial schema catalog. Do not advertise an alias whose
            # implementation schema is unavailable; the checked-in full
            # contract still makes accidental production drift fail in CI.
            continue

    # Workflow names are the product-facing path and belong at the top of
    # bounded CLI/discovery output. Preserve the canonical schema-catalog order
    # after them so this seam does not regress existing orientation UX.
    ordered_names = [name for name in alias_names if name in public]
    ordered_names.extend(
        tool.name
        for tool in definitions
        if tool.name in public and tool.name not in alias_names
    )
    return [public[name] for name in ordered_names]


def _capability_record(tool: Tool) -> dict[str, Any]:
    from src.mcp_handlers.tool_stability import resolve_tool_alias

    implementation, alias = resolve_tool_alias(tool.name)
    input_schema = get_tool_input_schema(tool, {}) or {}
    schema_bytes = json.dumps(
        input_schema,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "name": tool.name,
        "implementation": implementation,
        "kind": "workflow_alias" if alias is not None else "canonical_tool",
        "input_schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
        "transport_names": {
            transport: tool.name for transport in PUBLIC_TRANSPORTS
        },
    }


def build_interface_contract(mode: str = "lite") -> dict[str, Any]:
    """Build the deterministic machine-readable interface contract."""

    capabilities = [
        _capability_record(tool)
        for tool in get_public_tool_definitions(mode)
    ]
    canonical = json.dumps(
        capabilities,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema": INTERFACE_CONTRACT_SCHEMA,
        "version": INTERFACE_CONTRACT_VERSION,
        "scope": "tool_surface_and_lifecycle_envelopes",
        "mode": mode,
        "transports": list(PUBLIC_TRANSPORTS),
        "surface_sha256": hashlib.sha256(canonical).hexdigest(),
        "capabilities": capabilities,
        "federation": {
            "negotiation": {
                "tool": "list_tools",
                "arguments": {"lite": True},
                "contract_path": "interface_contract",
                "capabilities_path": "tools[*].name",
            },
            "mcp": {
                "version_specifier": SUPPORTED_MCP_SPECIFIER,
                "tested_majors": [1, 2],
                "newest_in_range_ci": "blocking",
            },
            "lifecycle": {
                "schema": LIFECYCLE_ENVELOPE_SCHEMA,
                "capabilities": list(FEDERATION_LIFECYCLE_CAPABILITIES),
                "success_envelope": {
                    "required": list(LIFECYCLE_SUCCESS_REQUIRED_FIELDS),
                    "constants": {"success": True},
                    "field_types": {
                        "success": "boolean",
                        "tool": "string",
                        "next_action": "string_or_object",
                        "state_summary": "object",
                        "risk_summary": "string",
                        "memory_suggestions": "array",
                        "recovery_hint": "string_or_object",
                        "raw_governance": "object",
                    },
                    "optional_fields_may_be_omitted": True,
                },
                "failure_envelope": {
                    "required": ["success"],
                    "constants": {"success": False},
                    "detail_fields_any_of": ["error", "message"],
                },
            },
        },
        "limits": {
            "lifecycle_automation": "not_implied",
            "host_event_capture": "not_implied",
            "policy_actuation_outside_governed_writes": "not_implied",
        },
    }


def get_interface_contract_summary(mode: str = "lite") -> dict[str, Any]:
    contract = build_interface_contract(mode)
    return {
        "schema": contract["schema"],
        "version": contract["version"],
        "mode": contract["mode"],
        "surface_sha256": contract["surface_sha256"],
        "capability_count": len(contract["capabilities"]),
        "transports": contract["transports"],
        "scope": contract["scope"],
        "federation": contract["federation"],
    }


if __name__ == "__main__":
    print(json.dumps(build_interface_contract(), indent=2))

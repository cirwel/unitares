#!/usr/bin/env python3
"""
Validate the current MCP tool registration contract.

The current server registers handlers through @mcp_tool/action_router and
advertises schemas through src.tool_schemas.get_tool_definitions(). Older
manual registries in mcp_server_std.py and TOOL_HANDLERS literals are no
longer authoritative, so this script validates the runtime surfaces that
dispatch and list_tools actually use:

1. Decorator registry has callable handlers.
2. src.mcp_handlers.TOOL_HANDLERS mirrors the decorator registry.
3. Every visible registered tool is advertised by get_tool_definitions().
4. Every advertised canonical tool has a handler.
5. Agent workflow aliases target registered canonical tools.
"""

from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))


def main() -> int:
    print("Validating MCP tool registration...\n")

    import src.mcp_handlers as handlers  # noqa: F401 - populates decorators
    from src.mcp_handlers import TOOL_HANDLERS
    from src.mcp_handlers.decorators import (
        get_tool_definition,
        get_tool_registry,
        list_registered_tools,
    )
    from src.mcp_handlers.tool_stability import (
        AGENT_WORKFLOW_ALIASES,
        list_all_aliases,
        resolve_tool_alias,
    )
    from src.tool_schemas import get_tool_definitions

    registry = get_tool_registry()
    handler_names = set(TOOL_HANDLERS)
    registered_all = set(list_registered_tools(include_hidden=True))
    registered_visible = set(list_registered_tools(include_hidden=False))
    schema_names = {tool.name for tool in get_tool_definitions(verbosity="full")}
    aliases = list_all_aliases()

    print("Coverage:")
    print(f"  decorator registry:        {len(registry)} tools")
    print(f"  TOOL_HANDLERS dispatch:    {len(handler_names)} tools")
    print(f"  visible registered tools:  {len(registered_visible)} tools")
    print(f"  advertised schemas:        {len(schema_names)} tools")
    print(f"  workflow aliases:          {len(AGENT_WORKFLOW_ALIASES)} aliases\n")

    issues: list[str] = []

    if not registry:
        issues.append("decorator registry is empty")

    for name, handler in sorted(registry.items()):
        if not callable(handler):
            issues.append(f"{name}: registered handler is not callable")

    missing_from_dispatch = sorted(set(registry) - handler_names)
    extra_dispatch = sorted(handler_names - set(registry))
    for name in missing_from_dispatch:
        issues.append(f"{name}: in decorator registry but missing from TOOL_HANDLERS")
    for name in extra_dispatch:
        issues.append(f"{name}: in TOOL_HANDLERS but missing from decorator registry")

    missing_schemas = sorted(registered_visible - schema_names)
    for name in missing_schemas:
        issues.append(f"{name}: visible registered tool missing advertised schema")

    def callable_name(name: str) -> bool:
        if name in registered_all:
            return True
        alias = aliases.get(name)
        return bool(alias and alias.new_name in registered_all)

    missing_handlers = sorted(name for name in schema_names if not callable_name(name))
    for name in missing_handlers:
        issues.append(f"{name}: advertised schema missing registered handler or alias target")

    hidden_advertised = []
    for name in registered_all - registered_visible:
        td = get_tool_definition(name)
        if td is not None and getattr(td, "hidden", False) and name in schema_names:
            hidden_advertised.append(name)
    for name in sorted(hidden_advertised):
        issues.append(f"{name}: hidden tool should not be advertised")

    for alias_name in AGENT_WORKFLOW_ALIASES:
        target, alias = resolve_tool_alias(alias_name)
        if alias is None:
            issues.append(f"{alias_name}: workflow alias is not registered in tool_stability")
            continue
        if target not in registered_all:
            issues.append(f"{alias_name}: alias target {target!r} is not registered")
        if target not in schema_names:
            issues.append(f"{alias_name}: alias target {target!r} lacks advertised schema")

    if issues:
        print("Registration issues found:\n")
        for issue in issues:
            print(f"  - {issue}")
        print(f"\nFound {len(issues)} issue(s)")
        return 1

    print("All runtime tool registration surfaces are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Tool Registration Guide

Status: specialized developer reference. Use for MCP/tool-surface changes, not runtime architecture semantics.

**For AI agents and developers adding/modifying tools in the governance MCP server.**

For the *current* wiring rather than how to add to it, see
[`TOOL_EDGE_INDEX.md`](TOOL_EDGE_INDEX.md) — generated from the live registries, it resolves
every registered tool to its handler, each consolidated tool's `action` → delegate map, and
the params schema that validates it.

## Quick Reference: Adding a New Tool

**Step 1: Define the tool schema** in `src/tool_schemas.py`:
```python
ToolDefinition(
    name="my_new_tool",
    description="What this tool does",
    inputSchema={
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "..."},
        },
        "required": ["param1"]
    }
)
```

**Step 2: Implement the handler** in `src/mcp_handlers/*.py`:
```python
@mcp_tool("my_new_tool", timeout=10.0)
async def handle_my_new_tool(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    # Your implementation
    return success_response({"result": "..."})
```

**Step 3 (optional): Add to session injection list** if it needs `client_session_id` (legacy/external client compatibility — identity is primarily UUID-based via `agent_uuid`):
In `src/mcp_server.py`, add to `TOOLS_NEEDING_SESSION_INJECTION`:
```python
TOOLS_NEEDING_SESSION_INJECTION = {
    "my_new_tool",  # Add here if tool needs session identity (legacy path)
    ...
}
```

**That's it!** The tool is automatically registered via the server's tool auto-registration pass.

---

## Architecture Overview

### The Three Registration Points

| File | Purpose | When to Edit |
|------|---------|--------------|
| `src/tool_schemas.py` | Tool definitions (name, description, schema) | Always - defines the tool |
| `src/mcp_handlers/*.py` | Handler implementations with `@mcp_tool` | Always - implements the logic |
| `src/mcp_server.py` | HTTP transport registration | **Rarely** - only for server-specific tools |

### Key Modules

| Module | Purpose |
|--------|---------|
| `decorators.py` | `@mcp_tool` decorator, `ToolDefinition` dataclass, action-router helper, unified `_TOOL_DEFINITIONS` registry |
| `middleware/` | 8-step dispatch pipeline: kwargs → identity → trajectory → alias → inject → validate → rate limit → patterns |
| `consolidated.py` | Consolidated tools built declaratively via the action-router helper |
| `response_formatter.py` | Response mode filtering (auto/minimal/compact/standard/full) for `process_agent_update` |
| `__init__.py` | Dispatch entrypoint orchestrates the middleware pipeline and calls handlers |

### Auto-Registration System

The auto-registration function in `mcp_server.py`:
1. Reads all tool definitions from `tool_schemas.py`
2. **Filters to only tools in `_TOOL_DEFINITIONS`** (tools with `register=True`)
3. Creates FastMCP wrappers for each tool
4. Injects `client_session_id` for tools in `TOOLS_NEEDING_SESSION_INJECTION`
5. Registers with `mcp.tool()` decorator

**Important:** A tool must be in BOTH `tool_schemas.py` AND have `@mcp_tool` with `register=True` (default) to be exposed via MCP.

### Dispatch Pipeline

When a tool is called, the dispatch entrypoint runs it through middleware steps defined in `middleware/`.
The pipeline runner first normalizes MCP `kwargs` wrappers so identity and
continuity inputs are visible before any session resolution or alias logic runs:

```
unwrap_kwargs → resolve_identity → verify_trajectory → resolve_alias → inject_identity → validate_params
    ↓ (handler lookup)
check_rate_limit → track_patterns → handler()
```

Each step is `async (name, arguments, ctx) → (name, arguments, ctx) | list[TextContent]`. Returning a list short-circuits with that error response. State flows via `DispatchContext` dataclass.

---

## Consolidated Tools (Feb 2026)

To reduce cognitive load, related tools are consolidated into single tools with an `action` parameter:

| Consolidated Tool | Replaces | Example |
|-------------------|----------|---------|
| `pi` | 12 pi_* tools | `pi(action='health')` |
| `observe` | 5 observability tools | `observe(action='anomalies')` |
| `agent` | 5 lifecycle tools | `agent(action='list')` |
| `knowledge` | 9 knowledge graph tools | `knowledge(action='search')` |
| `calibration` | 4 calibration tools | `calibration(action='check')` |
| `dialectic` | 4 dialectic tools | `dialectic(action='list')` |
| `config` | 2 config tools | `config(action='get')` |
| `export` | 2 export tools | `export(action='history')` |

### Creating a Consolidated Tool

Use the action-router helper in `src/mcp_handlers/consolidated.py` — no manual if/elif needed:

```python
from .decorators import action_router

handle_my_group = action_router(
    "my_group",
    actions={
        "action1": handle_individual_tool_1,
        "action2": handle_individual_tool_2,
    },
    timeout=30.0,
    description="Unified my_group operations: action1, action2",
    default_action="action1",                    # Optional: used when action is missing
    param_maps={"action2": {"q": "query"}},      # Optional: remap params per action
    examples=["my_group(action='action1')"],      # Optional: shown in error messages
)
```

The action-router helper handles action extraction, validation, error messages with valid actions + examples, parameter remapping, and MCP registration.

Individual handlers should use `register=False`:
```python
@mcp_tool("individual_tool_1", timeout=10.0, register=False)
async def handle_individual_tool_1(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    # Still works, just not exposed to MCP clients directly
    ...
```

Add backward-compat aliases in `tool_stability.py`:
```python
"individual_tool_1": ToolAlias(
    old_name="individual_tool_1",
    new_name="my_group",
    reason="consolidated",
    migration_note="Use my_group(action='action1')",
    inject_action="action1",  # Auto-inject action param for the alias
),
```

---

## @mcp_tool Decorator Parameters

```python
@mcp_tool(
    name="tool_name",           # Tool name (defaults to function name without 'handle_')
    timeout=30.0,               # Timeout in seconds
    description="...",          # Tool description (defaults to docstring)
    rate_limit_exempt=False,    # Skip rate limiting
    deprecated=False,           # Mark as deprecated
    hidden=False,               # Hide from list_tools (still callable)
    superseded_by="new_tool",   # What replaced this tool
    register=True               # If False, NOT exposed to MCP clients (Feb 2026)
)
```

### When to use `register=False`

Use `register=False` for handlers that are:
- Called by consolidated tools (e.g., `pi_health` called by `pi(action='health')`)
- Internal utilities not meant for direct use
- Deprecated tools that should only work via alias resolution

**Note:** Tools with `register=False` can still be called via aliases if configured in `tool_stability.py`.

---

## Session Injection

Identity is primarily UUID-based (`agent_uuid` from `onboard()`). Session injection of `client_session_id` is a legacy/external client compatibility mechanism.

**When to add a tool to `TOOLS_NEEDING_SESSION_INJECTION`:**
- Tool needs caller identity for external/non-UUID clients
- Tool stores data associated with an agent (prefer UUID lookup when available)
- Tool needs to know "who is calling" and cannot receive `agent_uuid` directly

---

## Tool Tiers (for list_tools filtering)

Tools are organized into tiers in `src/tool_modes.py`:

| Tier | Purpose | Example Tools |
|------|---------|---------------|
| `essential` | Core workflow (~10 tools) | onboard, identity, process_agent_update |
| `common` | Regular use (~20 tools) | observe, agent, knowledge |
| `advanced` | Rarely used (~15 tools) | cleanup_stale_locks, reset_monitor |

**When adding a new tool, add it to the appropriate tier.**

---

## Tool Aliases (Backwards Compatibility)

When renaming/consolidating tools, add aliases in `src/mcp_handlers/tool_stability.py`:

```python
_TOOL_ALIASES = {
    "old_tool_name": ToolAlias(
        old_name="old_tool_name",
        new_name="new_tool_name",
        reason="consolidated",  # or "renamed", "deprecated"
        migration_note="Use new_tool_name(action='...') instead"
    ),
}
```

Aliases are resolved at dispatch time, so old tool names continue to work.

---

## Common Mistakes

### 1. Tool not showing up in MCP clients
**Cause:** Tool in `tool_schemas.py` but handler has `register=False` or missing `@mcp_tool`.
**Fix:** Ensure handler has `@mcp_tool` with `register=True` (default).

### 2. Consolidated tool's sub-handler not working
**Cause:** Handler function not imported in `consolidated.py`.
**Fix:** Add import and route in the consolidated handler's action dispatch.

### 3. Old tool name not resolving
**Cause:** Missing alias in `tool_stability.py`.
**Fix:** Add alias mapping old name to new consolidated tool.

### 4. Session identity not working
**Cause:** Tool not in `TOOLS_NEEDING_SESSION_INJECTION`.
**Fix:** Add tool name to the set in `mcp_server.py`.

---

## Verification Commands

```bash
# Check registered tools count
curl -s -X POST "http://localhost:8767/v1/tools/call" \
  -H "Content-Type: application/json" \
  -d '{"name": "list_tools", "arguments": {"lite": false}}' | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Total tools: {len(d.get(\"result\",{}).get(\"tools\",[]))}')"

# Check server logs for auto-registration count
grep "AUTO_REGISTER" data/logs/mcp_server_error.log | tail -1

# Verify specific tool exists
curl -s -X POST "http://localhost:8767/v1/tools/call" \
  -H "Content-Type: application/json" \
  -d '{"name": "describe_tool", "arguments": {"tool_name": "my_new_tool"}}'
```

---

## Summary

| Task | Files to Edit |
|------|---------------|
| Add new standalone tool | `tool_schemas.py` + `mcp_handlers/*.py` with `@mcp_tool` |
| Add to consolidated tool | `consolidated.py` (add to `action_router` actions dict) + `register=False` on handler |
| Add dispatch middleware step | `middleware.py` (add function + add to `PRE_DISPATCH_STEPS` or `POST_VALIDATION_STEPS`) |
| Tool needs session | + `TOOLS_NEEDING_SESSION_INJECTION` in `mcp_server.py` |
| Rename/deprecate tool | `tool_stability.py` (add alias) |
| Categorize for list_tools | `tool_modes.py` (add to tier) |

---

**Last Updated:** 2026-02-06 (action_router, dispatch middleware, response formatter, ToolDefinition)

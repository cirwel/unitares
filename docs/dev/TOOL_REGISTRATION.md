# Tool Registration Guide

Status: specialized developer reference. Use for MCP/tool-surface changes, not runtime architecture semantics.

**For AI agents and developers adding/modifying tools in the governance MCP server.**

For the *current* wiring rather than how to add to it, see
[`TOOL_EDGE_INDEX.md`](TOOL_EDGE_INDEX.md) — generated from the live registries, it resolves
every registered tool to its handler, each consolidated tool's `action` → delegate map, and
the params schema that validates it.

## Quick Reference: Adding a New Tool

**Step 1: Define the tool's schema and description.** `src/tool_schemas.py` is
dynamically built — there are no hand-written schema literals in it. You add three things:

1. A Pydantic `{ToolName}Params` model in one of the modules scanned by
   `_load_pydantic_schemas()` (`src/tool_schemas.py`) — the tool name is derived
   from the class name by CamelCase → snake_case (`MyNewToolParams` → `my_new_tool`).
2. The tool's description in `src/tool_descriptions.py`.
3. The tool name in `TOOL_ORDER` in `src/tool_schemas.py` (controls listing order).

**Step 2: Implement the handler** in the matching subpackage under
`src/mcp_handlers/` (`admin/`, `lifecycle/`, `knowledge/`, `observability/`,
`dialectic/`, `identity/`, `introspection/`, `support/`, `cirs/` — nearly all
handlers live in subpackages now, not top-level `*.py`):
```python
@mcp_tool("my_new_tool", timeout=10.0)
async def handle_my_new_tool(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    # Your implementation
    return success_response({"result": "..."})
```

**Step 3 (optional): Add to session injection list** if it needs `client_session_id` (legacy/external client compatibility — identity is primarily UUID-based via `agent_uuid`):
In `src/tool_registration.py`, add to `TOOLS_NEEDING_SESSION_INJECTION`:
```python
TOOLS_NEEDING_SESSION_INJECTION = {
    "my_new_tool",  # Add here if tool needs session identity (legacy path)
    ...
}
```

**Step 4: Add the tool to a tier** in `src/tool_modes.py` (`TOOL_TIERS`) — if the
server runs a restricted tool mode, tools outside the mode's tiers are filtered
out of registration entirely (see Common Mistakes #5).

---

## Architecture Overview

### The Registration Points

| File | Purpose | When to Edit |
|------|---------|--------------|
| Pydantic `*Params` model + `src/tool_descriptions.py` + `TOOL_ORDER` | Tool schema, description, listing order | Always - defines the tool |
| `src/mcp_handlers/<subpackage>/*.py` | Handler implementations with `@mcp_tool` | Always - implements the logic |
| `src/tool_registration.py` | Auto-registration pass + `TOOLS_NEEDING_SESSION_INJECTION` | Rarely - session injection, registration behavior |
| `src/tool_modes.py` | `TOOL_TIERS` tier membership | Always - new tools need a tier |

### Key Modules

| Module | Purpose |
|--------|---------|
| `decorators.py` | `@mcp_tool` decorator, `ToolDefinition` dataclass, `action_router` helper, unified `_TOOL_DEFINITIONS` registry |
| `middleware/` (package, 7 step modules) | Three-phase dispatch pipeline: pre-dispatch → post-validation → post-execution |
| `consolidated.py` | Consolidated tools built declaratively via `action_router` |
| `response_formatter.py` | Response mode filtering (auto/minimal/compact/standard/full) for `process_agent_update` |
| `__init__.py` | Dispatch entrypoint; delegates to `run_tool_dispatch_pipeline` in `src/services/tool_dispatch_service.py` |

### Auto-Registration System

`auto_register_all_tools` in `src/tool_registration.py` (called from `mcp_server.py`):
1. Reads all tool definitions from `tool_schemas.py`
2. **Filters to only tools in `_TOOL_DEFINITIONS`** (tools with `register=True`)
3. **Filters by the active tool mode** — `get_tools_for_mode(TOOL_MODE)` unless the mode is `full`
4. Creates FastMCP wrappers for each tool
5. Injects `client_session_id` for tools in `TOOLS_NEEDING_SESSION_INJECTION`
6. Registers with `mcp.tool()` decorator

**The both-places rule, now enforced:** a core tool needs a `*Params` schema
(via `TOOL_ORDER`) AND `@mcp_tool` with `register=True` (default). Omitting
either is a startup error as of 2026-08-29 —
`_validate_consolidated_tool_order` in `src/tool_schemas.py` refuses to build
the tool list when a registered, non-hidden, core-handler tool is missing from
`TOOL_ORDER`.

This used to be a silent softening: `get_tool_definitions` auto-discovered the
tool and served an open `{"properties": {}, "additionalProperties": true}`
schema. That is worse than it sounds, because `validate_params` resolves the
real `*Params` model **by tool name** regardless of `TOOL_ORDER` — so the wire
advertised "any parameters accepted" and the server then rejected the call
against a schema the caller was never shown. Six tools sat in that state. The
guard originally covered action routers only, which is exactly how
single-purpose tools drifted out unnoticed.

If a tool is only ever reached through a consolidated router, the fix is
`register=False`, not a `TOOL_ORDER` entry. Plugin tools are exempt from the
guard and keep the auto-discovery path; a plugin that wants a real advertised
schema calls `register_extra_schemas`.

**One name, one home.** A name must be *either* a registered dispatch tool
*or* a `tool_stability` alias, never both — `resolve_alias` rewrites the tool
name before `TOOL_HANDLERS` is consulted, so a name that is both has an
unreachable registration. Fifteen names were in that state until 2026-08-29;
`direct_resume_if_safe` was the one where it was fatal, because its alias
target (`quick_resume`) is itself `register=False`, so every call returned
`tool_not_found_error`. `ALIAS_SHADOWS_REGISTERED_TOOL` in the tool-edge-index
audit and `test_no_alias_name_is_also_a_registered_tool` both guard this now.

### Dispatch Pipeline

When a tool is called, the dispatch entrypoint runs it through middleware steps defined in
the `middleware/` package. The pipeline runner first normalizes MCP `kwargs` wrappers so
identity and continuity inputs are visible before any session resolution or alias logic runs:

```
unwrap_kwargs → resolve_identity → verify_trajectory → resolve_alias → inject_identity → validate_params
    ↓ (handler lookup)
check_rate_limit → track_patterns → handler()
    ↓ (result)
apply_experience_envelope → apply_identity_warnings
```

Pre-dispatch and post-validation steps are `async (name, arguments, ctx) → (name, arguments, ctx) | list[TextContent]`;
returning a list short-circuits with that error response. The `POST_EXECUTION_STEPS`
run over the handler *result* with signature `(name, arguments, ctx, result) → result`.
State flows via the `DispatchContext` dataclass.

---

## Consolidated Tools

To reduce cognitive load, related tools are consolidated into single tools with an `action`
parameter. Action counts drift as tools evolve — the authoritative list is the `actions={}`
map each router passes in `src/mcp_handlers/consolidated.py` (and `TOOL_EDGE_INDEX.md`
renders it). As of 2026-08-16:

| Consolidated Tool | Actions | Example |
|-------------------|---------|---------|
| `knowledge` | 12 | `knowledge(action='search')` |
| `observe` | 9 | `observe(action='anomalies')` |
| `admin` | 9 | `admin(action='health')` |
| `dialectic` | 8 | `dialectic(action='list')` |
| `agent` | 6 | `agent(action='list')` |
| `calibration` | 4 | `calibration(action='check')` |
| `config` | 2 | `config(action='get')` |
| `export` | 2 | `export(action='history')` |

(The former `pi` consolidated tool moved to the `unitares-pi-plugin` package.)

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
    description="Unified my_group operations.",  # prose ONLY — the router derives
                                                 # and appends the action list itself;
                                                 # do NOT hand-list actions here
    default_action="action1",                    # Optional: used when action is missing
    param_maps={"action2": {"q": "query"}},      # Optional: remap params per action
    pre_onboard_actions={"action1"},             # Actions callable WITHOUT a bound
                                                 # identity; everything else is
                                                 # identity-gated (#425). Omit = all gated.
    examples=["my_group(action='action1')"],     # Optional: shown in error messages
)
```

The action-router helper handles action extraction, validation, error messages with valid
actions + examples, parameter remapping, and MCP registration. Think about
`pre_onboard_actions` deliberately: 6 of the 8 live routers use it, and omitting it gates
every action behind the strict-identity check.

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
    deprecated=False,           # Mark as deprecated
    hidden=False,               # Hide from list_tools (still callable)
    superseded_by="new_tool",   # What replaced this tool
    register=True               # If False, NOT exposed to MCP clients
)
```

(`rate_limit_exempt` was removed 2026-06-12; rate limiting is handled uniformly
by the middleware step.)

### When to use `register=False`

Use `register=False` for handlers that are:
- Called by consolidated tools (e.g., `get_dialectic_session` called by `dialectic(action='get')`)
- Internal utilities not meant for direct use
- Deprecated tools that should only work via alias resolution

**Note:** an alias does not make a `register=False` handler dispatchable — it
*rewrites the call* to the alias's `new_name` (a registered tool) and injects the
action. `TOOL_HANDLERS` is populated only from `_TOOL_DEFINITIONS`, so an alias
whose `new_name` is itself `register=False` would hit `tool_not_found_error`.

---

## Session Injection

Identity is primarily UUID-based (`agent_uuid` from `onboard()`). Session injection of `client_session_id` is a legacy/external client compatibility mechanism.

**When to add a tool to `TOOLS_NEEDING_SESSION_INJECTION` (in `src/tool_registration.py`):**
- Tool needs caller identity for external/non-UUID clients
- Tool stores data associated with an agent (prefer UUID lookup when available)
- Tool needs to know "who is calling" and cannot receive `agent_uuid` directly

---

## Tool Tiers (for list_tools filtering and tool modes)

Tools are organized into tiers in `src/tool_modes.py` (`TOOL_TIERS` is the
authoritative list; sizes drift, don't trust counts written into prose):

| Tier | Purpose | Example Tools |
|------|---------|---------------|
| `essential` | Core workflow | `identity`, `start_session`, `sync_state` |
| `common` | Regular use | `onboard`, `process_agent_update`, `list_tools` |
| `advanced` | Operator/rare use | `admin`, diagnostics tools |

**When adding a new tool, add it to the appropriate tier.** Tier membership is
not just cosmetic: restricted tool modes register only their tiers' tools.

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
**Cause:** Handler has `register=False` or missing `@mcp_tool`.
**Fix:** Ensure handler has `@mcp_tool` with `register=True` (default).

### 2. Consolidated tool's sub-handler not working
**Cause:** Handler function not imported in `consolidated.py`.
**Fix:** Add import and route in the consolidated handler's `actions={}` map.

### 3. Old tool name not resolving
**Cause:** Missing alias in `tool_stability.py`.
**Fix:** Add alias mapping old name to new consolidated tool.

### 4. Session identity not working
**Cause:** Tool not in `TOOLS_NEEDING_SESSION_INJECTION`.
**Fix:** Add tool name to the set in `src/tool_registration.py`.

### 5. Tool registered but absent under a restricted tool mode
**Cause:** Tool not in any tier the active `TOOL_MODE` includes — the
registration pass filters through `get_tools_for_mode`.
**Fix:** Add the tool to the right tier in `src/tool_modes.py`.

### 6. Every action of a new consolidated tool refused for unbound callers
**Cause:** `action_router` called without `pre_onboard_actions` — all actions
default to identity-gated.
**Fix:** Declare the read-only actions that should work pre-onboard.

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
| Add new standalone tool | `*Params` model + `tool_descriptions.py` + `TOOL_ORDER` + handler in `mcp_handlers/<subpackage>/` + tier in `tool_modes.py` |
| Add to consolidated tool | `consolidated.py` (add to `action_router` actions dict; check `pre_onboard_actions`) + `register=False` on handler |
| Add dispatch middleware step | `middleware/` package (add step module or function + wire into `PRE_DISPATCH_STEPS`, `POST_VALIDATION_STEPS`, or `POST_EXECUTION_STEPS`) |
| Tool needs session | + `TOOLS_NEEDING_SESSION_INJECTION` in `tool_registration.py` |
| Rename/deprecate tool | `tool_stability.py` (add alias) |
| Categorize for list_tools / tool modes | `tool_modes.py` (add to tier) |

---

**Last Updated:** 2026-08-16 (full re-verification against master: registration moved to `tool_registration.py`, Pydantic-built schemas, three-phase middleware package, tool-mode filter, `pre_onboard_actions`, current action counts; drift list in #1702)

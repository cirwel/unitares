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
3. One `ToolMeta` record in `src/tool_meta.py`: category, tier, operation
   (read / write / admin), stability, and relationships. The record's position
   among the registered tools is the `tools/list` order, and the five
   bookkeeping maps (`TOOL_TIERS`, `TOOL_OPERATIONS`, `TOOL_CATEGORIES`,
   `_TOOL_STABILITY`, `TOOL_RELATIONSHIPS`) derive from it; there is nothing
   else to add. Server startup refuses a registered tool with no record and a
   record with no registered tool.

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

**Step 4: Add the tool to a mode set** if agents should see it without
`GOVERNANCE_TOOL_MODE=full` (`LITE_MODE_TOOLS` in `src/tool_modes.py`;
`MINIMAL_MODE_TOOLS` is the five-tool checkpoint loop and does not grow). The
tier and category come from the `ToolMeta` record of Step 1. A tool outside the active mode set is still registered and
callable by name on every transport; it is absent from `tools/list` (see Common
Mistakes #5).

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
3. **Does not apply the tool mode** — every registered tool is registered with
   FastMCP whatever `GOVERNANCE_TOOL_MODE` says; the mode filters `tools/list`
   only, through `src/tool_mode_listing.py::mode_filtered_server_class`
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
    deprecated_since=datetime(2026, 9, 7),  # the day the old name stopped being canonical
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

`source_module` is also accepted but is not for hand-authored tools — only
`action_router` passes it, to record the module that declared the router rather
than `decorators.py`. See *Plugin tools vs. this repo's tools* below.

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

## Plugin tools vs. this repo's tools

`_TOOL_DEFINITIONS` describes **the running process, not the repo**. An
entry-point plugin (`governance_mcp.plugins`, e.g. the out-of-repo
`unitares-pi-plugin`) registers into the same dict through the same
`@mcp_tool` / `action_router` calls, so "registered" alone cannot answer "does
this repo ship it".

Every `ToolDefinition` therefore records `source_module`, the import path of
the module that **declared** the tool. It is filled in automatically:

- `@mcp_tool` records `func.__module__`.
- `action_router` records its **caller's** module. A router's handler is built
  inside `mcp_handlers/decorators.py`, so `handler.__module__` names governance
  for every router, a plugin's included — do not use it for provenance.

`decorators.list_plugin_registered_tools()` returns everything declared outside `src.` /
`governance_core.`. Two consumers:

- `tool_schemas._is_core_handler` — only a tool this repo ships must appear in
  `TOOL_ORDER`; a plugin keeps the auto-discovery path and supplies its own
  schemas via `tool_schemas.register_extra_schemas()`.
- `tests/conftest.py::first_party_tool_surface` — a fixture that lifts foreign
  registrations out for the duration of a test, so a surface-drift assertion
  compares the surface this repo ships. `tests/test_describe_tool_drift.py` and
  `tests/test_lite_wire_surface.py` use it module-wide. Without it those tests
  depended on collection/import order: `tests/test_pi_orchestration.py` imports
  the pi plugin's handlers at module scope, so pytest **collection** fires that
  package's decorators before any test runs, while `TOOL_HANDLERS` snapshots
  the registry when `src.mcp_handlers` is imported. They failed on
  `pi_restart_service` / `pi` in full local runs on machines with the plugin
  installed and passed everywhere else, CI included.

If you add a test that asserts something about the whole tool surface, request
that fixture.

---

## Session Injection

Identity is primarily UUID-based (`agent_uuid` from `onboard()`). Session injection of `client_session_id` is a legacy/external client compatibility mechanism.

**When to add a tool to `TOOLS_NEEDING_SESSION_INJECTION` (in `src/tool_registration.py`):**
- Tool needs caller identity for external/non-UUID clients
- Tool stores data associated with an agent (prefer UUID lookup when available)
- Tool needs to know "who is calling" and cannot receive `agent_uuid` directly

---

## What a Parameter Description Costs

A parameter description is paid for on every `tools/list`, in every session, by
every client — before the agent has decided it wants the tool. Measured on the
FastMCP wire, 2026-09-08, before the trim below:

| Surface | Advertised cost |
|---|---|
| `sync_state` alone | 8,076 chars (~2,020 tokens) |
| `minimal` profile (5 tools) | 21,884 chars (~5,470 tokens) |
| `standard` profile (11 tools) | 43,801 chars (~10,950 tokens) |

About half of that was parameter prose. A profile cut does not touch it: a
profile removes names from the list, not words from the names that remain.
After the trim: `sync_state` 6,283 chars (~1,570 tokens), `minimal` ~4,290,
`standard` ~9,380, `lite` ~22,600 — the profile totals move -22% / -14% / -12%.

So the advertised catalog serves an **abridged** description and
`describe_tool` serves the authored one. `src/schema_brief.py` owns the rule:

- A field may carry an authored short form —
  `Field(description=..., json_schema_extra={"brief": "..."})`. It always wins,
  is not held to the budget (some parameters *are* their enum list), and the
  `brief` key never reaches a caller on any surface.
- Otherwise the description is returned whole when it already fits
  `BRIEF_BUDGET` (140 chars), and trimmed to its first sentence when it does
  not. Abbreviations ("e.g.") and single-letter initials are not sentence ends.
- A first sentence still over budget is cut on a word boundary and marked with
  an ellipsis, so a truncated description looks truncated.

The same three modes are available on `apply_field_description_mode` and on
`UNITARES_TOOL_SCHEMA_FIELD_DESCRIPTIONS`:

| Mode | Advertised |
|---|---|
| `brief` (default) | authored short form, else the first sentence |
| `full` | the complete authored text — the pre-2026-09-08 surface, byte for byte |
| `off` | no field descriptions at all (the old `UNITARES_TOOL_SCHEMA_STRIP_FIELD_DESCRIPTIONS=1`) |

`UNITARES_TOOL_SCHEMA_BRIEF_BUDGET` moves the cap. `tool_schemas.advertised_input_schema`
is the one place the modes are applied, so the wire catalog, `describe_tool` and
the tool-surface audit cannot drift apart; workflow-alias property overrides in
`src/alias_schema.py` observe the same mode, which is why they carry a `brief`
of their own.

**When you write a parameter description**, put the sentence a caller needs to
make the call first, and the caveats after. If the first sentence alone would
make the parameter unusable — it names an action but not the enum, or a shape
but not its required keys — author a `brief` rather than lengthening the
sentence. `tests/test_schema_brief.py` holds the trim to being a trim: no
parameter name, type, default or requiredness may move between `brief` and
`full`.

Two things this does **not** do, deliberately:

- It does not remove anything from the system. Every word stays on the Pydantic
  model and `describe_tool(tool_name=..., action=...)` still serves it. The MCP
  `instructions` string says so once at initialize, rather than paying for a
  pointer on every parameter.
- It does not touch the structural half of the schema. Pydantic/FastMCP emit a
  `title` for every property (a titleized copy of the key) and
  `anyOf: [{type: X}, {type: "null"}]` for every optional — 663 and 396 chars
  respectively on `sync_state` after the trim. That is a separate lever with a
  separate risk profile (the advertised schema would stop matching what
  FastMCP's argument model generates), and it is not attempted here.

Half of that structural lever is now **applied**. The `title` half was
measured first (`scripts/diagnostics/tool_surface_cost.py --boilerplate`) and
then removed, because the two halves are not the same proposition:

| Profile | Before | After | Saved |
|---|---|---|---|
| `minimal` | 19,112 B | 17,096 B | 2,016 (10.5%) |
| `standard` | 52,612 B | 47,047 B | 5,565 (10.6%) |
| `lite` | 91,390 B | 81,618 B | 9,772 (10.7%) |
| `full` | 129,043 B | 115,115 B | 13,928 (10.8%) |

A `title` is not authored by anyone. Pydantic stamps the model's class name at
the root (`OnboardParams`) and a titleized echo of the key on every field
(`client_session_id` → "Client Session Id"). JSON Schema does not validate
against it, so unlike a description there is no fuller form to fall back to and
no surface on which keeping one explains anything — which is why
`describe_tool` drops it too, rather than serving it the way it serves full
descriptions. `src/schema_brief.py` owns the rule
(`apply_property_title_mode`), `src/tool_schemas.py::advertised_input_schema`
applies it to all three surfaces, and
`UNITARES_TOOL_SCHEMA_PROPERTY_TITLES=keep` restores the pre-2026-09-08 surface
byte-for-byte.

The measured saving (10.8%) is larger than the 9% the `--boilerplate` estimate
predicted, because that estimate counted only the per-property titles and not
the root model-class title on each of the 50 schemas.

The null-union half stays **measured but not applied**: flattening
`anyOf: [{type: X}, {type: "null"}]` is a real narrowing — an explicit `null`
stops validating — and it is worth 7% of a profile. That is a contract change,
not a trim, and it is a separate decision.

---

## What a Profile Costs

`scripts/diagnostics/count_tools.py` answers "how many tools are there".
`scripts/diagnostics/tool_surface_cost.py` answers what a context budget
actually asks — how much advertising them costs, in the bytes a client receives
before the agent has decided it wants any of them. Measured 2026-09-08:

| Profile | Tools | Advertised | ~tokens | vs `minimal` |
|---|---|---|---|---|
| `minimal` | 5 | 17,096 B | ~4,274 | 1.0x |
| `standard` (default) | 14 | 47,047 B | ~11,761 | 2.8x |
| `lite` | 29 | 81,618 B | ~20,404 | 4.8x |
| `full` | 50 | 115,115 B | ~28,778 | 6.7x |

Bytes are measured; tokens are an estimate at 4 B/token, not a tokenizer
result. Two things this table settles:

- **`lite` is the second-widest profile, not a light one.** The ladder is
  ordered `minimal < standard < lite < full` and `--check-ladder` confirms it
  holds in both senses that matter — each rung advertises a superset of the one
  below it, and each costs more. The structure is sound; only the *name* is
  wrong for its position, and renaming it recovers no bytes.
- **Cost tracks parameter breadth, not tool count.** In `standard` the
  knowledge graph is 46% of the payload and is advertised twice: the
  `knowledge` router (51 params, 1 required, 9,381 B) plus the aliases
  `search_shared_memory` (36 params, 6,880 B), `store_finding` (2,673 B) and
  `update_finding` (2,664 B). The last two are small because they use
  keep-lists in `src/alias_schema.py` that advertise only the parameters their
  pinned action reads; `search_shared_memory` uses a subtraction list and still
  carries `closure_class`, `closure_evidence`, `use_llm`, `topic`,
  `min_members` and other parameters belonging to *other* actions of the router.

Use `--mode <profile> --params` for the per-parameter breakdown behind those
numbers.

---

## Hints That Name Unadvertised Tools

A response saying "poll `dialectic(action='get', ...)`" is an instruction, and
a schema-driven client can only call names `tools/list` returned. When the
named tool is not advertised, the instruction is a dead end — the server told
the agent to do something it has no way to do. `src/tool_modes.py` is right
that an unadvertised name still *dispatches*; that is a property no
schema-driven client can use.

`tests/test_lite_wire_surface.py` holds this invariant against two hand-written
lists. `scripts/diagnostics/hint_target_advertisement.py` derives the set
instead, reading caller-facing response keys out of the handler tree. On
`standard`, 2026-09-08: **10 tools named in caller-facing hints are not
advertised, across 53 sites.** They split two ways, and `--classify` says which:

1. **The hint names the raw twin of an advertised alias** — `onboard` (24
   sites) for `start_session`, `process_agent_update` (4) for `sync_state`,
   `get_governance_metrics` (2) for `check_working_state`. The capability *is*
   advertised; the hint just says a name the client was never shown. Fix the
   hint text; costs nothing on the wire.
2. **The hint names a capability the profile does not advertise** —
   `dialectic` (8 sites, actions `get`/`reassign`/`request`/`thesis`),
   `observe` (7), `agent`, `bind_session`, `cirs_protocol`,
   `operator_resume_agent`, `verify_trajectory_identity`. Either advertise it,
   or route the hint through something that is.

An alias pins one action of its router, so alias coverage is checked per
action: `request_review` covers `dialectic(action='request')` and nothing else
on that router, which is why `dialectic` lands in class 2 despite having an
advertised alias.

This was found the hard way. A Claude Code session on the default `standard`
profile called `request_review`; the response told it to poll
`dialectic(action='get', session_id=...)`, and the session could not — the name
had never been advertised, so it was never offered to the model. Before
proposing that any capability be dropped from a profile on the grounds that it
"stays callable by name", run this script: that argument has a measured failure
rate.

---

## Tool Tiers (for list_tools filtering and tool modes)

Every advertised name has a tier on its `ToolMeta` record in
`src/tool_meta.py`; `TOOL_TIERS` in `src/tool_modes.py` is derived from those
records (sizes drift, don't trust counts written into prose):

| Tier | Purpose | Example Tools |
|------|---------|---------------|
| `essential` | Core workflow | `identity`, `start_session`, `sync_state` |
| `common` | Regular use | `onboard`, `process_agent_update`, `list_tools` |
| `advanced` | Operator/rare use | `admin`, diagnostics tools |

**When adding a new tool, give its record the appropriate tier**, and add the
name to a mode set if agents should be shown it below `full`. Mode membership decides what
`tools/list` advertises; registration (and therefore dispatch by name) is the
same in every mode.

---

## Tool Aliases (Backwards Compatibility)

When renaming/consolidating tools, add aliases in `src/mcp_handlers/tool_stability.py`:

```python
_TOOL_ALIASES = {
    "old_tool_name": ToolAlias(
        old_name="old_tool_name",
        new_name="new_tool_name",
        reason="consolidated",  # or "renamed", "deprecated"
        deprecated_since=datetime(2026, 9, 7),  # the day the old name stopped being canonical
        migration_note="Use new_tool_name(action='...') instead"
    ),
}
```

Aliases are resolved at dispatch time, so old tool names continue to work.

A consolidated, renamed, or deprecated alias carries `deprecated_since`: the
date the old name stopped being canonical, which `describe_tool` reports in
its `alias` block beside the migration note. An intuitive alias (`start`,
`status`, the workflow names such as `sync_state`) carries none, because that
name was never canonical and nothing was deprecated.
`tests/test_tool_registry_bookkeeping.py` holds the table to the rule, and
where `DEPRECATION_REGISTRY` in `introspection/tool_catalog.py` also names the
tool, the two dates must agree.

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

### 5. Tool registered but absent from `tools/list` under a restricted tool mode
**Cause:** Tool not in the active mode's set — `tools/list` is filtered through
`get_public_tool_definitions(TOOL_MODE)`. The tool still dispatches by name
(REST, stdio, and `/mcp/` alike); a schema-driven client just cannot see it.
**Fix:** Add the tool to the right mode set in `src/tool_modes.py`, or run the
server with a wider `GOVERNANCE_TOOL_MODE`.

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

# What each profile costs on the wire, and whether the ladder still holds
python3 scripts/diagnostics/tool_surface_cost.py
python3 scripts/diagnostics/tool_surface_cost.py --check-ladder
python3 scripts/diagnostics/tool_surface_cost.py --mode standard --params
python3 scripts/diagnostics/tool_surface_cost.py --boilerplate

# Hints that name a tool the profile does not advertise
python3 scripts/diagnostics/hint_target_advertisement.py --classify
python3 scripts/diagnostics/hint_target_advertisement.py --mode lite
```

---

## Summary

| Task | Files to Edit |
|------|---------------|
| Add new standalone tool | `*Params` model + `tool_descriptions.py` + `ToolMeta` record in `tool_meta.py` + handler in `mcp_handlers/<subpackage>/` |
| Add to consolidated tool | `consolidated.py` (add to `action_router` actions dict; check `pre_onboard_actions`) + `register=False` on handler |
| Add dispatch middleware step | `middleware/` package (add step module or function + wire into `PRE_DISPATCH_STEPS`, `POST_VALIDATION_STEPS`, or `POST_EXECUTION_STEPS`) |
| Tool needs session | + `TOOLS_NEEDING_SESSION_INJECTION` in `tool_registration.py` |
| Rename/deprecate tool | `tool_stability.py` (add alias) |
| Categorize / tier / classify for list_tools and tool modes | `tool_meta.py` (the tool's record) |
| Check what a profile costs, or what a hint promises | nothing to edit — run `scripts/diagnostics/tool_surface_cost.py` and `scripts/diagnostics/hint_target_advertisement.py` |

---

**Last Updated:** 2026-08-16 (full re-verification against master: registration moved to `tool_registration.py`, Pydantic-built schemas, three-phase middleware package, tool-mode filter, `pre_onboard_actions`, current action counts; drift list in #1702)

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
applies these modes to the source catalog and `describe_tool`; workflow-alias property overrides in
`src/alias_schema.py` observe the same mode, which is why they carry a `brief`
of their own.

**When you write a parameter description**, put the sentence a caller needs to
make the call first, and the caveats after. If the first sentence alone would
make the parameter unusable — it names an action but not the enum, or a shape
but not its required keys — author a `brief` rather than lengthening the
sentence. `tests/test_schema_brief.py` holds the trim to being a trim: no
parameter name, type, default or requiredness may move between `brief` and
`full`.

The complete descriptions remain on the Pydantic models and are served by
`describe_tool(tool_name=..., action=...)`. The transport may regenerate
schemas from those definitions; source-catalog equality is not a wire check.

### Generated titles and validation

`src/schema_brief.py::apply_property_title_mode` removes generated `title`
annotations by default. Catalog construction applies it upstream, and
`src/tool_mode_listing.py` applies it again **after FastMCP regenerates its
schemas**. The latter copies the advertised Tool objects; it does not mutate
argument models or dispatch validation. A parameter named `title`, or a
`title` inside caller defaults/examples, remains intact.

`UNITARES_TOOL_SCHEMA_PROPERTY_TITLES=keep` restores generated titles in the
current listing. It does not restore a historical payload or fingerprint
across unrelated changes. The final-listing tests check keep/strip/keep
behavior independently of catalog policy. Dropping titles preserves
validation but changes schema fingerprints.

With MCP 2.1.1, brief descriptions and the current search alias, measured
2026-09-08 as compact UTF-8 JSON `ListToolsResult` objects:

| Profile | Titles kept | Titles stripped | Saved |
|---|---:|---:|---:|
| `minimal` | 16,679 B | 14,945 B | 1,734 B |
| `standard` | 52,469 B | 46,829 B | 5,640 B |
| `lite` | 84,734 B | 75,576 B | 9,158 B |
| `full` | 120,437 B | 107,189 B | 13,248 B |

The earlier #2115 numbers measured the catalog and missed titles regenerated
by FastMCP. Do not use them as measured MCP savings. `--boilerplate` now
applies the same recursive title transform to the explicitly selected layer.
The null-union experiment remains diagnostic only: removing the `null`
alternative changes validation and is not applied to the server.

## What a Profile Costs

`scripts/diagnostics/tool_surface_cost.py` defaults to the final local MCP
listing, after registration and listing policy. `--surface catalog` explicitly
selects source definitions instead. Bytes include compact UTF-8 result JSON,
including SDK result metadata and separators. They exclude JSON-RPC IDs,
transport framing, compression and context added by a client. The command
constructs and lists the local server without starting its lifespan, calling
a tool, or contacting a database; it is not a deployed-server probe.

| Profile | Tools | MCP result | Estimated tokens at 4 B/token |
|---|---:|---:|---:|
| `minimal` | 5 | 14,945 B | 3,736 |
| `standard` (default) | 15 | 46,829 B | 11,707 |
| `lite` | 29 | 75,576 B | 18,894 |
| `full` | 50 | 107,189 B | 26,797 |

These are measurements under MCP 2.1.1 on 2026-09-08, with brief descriptions,
stripped titles and first-party tools. SDK versions, settings and installed
plugins can change the result. Tokens are estimates, not tokenizer counts.
`--check-ladder` checks containment of the **measured names** and increasing
bytes for `minimal < standard < lite < full`. A missing rung is unchecked and
fails the guard. Unknown profile spellings are errors, not a fallback to full.

The search alias now advertises 22 parameters rather than 36. Its subtraction
list drops 14 controls belonging to other actions (closure, synthesis, audit,
lineage and details pagination). `discovery_type`, `severity` and
`include_provenance` stay: the search parser reads all three even though the
older `ACTION_FIELDS["search"]` omitted them. The field map now includes them.
Keep-list or drop-list membership must be checked against the handler and its
helpers, not inferred from a possibly stale discovery map. This is contract
release 1.5.0; see `docs/INTERFACE_CONTRACT.md` for the exact removed names.

Against #2119 at `872349b3`, the standard result falls from 55,386 B to
46,829 B (8,557 B, 15.4%), retaining all 15 names. Use `--mode standard
--params` to locate remaining cost before designing further schema changes.
No usage count or byte budget authorizes retiring a capability.

## Hints That Name Unadvertised Tools

#2119 adds `dialectic` to `standard`, closing the concrete discovery gap where
an agent could open a review with `request_review` and then was not offered
its polling/progress actions. The router has **eight** advertised actions:
`request`, `thesis`, `antithesis`, `synthesis`, `get`, `list`, `reassign`, and
`quick`. This verifies advertised coverage; it does not prove a whole review
will complete successfully under every identity/state condition.

`scripts/diagnostics/hint_target_advertisement.py` produces a **static candidate
inventory**. Its `HINT_KEYS` list is a heuristic seed, not proof that a value is
serialized to a caller. It scans literals under keys such as `hint`, `next_call`,
`safe_options[*].call`, `message`, and `related_tools`; follows local bindings
and return builders; and handles nested lists/dicts/f-strings. Structured
`related_tools` values can name tools without parentheses. Prose requires an
adjacent `tool(` call shape, excluding the English plural `session(s)`.
Comments and docstrings are not seed values. Dynamic string construction and
arbitrary data flow remain outside the guarantee. JSON discloses these limits.

Emitter resolution follows bare calls, import aliases and attribute calls up
to three hops. It conservatively joins same-named helpers, treats middleware
as reachable on every profile, and keeps unresolved paths. This prevents a
known operator caller from hiding a second agent caller written as
`module.helper()`. It does not prove path conditions, auth state, actual
serialization, or that an instruction is meant for this caller rather than an
operator. Those are inputs to severity review, not facts the count establishes.

Coverage is per **name and action**. Search and store may be covered by two
different aliases; an uncovered third action remains visible. JSON includes
each site's action and candidate advertised alias. Even full name/action
coverage does not establish argument or response compatibility:
`get_governance_metrics(agent_id=...)` cannot simply become
`check_working_state(agent_id=...)`, since that alias hides the field.

### Reviewed candidates and open work

The four reviewed entries from #2119 remain in `KNOWN_DEAD_ENDS`, with reasons
and keys that include the action as well as the tool and source line. They
are not a complete baseline for the expanded scan. Three describe benign
context; the cross-agent metrics instruction remains an explicitly open
question. An accepted entry for one action cannot hide another at the same
line. Moved/deleted entries are reported stale.

The expanded inventory finds additional candidates in structured names and
previously unscanned fields. **`--fail-on-finding` currently exits 3 on
`standard`** because these candidates have not been reviewed or fixed. A green
test suite verifies scanner behavior; it does not certify that every agent
workflow is closed. Do not blindly add those findings to the ledger or expose
operator capabilities to make a count green. Assess the receiving profile and
caller, then correct actionable hints or design a separately reviewed surface
change. Other profiles do not inherit the standard ledger.

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
python3 scripts/diagnostics/tool_surface_cost.py --surface catalog --mode standard

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

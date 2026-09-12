# Tool surface audit — 2026-09-12

Scope: the MCP tool surface as shipped in this repository — registration and
dispatch (`@mcp_tool`, `action_router`, the alias table), the advertised wire
catalog on every transport, the introspection tools (`list_tools`,
`describe_tool`), and the bookkeeping tables that describe them (`tool_meta`,
`tool_annotations`, `stakes_table`, `tool_catalog`, session injection). No
runtime behavior, schema, default, tier, authorization, or storage was changed;
this document is the deliverable.

Source baseline: `be117c2` (`origin/master` at audit time). Environment: Python
3.12.3, `mcp` 2.1.1 (the `constraints.txt` pin), `pydantic` 2.13.5 /
`pydantic-core` 2.46.5, `requirements-full.txt` installed, entry-point plugins
disabled. No live server, database, Redis, or deployed peer was involved; every
result below comes from importing the handler package and reading the live
registries, the same objects dispatch uses.

## Instrument verdicts

Every deterministic instrument the repository already ships was run against
the baseline. All of them pass.

| Instrument | Result |
|---|---|
| `scripts/dev/tool_edge_index.py --check` | `docs/dev/TOOL_EDGE_INDEX.md` is up to date |
| `scripts/dev/tool_edge_index.py --lint` | 0 errors, 0 warnings, 0 informational; bundle `sha256:1a1bb073e6bc5d28…` |
| `scripts/dev/tool_edge_index.py --json` | validates against `docs/dev/tool_surface_audit_v1.schema.json`; 42 tools, 8 routers, 52 actions, 70 aliases, 0 import failures |
| `scripts/diagnostics/validate_tool_modes.py` | OK (advertised=50, categorized=50, schema_definitions=42, aliases=70) |
| `scripts/diagnostics/validate_tool_registration.py` | decorator registry, dispatch table, visible tools, advertised schemas all 42; 8 workflow aliases |
| `scripts/diagnostics/count_tools.py --json` | 42 |
| `scripts/diagnostics/update_docs_tool_count.py --check --require-registry` | documentation count correct |
| `scripts/diagnostics/audit_tool_categories.py` | 0 non-existent names; eleven categories totalling 50 |
| `scripts/diagnostics/hint_target_advertisement.py --fail-on-finding --classify` | no candidate advertisement mismatches |
| `scripts/diagnostics/check_doc_health.py --strict` (ghost-tool scan: a backticked identifier with empty parentheses in docs that is neither a registered tool nor an alias) | clean; the first draft of this document tripped it on a non-tool function name, which is the rule working |
| `python -m src.interface_contract` | byte-identical to `docs/interface-contract.v1.json` (1.6.0, 50 capabilities, `surface_sha256 3bf9f94d…`) |
| `python -m src.mcp_handlers.stakes_table` | 99 entries (23 high, 76 baseline) |
| `scripts/diagnostics/tool_surface_cost.py --surface mcp` | 50 tools, 133,061 bytes, identical for every legacy mode label |
| In-process `describe_tool` | 167 calls (50 wire names, 55 router actions, 62 dispatch-only alias names): 0 failures |
| In-process `list_tools` | six argument shapes (`lite`, default, `essential_only`, `category`, `tier`, `verbose`): 0 failures, 0 dispatch-only names in `tools[]` |
| Focused test files (`tests/test_tool_*.py`, wire surface, describe drift, stakes, action-level identity, interface contract, alias narrowing, hint gate, bookkeeping, residentless install, schema brief, onboard pin) | see the validation receipt |

The structural layer — name → handler → schema → alias → identity class →
stakes class — is coherent, and the guards that hold it (the edge-index lint,
`_validate_consolidated_tool_order`, the bookkeeping tests, the hint gate) are
wired into CI and green. The findings below are in the places those guards do
not reach.

## Findings

Severity is about consequence to a caller or to a measurement, not about effort
to fix. Every remedy is listed as an option; where a standard has to be chosen
before a fix is a fix, the choice is named as a decision point rather than
made here.

### F1 — `list_tools(lite=false)` names 30 tools an MCP client cannot call (high)

The non-lite `list_tools` response carries pre-consolidation names in four
blocks that are assembled outside the advertised catalog:

- a hand-written `categories` block inside `handle_list_tools`
  (`src/mcp_handlers/introspection/tool_introspection.py:606-677`) — a second
  category system beside the `tool_meta`-derived `categories_summary`. It lists
  47 names; 30 are dispatch-only (`list_agents`, `observe_agent`,
  `store_knowledge_graph`, `get_server_info`, `submit_thesis`, …) and it omits
  33 of the 50 advertised names — all eight workflow aliases, all eight
  routers, `consult`, `skills`, `bind_session` among them — which
  `categories_summary` places correctly. The block predates the router
  consolidation and was never regenerated;
- `getting_started.next_steps` (`:705-716`): `list_agents`,
  `store_knowledge_graph`;
- `workflows`, from `tool_catalog.WORKFLOWS`
  (`src/mcp_handlers/introspection/tool_catalog.py:121-151`): `list_agents`,
  `observe_agent`, `aggregate_metrics`, `detect_anomalies`,
  `get_system_history`, `export_to_file`;
- `relationships.*.related_to` / `depends_on`, from the `related_to` fields in
  `src/tool_meta.py`: twelve records name `get_server_info`,
  `get_telemetry_metrics`, `observe_agent`, `list_agents`, `archive_agent`,
  `get_agent_metadata`, `store_knowledge_graph`, `get_discovery_details`,
  `get_system_history`, `request_dialectic_review`; and the ASCII `tool_map`,
  which draws eleven dispatch-only names (`list_agents`, `observe_agent`,
  `detect_anomalies`, `export_to_file`, `delete_agent`, …).

On `/mcp/` FastMCP registers the 42 tools and the 8 workflow aliases and
nothing else, so `call_tool("list_agents", {})` returns `ToolError: Unknown
tool` (verified in-process through the production registrars). The same names
resolve on REST and stdio, whose dispatcher consults the alias table first.
This is the dead-end class #2165 closed for response hints, one surface over.
`hint_target_advertisement.py` structurally cannot see it: bare names are
admitted only under `related_tools`, and these sit under `tools`, `related_to`,
and workflow lists. The `lite=true` view is clean, but its own `more` field
sends callers to `lite=false` "for all tools with full category details".

Options: derive `categories` from `tool_meta` and delete the hand-written
block; write `WORKFLOWS` and `related_to` in the call shape #2165 adopted
(`agent(action='list')`); admit bare names under `tools`, `related_to`,
`depends_on` and workflow keys in the scanner so the state cannot recur.

### F2 — `list_tools` and `tools/list` describe 28 of 50 names differently (medium)

`list_tools` gives `tool_catalog.TOOL_DESCRIPTION_OVERRIDES` priority over the
catalog description (`tool_introspection.py:257`), and the lite `hint` is the
first 100 characters of that text (`:390`). The wire, `describe_tool`, and the
interface contract read `src/tool_descriptions.py`. #2148, #2151 and #2158
rewrote the wire text; the overrides were not part of those changes, so the two
discovery surfaces now disagree on 28 names, including the corrections:

| Name | `list_tools` / lite hint | `tools/list` |
|---|---|---|
| `identity` | "Check current binding or set your display name." | "Not a plain read: an argument-less call carries no proof, so the server infers a binding and may … mint and persist a new one" |
| `get_governance_metrics` | "Get current state and metrics without updating." | "runs no cycle and mints no identity, so a client_session_id resolving to no agent gets an explicit unbound payload" |
| `dialectic` | hand-listed action names | behavioral contract (which actions serve unbound callers, `SESSION_EXISTS`, `check_timeout` as a write) |

The `dialectic` override is the hand-maintained action list whose own comment
(`tool_catalog.py:244-248`) records two prior drifts; it is in sync today and
no test pins it. Of the 51 overrides, 20 are for registered tools, 8 for
workflow aliases, and 23 for dispatch-only names, where `describe_tool` still
uses them.

Options: let `list_tools` serve the same first line the wire serves for every
advertised name, keeping overrides only for dispatch-only names; or pin the
overrides to the wire text with a test.

### F3 — `tool_edge_index.py` misreports under its documented dependency floor (medium)

The generator's docstring (lines 26-28) says it needs `requirements-core.txt`.
Its exposure half, `_collect_wire_catalog`, imports `src.tool_registration`,
which imports `src.metrics_registry` and therefore `prometheus_client`, which
is in `requirements-full.txt` only. Reproduced this session on a core-only
install before the full set was added: the collection failure is folded into
the findings as `EXPOSURE_COLLECTION_FAILURE` plus 42 `ORIENTATION_NAME_NOT_ON_WIRE`
warnings (2 errors, 46 warnings), `--lint` exits 1, and `--check` exits 1
calling the committed index stale, instead of the exit 2 that means "cannot
look". CI is unaffected (the smoke job installs the full set,
`.github/workflows/tests.yml:48`); the doctor's `tool_edge_index_fresh` check
on a core-only machine reports FAIL where it should SKIP.

Options: move `prometheus_client` to the core requirements; import the metrics
registry lazily in `tool_registration`; or make an exposure collection failure
an exit-2 condition and keep it out of the committed markdown.

### F4 — `cirs_protocol` sub-actions are never recorded (medium)

`CirsProtocolParams` takes `protocol` (five values) and `action`
(emit / query / compute / set / get / initiate / respond,
`src/mcp_handlers/schemas/core.py:586`), but the decorator declares no
`known_actions`, so `build_tool_usage_payload`
(`src/services/tool_usage_recorder.py:324-336`) records nothing for it: a
registered tool with no vocabulary is treated as single-purpose and its
`action` as a stray kwarg. Every `cirs_protocol` call lands in
`audit.tool_usage` as one undifferentiated row. In the measurement-authority
taxonomy this is state 3, "not recorded": any later per-protocol question about
this tool would read a zero the instrument was blind to. `self_recovery` shows
the declarative fix (`known_actions={"check", "quick", "review"}`).
`describe_tool` also takes `action` (the router-action selector); recording it
is optional and not claimed here.

### F5 — `TOOLS_NEEDING_SESSION_INJECTION` is out of step with the registry (low)

The set (`src/tool_registration.py:364-386`) holds 21 names. Thirteen are no
longer registered tools — `archive_agent`, `compare_me_to_similar`,
`delete_agent`, `export_to_file`, `get_agent_metadata`, `get_discovery_details`,
`get_knowledge_graph`, `get_system_history`, `observe_agent`,
`request_dialectic_review`, `store_knowledge_graph`, `update_agent_metadata`,
`update_discovery_status_graph` — so the registrar, which iterates registered
tools only, never reads them. The routers that absorbed them (`agent`,
`export`, `knowledge`, `observe`) and `outcome_event` are not members, and the
alias registrar keys membership on the canonical name, so `store_finding`,
`update_finding`, `search_shared_memory` and `record_result` are not injected
either.

The consequence verified from code: the tool wrapper reads
`client_session_id` from the arguments before dispatch for the
`audit.tool_usage` row (`tool_registration.py:206`). A client that omits it
gets a `session_id` on `sync_state`, `start_session` and `request_review` rows
and NULL on `store_finding`, `record_result`, `knowledge`, `agent`, `observe`
and `export` rows; `scripts/dev/adoption_kpi.py:371-373` counts NULL as
`unattributed_events`. Identity resolution reads the transport signals on its
own path (`derive_session_key`), so no identity-binding difference is claimed;
that was read, not exercised against a live transport. The injected value is
marked transport-injected (`wrapper_generator.py:373-380`), which answers the
older laundering concern noted in `src/tool_schemas.py`.

Membership is part of the writer-locked identity/onboarding surface, so this is
recorded rather than changed. Option: decide membership per canonical tool,
routers included, and hold the set to registered names with a test.

### F6 — `stakes_table` classifies 13 names nothing dispatches (low)

`src/mcp_handlers/stakes_table.py` carries tool-level entries for
`cleanup_stale_locks`, `reset_monitor`, `reassign_reviewer`,
`submit_synthesis` (in `_HIGH`) and `debug_request_context`,
`get_connection_status`, `get_server_info`, `get_telemetry_metrics`,
`get_tool_usage_stats`, `request_dialectic_review`, `submit_antithesis`,
`submit_thesis`, `validate_file_path` (in `_BASELINE`). All thirteen are
aliases; `get_call_stakes_requirement` canonicalizes before lookup, so the
entries are never consulted, while the `export_table` serializer — the porting
contract for a non-Python gate — emits them anyway. `tests/test_stakes_table.py` proves
registered → table and not the reverse for tool-level keys. Option: prune, and
add the reverse assertion.

### F7 — annotations and stakes disagree on what "destructive" means (low, decision point)

`src/tool_annotations.py` marks `detect_stuck_agents`, `export` (`action=file`
truncates an existing file) and `self_recovery` `destructiveHint=True`, and
`update_finding` the same; `stakes_table` classifies every entry for those
tools (`knowledge:update` included) as baseline. The two tables apply different
standards — "can remove or overwrite something the caller had" against
"high-consequence boundary" — and the stakes table is inert today, so nothing
misbehaves. Which standard governs when the stakes gate is built is a choice,
and it should be made before the gate consults the table, not discovered by it.

### F8 — three documentation sites lag the code (low)

- `docs/dev/TOOL_REGISTRATION.md`: step 1 correctly says to add a `ToolMeta`
  record, but lines 63, 91, 95, 100 and 107 still present `TOOL_ORDER` as an
  edit point. It is derived from `tool_meta.WIRE_ORDER`
  (`src/tool_schemas.py:103-105`).
- The shared contract (`CLAUDE.md:284-287`, `AGENTS.md:302-305`) lists six
  workflow aliases; `AGENT_WORKFLOW_ALIASES` advertises eight (`store_finding`,
  `update_finding` are absent from the doc). Writer-locked surface; reported,
  not edited.
- `src/tool_registration.py` comments: "~51 tools" (`:163`), "~100 tools NOT
  in the routing table" (`:265`). The `list_tools` response describes itself
  as "~2KB vs ~15KB" (`options.lite_mode`) and "~10 tools" for
  `essential_only` (`note`); measured, the lite view is 21,357 bytes, the full
  view 45,837 bytes, and `essential_only` returns 13 tools.

### F9 — a served description frames usage counts as removal authority (low)

The text served for `get_tool_usage_stats` (`src/tool_descriptions.json`, and
the `list_tools` override at `tool_catalog.py:255`) says the tool identifies
"which tools are actually used vs unused" and "helps make data-driven decisions
about tool deprecation". That is the framing the measurement-authority rule in
`CLAUDE.md` rejects: a usage count may retire an instrument, never a
capability. The `admin` router's own wire description does not carry it; the
dispatch-only name's `describe_tool` text does.

### F10 — two consumers classify tools by `handler.__module__` (info)

`scripts/diagnostics/count_tools.py:84` and `tests/test_stakes_table.py:55,167`
read `handler.__module__`. Every `action_router` handler reports
`src.mcp_handlers.decorators`, so the CI breakdown shows `decorators: 8` and
the test needs a hard-coded `_EXTERNAL_PLUGIN_TOOLS` set to keep a plugin
router from being counted as core. `ToolDefinition.source_module` exists for
exactly this distinction.

### F11 — seven read-shaped tools mint an identity on an unbound call (info, decision point)

`dashboard`, `get_thresholds`, `get_trajectory_status`,
`get_workspace_health`, `list_process_bindings`, `outcome_correlation` and
`verify_trajectory_identity` are `requires_identity="required"` with no
pre-onboard exemption, so the middleware resolves an unbound call with
`force_new=True` (`src/mcp_handlers/middleware/identity_step.py:1069`) and
persists an agent row before the handler runs. `src/tool_annotations.py:29-49`
documents this and sets `readOnlyHint=False` for that reason alone. A
first-contact client that browses thresholds before `start_session` leaves an
agent behind. Whether that is acceptable is a policy choice. The datum that
would inform it — how many agent rows were minted by calls to these seven
names — is answerable from `audit.tool_usage` and was not available here.

## Telemetry: what a client pays at connect

Reported as a measurement with no removal authority attached.

- `tools/list` is 133,061 bytes of compact JSON (about 33k tokens at the
  4-bytes-per-token heuristic) on every transport and under every legacy mode
  label; since interface 1.6.0 there is no narrower profile.
- Parameter schemas are 79% of those bytes, descriptions 21%. `knowledge` alone
  is 10,976 bytes with 51 parameters; the eight routers and the four alias
  views over them are 36% of the total.
- All 50 advertised first lines exceed the 140-character routing line proposed
  in `docs/proposals/tool-surface-legibility-v0.md` (median 523 characters).

What this does not establish: anything about which tools are valued, and
nothing about whether the size changes what an agent calls. It is the fixed
cost, and the decision points that could move it already belong to the
legibility proposal.

## What this audit did not do

No live transport was probed (`scripts/ci/check_mcp_tool_surface.py` needs the
Compose server), no database or Redis was reached, no usage data was read, and
no deployed peer was compared against the repository. The identity statements
in F5 and F11 are read from code paths, not exercised end to end.

## Validation receipt

- `scripts/dev/tool_edge_index.py --check`, `--lint`, `--json`: up to date; 0/0/0; schema-valid.
- `validate_tool_modes.py`, `validate_tool_registration.py`, `count_tools.py --json`, `update_docs_tool_count.py --check --require-registry`, `audit_tool_categories.py`, `hint_target_advertisement.py --fail-on-finding`, `check_doc_health.py --strict`: all pass.
- `python -m src.interface_contract` diffed against `docs/interface-contract.v1.json`: identical.
- In-process `describe_tool` × 167 and `list_tools` × 6: no failures.
- Focused test files (`tests/test_tool_*.py`, `test_lite_wire_surface.py`,
  `test_describe_tool_drift.py`, `test_stakes_table.py`,
  `test_action_level_identity.py`, `test_interface_contract.py`,
  `test_alias_schema_narrowing.py`, `test_hint_target_advertisement.py`,
  `test_list_tools_workflow_aliases.py`, `test_tool_registry_bookkeeping.py`,
  `test_residentless_install.py`, `test_schema_brief.py`,
  `test_onboard_pin.py`): 661 passed, 1 skipped in 75s. The full
  `test-cache.sh` gate was not run for a documentation-only change.

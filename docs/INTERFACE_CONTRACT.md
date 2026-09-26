# UNITARES public interface contract

**Current contract:** `unitares.interface-contract.v1`, version `1.18.0`

UNITARES is MCP-native, but the integration boundary is a set of capabilities,
not one transport. Every transport negotiates the same complete catalog, while
the initial schema advertisement is progressive by default:

- Streamable HTTP MCP at `/mcp/`
- REST discovery at `GET /v1/tools`
- local stdio discovery

The checked-in [`interface-contract.v1.json`](interface-contract.v1.json) is the
machine-readable complete contract. A live client negotiates the same contract by
calling `list_tools(lite=true)` and reading `interface_contract`; no repository
tag lookup or private server import is required. Its `surface_sha256` changes
whenever the ordered capability records change. CI compares that artifact with
the live catalog. Each `input_schema_sha256` is taken over the catalog schema,
and that schema is what every transport advertises byte for byte. FastMCP
derives a schema of its own from each tool's typed wrapper, and that derivation
drops bounds, concrete defaults and `$defs` (finding F12 of the 2026-09-12
tool-surface audit: before 2026-09-11 a `/mcp/` client saw 106 defaults as
`null` and no bound on `delegate_inference.timeout_s`), so the `/mcp/`
registrar replaces it with the catalog schema after registration
(`src/tool_registration.py`, `_advertise_catalog_schema`). Dispatch validation
is unchanged: the wrapper's argument model still decides what the transport
accepts, and the handler's Pydantic model enforces the advertised bounds.
`tests/test_mcp_schema_parity.py` diffs the mounted listing against the
catalog per tool and per property. The generated-title policy is the one step
still applied per listing, so that it stays reversible.
`scripts/diagnostics/tool_surface_cost.py --surface mcp` measures the final
local MCP listing and `--surface catalog` the source layer; the two agree, and
a gap between them is a finding rather than an expected difference. Neither
command is a probe of a deployed peer.

## What v1 guarantees

Each capability record gives one public name, its canonical implementation,
its kind, and its name on each transport. A `workflow_alias` is a
first-class public spelling such as `start_session` or `sync_state`; a
`canonical_tool` is the underlying registered tool. Both are dispatched by the
same server authority.

A contract capability is:

1. present in the complete `list_tools(lite=true)` negotiation index;
2. accepted by the common dispatcher, directly when advertised or through
   `use_tool`; and
3. described from the same source input schema through `describe_tool` and,
   under full advertisement, every transport listing.

The `federation.lifecycle` section also names the product-facing lifecycle
capabilities and their normalized success/failure envelope. It guarantees the
small stable seam a separate userland needs: successful lifecycle calls carry
`success`, `tool`, and `next_action`; optional state, risk, recovery, memory,
and raw-governance fields may be omitted when they have nothing to say. It does
not freeze every diagnostic field returned by Core.

The contract deliberately does **not** imply that a
client host installs lifecycle hooks, forwards edit or stop events, schedules
check-ins, or honors a returned policy action outside UNITARES-governed writes.
Those are host-integration capabilities, documented separately in the
[client capability matrix](integration/CLIENT_CAPABILITY_MATRIX.md).

## Core workflow and advanced capabilities

The [capability guide](CAPABILITIES_AND_DEPLOYMENT.md) presents identity,
claims/evidence, review, outcomes, and reconstruction as the core reading path.
These are conceptual groupings of existing operations, not new tool names,
permission levels, installation modes, or changes to this versioned contract.
The existing `essential`, `common`, and `advanced` discovery tiers remain
unchanged. A listed tool is dispatchable, but successful execution still
depends on storage, identity/authorization, and any configured inference or
reviewer services. Tool counts alone establish none of those conditions.

## Complete catalog, progressive advertisement, and compatibility

Since interface release 1.13.0 the contract distinguishes capability identity
from initial advertisement. Every registered-and-mounted public tool, including
primary workflow aliases, remains in the complete catalog. By default MCP,
REST, and stdio advertise a small workflow surface plus `list_tools`,
`describe_tool`, and `use_tool`; schema-driven clients discover an omitted name,
inspect it, then invoke it through `use_tool`. The gateway runs the target's
normal identity, validation, authorization, timeout, response, and
telemetry paths. `UNITARES_TOOL_ADVERTISEMENT=full` advertises every schema up
front. Legacy `GOVERNANCE_TOOL_MODE` values are ignored. Existing action
authorization and identity gates remain in force.

The retained `mode` contract field always reports `full`, because it describes
the complete capability set. All advertisement modes produce the same surface
hash. The separate `advertisement` block declares the default, the full-mode
environment switch, and the progressive entrypoints. `list_tools(lite=true)`
is the compact name index of the complete catalog: each capability appears once as
`{"name": "..."}` beside the interface summary, without descriptions,
categories, signatures, workflows, or relationship copies. `lite` controls
response detail, not capability availability. Use `lite=false` to browse rich
metadata; category and tier filters narrow either view, and `describe_tool`
provides one capability's parameters on demand.

Raw implementation names remain discoverable and callable so existing clients
can upgrade independently. Prefer primary workflow names for normalized
lifecycle responses. Specialized raw operations remain first-class where their
parameters or behavior differ. Consolidated routers expose their action
parameters through `describe_tool(tool_name=..., action=...)`.

Adding a compatible capability increments the contract version. Renaming,
removing, or changing the meaning of an existing capability requires a new
major schema contract or an explicit deprecation window. Raw implementation
names may remain callable for compatibility even when the preferred workflow
alias is the documented integration name.

The two identifiers serve different jobs:

- `unitares.interface-contract.v1` is the schema family. Its `v1` changes only
  for a breaking change to the contract document's shape.
- `version: 1.18.0` is the negotiated interface release. Compatible additions
  advance it without forcing clients to learn a new schema family (1.2.0,
  2026-09-07: `observe` and `describe_tool` declare parameters their handlers
  already read; 1.3.0, 2026-09-08: `describe_tool` takes `action` and answers
  for one action of a consolidated router, and `dialectic` drops the `vote`
  parameter, which named an action the router does not route and which no
  handler read; 1.4.0, 2026-09-08: parameter *descriptions* are advertised
  abridged to their first sentence, with the full text served by
  `describe_tool`; 1.5.0: `search_shared_memory` drops 14 unused parameters
  belonging to other actions, while search's type, severity and provenance
  options remain and appear in action-specific discovery; 1.6.0: one complete
  catalog on every transport, as the section above describes; 1.7.0,
  2026-09-12: `cirs_protocol`'s `action` description names which sub-action
  each protocol routes, adding the `list` and `status` it had omitted.
  Description-only: one digest moves and no parameter name, type, default or
  requiredness changes; 1.8.0, 2026-09-13: `cirs_protocol` declares the 24
  parameters its selectable protocol handlers read, which the MCP argument
  model had dropped before dispatch, and `limit` advertises the handlers'
  default of 50 in place of null, which the middleware had delivered to
  `int()` and so failed every query action through dispatch. No advertised
  parameter is removed or renamed; the one retyped, `limit`, stops admitting a
  null that never worked; and again only that digest moves.
  REST and in-process callers that sent these previously undeclared keys with
  the wrong type or an explicit null now get a validation error, and string
  booleans are parsed rather than read as truthy, as 1.2.0 did for
  `observe`'s `include_calibration`; 1.9.0, 2026-09-13: `complexity` and
  `confidence` on `process_agent_update`, `simulate_update` and `sync_state`
  advertise what the server actually accepts. Their 0-1 bound had been emitted
  as Pydantic `ge`/`le`, which no client validator reads, so 99 was advertised
  as legal; it is now `minimum`/`maximum`. Their string branch had allowed any
  string; it is now a regex of numeric strings in [0, 1]. `sync_state`'s
  `complexity` also lists the named levels its normalizer accepts. Only the
  advertised schema changes: acceptance, refusal and every validation error,
  down to its location, are unchanged on every transport; three digests move;
  1.11.0, 2026-09-13: `knowledge` and `search_shared_memory` declare
  `agent_id_filter`, the search handler's preferred author filter. It takes
  precedence over the retained `agent_id` filter and is disclosed by
  `describe_tool(tool_name="knowledge", action="search")`.
  `search_knowledge_graph` also records its clarified filter description, so
  three input digests and the surface digest move. This release follows 1.10.0's
  `list_tools` wire correction; 1.12.0, 2026-09-19: `dialectic` declares
  `judgment_formed` on `antithesis` and `synthesis`. Unlike 1.10.0 and 1.11.0,
  which advertised parameters the handler already read, this is new behavior: a
  reviewer that could not form a judgment passes `judgment_formed: false`, and
  the server records an abstention without claiming or changing reviewer-slot
  ownership rather than filing a binding rejection with no reasoning behind it.
  The slot is open only when no reviewer was already assigned. The default is true,
  so omitting it is the prior behavior exactly and no existing caller changes;
  nothing is removed or renamed, and one input digest and the surface digest
  move; 1.13.0, 2026-09-20: `use_tool` is added and capability negotiation is
  separated from the default progressive transport advertisement. The complete
  catalog and its hashes remain transport-neutral, while operators can restore
  the up-front full listing with `UNITARES_TOOL_ADVERTISEMENT=full`; 1.14.0,
  2026-09-24: `agent` gains `action="release_presence"`, which releases the
  caller's own presence lease at a clean exit so a successor can declare it as
  parent right away. Nothing is removed or renamed; `agent`'s input digest and
  the surface digest move; 1.15.0, 2026-09-24: `get_governance_metrics` and
  `check_working_state` declare `verbosity`, which the handler already read;
  undeclared, `/mcp/` dropped it, so the `standard` tier was unreachable there.
  `verbosity` overrides `lite` when set. Existing inputs that behave
  differently: validated routes (`/mcp/` and REST `check_working_state`) refuse
  an off-list `verbosity` such as `"Standard"` that they used to ignore; an
  explicit `lite: null` gets the default (`minimal`, previously `full`); and on
  REST `get_governance_metrics`, the one unvalidated route, a string the schema
  reads as false, such as `"no"` or `"0"`, gets `full` (previously `minimal`),
  as every validated route already did. Both input digests and the surface
  digest move; 1.16.0, 2026-09-25: the `skills` `name` description says a bare
  call returns an index without content, which is what the handler now
  returns; no parameter changes, and `skills`' input digest and the surface
  digest move;
  1.17.0, 2026-09-25, numbered after 1.16.0 (#2435): the
  progressive `tools/list` shrinks from 44,199 to 40,455 bytes.
  `search_shared_memory` stops advertising six fields only other `knowledge`
  actions read (`offset`, `epoch_scope`, `scope`, `evidence_ids`,
  `verification_basis`, `decision_standard`); search never read them, and
  `knowledge` keeps all six. The EISV field contract rides once on the
  advertised surface, on `check_working_state`; `sync_state` and
  `record_result` point at `describe_tool(tool_name='check_working_state')`,
  and `describe_tool`'s full view of either still appends the contract.
  Identity briefs, several alias parameter texts and six leaked model
  docstrings (`inputSchema.description`) are shortened or corrected. Nothing
  callable is removed or renamed; many input digests and the surface digest
  move; 1.18.0, 2026-09-26: `onboard`'s `resume` description, and so the
  `start_session` alias's, no longer names `continuity_token`, `agent_id` and
  `name` as resume signals. A name is never looked up, and a token without
  `force_new` is refused (S1-c); the text now points at
  `docs/ontology/identity.md` for the resolution rules. No parameter changes;
  `onboard`'s and `start_session`'s input digests and the surface digest
  move).

Every `input_schema_sha256` moved in 1.4.0 without a single parameter name,
type, default or requiredness changing: descriptions live inside the hashed
schema. In 1.5.0, clients pinning hashes should re-pin against the current
catalog. `UNITARES_TOOL_SCHEMA_FIELD_DESCRIPTIONS=full` restores authored
descriptions; `UNITARES_TOOL_SCHEMA_PROPERTY_TITLES=keep` restores generated
titles. Neither switch restores parameters removed in later releases or
guarantees an older digest. The default title policy applies on each MCP
listing as well as to the catalog. Titles are annotations, so that part
preserves validation; schema fingerprints still change.

The 14 fields removed from `search_shared_memory` are `closure_class`,
`closure_evidence`, `confidence`, `dry_run`, `include_response_chain`,
`including_cold`, `length`, `max_chain_depth`, `memory_context`, `min_members`,
`top_n`, `topic`, `use_llm`, and `use_model`. They belong to other knowledge
actions and had no effect on search. Calls using those fields should use the
corresponding action of `knowledge`; clients should stop sending them on the
search alias. The router retains them. Search's `discovery_type`, `severity`,
and `include_provenance` are deliberately retained after checking the parser,
not inferred absent from the older `ACTION_FIELDS` map.

The MCP argument model silently discards undeclared fields on this alias;
removal does not promise a validation error. Removing a field that search
actually reads would therefore silently change results. Tests cover both
retained filters and discarded controls. Clients generating bindings from
the advertised schema should regenerate them for this release.

A consolidated router advertises the union of every action's parameters,
because the wire schema must be flat: the MCP wrapper builds a tool's argument
model from top-level properties, so a per-action `oneOf` would not survive
registration. That union is the contract. Which of those parameters each
action uses is declared alongside them, as `ACTION_FIELDS` on the router's
parameter model, and `describe_tool(tool_name=..., action=...)` serves it. The
narrowed schema is a description of one call, not a second contract: the wire
still accepts and ignores the parameters of other actions.

Core currently supports `mcp>=1.26.0,<3.0.0`, but CI exercises only the 2.x
major: every lane but `mcp-newest` installs under `constraints.txt`, whose
`mcp` pin is a 2.x release, and the blocking `mcp-newest` lane resolves the
newest in-range version, which is 2.x as well. A client
should negotiate the UNITARES contract above rather than infer compatibility
from its locally installed MCP package version.

Regenerate the artifact from the repository root with:

```bash
python3 -m src.interface_contract > docs/interface-contract.v1.json
```

Then run `tests/test_interface_contract.py` and the normal repository test gate.

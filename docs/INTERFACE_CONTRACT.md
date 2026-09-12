# UNITARES public interface contract

**Current contract:** `unitares.interface-contract.v1`, version `1.6.0`

UNITARES is MCP-native, but the integration boundary is a set of capabilities,
not one transport. The server advertises the same
callable names and source input schemas through:

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

A listed capability is:

1. advertised by each local public discovery surface;
2. accepted by the common dispatcher; and
3. described by the same source input schema before transport-specific
   serialization.

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

## One catalog and compatibility

Interface release 1.6.0 advertises every registered-and-mounted public tool,
including primary workflow aliases, on every transport. A definition registered
after server mounting is omitted rather than advertised without a dispatch path.
No mode selection is required. Legacy
`GOVERNANCE_TOOL_MODE` settings and REST `mode` query parameters are accepted
but ignored, including the former operator profiles. They were discovery
filters, never authorization boundaries. Existing action authorization and
identity gates remain in force.

The retained `mode` contract field always reports `full`. All legacy mode
values produce the same surface hash. `list_tools(lite=true)` is the compact
view of this complete catalog; `lite` controls response detail, not capability
availability. Category and tier filters are optional browsing aids.

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
- `version: 1.6.0` is the negotiated interface release. Compatible additions
  advance it without forcing clients to learn a new schema family (1.2.0,
  2026-09-07: `observe` and `describe_tool` declare parameters their handlers
  already read; 1.3.0, 2026-09-08: `describe_tool` takes `action` and answers
  for one action of a consolidated router, and `dialectic` drops the `vote`
  parameter, which named an action the router does not route and which no
  handler read; 1.4.0, 2026-09-08: parameter *descriptions* are advertised
  abridged to their first sentence, with the full text served by
  `describe_tool`; 1.5.0: `search_shared_memory` drops 14 unused parameters
  belonging to other actions, while search's type, severity and provenance
  options remain and appear in action-specific discovery).

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

Core currently supports `mcp>=1.26.0,<3.0.0`. Both admitted major versions are
tested, and the newest in-range resolution is a blocking CI lane. A client
should negotiate the UNITARES contract above rather than infer compatibility
from its locally installed MCP package version.

Regenerate the artifact from the repository root with:

```bash
python3 -m src.interface_contract > docs/interface-contract.v1.json
```

Then run `tests/test_interface_contract.py` and the normal repository test gate.

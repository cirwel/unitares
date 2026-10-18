# UNITARES public interface contract

**Current contract:** `unitares.interface-contract.v1`, version `1.52.0`

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
authorization and identity gates remain in force. Both settings are operator
configuration, so the MCP `instructions` string that agents receive at
initialize does not name them; this section is where they are documented. That
string stays within 1,400 UTF-8 bytes in either mode because clients truncate
it. It is not part of the capability contract: neither `surface_sha256` nor
any `input_schema_sha256` covers it, so rewording it moves no contract digest.

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
- `version` (the current value is in the header above) is the negotiated
  interface release. Compatible additions
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
  move; 1.18.0, 2026-09-26: `delegate_inference.host_id` accepts a third
  value, `antigravity:host-adapter`; the default stays `claude:host-adapter`,
  nothing is removed or renamed, and the input digests of `delegate_inference`
  and `list_inference_hosts` and the surface digest move;
  1.19.0, 2026-09-26, numbered after 1.18.0 (#2470): parameter
  descriptions only. `include_state` on
  `get_governance_metrics` and `check_working_state` says it is retained for
  compatibility and has no effect (the handler no longer requests the nested
  state, which surfaced only when state interpretation failed); `verbosity`
  says what `standard` holds (bare EISV, coherence and risk; the verdict, basin
  and mode with their meanings; guidance) instead of EISV meanings it never
  carried; and the `response_mode` description of `process_agent_update`
  and `sync_state` names the degraded-identity and warnings triggers that make
  `auto` resolve to mirror (its advertised brief is unchanged). Nothing is
  added, removed, retyped or renamed; the two metrics input digests and the
  surface digest move;
  1.20.0, 2026-09-26, numbered after 1.19.0 (#2472): `update_finding` declares
  `closure_class` and `closure_evidence`, with the `knowledge` router's
  descriptions. The server now stores both, and migration 071 lets a
  classified finding move to `archived` and `cold`; until now `/mcp/` dropped
  them from an `update_finding` call, and only `knowledge(action='update')`
  carried them to the handler. Descriptions that lagged their handlers are
  corrected: `knowledge`'s `discovery_type` says store defaults it to `note`
  (it said "Required"), promote to `insight`, update retypes and search
  filters, and on `update_finding` it says an omitted type keeps the stored
  one; `closure_class` says a class may also be set alone on a finding that is
  already closed, and `closure_evidence` states its 8 KiB bound; `observe`'s
  `target_agent_id` names the `audit_events` and `outcome_evidence` filters
  and says they match the stored agent_id exactly without resolving labels (a
  UUID for most agents; some audit writers are stored by name), `until` names
  `outcome_evidence`, and `agent_ids` names the `anomalies` and `aggregate`
  scope; `dialectic`'s `issue_description` names `action=quick`. Nothing is
  removed, retyped or renamed; the input digests of
  `update_finding`, `knowledge`, `observe`, `dialectic` and
  `request_review`, and the surface digest, move;
  1.21.0, 2026-09-26, numbered after 1.20.0
  (#2489): tool descriptions only. `check_working_state` and
  `get_governance_metrics` no longer promise that a binding the server merely
  inferred returns that agent's real state marked
  `identity_assurance.caller_proven=false`: no pre-onboard self-read binds to
  one any more (the sticky transport binding is not consulted for a read, and
  an `agent_uuid` argument or a UUID `X-Agent-Id` header is not read proof),
  so such a read is unbound, and both texts name the proof that reads your own
  state (your `client_session_id`, an `X-Session-ID` header, or a verified
  `continuity_token`). `get_governance_metrics` no longer offers an explicit
  `agent_id` as a way to be sure the reading is yours: `/mcp/` does not
  declare `agent_id` and drops it, and it proves nothing about the caller.
  Both texts say where it names the agent to read: `check_working_state`
  through `use_tool` or REST, and `get_governance_metrics` through `/mcp/`'s
  `use_tool`, unless the caller is bound as a different agent, which is
  refused (`identity_mismatch`); `get_governance_metrics` over REST answers it
  for any caller. `check_working_state` marks such a reading
  `caller_proven=false` on an inferred session. `identity`'s description
  names `client_session_id` in the call that sets a display name. No
  parameter is added, removed, retyped or renamed, and no input digest or the
  surface digest moves;
  1.22.0, 2026-09-27, numbered after 1.21.0 (#2490):
  `knowledge` and `search_shared_memory` declare `sort_by` (`relevance` or
  `created_at`), `created_after` and `created_before` for search, which
  `search_knowledge_graph` declared without any handler reading them; its
  `sort_by` default moves from `created_at` to unset (read as `relevance`), and its
  never-implemented `score` and `related_count` values are refused. The
  default order is unchanged. `search_shared_memory` advertises shorter briefs
  for nine existing search parameters to stay inside the progressive-surface
  ratchet; their full descriptions are unchanged. The three input digests and
  the surface digest move;
  1.23.0, 2026-09-27, numbered after 1.22.0 (#2517): `agent(action='archive')`
  and `agent(action='delete')` act only on the agent named in `agent_id`.
  Behavior that changes for existing inputs: a call with no `agent_id` (or a
  blank one) is refused with `TARGET_AGENT_REQUIRED` where it used to act on
  the caller's own agent, because dispatch wrote the session's id in; a UUID
  no agent holds is refused with `TARGET_AGENT_NOT_FOUND` where it used to
  fall back to the caller; and an `agent_id` that is not a UUID (a label,
  public id or legacy non-UUID key) is refused with
  `TARGET_AGENT_UUID_REQUIRED` where it used to select the first cached holder
  (public ids are shared by most identities that carry one). The legacy
  `archive_agent` and `delete_agent` names follow; `get`, `update`, `resume`
  and `release_presence` are unchanged. `agent`'s `agent_id` description says
  it is the target's UUID, required for archive and delete, and names who get,
  resume and update default to, and `describe_tool`'s lite view of archive and
  delete lists `agent_id` (and delete's `confirm`) as required at call time.
  No parameter is added, removed, retyped or renamed; `agent`'s input digest
  and the surface digest move;
  1.24.0, 2026-09-28, numbered after 1.23.0 (#2532): `dialectic` gains `action="consult"`, which files an outside
  verdict on any session as a transcript record with no authority. It needs
  no reviewer slot, never advances a phase, never counts as a verdict, and
  never refreshes the session's liveness clock. The route documented for an
  outside consult (an antithesis with `reviewer_kind='external_consult'`)
  required the reviewer slot, which the orchestrated reviewer takes within
  about a minute. Nothing is removed, retyped or renamed; the input digests
  of `dialectic` and `request_review`, and the surface digest, move;
  1.25.0, 2026-09-28, numbered after 1.24.0 (#2564): `knowledge` and
  `search_shared_memory` declare `recency_half_life_days` for search, an
  opt-in recency weight that is off by default; two advertised briefs are
  shortened to stay inside the progressive-surface ratchet. The `knowledge`
  and `search_shared_memory` input digests and the surface digest move;
  1.26.0, 2026-09-28, numbered after 1.25.0 (#2564): parameter descriptions
  only. `call_model`'s `provider` says `ollama` is the configured local
  endpoint (an OpenAI-compatible server, Ollama by default) and `hf` the
  Hugging Face router, and its `model` names `UNITARES_MODEL_ID`, because
  the local endpoint setting can now name a server other than Ollama. No value is added, removed or renamed, and the
  default is unchanged; `call_model`'s input digest and the surface digest
  move;
  1.27.0, 2026-10-03: parameter descriptions only. `knowledge` says a supplied
  `details` replaces the stored details; `search_shared_memory`'s `agent_id`,
  `status` and `query` describe search filters; `sync_state`'s `complexity`
  no longer advertises a `{value, scale}` object the MCP schema cannot carry;
  `delegate_inference`'s `timeout_s` notes client timeouts. The input digests
  of `search_shared_memory` and `delegate_inference` and the surface digest
  move;
  1.28.0, 2026-10-05: authorization only. `agent`'s `archive`, `resume` and
  `delete` act on the caller's own agent, or on any agent when the request
  carries a valid `X-Unitares-Operator` token; a bound session or bearer API
  key alone now gets `LIFECYCLE_NOT_OWNER_OR_OPERATOR` where it used to
  succeed. `operator_resume_agent` keys on the same token; a self-claimed
  `operator` label or tag no longer counts, and `operator` is a server-granted
  tag. Nothing is added, removed, retyped or renamed, and no digest moves: the
  version alone records the behavior change;
  1.29.0, 2026-10-06: output only. `list_process_bindings` rows carry
  `client_session_ref`, an opaque reference keyed per server process, in
  place of the `client_session_id` each process onboarded with, and
  `admin(action='debug_context')` shows session-cache keys as references
  and the uuid prefix index only as a count. A `client_session_id` is a bearer
  credential, and both surfaces could name other agents' sessions. No input
  schema changes, so no digest moves;
  1.30.0, 2026-10-06: authorization of outputs. Under strict identity,
  `identity()` returns `client_session_id: null` and no `continuity_token`,
  with `credentials_withheld: {basis, hint}`, when the call was matched to
  the agent only by inference (an onboard pin or transport fingerprint), and
  `onboard()` refuses such a resume with `status: resume_proof_required`. A
  mint, a session the caller sent, a continuity token or UDS attestation for
  the agent, or an operator token still receives them. No input schema
  changes, so no digest moves;
  1.31.0, 2026-10-06: the stable `client_session_id` that `onboard` and
  `identity` return is `agent-{uuid12}-{tag}`, keyed with the server's
  continuity key, in place of `agent-{uuid12}`. Callers that store and echo
  it are unaffected. A legacy `agent-{uuid12}` id is refused by default
  (`UNITARES_LEGACY_SESSION_IDS` = `refuse` | `log` | `accept`) with
  `stable_session_id_rejected`, and a verified continuity token naming the
  agent converts to the keyed id. No input schema changes, so no digest
  moves;
  1.32.0, 2026-10-11: `observe(action="agent")`, output and target
  resolution. `target_agent_id` resolves the identifier
  `agent(action="list")` prints for an agent whose UUID it withholds
  (`{public_agent_id}_{uuid8}`, or the `agent-redacted` digest); those
  answered not-found before. A caller that is neither the operator nor the
  observed agent receives that identifier in `agent_id`, with
  `uuid_redacted: true`, where it used to receive the UUID. Resolution order
  is UUID, server-composed listed identifier, then alias; an alias names its
  one live holder ahead of archived or deleted ones, and a target naming more
  than one agent is refused with `AMBIGUOUS_TARGET` where the first matching
  registry row used to be returned. Only the long description of `target_agent_id`
  changes, which is outside every input digest, so no digest moves;
  1.33.0, 2026-10-13: `search_shared_memory`, output only. A `lean` or
  `compact` search called with `limit` above 3 lists the results after the
  three digests under `more_results`, one row each (`discovery_id`, `type`,
  `status`, a summary bounded at 100 characters), up to 17 rows inside a
  2,000-byte budget of their own, and reports their number as
  `state_summary.more_results_shown`. A search with no `limit`, or a `limit`
  of 3 or less, returns what it did before, and the `full` tier is
  unchanged. Only the long description of `limit` changes, which is outside
  every input digest, so no digest moves).

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

For an output or description change, prepare the release files in one command
from the repository root (choose the next version deliberately):

```bash
python3 scripts/dev/prepare_interface_release.py --version 1.34.0 \
  --slug describe-output --summary "Describe the observable change."
```

Use `--dry-run` to preview destinations and skills citing affected files. The
command bumps the source version with a dated note, regenerates the artifact,
adds a release-entry stub here and writes a changelog fragment. It refuses an
existing fragment or a nonadvancing version and restores the original files if
generation fails. Finish the compatibility/digest/client-action prose, re-check
the listed skills, then explicitly stamp them with a named verifier. It never
certifies skills on your behalf. Commit all release files with the actual fix.

To regenerate only the artifact from the repository root:

```bash
python3 -m src.interface_contract > docs/interface-contract.v1.json
```

Then run `tests/test_interface_contract.py` and the normal repository test gate.

## 1.34.0 (2026-10-13)

Imported memory search/get previews are bounded to 2 KiB UTF-8 and disclose unreviewed status and creation age.

Search and agent-scoped get retain full stored bodies but cap imported inline
`details` at 2,048 UTF-8 bytes. Oversized rows report `details_truncated`, original
`details_length` (characters), `details_bytes`, `has_more_details`, and a
`details_retrieval` argument object for `knowledge(action="details")`. Its explicit
paged read is unchanged. Native findings and governed promotions are unchanged.

Imported rows carry `imported_memory` with `trust="unreviewed"`, `age_days`
(or null for an unreadable timestamp) and `age_basis="created_at"`. This age is
KG row age, not source-file age or time since verification. Routine sync/status
updates do not reset it. Digest and additional-result rows retain the marker
inside their existing byte budgets, so fewer rows may fit. No ranking or
retention policy changes.

The knowledge include_details description changes the knowledge schema digest;
search_shared_memory retains its brief description and digest. Clients should
follow `details_retrieval` to read long imported evidence instead of assuming
inline content is complete. No arguments are removed or retyped.

## 1.35.0 (2026-10-13)

Add bounded replayable knowledge notices for proven active sessions

`knowledge(action="notices")` adds optional `acknowledge` (at most three
revision UUIDs), `acknowledge_gap` (one gap UUID) and `muted` (boolean).
The knowledge schema digest changes; no existing argument is removed.
The action requires a caller-proven process binding and never accepts another
recipient from an argument. Reads replay; exact acknowledgements cannot erase
newer revisions or another process's pending notice.

Successful active tool responses may carry `knowledge_changes`: at most three
coalesced row pointers under a separate 2,048-byte UTF-8 budget. Existing base
response budgets still apply before this optional field. Each pointer names a
row, revision, kind, changed fields and details arguments. If `_response_size`
exists, its measurement includes this field. No background/model work is started.
Typed refusals and errors pass through without notices.

Recipients are the row owner (on updates) and exact `notify-agent-<UUID>` tags.
A removed mention revokes its pending pointer. Process identity, not a display
name or predecessor, owns an inbox. The delivery backlog keeps at most 100
rows for seven days; lazy trimming reports a stable `gap_token`. Intermediate
changes to one row deliberately coalesce. Read details before acting, acknowledge
only received revisions, and rescan before acknowledging a gap. Muting suppresses
automatic display without granting authority or changing KG status/content.

Migration 074 couples pointers to canonical-table commits for both KG backends.
No historical events are backfilled. Writer attribution is not inferred from
ownership: this version exposes no actor field because system mutation paths do
not consistently carry a proven writer. The current shared KG permits unbound
row reads; delivery still joins the current row and checks current recipient
membership. A future row-ACL feature must extend both details and notice checks.
No credentials, permission checks or scientific instruments are changed.

## 1.35.1 (2026-10-13)

`knowledge(action="notices")` now returns one MCP text-content item containing
the JSON notice response. Previously its success path nested the content list,
causing the gateway to stringify the Python representation into `result` instead
of exposing `notices`, `muted`, `has_more`, and acknowledgement guidance.

This corrects the 1.35.0 output contract without changing inputs, permissions,
or notice delivery. Schema digests do not move. Clients can read the documented
JSON fields directly after the server upgrade; no request changes are needed.

## 1.36.0 (2026-10-13)

`consult` accepts optional `host_id` for the five existing advisory hosts.
HTTP hosts (`ollama:local`, `hf:router`) require standard effort; the three CLI
adapters require thorough effort. External hosts require `cloud_allowed`.
Pinned calls reject `allow_degraded=true` and never try another peer or local
fallback. The compact request receipt and text-free audit include the selected
host; existing full diagnostics retain the actual route. Selecting the caller's
own family is permitted for advice and never grants governed-review authority.

Discovery lists `consult` in `accepts_host_id_from` for all five hosts. Raw tool
contracts and default recommendations remain compatible. `consult`'s input
schema digest and the aggregate surface hash change; callers omitting `host_id`
retain automatic routing and need no request changes. Refresh cached schemas
to use explicit selection.

Full consultation failures retain a bounded, unvalidated adapter-output excerpt
for the caller, excluded from audit storage. HF HTTP 401/402/429 failures now
feed the existing bounded auth/quota cooldown; discovery reports the observed
unavailability, without a model or billing probe. These output/description fixes
change no raw-tool input schemas.

## 1.37.0 (2026-10-14)

Infer omitted consult effort from a selected host and explain aggregate token usage in full diagnostics.

For `consult`, omitted `effort` becomes thorough for an explicitly selected CLI
host, otherwise standard. An explicit value is never overwritten; incompatible
host/effort pairs still fail before dispatch. Privacy, pinning, failover and
advisory authority are unchanged. The compact request receipt and audit show
the resolved effort.

The `effort` input retains its string enum and optionality but no longer
advertises an unconditional standard default. Descriptions state the
conditional default. Only the `consult` input digest and aggregate surface hash
move; refresh cached schemas and omit effort to use inference. Existing calls
with a matching explicit effort, and automatic calls with no host, keep their
behavior. Null remains outside the advertised effort schema.

Full consultation diagnostics add `tokens_used_note` when a token aggregate is
present. It explains provider-specific counting and zero-as-unreported behavior.
Counts, pricing and EISV accounting are unchanged. Compact responses and the
durable audit do not include this explanatory text. Full failure diagnostics
also retain the backend message, bounded to 500 characters and excluded from
compact responses and audit storage; the facade summary and failure semantics
remain unchanged.

## 1.38.0 (2026-10-14)

KG search, get and details reads expose character offsets into appended resolution notes without changing stored claims or ranking.

Output-only compatible addition to knowledge search, agent-scoped get and
details. A record with recognizable dated `Resolution notes (...)` blocks in its
full details carries `resolution_notes={source: "details_text", count,
first_offset, latest_offset}`. Offsets count Unicode characters, matching the
existing details pagination, and refer to the full stored text before any
preview or imported-content truncation. Records without such blocks are unchanged.
The metadata survives canonical lean search, the friendly search digests and
`more_results`; a response-level `resolution_notes_hint` explains the read route.
The existing 3,000-byte digest and 2,000-byte extra-row budgets still apply.

Clients may call `knowledge(action="details", discovery_id=..., offset=...)`
at `latest_offset` to read the last block or `first_offset` to inspect the history.
The details read returns fresh locations: replacing text can invalidate an earlier
offset. The locations are parsed from text, not authenticated update receipts;
the last note may be administrative, and no claim of truth, current policy or
note-author identity is implied. No excerpts are substituted for original claims.
Stored content, identity, authority, ranking, input schemas and schema digests
are unchanged. Existing clients may ignore the added fields; no schema refresh
is needed to keep using existing inputs.

## 1.39.0 (2026-10-14)

The default/explicit `lean` response from `search_shared_memory` omits the optional
`response_options` tier ladder and repeated write recipes in `next_action`.
`discovery_retrieval_options` keeps `current_tier`, `open_one`, and, when present,
`requested_tier` and `details_omitted_by`. Serialization diagnostics and all-inline
coaching remain in `compact`; `full` retains the canonical response. Confidence,
degradation and imported-memory caveats, lifecycle markers, correction pointers,
and attribution keep their existing semantics and budgets.

Under byte pressure, the full-mode hint may shrink to its executable call while
preserving the expansion route. This also reserves room for the existing
attribution-omitted marker after lean coaching is removed. Result summaries,
ranking, storage and write behavior are unchanged. The digest and extra-row
budgets remain 3 KB and 2 KB.

This is output shaping within fields already optional under response budgeting;
input schemas and schema digests are unchanged. Clients must tolerate absent
optional coaching and should use the structured result count and retrieval tier.
Request `compact` for serialization diagnostics or `full` for canonical fields.

## 1.40.0 (2026-10-14)

`list_inference_hosts` now defaults `include_unconfigured` to `false` in the
advertised schema, handler, registry and both Python SDK clients. Omitted
arguments list configured hosts only, including when `provider_kind` filters
the result. Configured hosts remain
listed during outages or quota/auth cooldowns; callers must inspect `available`
and `cooldown` before selecting a host.

Explicit `include_unconfigured=true` retains the complete registered inventory.
`describe_inference_host` still accepts any registered host ID, and the static
host enums remain unchanged. No adapter or invocation capability is removed.
The discovery description and intent map now explain these distinctions.

The `list_inference_hosts` input-schema digest and aggregate surface digest
change; output envelope shapes and other input-schema digests are unchanged.
Clients relying on omission to return every registered host must send
`include_unconfigured=true`. Refresh cached schemas so clients do not inject
the previous `true` default. Older Python SDK clients serialize `true` on
omission; upgrade or explicitly pass `include_unconfigured=False` to request
configured hosts only. Explicit `false` and `true` retain their existing behavior.

## 1.41.0 (2026-10-15)

`consult` and `delegate_inference` take `host_id` as a bounded string (1 to 128
characters) instead of an enum. The handler checks it at runtime: `consult`
accepts the hosts it can route (`ollama:local`, `hf:router` and the three
host adapters), and `delegate_inference` keeps its registry check on
`accepts_host_id_from`. Every id accepted before is still accepted; an unknown
id is refused with the valid ids.

`consult`'s thorough lane takes its host order from the `consult.thorough` job
in the operator's inference hosts file, or claude, codex, antigravity when no
job is set, which is the order the per-family peer table gave. Results:

- `family` (compact and full) names the served host's model family, and the
  audit record's route carries it.
- `skipped` lists the hosts passed over before the one that served, in order,
  each with a closed reason code (`disabled`, `unconfigured`, `cooldown`,
  `excluded_family`) and a sentence. It appears only when a host was skipped,
  and never on `privacy="local"`, which contacts no thorough host.
- New refusals: `INFERENCE_CONFIG_INVALID` (the hosts file does not load; the
  standard lane is unaffected), `CONSULT_NO_ELIGIBLE_HOST` (every host in the
  job was skipped; with `allow_degraded=true` the call answers locally
  instead, contacting no thorough host), `CONSULT_HOST_NOT_IN_JOB` and
  `CONSULT_HOST_DISABLED` for a pinned host. All are recorded.

`list_inference_hosts` and `describe_inference_host` report each host's
`family` (added in the step 1 change, recorded here).

Compatibility: additive for callers. A client that cached the old enum will
still send valid ids. Schema digests move for `consult` and
`delegate_inference` (the `host_id` property lost its enum); no other digest
moves. Client action: none required; read `skipped` and `family` where useful.

## 1.42.0 (2026-10-16)

call_model is deprecated in favour of consult(effort='standard'), which reaches the same local host; it stays callable and is still the only way to set model, temperature or max_tokens on an HTTP host. Descriptions and recovery hints stop recommending it, and describe_tool returns a deprecation block. Nothing is added, removed, retyped or renamed.

Affected outputs: `describe_tool(tool_name='call_model')` gains a `deprecation`
block (`deprecated_since` 2026-10-16, `superseded_by` consult, `migration`);
`list_tools` keeps listing `call_model`, now with `deprecated: true` and
`superseded_by: consult`, because a deprecated name the deployment still exposes
stays in the negotiation index. The descriptions of `call_model`, `consult`,
`delegate_inference`, `list_inference_hosts`, `describe_inference_host` and
`dialectic`, and the recovery hints of `delegate_inference`'s missing-extension
refusal and the dialectic LLM failure, now point at `consult`. Each server call
of the old name logs `deprecated_tool_called`.

Compatibility: every existing `call_model` call works unchanged, and
`accepts_host_id_from` still lists it for the HTTP hosts. No input schema
changes, so no input digest or the surface digest moves. Removal is not
scheduled: it waits on the gateway intent classifier and the knowledge-graph
stale-entry check moving off the handler, and on `consult` taking model and
sampling controls or a further operator decision. Client action: use
`consult(brief=..., effort='standard')`; a client that needs `model`,
`temperature` or `max_tokens` on an HTTP host keeps calling `call_model` and
should say so with `leave_note`. Model selection on a CLI host stays on
`delegate_inference`.

## 1.43.0 (2026-10-16)

call_model's deprecation states its removal window: no earlier than 2026-12-02, and its model, temperature and max_tokens controls for HTTP hosts retire with it rather than moving onto consult. Descriptions and the describe_tool migration text say so. Nothing is added, removed, retyped or renamed.

Affected outputs: the `migration` text of `describe_tool(tool_name='call_model')`
and the descriptions of `call_model` and `consult` now state the removal
window and that the raw controls retire with the tool. The decision, delegated
by the operator, keeps `consult` bounded as the advisory facade designs it
(`docs/proposals/active/consult-advisory-facade-v1.md`, amended 2026-10-16).

Compatibility: every `call_model` call still works until the removal. No input
schema changes, so no input digest or the surface digest moves. The removal
itself will be a breaking change taken under this deprecation window, no
earlier than 2026-12-02, after the pre-registered 2026-12-01 read. Client
action: move to `consult(brief=..., effort='standard')` before then. A client
that depends on `model`, `temperature` or `max_tokens` on an HTTP host should
say so with `leave_note` before that date.

## 1.44.0 (2026-10-17)

Queryless KG listings and check-in memory suggestions omit unpromoted imported memory by default; listings report imported_omitted with a hint, authority_mode='all' or an imported-memory tag filter still reads the lane, and query search is unchanged.

Affected outputs: a `knowledge(action=search)` / `search_shared_memory` call
with no `query` may now return fewer rows. When it left imported memory out it
adds `imported_omitted` (a count of rows skipped while filling the page) and
`imported_omitted_hint`, both lifted into the lean digest's `state_summary`.
Check-in `relevant_discoveries` and `_mirror_kg_results` no longer include
unpromoted imported memory. Promoted governed claims and channel messages are
unaffected; query search is unchanged.

Compatibility: no parameter name, type, default or requiredness changes. A
client that listed imported rows by sweeping open work sees them omitted and
gets them back with `authority_mode="all"` or an imported-memory tag filter.
Schema digests move for `knowledge`, `search_knowledge_graph` and
`search_shared_memory` (the `authority_mode` description); no other digest
moves. Client action: none required; read `imported_omitted` where useful.

## 1.45.0 (2026-10-17)

Output and description changes from the 2026-10-16 agent-experience dogfood
read (`docs/evaluations/ax-dogfood/read-2026-10-16.md`). Nothing is added,
removed, retyped or renamed.

- **`search_shared_memory` output.** The `lean` and `compact` tiers now list
  the results after the three digests under `more_results` when `limit` is
  unset, with the same rows, 17-row cap and 2,000-byte budget as before. A
  `limit` of 3 or less omits them; an explicit `limit` above 3 behaves as in
  1.33.0. When the shown digests carry more than one `relevance_basis`,
  `state_summary.relevance_note` says the scores are not comparable and that
  list order is the ranking.
- **Response hints.** A server-written hint that named
  `knowledge(action=..., ...)` (`next_action`,
  `discovery_retrieval_options.open_one`, `raw_governance_hint`,
  `resolution_notes_hint` and the other lifted search notes) now names
  `use_tool(tool_name='knowledge', arguments={...})`. The router is not on the
  progressive surface, and `use_tool` is reachable on every surface, as
  `update_finding` hints have been since they moved to `use_tool`. The
  `next_action` of a finding alias called with an explicit other action
  reports what ran as "the knowledge router's '<action>' action" rather than
  as a call, so the rewrite cannot present a route the caller never took.
- **Descriptions.** `store_finding`'s field descriptions drop router wording
  ("for action=store or promote", "action=search", "S22 H5 provenance") and
  `discovery_type` lists every value in its brief. The knowledge router's
  `response_to` description lists the valid `response_type` values.
  `search_shared_memory`'s `include_details` brief names the `use_tool`
  route.

**Compatibility.** A client that relied on a no-`limit` lean search returning
exactly three digests should pass `limit=3`. A client that parses hint text
for `knowledge(` should read the `use_tool` form; the call it names is the same
read.

**Schema digests.** `search_shared_memory`, `store_finding` and `knowledge`
move (descriptions only); `surface_sha256` moves with them.

**Client action.** None required beyond the compatibility notes above.

## 1.46.0 (2026-10-17)

Imported-memory classification keys only on source-<provider>-memory tags; the bare topic words memory-sync, memory-import, imported-memory and source-memory no longer mark a row imported_context.

Affected outputs: the `authority` block on knowledge search, get and details
rows (`tier`, `trust`, `ranking_multiplier`), the `imported_memory` marker,
and everything keyed on that tier: the close-contest down-rank, queryless
listing omission (1.44.0), check-in suggestion filtering, and the lane-filter
test. A row whose only memory-related tag is one of those four words now
classifies as `native_finding`. Rows tagged `source-<provider>-memory` are
unchanged. In the live graph on 2026-10-17, four rows carried a bare word:
three native writes that were misclassified, and one import that also carried
`source-claude-memory`.

Compatibility: no parameter or schema change, so no digest moves. A writer
that marked imports with a bare word alone must use `source-<provider>-memory`.
None is known; the repository's own writers and the memory sync use the
source pattern. Client action: none.

## 1.47.0 (2026-10-17)

Dialectic review requests accept requester-declared author-agent UUIDs and
recuse those authors from independent reviewer selection, assignment, and
verdict paths. Terminal decisions also bind their runtime mutation to a
durable resolution-effect record, with explicit operator recovery for an
interrupted effect. `request_review` and `dialectic(action="request")` accept
`reviewed_work_author_agent_ids`, a bounded list of canonical registered-agent
UUIDs. The server records the declaration with `authorship_verified: false` and
returns its status as `reviewed_work_authorship`; omission remains
`not_supplied`, while an explicit empty list is refused.

The declaration only removes authority. Declared authors cannot be selected,
claim an open slot, be assigned or reassigned, or supply the independent
antithesis/synthesis verdict. A malformed stored declaration fails closed for
independent assignment. Explicit self-review remains represented as self-review
and never becomes independent merely because the marker is absent or malformed.

Accepted thesis, antithesis, and synthesis messages advance a monotonic durable
`transcript_revision`; the message, next phase and synthesis round commit in one
transaction. Reviewer reassignment likewise binds the prior reviewer and
revision while committing its audit message. A terminal decision
compare-and-sets the exact phase, reviewer, and revision on which it was
computed. If another worker appends a message or changes the reviewer first,
the stale message or terminal decision is refused and the caller must reload. A
BEAM `saga_in_flight` response is also an authoritative contention result; it
never opens a concurrent Python terminal writer. A deterministic post-claim
frontier refusal releases that request's reserved saga immediately. Ambiguous
reviewer-claim responses fail closed rather than treating a same-identity
readback as proof that this request won the claim.

`dialectic(action="reconcile_effect")` is operator-only. Its default
`effect_disposition="inspect"` is read-only. `apply_pending` may claim and apply
an untouched pending effect. An `applying` effect is never replayed
automatically: after confirming its worker stopped, the operator fences that
exact owner token with `mark_uncertain`, inspects and repairs live state, then
uses `mark_applied` with a reason and the exact `effect_updated_at` returned by
that inspect. The timestamp is a compare-and-set version: a changed record is
refused even when its state is still `unknown_partial`. Repeating an
already-applied acknowledgement is idempotent and emits no second downstream
signal. The durable effect row is authoritative; outcome, calibration and
lifecycle telemetry after application are best-effort and cannot make an
applied effect replayable.

Operators can enumerate every non-applied row without mutating it with
`python3 scripts/ops/dialectic_resolution_effects.py`; the state-specific
procedure and the limits of `mark_applied` are documented in
`docs/operations/dialectic-resolution-effects.md`.

Exact-token pause expiry and resolution resume both lock the two durable
status copies with the identity metadata. TTL expiry has a narrower repair
path of its own: matching `active` copies are authoritative, and an exact-token
`active`/`paused` split may be normalized because expiry carries no dialectic
conditions or verdict effects. Matching active copies clear any stale string
`paused_at` under an observed-token fence; a different token refuses when a
durable paused copy exists. Unrelated status, terminal status, or a missing
durable agent still refuses without mutation and reports that operator
reconciliation is required.

An elapsed pause TTL is eligibility for that durable transition, not proof it
committed. State reads therefore keep `write_gate.writes_held=true` until the
resume succeeds and expose `expiry_eligible=true` separately; a gated write may
start or retry expiry but remains refused and must be resubmitted after the
pause lifts.

Compatibility: additive for callers that omit the authorship field or never
use the recovery action, and deliberately restrictive only when a requester
declares a conflict. Existing dialectic calls retain their action and parameter
meanings. Migrations 075, 077, and 078 are hard deployment prerequisites for
this server version: Python session reads and terminal writes use the
durable-effect table, transcript frontier, and resolution-saga generation, and
the Python client selects the distinct frontier-required
`/v2/dialectic/resolve-with-effect` route whenever BEAM resolution is enabled.
An older service returns 404 before Python falls back to its own guarded writer;
it cannot accept and ignore the revision as an unknown JSON field.
NULL-to-reviewer claims likewise use a distinct guarded BEAM route. The BEAM
service retains its v1 resolution endpoints for protocol compatibility and
offline diagnosis, not as an online old-Python rollback guarantee.
Before applying the migrations, block dialectic traffic and stop/drain both old
writer runtimes; then apply 075/077/078, start new Python and new BEAM, verify
both revisions, and restore traffic. A pre-078 lease plane is below the
rollback floor after migration 078 removes the permanent same-payload
uniqueness constraint it assumes. Old Python is also below the online rollback
floor because its maintenance writers do not bind the saga generation; any
rollback keeps dialectic traffic and both liveness sweepers quiesced until the
generation-aware Python/BEAM pair is restored. The
`request_review` input digest moves from
`4beb141a8d9f85f811e249c7ab7a2a2f7bc2fc859a2269e80654e68f0807c7a3` to
`df7154d376086bb0af83bfcaee7e0e48d5821f347d9efce60d9487bfd7ba8e84`;
the `dialectic` input digest moves from
`246dcb660edf3c967844d3f0f7b24665030757e0278065ff123241e46f1b0b50` to
`958b4c233a91937c83e555761aff659b71f04c0ffc6f9f5daba488ffddfc9e28`.
The full-surface digest moves from
`b4e12f036620def817aa7e7cc5f28918b4987e80a5f12c8d2a8edc8e401e4b88` to
`56ffafc140a6f92d248be154354f1467b17684d92a0419257f78b80ac82316f6`.
Client action: proxy requesters should pass every known UNITARES author UUID;
ordinary self-filed review requests require no change. Operators should use
`reconcile_effect` only after reading the returned state and recovery guidance,
and must echo the inspected `effect_updated_at` when using `mark_applied`.

## 1.48.0 (2026-10-17)

`store_finding` accepts an optional `response_to`
(`{discovery_id, response_type}`), the typed link `knowledge(action='store')`
already takes, with the same validation: a malformed link or an unknown
`response_type` is refused rather than stored unthreaded. Its description
lists the nine `response_type` values. The `store_finding` and
`update_finding` acknowledgements lift the record's link as
`state_summary.response_to`; a record without one carries no such field.
Item 5 of the 2026-10-16 agent-experience dogfood read: a linked finding no
longer needs `describe_tool` plus `use_tool(tool_name='knowledge', ...)`.

**Compatibility.** Additive: an optional parameter on an existing alias.
Calls without it are unchanged.

**Schema digests.** `store_finding` moves (one added optional property);
`surface_sha256` moves with it.

**Client action.** None. A client that links findings through
`use_tool(tool_name='knowledge', arguments={'action': 'store', ...})` may call
`store_finding(..., response_to=...)` instead.

## 1.49.0 (2026-10-17)

Authorization only. `agent(action="resume")` on a paused agent now needs a
valid `X-Unitares-Operator` token; the agent itself gets
`PAUSE_SELF_RESUME_REFUSED`, with the paused-write `recovery` block, where it
used to succeed. Resuming from `waiting_input`, unsticking an active agent and
every operator-token call are unchanged. Nothing is added, removed, retyped or
renamed, and the description that says the rule is outside every input schema,
so no input digest moves. Client action: a paused agent follows the refusal's
`recovery` block (`self_recovery(action='check')`, its dialectic review, an
operator, or the pause's expiry) instead of calling `resume` on itself.

## 1.50.0 (2026-10-17)

search_knowledge_graph's discovery_type filter description lists the values search accepts, generated from DiscoveryType plus topic_rollup, and its tool description stops hand-listing a drifted set that named an invalid answer type. Nothing is added, removed, retyped or renamed.

- **Affected:** `search_knowledge_graph`'s `discovery_type` parameter
  description (was "Filter by type") and its tool description's
  VALID ENUM VALUES block and example comment, which listed `answer`
  (rejected) and omitted most accepted types.
- **Compatibility:** description-only. Accepted values, validation and the
  `bug` → `bug_found` normalization are unchanged.
- **Schema digests:** `search_knowledge_graph` moves (its parameter
  description changed); no other capability's digest moves.
- **Client action:** none. A client that copied the old list should read the
  parameter description instead.

## 1.51.0 (2026-10-18)

`consult` accepts an optional `host_ids`: a council of 2 or 3 distinct CLI
hosts (`claude:host-adapter`, `codex:host-adapter`,
`antigravity:host-adapter`) asked the same brief in parallel. Each member is
an ordinary pinned consultation: the same route postconditions, its own
`consultation_id`, audit record and `hash_key`, and no failover. The records
carry a shared `council_id`. The result (`unitares.consultation_council.v1`)
lists the members side by side under `members`, with `answered` and `asked`;
nothing combines, ranks or votes on the answers.

- **Affected:** `consult`'s input schema (one added optional property, and
  the `effort` default, which becomes `thorough` when `host_ids` is set) and
  its tool description.
- **Compatibility:** additive. A call without `host_ids` is unchanged. A
  council needs `privacy='cloud_allowed'`, `effort='thorough'` (the default)
  and `allow_degraded=false`, and cannot be combined with `host_id`; any
  other combination, a size outside 2 or 3, a repeated host or a non-CLI host
  is refused before any host is contacted, and is not recorded. A member the
  operator's `consult.thorough` job excludes or switches off fails alone and
  is recorded. The call succeeds with `status` `completed` or `partial` when
  at least one member answered, and fails with `CONSULT_COUNCIL_FAILED`
  when none did.
- **Schema digests:** `consult` moves; `surface_sha256` moves with it.
- **Client action:** none. A client that wants several families' advice
  can make one `consult(host_ids=[...], privacy='cloud_allowed')` call
  instead of one call per host.

## 1.52.0 (2026-10-18)

Corrections to the 1.51.0 council (`consult(host_ids=...)`), from an
adversarial review after it merged.

- **Affected:**
  - `consult`'s `host_ids` schema loses its size bounds (2 to 3 items, 1 to
    128 characters each). A wrong size now reaches the handler's
    `INVALID_CONSULT_COUNCIL` refusal, which lists the valid hosts. It used
    to fail schema validation with a generic `PARAMETER_ERROR` that also
    blamed `effort`'s default.
  - `host_ids` sent as JSON text (REST, `use_tool`) is parsed into a list.
    `/mcp/` already did this. A lone host id becomes a one-item list, which
    the handler refuses with the valid hosts.
  - The `host_ids` brief names the `cloud_allowed` requirement, and the
    `effort` brief covers `host_ids`.
  - `CONSULT_COUNCIL_FAILED` reports `validation_error` when every member
    was refused for a validation reason, such as a host outside the
    operator's job. Otherwise it reports `system_error`, as before.
  - Council records are written once the whole council returns. When a
    council is cancelled:
    - a member that had already answered is recorded as `cancelled` without
      hashes, because its answer and hash key never reached the caller;
    - a member already refused or failed keeps that outcome;
    - a member still running is recorded as `cancelled`.
  - A council's hosts-file refusal no longer suggests standard effort, which
    a council cannot use.
- **Compatibility:** additive or corrective. Every call 1.51.0 accepted is
  still accepted. Calls with the wrong size are still refused, now with the
  council's own code.
- **Schema digests:** `consult` moves; `surface_sha256` moves with it.
- **Client action:** none. The Python SDK's `consult()` gains `host_ids`, and
  `ConsultResult` gains `council_id`, `answered`, `asked` and `members`.
  Before this release the SDK dropped every council answer.

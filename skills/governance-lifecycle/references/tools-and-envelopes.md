# MCP Tools Reference and Progressive Catalog

Reference companion to `skills/governance-lifecycle/SKILL.md`. Load when
discovering omitted capabilities via `list_tools`/`describe_tool`/`use_tool`,
choosing between `consult` and `dialectic`, or handling `knowledge_changes`
pointers.

## Progressive Tool Advertisement (Interface Contract 1.13.0+)

Interface contract 1.13.0 and later separates the complete negotiated catalog
from the initial transport advertisement. MCP, REST, and stdio start with a
small progressive surface by default:

- `list_tools(lite=true)` — returns live interface version, surface hash, and a
  name-only record for every complete capability (`lite` controls response
  detail, not reachability).
- `list_tools(lite=false, category=...)` — browse rich catalog metadata.
- `describe_tool(tool_name=..., action=...)` — returns full unabridged parameter
  descriptions (initial advertised parameter descriptions are abridged).
- `use_tool(tool_name=..., arguments={...})` — invokes a capability omitted from
  the initial listing through its normal identity, validation, authorization,
  routing, timeout, response, and telemetry paths.

Operators requiring every schema up front can set
`UNITARES_TOOL_ADVERTISEMENT=full` (legacy `GOVERNANCE_TOOL_MODE` is ignored).
Prefer primary workflow names; raw implementations remain callable for
compatibility. Older servers may advertise a restricted profile — inspect the
live catalog and server instructions rather than assuming every named tool is
callable.

## Essential Tools (every session)

- `start_session(force_new=true, parent_agent_id=...)` — create a fresh process
  identity once, optionally declaring lineage.
- `sync_state()` — check in with work summary and complexity; pass `confidence`
  only for a genuine forecast.
- `check_working_state(client_session_id=...)` — read current EISV state. Reads
  your state only on proof sent with the call (`client_session_id`,
  `X-Session-ID`, or verified `continuity_token`); without proof it answers
  `unbound` (never a server-inferred state) and `next_action` says how to
  recover.
- `identity(client_session_id=...)` — confirm runtime identity and continuity
  resolution; never call with no arguments, and pass `continuity_token` for
  proof-owned UUID rebinds.
- `health_check()` — operator-facing server health check; discover and invoke
  through `use_tool` under progressive advertisement.
- `search_shared_memory(query=...)` — search existing knowledge before creating
  new entries.
- `store_finding(...)` — store a durable discovery, root cause, or correction.
- `update_finding(discovery_id=..., ...)` — revise or close an existing finding.
- `knowledge(action="note", ...)` — quick contribution to the knowledge graph.
- `self_recovery(action="check"|"quick"|"review")` — lift a pause while risk is
  below its gates (`review` refuses at risk `0.65` and above, except for the
  legacy cold-start trap). Advertised by default so schema-driven clients can
  call it; the check-in envelope names it below those gates and names the
  dialectic session above them.

## Common Tools (use when needed)

- `knowledge()` — full knowledge graph CRUD, search, synthesis, and audit router.
- `agent()` — agent lifecycle router (`list`, `get`, `update`, `archive`,
  `resume`, `delete`).
- `calibration()` — inspect or update calibration data.
- `consult(brief=..., purpose="critique")` — advisory model or council critique
  when choosing between plausible designs, before an irreversible schema or
  contract step, or when a key assumption is unverified (never an on-record
  verdict and never satisfies implementation review; use `request_review` once a
  diff exists). Advertised by default; requires a configured inference host
  (`list_inference_hosts`): `effort="standard"` queries a local host, while
  `effort="thorough"` or `host_ids=[...]` (parallel 2–3 host council) requires
  `privacy="cloud_allowed"` and the opt-in CLI host-adapter extension. Cap at
  one consult per design decision.
- `dialectic()` — structured review router (`get`, `list`, `quick`, `request`,
  `thesis`, `antithesis`, `synthesis`, `consult`, `reassign`,
  `reconcile_effect`). `consult` files an outside verdict as a record with no
  authority and needs no reviewer slot. `reconcile_effect` is operator-only
  recovery for a durable terminal effect (inspect first, apply only untouched
  pending work, never replay an applying or unknown-partial effect
  automatically). Advertised in the complete catalog because `request_review`
  pins `action="request"`.
- `export()` — export session history.

## Specialized Tools

- `call_model()` — deprecated since 2026-10-16: use `consult(effort='standard')`,
  which reaches the same local host. Still callable until removal no earlier
  than 2026-12-02; `temperature` and `max_tokens` go with it (`consult` omits
  them by design). Choose a local model via `consult(model=...)` from
  `list_inference_hosts` for `ollama:local`.
- `observe()` — read governance observations and fleet diagnostics.
- `config()` — read or change runtime thresholds; writes are privileged.
- `list_tools()` / `describe_tool()` — inspect the deployed catalog; default
  `list_tools()` is a name-only handshake, `list_tools(lite=false)` returns rich
  metadata, and `describe_tool()` returns full action parameters.
- `use_tool(tool_name=..., arguments={...})` — invoke an omitted complete-catalog
  capability with full middleware and authorization.

## Row-Change Pointers (`knowledge_changes`) During Active Work

A successful active call can carry `knowledge_changes`: at most three coalesced
row pointers in a separate 2 KiB budget. Read details before acting; acknowledge
received revision UUIDs through `knowledge(action="notices")`. Delivery does not
wake a session, perform a model call, or create a check-in. A successor does not
inherit its predecessor's inbox merely by sharing its workspace or lineage. See the `knowledge-graph` skill for mute, gap tokens, and retention.

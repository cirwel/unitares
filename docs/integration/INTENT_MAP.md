# Intent map: which tool for which need

**Status: draft for maintainer reaction (2026-10-10).** Not an adopted policy.
Tool names and parameters were checked against the live catalog
(`list_tools(lite=false)`, `describe_tool`) and the handler source on that
date; where a claim rests on source, the file is named. If this page and the
catalog disagree, the catalog wins and this page is stale.

This page is organized by what an agent wants to do, not by how the server
implements it. The authoritative per-tool reference is the generated
[`dev/TOOL_REFERENCE.md`](../dev/TOOL_REFERENCE.md); the versioned contract is
[`INTERFACE_CONTRACT.md`](../INTERFACE_CONTRACT.md).

**Reaching a tool.** The initial tools/list is progressive
(`PROGRESSIVE_MODE_TOOLS` in `src/tool_modes.py`). Below, *(hidden)* marks a
tool outside that set: find it with `list_tools`, read its schema with
`describe_tool`, then call it as `use_tool(tool_name=..., arguments={...})`.
The server would dispatch it directly, but most MCP clients will not call a
name they were never given a schema for.

## By need

| Need | Start here | Alternatives, and when |
|---|---|---|
| Find your way around | `list_tools` (lite for names, `lite=false` for descriptions) | `describe_tool` for one schema; `use_tool` to call a hidden tool; `skills` *(hidden)* for the server-authored guides |
| Establish identity | `start_session(force_new=true)`; keep `client_session_id` | `onboard` *(hidden)* is the same tool underneath. `parent_agent_id` + `spawn_reason="explicit"` only for a real handoff from a finished predecessor; `"subagent"` for a dispatched subagent |
| Inspect identity and lineage | `identity(client_session_id=...)` | `bind_session` *(hidden)* to attach a transport session onboarded elsewhere; `list_process_bindings`, `get_trajectory_status`, `verify_trajectory_identity` *(hidden, advanced)* for shared-identity and drift diagnostics |
| Record a work check-in | `sync_state` (pass `confidence` to get a `prediction_id`) | `process_agent_update` *(hidden)* is the canonical name; `simulate_update` *(hidden)* previews without advancing state; `mark_response_complete` *(hidden)* marks waiting-for-input with no cycle; `record_progress_pulse` *(hidden)* is a bare liveness row |
| Read your own state | `check_working_state` | `get_governance_metrics` *(hidden)* is the canonical name; `get_thresholds` *(hidden)* for the thresholds in force |
| Record an outcome against a check-in | `record_result(prediction_id=...)` | `outcome_event` *(hidden)* is the canonical name; `outcome_correlation` *(hidden)* reads your own outcomes against EISV; `calibration` *(hidden)* reads or repairs the shared confidence model |
| Search shared memory | `search_shared_memory` (works before `start_session`) | `knowledge(action="search")` and `search_knowledge_graph` *(both hidden)* reach the same handler; `knowledge` also has `get`, `list`, `details`, `stats` |
| Write a finding | `store_finding` (search first) | `leave_note` *(hidden)* for a quick note, sharing `knowledge(action="note")`'s handler |
| Revise a finding | `update_finding` *(hidden)* | `knowledge(action="update" \| "supersede" \| "promote")` *(hidden)* |
| Ask another model for advice | `consult` | see [Model calls](#model-calls) |
| Get a governed, on-record review | `request_review` (one call can reach a verdict) | `dialectic` *(hidden)* to read or advance a session (`get`, `list`, `thesis`, `antithesis`, `synthesis`, `reassign`) |
| Lift a pause on yourself | `self_recovery` (`check`, then `quick` or `review`) | `request_review` / `dialectic` when self-recovery is refused; `operator_resume_agent` *(hidden)* needs an operator token |
| Coordinate with live agents | `cirs_protocol` *(hidden)* | in-memory, process-local buffers; anything durable goes to `store_finding`. No lease or surface-claim tool is in the catalog |
| Look at other agents or the fleet | `observe` *(hidden)* | `agent(action="list" \| "get")`, `dashboard`, `detect_stuck_agents`, `export` *(all hidden)* |
| Operate the server | `health_check` *(hidden)* | `admin`, `get_workspace_health`, `config`, `set_thresholds`, `archive_orphan_agents`, `archive_old_test_agents` *(all hidden; several need operator authority)* |

## Model calls

Two inputs decide the route: **who answers**, and **whether it goes on the
record**. Privacy narrows the first.

| You want | Use | Notes |
|---|---|---|
| Advice from whatever is configured, local only | `consult` (defaults: `effort="standard"`, `privacy="local"`) | `purpose` is `answer`, `critique`, `summarize` or `generate`. Writes an audit row with keyed hashes, not text (`_record_consultation`, `src/mcp_handlers/support/consultation.py`) |
| Advice from a strong model of another family | `consult(effort="thorough", privacy="cloud_allowed")` | Needs the operator's agent-orchestrator extension (off by default). The server picks the host: it excludes the caller's own family, inferred from transport signals, and takes the first available peer (`_THOROUGH_PEERS`, `_caller_family`). The caller cannot name or exclude a host |
| A specific subscription CLI: Claude, Codex or Antigravity | `delegate_inference(host_id=...)` *(hidden, advanced)* | Same orchestrator lane as thorough consult; advisory, no tools. `host_id` is one of `claude:host-adapter` (default), `codex:host-adapter`, `antigravity:host-adapter` (`DelegateInferenceParams`, `src/mcp_handlers/schemas/core.py`) |
| A raw completion on a local or Hugging Face host | `call_model` *(hidden)* | `provider` `auto`/`ollama`/`hf`, or `host_id` from `list_inference_hosts`; only hosts whose `accepts_host_id_from` includes `call_model` (local Ollama, HF router) are accepted |
| Which hosts exist and are ready | `list_inference_hosts`, `describe_inference_host` *(hidden)* | readable before `start_session`; calling any inference tool needs an identity |
| A verdict that goes on the record | `request_review` | The reviewer is not caller-selectable. By default an in-process reviewer answers on the local model; the orchestrated reviewer is opt-in (`UNITARES_DIALECTIC_ORCHESTRATED_REVIEW=1`) and its backend is operator config, `UNITARES_DIALECTIC_REVIEWER_HOST(S)` (local, codex, claude, antigravity, external), outside the inference registry (`src/mcp_handlers/dialectic/orchestrator_dispatch.py`) |

## Overlaps and gaps

Observations, each with a pointer. None is a proposal to remove a capability;
the questions at the end are for the maintainer.

**Overlaps (one need, several tools)**

1. *Model calls are split by transport.* `call_model` accepts only the
   synchronous HTTP hosts and `delegate_inference` only the orchestrator CLI
   hosts (`accepts_host_id_from` in `list_inference_hosts`; the refusal at
   `src/mcp_handlers/support/model_inference.py`, "Pick a host whose
   accepts_host_id_from includes call_model"). For an agent, both mean "ask
   model X".
2. *Descriptions route agents between each other.* `consult`: "on-record:
   request_review"; `call_model`: "for advisory help prefer consult";
   `delegate_inference`: "for a raw completion use call_model"; `dialectic`:
   "For advice outside a governed-review session, use consult"
   (`src/tool_descriptions.py` and the catalog).
3. *"consult" names two things.* The `consult` tool gives advice with no
   review record; `dialectic(action="consult")` files an outside verdict on an
   existing session as a non-authoritative record (`dialectic` description).
4. *Three search entry points, one handler:* `search_shared_memory`,
   `search_knowledge_graph`, `knowledge(action="search")` (stated in the
   `search_knowledge_graph` description).
5. *Two note entry points, one handler:* `leave_note` and
   `knowledge(action="note")` (`handle_leave_note`,
   `src/mcp_handlers/knowledge/handlers.py`).
6. *Alias pairs carry different timeouts.* `sync_state` 30 s vs
   `process_agent_update` 60 s; `check_working_state` 30 s vs
   `get_governance_metrics` 10 s (`timeout` field in `list_tools(lite=false)`).
   Whether that is intended is not established here.
7. *Operator reads and writes appear twice:* `get_thresholds`/`set_thresholds`
   vs `config(action="get" | "set")`; `get_workspace_health` vs
   `admin(action="workspace_health")`; `archive_*` vs `agent(action="archive")`;
   `operator_resume_agent` vs `agent(action="resume")`.

**Needs no single tool expresses**

1. *"A strong model that is not family X."* `consult` excludes only the
   caller's own inferred family. A Claude caller wanting a non-Codex answer
   gets Codex first (`_THOROUGH_PEERS["anthropic"]`); to choose, it must drop
   to `delegate_inference`, which is hidden and reached through `use_tool`.
2. *"A cloud model, chosen by me, as advice."* `consult` has no model or host
   field (`ConsultParams` forbids extra keys); `call_model` has them but is
   hidden. Whether `call_model` leaves an audit row comparable to
   `consult`'s was not checked for this draft.
3. *"A strong model, on the record, chosen by me."* `request_review` has no
   reviewer field; the backend is deployment config, and the default reviewer
   is the local model.
4. *Coordinating over a surface.* No lease or claim tool is in the catalog;
   `cirs_protocol` is the only live channel and is lost on restart.

**Hidden tools that a listed need depends on** (absent from
`PROGRESSIVE_MODE_TOOLS`)

- Revising a finding needs `update_finding`. `src/tool_modes.py` says why it
  is hidden: advertising it costs ~3.4 KB against a down-only byte ratchet.
- Choosing a model needs `list_inference_hosts`, plus `call_model` or
  `delegate_inference`.
- Advancing an open review needs `dialectic`.
- Tier and advertisement disagree: `leave_note` and `health_check` are tier
  `essential` but hidden; `self_recovery` is tier `common` but advertised.

**Open questions for the maintainer**

- Could one advice entry point take an optional "who answers" input (any,
  a named family, not a named family, local only) and route to the existing
  lanes, leaving `call_model` and `delegate_inference` as they are?
- Should every tool a listed need depends on be advertised, or should the
  initial listing at least name them?
- Should `dialectic(action="consult")` get a name that does not collide with
  `consult`?

## Adding to the surface (proposal)

Before a new tool, alias, action or model channel is added:

1. Find the need on this page it serves. If none fits, add the need first.
2. Say in the PR why the entries already listed for that need do not cover it.
   "A different transport or host" is an implementation reason, not a need.
3. Update this page in the same PR.

This is a proposal for the maintainer, not an adopted rule.

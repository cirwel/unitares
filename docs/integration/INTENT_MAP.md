# Intent map: which tool for which need

**Status: draft for maintainer reaction (2026-10-10).** Not an adopted policy.
Tool names and parameters were checked against the live catalog
(`list_tools(lite=false)`, `describe_tool`) and the handler source on that
date; where a claim rests on source, the file is named. If this page and the
catalog disagree, the catalog wins and this page is stale.

This page is organized by what an agent wants to do, not by how the server
implements it. Overlaps, gaps and a proposed check for new tools live in the
companion [audit](INTENT_MAP_AUDIT.md). [Recipes](#recipes) below give exact
calls for common tasks. The authoritative per-tool reference is the generated
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
| Notice changes to relevant findings | `knowledge_changes` on the next successful proved call | `knowledge(action="notices")` *(hidden)* reads/replays the bounded inbox; acknowledge exact revision UUIDs or mute. No wake or predecessor-inbox inheritance; requires migration 074. |
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
| Advice from a strong model of another family | `consult(effort="thorough", privacy="cloud_allowed")` | Needs the operator's agent-orchestrator extension (off by default). The server picks the host from the `consult.thorough` job (the operator's inference hosts file, or claude, codex, antigravity by default): it skips the caller's own family, inferred from transport signals, and takes the first available host, reporting the rest in `skipped` with a reason code (`_thorough_plan`, `_caller_family`). Set `host_id` to select a specific existing host instead |
| A specific subscription CLI: Claude, Codex or Antigravity | `consult(host_id=..., privacy="cloud_allowed")` | Pin `claude:host-adapter`, `codex:host-adapter` or `antigravity:host-adapter`; `allow_degraded` must stay false. No alternate host is tried. `delegate_inference` remains available for raw model/timeout controls |
| Advice from a specific HTTP host | `consult(host_id=...)` | Pin `ollama:local` or `hf:router`; HF requires `privacy="cloud_allowed"`. No fallback; raw model/token/temperature controls remain on `call_model` |
| A raw completion on a local or Hugging Face host | `call_model` *(hidden)* | `provider` `auto`/`ollama`/`hf`, or `host_id` from `list_inference_hosts`; only hosts whose `accepts_host_id_from` includes `call_model` (local Ollama, HF router) are accepted |
| Which configured hosts can serve | `list_inference_hosts`, `describe_inference_host` *(hidden)* | default list includes configured hosts, including temporary outages; inspect `available` and `cooldown`. Use `include_unconfigured=true` for the full inventory. Readable before `start_session`; inference calls need identity |
| A verdict that goes on the record | `request_review` | The reviewer is not caller-selectable. By default an in-process reviewer answers on the local model; the orchestrated reviewer is opt-in (`UNITARES_DIALECTIC_ORCHESTRATED_REVIEW=1`) and its backend is operator config, `UNITARES_DIALECTIC_REVIEWER_HOST(S)` (local, codex, claude, antigravity, external), outside the inference registry (`src/mcp_handlers/dialectic/orchestrator_dispatch.py`) |

**Routing facts.**

- **The on-record reviewer is not caller-selectable.** `request_review` has no
  reviewer field. The default reviewer is the server's local model; the
  orchestrated reviewer and its backend are operator configuration.
  Reassigning a reviewer needs an operator credential or the current
  reviewer.
- **Automatic thorough routing excludes the inferred caller family.** The
  server reads transport signals and skips that family; unknown callers try
  Claude first. `host_id` overrides this choice and pins one existing host,
  including the caller's own family. It remains advisory, never peer review.
  Explicit selection appears in the compact `request.host_id`; full
  `diagnostics` retain the actual host, provider and model. Automatic calls
  report any peer failover in `failover`; pinned calls never fail over.
- **Omitted effort follows a selected host.** Ollama/HF select standard; CLI
  hosts select thorough. With no host, omitted effort stays standard. An
  explicit effort must match the host; privacy is always a separate constraint.
- **`effort="thorough"` routes to the operator-authorized subscription-CLI
  lane** (Claude, Codex, Antigravity); it names a lane, not a measured
  capability tier.
- **The orchestrator lane is configuration.** The agent-orchestrator extension
  behind `consult(effort="thorough")` and `delegate_inference` is off on a
  default install. Without it, thorough `consult` fails with a recovery hint
  unless automatic routing uses `allow_degraded=true`, which returns a standard local answer marked
  `status: "degraded"` instead. `delegate_inference` has no fallback.
  `list_inference_hosts` defaults to configured hosts. Unconfigured adapters
  remain inspectable with `include_unconfigured=true` or by exact ID through
  `describe_inference_host`. Configured hosts with outages or cooldowns remain
  listed with their availability; configuration is not a readiness guarantee.
- **A failed adapter call has caller-only diagnostics.** With
  `response_mode="full"`, `consult` keeps a bounded excerpt of unvalidated
  CLI output, the specific backend error message (up to 500 characters), and
  any answer-region diagnostic supplied by the adapter. The
  excerpt is not advice and is omitted from the durable consultation audit.
  Malformed output remains a failure; an exit code of zero alone does not
  establish a complete answer or trigger peer failover.
- **HF availability is based on configuration and observed failures.** A token
  with no known cooldown makes `hf:router` eligible to try; this is not a live
  credit check. Observed HTTP 401, 402 and 429 responses start the existing
  bounded auth/quota cooldown. Listing shows `available=false` and the retry
  time, and every HF call route respects it. After expiry, an ordinary call
  tries again; success clears the failure history. Discovery makes no paid
  probe. Model-specific 403/404 responses do not disable the whole host.

**Interpreting usage.** Full `consult` diagnostics include `tokens_used_note`.
`tokens_used` is a backend aggregate, not answer length or a normalized billing
measure. Missing usage can become zero. Current adapter rules are:

| Backend | Aggregate exposed as `tokens_used` |
|---|---|
| Standard Ollama/HF | Provider `usage.total_tokens` |
| Claude CLI | Input + output + cache creation + cache read tokens |
| Codex app-server | Reported total; input + output when total is absent |
| Codex exec JSONL | Input + output for the reported completed turn |
| Antigravity CLI | Reported `usage.total_tokens` |

Different tokenizers, context and cache rules limit comparisons between hosts.
See `host_adapter.py`, `codex_app_server_client.py`, and `model_inference.py`.
The clarification changes no counters, cost calculation or EISV accounting.

## Recipes

Every recipe except reading the fleet assumes you have run
`start_session(force_new=true)` and pass the returned `client_session_id` on
each later call (adapters may do this for you). Hidden tools go through
`use_tool(tool_name="...", arguments={...})`. Argument names below were
checked against `describe_tool` and the schemas; anything not checked says so.

### (a) Advice from a strong non-Anthropic model, no review record

1. Optional: `use_tool(tool_name="list_inference_hosts", arguments={})` and
   look for `available: true` on the `*:host-adapter` entries.
2. `consult(brief="<question plus the material, pasted inline>",
   purpose="critique", host_id="codex:host-adapter",
   privacy="cloud_allowed", response_mode="full")`. The material goes in `brief` (up to 32,000
   characters); there is no file or attachment argument. `purpose` is
   `answer`, `critique`, `summarize` or `generate`.
3. Read `advice`. Keep `consultation_id`; `record.hash_key` lets you prove
   later which audit row describes the exchange (the server keeps hashes, not
   text).
4. Check `diagnostics.host_id` and `model_used`. The selected host must
   answer or the call fails with recovery guidance. To choose another host,
   change `host_id` explicitly. Omit `host_id` to use automatic selection,
   which excludes the inferred caller family and may try another peer.

Nothing to close: `consult` opens no review. For a verdict on the record,
see (f). Raw model and timeout controls remain on hidden `delegate_inference`.

### (b) Grade an earlier check-in

1. `sync_state(response_text="<what you did>", complexity=0.5,
   confidence=0.8)`. A `prediction_id` comes back only when you pass
   `confidence`. Keep it. It is single-use and expires (an hour by default).
2. When the real result is known: `record_result(outcome_type="task_completed",
   prediction_id="<id>", outcome_score=1.0, detail={"test_name": "..."})`.
   `outcome_type` is required; it is one of the enum values in
   `describe_tool(tool_name="record_result")`, for example `task_completed`,
   `task_failed`, `test_passed` or `test_failed`. `outcome_score` and
   `is_bad` are inferred from the type if omitted.
3. If you lost the `prediction_id` or it expired, call `record_result`
   without it. The server then uses your last check-in's confidence as a
   proxy, or the `confidence` you pass; the outcome is recorded but not bound
   to that check-in.

### (c) Store a finding, then close it

1. Search first: `search_shared_memory(query="<topic>")`. If it exists,
   revise it instead (step 3).
2. `store_finding(summary="<one line>", details="<evidence>",
   discovery_type="bug_found", severity="medium", tags=["<topic>"])`. Keep the
   returned `discovery_id`. Every call creates a new finding.
3. Close it: `use_tool(tool_name="update_finding", arguments={"discovery_id":
   "<id>", "status": "resolved", "closure_class": "fix_verified",
   "closure_evidence": {"deployed": "<what shipped, confirmed in the running
   build>", "observed": "<the new behavior you saw>"}, "resolution_notes":
   "<why>"})`.
   - `closure_class` is one of `fix_verified`, `unobserved`,
     `not_reproducible`, `obsolete`, `duplicate`. It is optional but the
     reply asks for it when missing.
   - `fix_verified` needs `closure_evidence` with `deployed` and `observed`.
     If all you have is the symptom no longer appearing, use `unobserved`,
     with `window` (the period it did not occur) and `instrument_check` (how
     you know the recorder for this condition is still live).
   - For `duplicate`, the server suggests `{"of": "<discovery_id>"}`.
   - `closure_class` is accepted with `resolved`, `closed`, `wont_fix`,
     `superseded`, `archived` and `cold`, and refused on `open` or
     `disputed`. `resolution_notes` appends rather than replaces. Source:
     `_validate_closure_class`, `src/mcp_handlers/knowledge/handlers.py`.

### (d) Paused: get going again

1. `self_recovery(action="check")` diagnoses without changing anything.
2. Choose by the risk it reports. `action="quick"` (optional `reason`) works
   only at risk 0.40 or below. `action="review"` works up to 0.65 and needs
   `reflection` of at least 20 characters on what happened and what changes,
   optionally `conditions`. The reflection is stored in shared memory
   whether or not you resume. Neither works while a void is active, and
   each attempt that reaches the safety checks is recorded, so a retry is
   not free.
3. If refused, open a review: `request_review(issue_description="<what
   happened and why you should resume>")`. A converged verdict from an
   independent reviewer resumes you when the resolution executes: look for
   `action: "resume"` and a `next_step` saying the agent resumed. A verdict
   whose resolution did not execute says so in `next_step`. A self-review
   never resumes you (`SELF_REVIEW_NOT_AUTHORIZING`).
4. Otherwise an operator can resume you (`operator_resume_agent` needs an
   operator token), or the pause expires.

### (e) See what other agents are doing now

- `use_tool(tool_name="agent", arguments={"action": "list", "lite": true,
  "limit": 20})` gives the agents active in the last 7 days (`recent_days=0`
  for all; `status_filter` for `paused`, `waiting_input` and the rest).
  Works before `start_session`.
- `use_tool(tool_name="observe", arguments={"action": "agent",
  "target_agent_id": "<id>"})` gives one agent's governance patterns;
  `{"action": "aggregate"}` gives the fleet overview.

Neither reports current work. `agent` returns registration and lifecycle:
label, status, a declared `purpose`, update count and last-seen date.
`observe` returns EISV state and verdict patterns. The server does not keep
check-in text as history, and `cirs_protocol(protocol="state_announce")`
carries EISV state and a trajectory signature, not a task description
(`src/mcp_handlers/cirs/state.py`). The nearest thing to "what are they
working on" is what agents chose to write to shared memory:
`search_shared_memory`.

### (f) An on-record review by a reviewer you choose

You cannot choose one. `request_review` takes no reviewer argument; which
model reviews is deployment configuration. The closest options:

1. `request_review(issue_description="...")` and accept the configured
   reviewer. Keep the `session_id`.
2. Get the verdict you want from the model you want with (a) step 4, then
   file it on that session:
   `use_tool(tool_name="dialectic", arguments={"action": "consult",
   "session_id": "<id>", "reasoning": "<the verdict>",
   "reviewer_provenance": {"reviewer_kind": "external_consult", "backend":
   "<host_id>"}})`. Any bound agent may do this. It is on the record but has
   no authority: it never advances the review or counts as its verdict.
3. Ask the operator to reassign the reviewer
   (`dialectic(action="reassign")` needs an operator credential or the
   current reviewer) or to configure the reviewer backend.

---
name: governance-lifecycle
description: >
  Use when an agent is interacting with UNITARES for the first time, needs to
  onboard, check in, or recover from an applied lifecycle hold. Covers the full
  agent lifecycle from session start through check-ins to recovery.
last_verified: "2026-10-29"
freshness_days: 14
source_files:
  - unitares/commands/closeout.md
  - unitares/src/agent_loop_detection.py
  - unitares/src/eisv/adapter.py
  - unitares/src/mcp_handlers/core.py
  - unitares/src/mcp_handlers/identity/handlers.py
  - unitares/src/mcp_handlers/admin/handlers.py
  - unitares/src/mcp_handlers/tool_stability.py
  - unitares/src/mcp_handlers/middleware/envelope_step.py
  # Added 2026-09-14: strict identity refusal and its no-handler-execution
  # guarantee live here; resolver failure can still perform bookkeeping.
  - unitares/src/mcp_handlers/middleware/identity_step.py
  # Added 2026-08-09: this skill documents check-in and dialectic semantics but
  # was not verified against the code implementing either. That is why stale
  # `confidence` guidance survived several freshness cycles — the field it was
  # wrong about lives in phases.py, which nobody was checking.
  - unitares/src/mcp_handlers/updates/phases.py
  - unitares/src/outcomes.py
  - unitares/src/state_persistence.py
  - unitares/src/checkpoints.py
  - unitares/src/db/mixins/checkpoint.py
  - unitares/src/services/checkpoint_service.py
  - unitares/src/services/update_response_service.py
  - unitares/src/governance_monitor.py
  - unitares/src/monitor_calibration.py
  - unitares/src/mcp_handlers/updates/enrichments.py
  - unitares/src/dialectic_reviewed_work.py
  - unitares/src/mcp_handlers/dialectic/handlers.py
  - unitares/src/mcp_handlers/lifecycle/self_recovery.py
  - unitares/src/mcp_handlers/lifecycle/recovery_policy.py
  - unitares/src/mcp_handlers/support/agent_auth.py
  - unitares/src/mcp_handlers/support/pause_ttl.py
  - unitares/src/agent_storage.py
  - unitares/src/db/mixins/identity.py
  # The field guide names the negotiated contract version it was checked
  # against, so a contract change must re-verify these six rules.
  - unitares/src/interface_contract.py
  # Added 2026-09-07: the MCP Tools Reference now says which of its names a
  # tool mode advertises. The default surface and the listing/dispatch split
  # live in these two files; the reference drifts silently when they move.
  - unitares/src/tool_modes.py
  - unitares/src/tool_mode_listing.py
  # Added 2026-09-21: the reference distinguishes the name-only lite handshake
  # from rich list_tools browsing; that response shape lives here.
  - unitares/src/mcp_handlers/introspection/tool_introspection.py
  # Added 2026-09-08: the reference now says the advertised parameter
  # descriptions are abridged and names describe_tool as where the full text
  # lives. The trim rule is here; if it changes, that claim drifts silently.
  - unitares/src/schema_brief.py
  - unitares/skills/governance-lifecycle/references/checkpoints-and-envelopes.md
  - unitares/skills/governance-lifecycle/references/identity-and-recovery.md
  - unitares/skills/governance-lifecycle/references/tools-and-envelopes.md
---

# Agent Lifecycle

**Last Updated:** 2026-10-26

## Don't Panic: six rules

Mostly Harmless Guide to the normal agent path, verified against interface
contract `2.2.0` on 2026-10-26.

1. **Keep process identity honest.** Call `start_session(force_new=true)` once
   per process; reuse its `client_session_id` only within that process. Restarts
   mint fresh, and proof-rebind is for the same live owner. Declare lineage only
   for a real dispatch or handoff from an exited predecessor.
2. **Branch on control flow, not `success` alone.** Read
   `action_summary.action` and `sub_action`; check `refused`, `status`, and
   `rollout_flag` even when `success` is true. `proceed` and `guide` continue;
   an **applied** `pause` stops ordinary governed writes for its named recovery.
   Under the default observation posture, a raw pause recommendation appears in
   `policy_evaluation` while the effective action remains `proceed` / `guide`.
   Follow
   `next_step` and `safe_options` rather than repeat a refusal. EISV and verdicts
   describe state, not outcomes.
3. **Use each record for its job.** Call `sync_state` only at a meaningful
   milestone when you also hold a judgment the substrate cannot infer, not
   after every tool call. Use `store_finding` / `update_finding` for durable
   knowledge and `record_result` only for an outcome that actually occurred.
4. **Reuse durable knowledge safely.** Search before `store_finding`. If
   authorized, update or supersede an existing finding rather than duplicate
   it; otherwise leave it untouched. Operator-authored architectural decisions
   stay operator-owned. Never store secrets, and label uncertainty.
5. **Make confidence mean something.** Supply `confidence` only for a genuine
   forecast; each value can mint a scored prediction. Keep its `prediction_id`
   and pair it with the observed outcome. Do not report
   `task_completed` for a turn or local edit.
6. **Discover conditionally.** Use advertised tools and schemas directly. Only
   when one is absent or unclear, call `list_tools` (which also reports the
   contract version), then `describe_tool`; use `use_tool` only when the target
   is omitted from the initial listing.

See the
[two-process walkthrough](https://github.com/cirwel/unitares/blob/master/docs/guides/AGENT_SESSION_WALKTHROUGH.md)
for clean exit and handoff; exceptions and recovery follow.

## Primary Workflow Names

Use primary task-verb tools by default; each wraps a raw tool and returns a
normalized envelope (`next_action`, `state_summary`, `risk_summary`,
`memory_suggestions`, `recovery_hint`).

| Task | Primary workflow tool | Raw implementation tool |
|------|---------------|----------------|
| Start a fresh process identity | `start_session(force_new=true, ...)` | `onboard` |
| Check in at a meaningful milestone with an agent-held judgment | `sync_state(response_text=..., complexity=...)` | `process_agent_update` |
| Check your working state | `check_working_state(client_session_id=...)` | `get_governance_metrics` |
| Avoid duplicate work | `search_shared_memory(query=...)` | `knowledge(action="search")` |
| Record what actually happened | `record_result(...)` | `outcome_event` |
| Ask for a structured review | `request_review(issue_description=...)` | `dialectic(action="request")` |
| Store a durable finding | `store_finding(summary=..., discovery_type=...)` | `knowledge(action="store")` |
| Update a durable finding | `update_finding(discovery_id=..., ...)` | `knowledge(action="update")` |

- Routine reads, write acks, fresh `start_session`, and routine `request_review`
  omit `raw_governance` by default (`response_shape: "routine"`); never repeat
  `store_finding` just to see the full payload (each call mints a new finding).
- `request_review` reuses `issue_description` as the thesis by default (one-call
  review). For proxy reviews of another agent's work, declare known authors in
  `reviewed_work_author_agent_ids` (never an empty list).

When a response includes `raw_governance_hint`, `response_shape_reason`,
`idempotent_replay`, or `episode_fork_kind`, read
`references/checkpoints-and-envelopes.md`
(remote MCP: `skills(name="governance-lifecycle", reference="checkpoints-and-envelopes.md")`).

## Starting a Session

Choose creation, lineage, or proof-owned resume explicitly:

~~~text
start_session(force_new=true)                                        # fresh process identity (default; co-location is not lineage)
start_session(force_new=true, parent_agent_id="<dispatcher-uuid>",
              spawn_reason="subagent")                               # dispatched subagent
start_session(force_new=true, parent_agent_id="<prior-uuid>",
              spawn_reason="explicit")                               # deliberate handoff from an exited prior session
identity(agent_uuid="<uuid>", continuity_token="<token>", resume=true) # same live owner / proof-owned rebind
~~~

- **Do not** call bare `onboard()` or bare `identity()` (no proof signal mints
  a fresh identity or returns a refusal), do not pass `continuity_token` on
  every call or to `onboard(continuity_token=...)`, and do not treat `name=` as
  a resume key.
- Pass `client_session_id` on ordinary in-process calls.

When `start_session` or `identity` returns `refused: true` (`identity_required`,
`lineage_declaration_required`, `lineage_coincidental_rejected`,
`resume_proof_required`, `continuity_token_resume_rejected`,
`credentials_withheld`), or `bind_session` returns `rebind_refused` (do not
retry or forge), read `references/identity-and-recovery.md`
(remote MCP: `skills(name="governance-lifecycle", reference="identity-and-recovery.md")`).

## Check-ins

Call `sync_state()` only at a meaningful milestone when you also hold a
judgment the substrate cannot infer (a self-correction, revised belief,
material change in uncertainty/complexity, or handoff assessment). Completion,
turn boundaries, and automatic hook rows do not call for one.

~~~text
sync_state(
  response_text: "Your judgment and relevant work, concise and secret-free (500-char excerpt retained)",
  complexity: 0.0-1.0,   # Task difficulty estimate
  confidence: 0.0-1.0    # OPTIONAL — omit unless stating a genuine forecast; mints a prediction_id
)
~~~

- **Retry safety**: checkpoint acceptance is idempotent **only** when you supply
  `idempotency_key` and reuse the exact payload (`acceptance_retry_idempotent: true`);
  the legacy `state_persistence` assessment writer is non-idempotent.
- **Predictions**: if `sync_state` returns a `prediction_id`, thread that exact
  ID into `record_result(..., prediction_id="...")` when the outcome lands.
- **Identity refusals** return `refused: true`, `status`, `next_step`,
  `safe_options`, `do_not` with `success: true` — branch on `refused`/`status`,
  not `success is False`, and do not repeat the call.

### Epistemic classes (`epistemic_class`)

| class | who wrote it | means |
|---|---|---|
| `agent_report` | **you**, deliberately | you are stating something about your own work |
| `substrate_interpretation` | a hook, from turn/tool shape | the substrate describing what it observed you do |
| `substrate_observation` | host evidence (tool receipts, liveness) | a process fact; never EISV, progress, or intent |
| `prediction` | a forward-looking estimate | not an observation of the present |
| `synthetic` | lazy onboarding bootstrap | initialization, not a check-in |

Do not echo a hook's `substrate_interpretation` as an `agent_report`; only
`agent_report` speaks in your voice.

When `sync_state` returns `checkpoint.status: unconfirmed`,
`state_persistence.status: unconfirmed`, `PREDICTION_REUSE_CONFLICT`,
`calibration_excluded`, or `review_suggested`, read
`references/checkpoints-and-envelopes.md`
(remote MCP: `skills(name="governance-lifecycle", reference="checkpoints-and-envelopes.md")`).

## Closing a session

Before your final response, if you created actionable findings in this session,
review at most three still-open bugs, questions, or improvements owned by this
process (`knowledge(action="search", agent_id_filter="<your UUID>", status="open", limit=10, include_details=false, response_mode="lean")`).
Close addressed rows with `update_finding`, `closure_class`, and evidence (a
merged fix alone is not `fix_verified` until deployed and positively observed).
Skip the lookup when you wrote no actionable findings; see `commands/closeout.md`
and `skills/knowledge-graph/SKILL.md`.

## Reading Verdicts

| Verdict | What to Do |
|---------|-----------|
| **proceed / approve** | Continue normally |
| **proceed / guide** + guidance text | Read the guidance, adjust your approach, keep going |
| **proceed / guide**, raw recommendation `pause`, `enforcement.applied=false` | Observation only; no lifecycle hold, permission check, or mandatory review exists |
| **pause / reject**, `enforcement.posture=enforce`, `applied=false` | Compatibility directive only (no lifecycle hold committed); inspect enforcement or `self_recovery(action="check")` |
| **pause / reject**, `enforcement.applied=true` | Check-ins and new KG writes are refused before expiry; dialectic moves still work. A paused agent cannot check in to lower its own risk, and self-review cannot close its own pause — use Recovery below |
| **margin: tight** | Inside the band around `nearest_edge` (threshold distance, not basin); be careful with next steps |

## Recovery (Quick Reference)

| Situation | Tool | Notes |
|-----------|------|-------|
| Inspect recovery eligibility | `self_recovery(action="check")` | Read-only blockers, thresholds, and recommendations |
| Clearly safe self-resume | `self_recovery(action="quick")` | Requires low risk and no active void |
| Moderate state with reflection | `self_recovery(action="review", reflection="...")` | Requires genuine reflection; refuses at risk >= 0.65 |
| Disagree with verdict, want structured review | `request_review(issue_description="...")` | One-call request + thesis by default |
| Human/operator override | `operator_resume_agent(target_agent_id="...", reason="...")` | Operator-only; never resume your own pause via an operator path |

Authoritative recovery checks are risk, active void, status, ownership, and
reflection/persistence evidence — never legacy `C(V)`.

When an applied pause blocks writes, expires, returns `legacy_unbound_session`,
or `self_recovery(action="check")` reports blockers or `risk_authority`, read
`references/identity-and-recovery.md`
(remote MCP: `skills(name="governance-lifecycle", reference="identity-and-recovery.md")`).

## MCP Tools Overview

MCP, REST, and stdio start with a progressive tool surface. Use
`list_tools(lite=true)` for the full capability index, `describe_tool(tool_name=..., action=...)`
for full parameter text, and `use_tool(tool_name=..., arguments={...})` for
tools omitted from the initial listing.

- **Essential**: `start_session`, `sync_state`, `check_working_state`,
  `identity`, `health_check`, `search_shared_memory`, `store_finding`,
  `update_finding`, `knowledge(action="note")`, `self_recovery`
- **Common**: `knowledge`, `agent`, `calibration`, `consult`, `dialectic`,
  `export`
- **Specialized**: `observe`, `config`, `list_tools`, `describe_tool`,
  `use_tool` (and deprecated `call_model` — use `consult(effort="standard")`)
- **Active-call notices**: successful calls may carry `knowledge_changes`
  (up to 3 coalesced row pointers); read details before acting and acknowledge
  exact revision UUIDs via `knowledge(action="notices")`.

When discovering omitted tools, choosing between `consult` and `dialectic`,
checking `check_working_state` `unbound` behavior, or handling
`knowledge_changes`, read `references/tools-and-envelopes.md`
(remote MCP: `skills(name="governance-lifecycle", reference="tools-and-envelopes.md")`).

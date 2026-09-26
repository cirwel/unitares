---
name: governance-lifecycle
description: >
  Use when an agent is interacting with UNITARES governance for the first time, needs to
  onboard, check in, or recover from a pause/reject verdict. Covers the full agent lifecycle
  from session start through check-ins to recovery.
last_verified: "2026-09-24"
freshness_days: 14
source_files:
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
  - unitares/src/governance_monitor.py
  - unitares/src/monitor_calibration.py
  - unitares/src/mcp_handlers/updates/enrichments.py
  - unitares/src/mcp_handlers/dialectic/handlers.py
  - unitares/src/mcp_handlers/lifecycle/self_recovery.py
  - unitares/src/mcp_handlers/lifecycle/recovery_policy.py
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
---

# Agent Lifecycle

**Last Updated:** 2026-09-25

## Primary Workflow Names

The core lifecycle should use primary task-verb tools. Each is implemented by a raw tool with the same identity rules and returns a **normalized envelope** with the operationally useful fields first (`next_action`, `state_summary`, `risk_summary`, `memory_suggestions`, `recovery_hint`). Read aliases, bounded `sync_state` modes, and the write aliases `store_finding`, `update_finding` and `record_result` omit the repeated canonical payload and explain how to request it explicitly (`verbosity="full"` on `check_working_state`, `response_mode="full"` elsewhere, except as below for the finding writes); a plain fresh `start_session` omits it too (`response_shape: "routine"`, pass `response_mode="full"` on the mint to keep it); other state-changing aliases preserve it under `raw_governance`. Those three write acknowledgements keep the ids you need next (`discovery_id`, `state_summary.outcome_id`). The finding writes also carry `agent_uuid` and `written_as` (the writer's `agent_id`, `display_name` and assurance tier), so check it when you bound without an explicit `client_session_id`; `record_result` names its writer only when its binding was not server-inferred, so pass `client_session_id` if you need to see it. Their `raw_governance_hint` names what is still reachable. `record_result` has no read by outcome id: for an outcome recorded with a `prediction_id`, repeating the identical call with `response_mode="full"` returns the stored outcome in full (`idempotent_replay: true`) and records no second outcome while the binding is retained (a changed outcome is refused with `PREDICTION_REUSE_CONFLICT`); without a `prediction_id` a repeat records a second outcome, so the hint names `response_mode="full"` on a later `record_result`. For `store_finding` and `update_finding` it is a `knowledge(action="details", discovery_id=...)` read, which returns the stored record rather than the ack's payload. No read returns the omitted payload, so these acks do not set `raw_governance_available`. Write-time warnings and a bounded `related_discoveries` snapshot are kept in the ack itself because that read does not return the warnings or the snapshot's summary previews; the snapshot's ids are the stored record's `related_to`, so a `store_finding` ack carrying the snapshot does not repeat them as `state_summary.related_to`. Do not repeat a write just to see its payload: `store_finding` mints a new finding on every call. `sync_state` does not retrieve shared memory unless `include_memory_suggestions=true` is explicit.

| Task | Primary workflow tool | Raw implementation tool |
|------|---------------|----------------|
| Start a fresh process identity | `start_session(force_new=true, ...)` | `onboard` |
| Check in after meaningful work | `sync_state(response_text=..., complexity=...)` | `process_agent_update` |
| Check your working state | `check_working_state()` | `get_governance_metrics` |
| Avoid duplicate work | `search_shared_memory(query=...)` | `knowledge(action="search")` |
| Record what actually happened | `record_result(...)` | `outcome_event` |
| Ask for a structured review | `request_review(issue_description=...)` | `dialectic(action="request")` |
| Store a durable finding | `store_finding(summary=..., discovery_type=...)` | `knowledge(action="store")` |
| Update a durable finding | `update_finding(discovery_id=..., ...)` | `knowledge(action="update")` |

Use the primary workflow tools by default. Use raw implementation names only for older servers, compatibility code, or when you explicitly need the unwrapped handler response. `start_session(force_new=true)` is a process-start operation, not a per-turn continuation primitive. `request_review` reuses its `issue_description` as the thesis by default, so a lone review brief is actionable in one call. Pass explicit `reasoning`/`root_cause` to distinguish the position from the subject, or `use_brief_as_thesis=false` for the neutral two-call flow. Raw `dialectic(action="request")` remains two-call unless thesis fields or `use_brief_as_thesis=true` are supplied. Session reads carry plain-language `whose_move`/`next_call` guidance, plus `wait_assessment` for whether a wait is yet unusual — `too_early` there means unremarkable, never that the reviewer is known alive.

## Starting a Session

Choose creation, lineage, or proof-owned resume explicitly:

~~~text
start_session(force_new=true)                                        # one fresh process identity — the default; co-location is not lineage
start_session(force_new=true, parent_agent_id="<dispatcher-uuid>",
              spawn_reason="subagent")                               # dispatched subagent (usually set automatically by the dispatcher)
start_session(force_new=true, parent_agent_id="<prior-uuid>",
              spawn_reason="explicit")                               # deliberate handoff from an exited prior session
identity(agent_uuid="<uuid>", continuity_token="<token>", resume=true) # same live owner / proof-owned rebind
~~~

Declaring a currently-live agent as parent for a succession is rejected (`lineage_coincidental_rejected`): a live agent is then a concurrent sibling, not a predecessor. Registered dispatched-child reasons (`subagent`, internal `dialectic_reviewer`, and `dispatch`) plus the `compaction` continuation are exempt because their parent is legitimately live; unknown reasons receive no exemption. Use `explicit` for a deliberate handoff from an exited predecessor; the older `new_session` reason remains succession-shaped but does not, by itself, prove intentional lineage. A genuine handoff stays provisional until R1 confirms it. Continuing the same still-running process means reusing the active binding or `client_session_id`, not minting another child.

Use raw `onboard(...)` instead when targeting older servers or when you
need the unwrapped raw response.

Returns:
- **agent_uuid / UUID**: The server identity anchor for this process instance
- **client_session_id**: In-session transport continuity metadata
- **continuity_token**: Short-lived ownership proof for PATH 0 anti-hijack, not indefinite cross-process continuity
- **session diagnostics**: `session_resolution_source`, `identity_assurance`, and deprecation warnings when relevant

### Creation, lineage, and resume (updated 2026-04-25)

`name=` is a cosmetic label, not a resume key. Passing the same name on a later session does not prove identity.

Default rules:

1. Any fresh process: call `start_session(force_new=true)` with no parent. Co-location in a workspace is not lineage.
2. Declare lineage only for a real causal event — a dispatched subagent (`parent_agent_id="<dispatcher-uuid>", spawn_reason="subagent"`, usually set automatically by the dispatcher) or a deliberate handoff from an exited prior session (`parent_agent_id="<prior-uuid>", spawn_reason="explicit"`). Declaring a currently-live succession parent is rejected.
3. Same live process or explicit ownership rebind: call `identity(agent_uuid="<uuid>", continuity_token="<token>", resume=true)`.
4. Ordinary same-process check-ins: pass `client_session_id` (adapters may inject it). Bearer-token `/mcp/` with no session header keeps no binding between calls, and an OAuth client's binding is per client, shared across its conversations, not per process. Reserve `continuity_token` for explicit proof-owned rebinds.

Avoid these patterns:

- Bare `identity(agent_uuid=X, resume=true)`: UUID alone is an unsigned claim. It currently logs/emits hijack-suspected telemetry and is strict-mode rejected when `UNITARES_IDENTITY_STRICT=strict`.
- `onboard(continuity_token=...)` as cross-process resume: S1-c refuses it (`status: continuity_token_resume_rejected`); the S1-a deprecation window has closed. Declare lineage with `parent_agent_id` instead, or rebind the same live process with `identity(agent_uuid, continuity_token, resume=true)`.
- Bare `onboard()`: older code may still pin-resume by weak session/IP:UA evidence. Use `force_new=true` when creating a new process identity.

`continuity_token` is now intentionally narrow: 1-hour TTL, rolling, and retained as possession proof for anti-hijack gates. It does not establish process-instance continuity by itself.

## Check-ins

Call `sync_state()` after meaningful work:

~~~text
sync_state(
  response_text: "Brief summary of what you did",
  complexity: 0.0-1.0,   # Task difficulty estimate
  confidence: 0.0-1.0    # OPTIONAL — omit unless you are genuinely stating a
                         # belief about your own work. Any value you pass mints
                         # a tactical prediction that is scored into the fleet
                         # calibration curve, so a habitual number becomes a
                         # forecast nobody made. Omitting it mints nothing.
)
~~~

Use raw `process_agent_update(...)` when you need the raw handler payload, or
call `sync_state(..., response_mode="full")` to retain it under
`raw_governance` in the primary workflow response.

### When to Check In

- After completing a meaningful unit of work
- Before and after high-complexity tasks
- When you feel uncertain or notice drift
- **Not** after every single tool call — use judgment between these bounds

### What You Get Back

The friendly tools return a normalized envelope. Read `action_summary` when
present for the action, verdict, and evidence maturity, then `next_action`,
`state_summary`, `risk_summary`, `memory_suggestions`, and `recovery_hint` when
present. `check_working_state()` and `search_shared_memory()` omit the repeated
canonical payload by default; use `verbosity="full"` (alias `lite=false`) or
`response_mode="full"`, respectively, when you need it under `raw_governance`.
`check_working_state(verbosity="standard")` is the middle tier: EISV, verdict,
risk_score, basin and mode with their meanings, without the diagnostics. A
response marked `response_shape: "routine"` was trimmed because nothing in it
needed explaining: a clean `sync_state` proceed keeps `action_summary.action`,
`reason` and `risk_score` and the margin with its scope, but drops the repeated
approve/safe/healthy values, and a plain fresh `start_session` omits the onboard
record. Anything
unusual (a guide, a pause, a resume miss, a reactivated identity, a declared
lineage) keeps the full shape.

One response is deliberately **not** that envelope. When a call is refused for
identity, you get the typed refusal contract instead: `status`
(`identity_required` or `lineage_declaration_required`), `hint`, `next_step`,
`safe_options`, `do_not`, and `rollout_flag`. There is no `next_action` —
read `next_step` and `safe_options`. It carries `success: true`, because it is
a structured refusal rather than a transport error, so branching on
`success is False` will miss it; branch on `status` or `rollout_flag`. The target
tool handler did not run. Treat that as a no-handler-execution receipt, not a
blanket no-write receipt: resolver-failure paths may already have performed
identity-resolution bookkeeping. Follow `next_step` rather than retrying the
same call.

Cold-start action summaries carry a provisional headline. A `proceed` action
before the behavioral baseline forms is permission to continue under the current
policy, not a validated all-clear. The default state read preserves the verdict.

If you supplied a genuine `confidence`, the response may mint a concrete
`prediction_id`. Preserve that identifier and pass it to
`record_result(..., prediction_id="...")` when the outcome lands; otherwise the
outcome may grade an unrelated fallback prediction. The advertised id is the
check-in's own mint: evidence rows passed in `recent_tool_results` bind to
prediction ids of their own, which the reply never advertises (before
2026-09-17 it advertised the last evidence row's already-consumed id, so a
`record_result` with it was refused as `PREDICTION_REUSE_CONFLICT`). The `record_result`
`state_summary` says which happened: `prediction_binding` and
`prediction_source` name the prediction the outcome actually graded, and
`calibration_excluded` is true when the confidence was scraped rather than
bound, meaning the row does not train calibration. When
`UNITARES_REVIEW_NUDGE` is enabled, a warmed session can also receive a
once-per-session `review_suggested` nudge for low confidence, high complexity,
or a guide verdict. It is optional guidance, not a forced review.

### Your check-in is one evidence class among several

Rows in your state history are not all authored by you. Every row carries an
`epistemic_class`, and the distinction is load-bearing — it is what keeps the
substrate's observations about you separate from your own speech.

| class | who wrote it | means |
|---|---|---|
| `agent_report` | **you**, deliberately | you are stating something about your own work |
| `substrate_interpretation` | a hook, from turn/tool shape | the substrate describing what it observed you do |
| `substrate_observation` | host evidence (tool receipts, liveness) | a process fact; never EISV, progress, or intent |
| `prediction` | a forward-looking estimate | not an observation of the present |
| `synthetic` | lazy onboarding bootstrap | initialization, not a check-in |

Two rules follow, and both matter more than they look:

1. **Do not echo a hook's row as if it were yours.** If your harness writes a
   `substrate_interpretation` after each turn, that is the substrate's account,
   not a check-in you owe or should restate. Manufacturing an `agent_report` to
   match it is the failure this taxonomy exists to prevent.
2. **Only `agent_report` may speak in your voice.** When you *do* have something
   to say — a belief, an uncertainty, a judgment about your own state — that is
   the row only you can write, and it is the reason check-ins exist at all.

Hosts differ in what they automate: some write a per-turn interpretation for you,
some write almost nothing. Do not infer from a quiet history that you are being
watched less, or from a busy one that you have already reported.

## Reading Verdicts

| Verdict | What to Do |
|---------|-----------|
| **proceed / approve** | Continue normally |
| **proceed / guide** + guidance text | Read the guidance, adjust your approach, keep going |
| **pause / reject** | Check-ins and new shared-memory entries are refused (not queued); dialectic moves still work. Stop and read the `reason` and `guidance`. A paused agent's risk is frozen at the reading that paused it, so self-recovery rarely applies; a dialectic review opened for the pause, an operator, or re-evaluation at expiry usually ends it (see Recovery) |
| **margin: tight** | You are inside the band around a decision threshold — `nearest_edge` names which. This is a threshold distance, not a basin position. Be more careful with next steps |

A `guide` verdict is an early warning. Ignoring it makes `pause` more likely.

## Identity

- UUID is an identity anchor, not proof that the current process owns that identity
- Session binding can happen via a transport session signal the client sends (for example an `X-Session-ID` header set per process, never a static value shared across processes; stateless `/mcp/` issues no protocol session of its own), `client_session_id`, or short-lived continuity token
- Binding a transport session is explicit — `bind_session`, not a side effect of `identity()` — and it can be **refused**. When the destination key resolves from a store keyed on the User-Agent alone it may belong to another caller, so the response carries `bound: false` with `rebind_refused` naming the source. A destination that is another agent's stable `agent-...` session id (for example one sent in an `X-Session-ID` header) is refused the same way, as `rebind_refused: "foreign_stable_session_id"`, whichever transport header carried it. Your identity is unchanged; retry from a client that sends its own session identifier.
- When continuity seems unclear, call `identity(client_session_id="<your client_session_id>")`. Do not call it with no arguments: a call carrying no proof signal at all is gated to a fresh mint (`[FRESH_INSTANCE]`, S13), so it answers with a newly created identity rather than reporting on yours, and leaves a spurious record behind. The gate is what keeps the unauthenticated read off the User-Agent pin path; passing your own `client_session_id` is what makes the answer about you.
- Trust the answer only when `identity_assurance.caller_proven` is true; a `weak` tier with `proof_origin: "server_inferred"` means the server guessed.
- Inspect:
  - `identity_status`
  - `bound_identity`
  - `session_resolution_source`
  - `continuity_token_supported`
  - `identity_assurance`
  - `deprecations`

Strong ownership proof is better than implicit continuity. If a call in a process that already called `start_session` resolved only by weak signals such as fingerprinting, pass that process's `client_session_id` on the next call; minting again would split its work across two identities. Mint a fresh identity only for a new process, and declare lineage only from a finished predecessor.

## Recovery

When you are paused, stuck, or need intervention. A paused agent cannot write
the check-in that would lower its risk, so quick and review succeed only when
the reading that paused it is already under their gates; otherwise the dialectic
review opened for the pause, an operator, or re-evaluation at expiry ends it.

| Situation | Tool | Notes |
|-----------|------|-------|
| Inspect recovery eligibility | `self_recovery(action="check")` | Read-only blockers, thresholds, and recommendations |
| Clearly safe self-resume | `self_recovery(action="quick")` | Requires low risk and no active void |
| Moderate state with reflection | `self_recovery(action="review", reflection="...")` | Requires a genuine reflection; may accept conditions |
| Disagree with verdict, want structured review | `request_review(issue_description="...")` | One-call request + thesis by default; pass `use_brief_as_thesis=false` for a neutral two-call flow |
| Human/operator override | `operator_resume_agent(target_agent_id="...", reason="...")` | Operator-only. Refuses an active void or risk above 0.80 ("requires human intervention"), and needs `force=true` above 0.60. Never resume your own pause through an operator path |

Recovery is not a shortcut. Its authoritative checks are risk, active void, status,
ownership, and (for review recovery) reflection/persistence evidence. Legacy
`C(V)` remains visible with source/role provenance but cannot authorize or deny a
recovery. If the authoritative inputs are genuinely degraded, self-recovery will
not force a resume.

The read-only check separates `recovery_needed` from `eligible`. An active
identity reports `recovery_needed=false` and `recovery_status=not_needed`, even
before its first check-in; it does not need a recovery reflection. Inspect
`risk_authority` to distinguish an unmeasured first state from lost risk evidence.

## MCP Tools Reference

Interface contract 1.13.0 and later separates the complete negotiated catalog
from the initial transport advertisement. MCP, REST, and stdio begin with a
small progressive surface by default. `list_tools(lite=true)` returns the live
interface version, surface hash, and a name-only record for every complete
capability; `describe_tool(tool_name=..., action=...)` returns its parameters;
`use_tool(tool_name=..., arguments={...})` invokes a capability omitted from
the initial listing through its normal identity, validation, authorization,
routing, timeout, response, and telemetry paths. Here `lite` controls response
detail, not capability reachability. Use
`list_tools(lite=false, category=...)` to browse rich metadata.
Operators that require every schema up front can set
`UNITARES_TOOL_ADVERTISEMENT=full`. Legacy `GOVERNANCE_TOOL_MODE` settings are
ignored. Prefer primary workflow names; raw implementations remain callable
for compatibility.

Older servers may advertise a restricted profile. Inspect the client's actual
tool catalog and server instructions; do not assume a name is callable merely
because this skill mentions it. Upgrade the server for the complete catalog.

### Essential (use in every session)

- `start_session(force_new=true, parent_agent_id=...)` — Create a fresh process identity once, optionally declaring lineage
- `sync_state()` — Check in with work summary and complexity. Pass `confidence` **only when you are actually stating a belief about your own work**: the server mints a tactical prediction from any value supplied and scores it into the fleet calibration curve, so a habitual or placeholder number becomes a forecast nobody made. Omitting it mints nothing and costs nothing.
- `check_working_state()` — Read your current EISV state
- `identity(client_session_id=...)` — Confirm who the runtime thinks you are and how continuity was resolved; never call it with no arguments (see Identity above), and include `continuity_token` for proof-owned UUID rebinds
- `health_check()` — Check operator-facing server health when behavior seems odd; discover its schema and invoke it through `use_tool` under progressive advertisement
- `search_shared_memory(query=...)` — Find existing knowledge before creating new entries
- `store_finding(...)` — Store a durable discovery, root cause, or correction
- `update_finding(discovery_id=..., ...)` — Revise or close an existing finding
- `knowledge(action="note", ...)` — Quick contribution to the knowledge graph
- `self_recovery(action="check"|"quick"|"review")` — Get moving again after a pause while risk is below its gates (review refuses at risk 0.65 and above, except for the legacy cold-start trap). The check-in envelope names it for a pause below those gates and names the dialectic session above them. It is advertised by default so a schema-driven client can actually call it.

### Common (use when needed)

- `knowledge()` — Full knowledge graph CRUD, search, synthesis, and audit router
- `agent()` — Agent lifecycle router (list, get, update, archive, resume, delete)
- `calibration()` — Check or update calibration data
- `dialectic()` — Structured review router (`get`, `list`, `quick`, `request`, `thesis`, `antithesis`, `synthesis`, `reassign`). Advertised in the complete catalog: `request_review` pins `action="request"`, so without the router an older restricted-profile server could open a review while exposing none of the actions that finish one
- `export()` — Export session history

### Specialized

- `call_model()` — Delegate to a configured secondary model for analysis
- `observe()` — Read governance observations and fleet diagnostics
- `config()` — Read or change runtime thresholds; writes are privileged
- `list_tools()` / `describe_tool()` — Inspect the deployed catalog instead of guessing tool names. The default list is a name-only handshake; use `list_tools(lite=false)` for rich catalog metadata and `describe_tool()` for full action parameters. Available in the complete catalog; older servers may require their own discovery-profile configuration.
- `use_tool(tool_name=..., arguments={...})` — Invoke a complete-catalog capability omitted from the initial progressive `tools/list`; target middleware and authorization still apply

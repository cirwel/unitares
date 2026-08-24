---
name: governance-lifecycle
description: >
  Use when an agent is interacting with UNITARES governance for the first time, needs to
  onboard, check in, or recover from a pause/reject verdict. Covers the full agent lifecycle
  from session start through check-ins to recovery.
last_verified: "2026-08-21"
freshness_days: 14
source_files:
  - unitares/src/mcp_handlers/core.py
  - unitares/src/mcp_handlers/identity/handlers.py
  - unitares/src/mcp_handlers/admin/handlers.py
  - unitares/src/mcp_handlers/tool_stability.py
  - unitares/src/mcp_handlers/middleware/envelope_step.py
  # Added 2026-08-09: this skill documents check-in and dialectic semantics but
  # was not verified against the code implementing either. That is why stale
  # `confidence` guidance survived several freshness cycles — the field it was
  # wrong about lives in phases.py, which nobody was checking.
  - unitares/src/mcp_handlers/updates/phases.py
  - unitares/src/mcp_handlers/updates/enrichments.py
  - unitares/src/mcp_handlers/dialectic/handlers.py
  - unitares/src/mcp_handlers/lifecycle/self_recovery.py
  - unitares/src/mcp_handlers/lifecycle/recovery_policy.py
---

# Agent Lifecycle

**Last Updated:** 2026-08-17

## Primary Workflow Names

The core lifecycle should use primary task-verb tools. Each is implemented by a raw tool with the same identity rules and returns a **normalized envelope** with the operationally useful fields first (`next_action`, `state_summary`, `risk_summary`, `memory_suggestions`, `recovery_hint`). State-changing aliases preserve the full payload under `raw_governance`; read aliases default to a bounded lean/compact shape and explain how to request the full canonical payload. `sync_state` does not retrieve shared memory unless `include_memory_suggestions=true` is explicit.

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

Use the primary workflow tools by default. Use raw implementation names only for older servers, compatibility code, or when you explicitly need the unwrapped handler response. `start_session(force_new=true)` is a process-start operation, not a per-turn continuation primitive. `request_review` also supports a one-call form: pass `reasoning` (and optionally `root_cause`/`proposed_conditions`) and the thesis is submitted in the same call, with the response carrying plain-language `whose_move`/`next_call` guidance on every dialectic session read.

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
4. Ordinary same-process check-ins: rely on the active session binding or `client_session_id`; reserve `continuity_token` for explicit proof-owned rebinds.

Avoid these patterns:

- Bare `identity(agent_uuid=X, resume=true)`: UUID alone is an unsigned claim. It currently logs/emits hijack-suspected telemetry and is strict-mode rejected when `UNITARES_IDENTITY_STRICT=strict`.
- `onboard(continuity_token=...)` as cross-process resume: S1-a accepts this only during the deprecation window and returns a warning. Declare lineage with `parent_agent_id` instead.
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

Use raw `process_agent_update(...)` when you need the raw handler payload;
primary workflow responses preserve it under `raw_governance`.

### When to Check In

- After completing a meaningful unit of work
- Before and after high-complexity tasks
- When you feel uncertain or notice drift
- **Not** after every single tool call — use judgment between these bounds

### What You Get Back

The friendly tools return a normalized envelope. Read `next_action` first, then
`state_summary`, `risk_summary`, `memory_suggestions`, and `recovery_hint` when
present. `check_working_state()` and `search_shared_memory()` omit the repeated
canonical payload by default; use `lite=false` or `response_mode="full"`,
respectively, when you need it under `raw_governance`.

If you supplied a genuine `confidence`, the response may mint a concrete
`prediction_id`. Preserve that identifier and pass it to
`record_result(..., prediction_id="...")` when the outcome lands; otherwise the
outcome may grade an unrelated fallback prediction. When
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
| **pause / reject** | Stop your current task. Reflect on what is flagged. Consider requesting a dialectic review |
| **margin: tight** | You are inside the band around a decision threshold — `nearest_edge` names which. This is a threshold distance, not a basin position. Be more careful with next steps |

A `guide` verdict is an early warning. Ignoring it makes `pause` more likely.

## Identity

- UUID is an identity anchor, not proof that the current process owns that identity
- Session binding can happen via transport session, `client_session_id`, or short-lived continuity token
- Use `identity()` when continuity seems unclear
- Inspect:
  - `identity_status`
  - `bound_identity`
  - `session_resolution_source`
  - `continuity_token_supported`
  - `identity_assurance`
  - `deprecations`

Strong ownership proof is better than implicit continuity. If the runtime falls back to weak signals such as fingerprinting, mint a fresh process identity and declare lineage.

## Recovery

When you are paused, stuck, or need intervention:

| Situation | Tool | Notes |
|-----------|------|-------|
| Inspect recovery eligibility | `self_recovery(action="check")` | Read-only blockers, thresholds, and recommendations |
| Clearly safe self-resume | `self_recovery(action="quick")` | Requires low risk and no active void |
| Moderate state with reflection | `self_recovery(action="review", reflection="...")` | Requires a genuine reflection; may accept conditions |
| Disagree with verdict, want structured review | `request_review(issue_description="...", reasoning="...")` | One-call request + thesis, or omit thesis fields for the two-call flow |
| Human/operator override | `agent(action="resume", agent_id="...")` | Privileged lifecycle mutation; not ordinary self-recovery |

Recovery is not a shortcut. Its authoritative checks are risk, active void, status,
ownership, and (for review recovery) reflection/persistence evidence. Legacy
`C(V)` remains visible with source/role provenance but cannot authorize or deny a
recovery. If the authoritative inputs are genuinely degraded, self-recovery will
not force a resume.

## MCP Tools Reference

### Essential (use in every session)

- `start_session(force_new=true, parent_agent_id=...)` — Create a fresh process identity once, optionally declaring lineage
- `sync_state()` — Check in with work summary and complexity. Pass `confidence` **only when you are actually stating a belief about your own work**: the server mints a tactical prediction from any value supplied and scores it into the fleet calibration curve, so a habitual or placeholder number becomes a forecast nobody made. Omitting it mints nothing and costs nothing.
- `check_working_state()` — Read your current EISV state
- `identity()` — Confirm who the runtime thinks you are and how continuity was resolved; include `continuity_token` for proof-owned UUID rebinds
- `health_check()` — Check operator-facing server health when behavior seems odd
- `search_shared_memory(query=...)` — Find existing knowledge before creating new entries
- `store_finding(...)` — Store a durable discovery, root cause, or correction
- `update_finding(discovery_id=..., ...)` — Revise or close an existing finding
- `knowledge(action="note", ...)` — Quick contribution to the knowledge graph

### Common (use when needed)

- `knowledge()` — Full knowledge graph CRUD, search, synthesis, and audit router
- `agent()` — Agent lifecycle router (list, get, update, archive, resume, delete)
- `calibration()` — Check or update calibration data
- `dialectic()` — Structured review router (`get`, `list`, `quick`, `request`, `thesis`, `antithesis`, `synthesis`, `reassign`)
- `self_recovery()` — Recovery router (`check`, `quick`, `review`)
- `export()` — Export session history

### Specialized

- `call_model()` — Delegate to a configured secondary model for analysis
- `observe()` — Read governance observations and fleet diagnostics
- `config()` — Read or change runtime thresholds; writes are privileged
- `list_tools()` / `describe_tool()` — Inspect the deployed surface instead of guessing an old tool name

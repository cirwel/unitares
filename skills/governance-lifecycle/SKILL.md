---
name: governance-lifecycle
description: >
  Use when an agent is interacting with UNITARES governance for the first time, needs to
  onboard, check in, or recover from a pause/reject verdict. Covers the full agent lifecycle
  from session start through check-ins to recovery.
last_verified: "2026-09-09"
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
  # Added 2026-09-08: the reference now says the advertised parameter
  # descriptions are abridged and names describe_tool as where the full text
  # lives. The trim rule is here; if it changes, that claim drifts silently.
  - unitares/src/schema_brief.py
source_digests:
  unitares/src/mcp_handlers/core.py: "d7d09d260fedd7ec"
  unitares/src/mcp_handlers/identity/handlers.py: "c5bd71f4ab659d05"
  unitares/src/mcp_handlers/admin/handlers.py: "d7dec13e6a422b43"
  unitares/src/mcp_handlers/tool_stability.py: "430c2b2b206ecd99"
  unitares/src/mcp_handlers/middleware/envelope_step.py: "0327e6202ed5cbb4"
  unitares/src/mcp_handlers/updates/phases.py: "0c28700d12434e77"
  unitares/src/governance_monitor.py: "cecc4bde0de1c02b"
  unitares/src/monitor_calibration.py: "c99375f368dd98aa"
  unitares/src/mcp_handlers/updates/enrichments.py: "f91c10502c48275b"
  unitares/src/mcp_handlers/dialectic/handlers.py: "9d8b8ae1d271de80"
  unitares/src/mcp_handlers/lifecycle/self_recovery.py: "9bfffd3b09f6cc0f"
  unitares/src/mcp_handlers/lifecycle/recovery_policy.py: "3d108c675fb24421"
  unitares/src/tool_modes.py: "aa75ef30ee2c2383"
  unitares/src/tool_mode_listing.py: "e14ecf4249c3007b"
  unitares/src/schema_brief.py: "6463bc8ed3919816"
---

# Agent Lifecycle

**Last Updated:** 2026-09-08

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

Use the primary workflow tools by default. Use raw implementation names only for older servers, compatibility code, or when you explicitly need the unwrapped handler response. `start_session(force_new=true)` is a process-start operation, not a per-turn continuation primitive. `request_review` reuses its `issue_description` as the thesis by default, so a lone review brief is actionable in one call. Pass explicit `reasoning`/`root_cause` to distinguish the position from the subject, or `use_brief_as_thesis=false` for the neutral two-call flow. Raw `dialectic(action="request")` remains two-call unless thesis fields or `use_brief_as_thesis=true` are supplied. Session reads carry plain-language `whose_move`/`next_call` guidance.

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

The friendly tools return a normalized envelope. Read `action_summary` when
present for the action, verdict, and evidence maturity, then `next_action`,
`state_summary`, `risk_summary`, `memory_suggestions`, and `recovery_hint` when
present. `check_working_state()` and `search_shared_memory()` omit the repeated
canonical payload by default; use `lite=false` or `response_mode="full"`,
respectively, when you need it under `raw_governance`.

One response is deliberately **not** that envelope. When a call is refused for
identity, you get the typed refusal contract instead: `status`
(`identity_required` or `lineage_declaration_required`), `hint`, `next_step`,
`safe_options`, `do_not`, and `rollout_flag`. There is no `next_action` —
read `next_step` and `safe_options`. It carries `success: true`, because it is
a structured refusal rather than a transport error, so branching on
`success is False` will miss it; branch on `status` or `rollout_flag`. Nothing
was written. Follow `next_step` rather than retrying the same call.

Cold-start action summaries carry a provisional headline. A `proceed` action
before the behavioral baseline forms is permission to continue under the current
policy, not a validated all-clear. The default state read preserves the verdict.

If you supplied a genuine `confidence`, the response may mint a concrete
`prediction_id`. Preserve that identifier and pass it to
`record_result(..., prediction_id="...")` when the outcome lands; otherwise the
outcome may grade an unrelated fallback prediction. The `record_result`
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
| **pause / reject** | Stop your current task. Reflect on what is flagged. Consider requesting a dialectic review |
| **margin: tight** | You are inside the band around a decision threshold — `nearest_edge` names which. This is a threshold distance, not a basin position. Be more careful with next steps |

A `guide` verdict is an early warning. Ignoring it makes `pause` more likely.

## Identity

- UUID is an identity anchor, not proof that the current process owns that identity
- Session binding can happen via transport session, `client_session_id`, or short-lived continuity token
- Binding a transport session is explicit — `bind_session`, not a side effect of `identity()` — and it can be **refused**. When the destination key resolves from a store keyed on the User-Agent alone it may belong to another caller, so the response carries `bound: false` with `rebind_refused` naming the source. Your identity is unchanged; retry from a client that sends its own session identifier.
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
| Disagree with verdict, want structured review | `request_review(issue_description="...")` | One-call request + thesis by default; pass `use_brief_as_thesis=false` for a neutral two-call flow |
| Human/operator override | `agent(action="resume", agent_id="...")` | Privileged lifecycle mutation; not ordinary self-recovery |

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

Interface contract 1.6.0 and later exposes one complete catalog on MCP, REST,
and stdio, including installed plugin tools. No tool mode is needed; legacy
`GOVERNANCE_TOOL_MODE` settings are ignored. `list_tools(lite=true)` reports
the live interface version and surface hash. Here `lite` only controls response
detail. Use categories to browse and `describe_tool(tool_name=..., action=...)`
to inspect the parameters of one router action. Prefer primary workflow names;
raw implementations remain discoverable and callable for compatibility.
Authorization and identity gates still apply to each action.

Older servers may advertise a restricted profile. Inspect the client's actual
tool catalog and server instructions; do not assume a name is callable merely
because this skill mentions it. Upgrade the server for the complete catalog.

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
- `self_recovery(action="check"|"quick"|"review")` — Get moving again after a pause. The pause and auth-refusal responses name this tool by hand, and it is advertised by default so a schema-driven client can actually call it.

### Common (use when needed)

- `knowledge()` — Full knowledge graph CRUD, search, synthesis, and audit router
- `agent()` — Agent lifecycle router (list, get, update, archive, resume, delete)
- `calibration()` — Check or update calibration data
- `dialectic()` — Structured review router (`get`, `list`, `quick`, `request`, `thesis`, `antithesis`, `synthesis`, `reassign`). Advertised on the default `standard` since 2026-09-08: `request_review` pins `action="request"`, so without the router you could open a review and reach none of the actions that finish one
- `export()` — Export session history

### Specialized

- `call_model()` — Delegate to a configured secondary model for analysis
- `observe()` — Read governance observations and fleet diagnostics
- `config()` — Read or change runtime thresholds; writes are privileged
- `list_tools()` / `describe_tool()` — Inspect the deployed catalog and full action parameters instead of guessing tool names. Available in the complete catalog; older servers may require their own discovery-profile configuration.

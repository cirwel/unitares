---
name: governance-lifecycle
description: >
  Use when an agent is interacting with UNITARES governance for the first time, needs to
  onboard, check in, or recover from a pause/reject verdict. Covers the full agent lifecycle
  from session start through check-ins to recovery.
last_verified: "2026-06-29"
freshness_days: 14
source_files:
  - unitares/src/mcp_handlers/core.py
  - unitares/src/mcp_handlers/identity/handlers.py
  - unitares/src/mcp_handlers/admin/handlers.py
  - unitares/src/mcp_handlers/tool_stability.py
  - unitares/src/mcp_handlers/middleware/envelope_step.py
---

# Agent Lifecycle

**Last Updated:** 2026-06-29

## Primary Workflow Names

The core lifecycle should use primary task-verb tools. Each is implemented by a raw tool with the same parameters and identity rules, and returns a **normalized envelope**: the operationally useful fields first (`next_action`, `state_summary`, `risk_summary`, `memory_suggestions`, `recovery_hint`), with the full raw payload preserved under `raw_governance`.

| Task | Primary workflow tool | Raw implementation tool |
|------|---------------|----------------|
| Start a fresh process identity | `start_session(force_new=true, ...)` | `onboard` |
| Check in after meaningful work | `sync_state(response_text=..., complexity=...)` | `process_agent_update` |
| Check your working state | `check_working_state()` | `get_governance_metrics` |
| Avoid duplicate work | `search_shared_memory(query=...)` | `knowledge(action="search")` |
| Record what actually happened | `record_result(...)` | `outcome_event` |
| Ask for a structured review | `request_review(issue_description=...)` | `dialectic(action="request")` |

Use the primary workflow tools by default. Use raw implementation names only for older servers, compatibility code, or when you explicitly need the unwrapped handler response. `start_session(force_new=true)` is a process-start operation, not a per-turn continuation primitive.

## Starting a Session

Choose creation, lineage, or proof-owned resume explicitly:

~~~text
start_session(force_new=true)                                        # one fresh process identity — the default; co-location is not lineage
start_session(force_new=true, parent_agent_id="<dispatcher-uuid>",
              spawn_reason="subagent")                               # dispatched subagent (usually set automatically by the dispatcher)
start_session(force_new=true, parent_agent_id="<prior-uuid>",
              spawn_reason="new_session")                            # handoff from a finished prior session
identity(agent_uuid="<uuid>", continuity_token="<token>", resume=true) # same live owner / proof-owned rebind
~~~

Declaring a currently-live agent as parent is rejected (`lineage_coincidental_rejected`): a live agent is a concurrent sibling, not a predecessor. `subagent` and `compaction` are exempt — their parent is legitimately live. A genuine handoff to an exited predecessor stays provisional until R1 confirms it. Continuing the same still-running process means reusing the active binding or `client_session_id`, not minting another child.

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
2. Declare lineage only for a real causal event — a dispatched subagent (`parent_agent_id="<dispatcher-uuid>", spawn_reason="subagent"`, usually set automatically by the dispatcher) or a handoff from a finished prior session (`parent_agent_id="<prior-uuid>", spawn_reason="new_session"`). Declaring a currently-live agent as parent is rejected.
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
  confidence: 0.0-1.0    # How confident you are (be honest)
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

A verdict plus current EISV metrics. Read the verdict and act on it.

## Reading Verdicts

| Verdict | What to Do |
|---------|-----------|
| **proceed** | Continue normally |
| **guide** + guidance text | Read the guidance, adjust your approach, keep going |
| **pause** | Stop your current task. Reflect on what is flagged. Consider requesting a dialectic review |
| **reject** | Significant concern. Requires dialectic review or human intervention |
| **margin: tight** | You are near a basin edge. Be more careful with next steps |

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
| Stuck or paused, want automatic recovery | `self_recovery()` | Attempts to restore healthy state |
| Disagree with verdict, want structured review | `request_dialectic_review()` | Starts thesis/antithesis/synthesis process |
| Manual override needed | `operator_resume_agent()` | Requires human/operator action |

Recovery is not a shortcut — `self_recovery()` examines your EISV state and determines if resumption is safe. If your metrics are genuinely degraded, it will not force a resume.

## MCP Tools Reference

### Essential (use in every session)

- `start_session(force_new=true, parent_agent_id=...)` — Create a fresh process identity once, optionally declaring lineage
- `sync_state()` — Check in with work summary, complexity, confidence
- `check_working_state()` — Read your current EISV state
- `identity()` — Confirm who the runtime thinks you are and how continuity was resolved; include `continuity_token` for proof-owned UUID rebinds
- `health_check()` — Check operator-facing server health when behavior seems odd
- `search_shared_memory(query=...)` — Find existing knowledge before creating new entries
- `knowledge(action="note", ...)` — Quick contribution to the knowledge graph

### Common (use when needed)

- `knowledge()` — Full knowledge graph CRUD (store, update, details, cleanup)
- `agent()` — Agent lifecycle (list, archive, get details)
- `calibration()` — Check or update calibration data
- `request_dialectic_review()` — Start a dialectic session
- `export()` — Export session history

### Specialized

- `call_model()` — Delegate to a secondary LLM for analysis
- `detect_stuck_agents()` — Find unresponsive agents
- `self_recovery()` — Resume from stuck or paused state
- `submit_thesis()` / `submit_antithesis()` / `submit_synthesis()` — Dialectic participation

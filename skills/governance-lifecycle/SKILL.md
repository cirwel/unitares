---
name: governance-lifecycle
description: >
  Use when an agent is interacting with UNITARES governance for the first time, needs to
  onboard, check in, or recover from a pause/reject verdict. Covers the full agent lifecycle
  from session start through check-ins to recovery.
last_verified: "2026-04-25"
freshness_days: 14
source_files:
  - unitares/src/mcp_handlers/core.py
  - unitares/src/mcp_handlers/identity/handlers.py
  - unitares/src/mcp_handlers/admin/handlers.py
---

# Agent Lifecycle

**Last Updated:** 2026-05-01

## Starting a Session

Choose creation, lineage, or proof-owned resume explicitly:

~~~text
onboard(force_new=true)                                              # first run / fresh process
onboard(force_new=true, parent_agent_id="<prior-uuid>",
        spawn_reason="new_session")                                  # fresh process inheriting prior work
identity(agent_uuid="<uuid>", continuity_token="<token>", resume=true) # same live owner / proof-owned rebind
~~~

Returns:
- **UUID**: The server identity anchor for this process instance
- **client_session_id**: In-session transport continuity metadata
- **continuity_token**: Short-lived ownership proof for PATH 0 anti-hijack, not indefinite cross-process continuity
- **session diagnostics**: `session_resolution_source`, `identity_assurance`, and deprecation warnings when relevant

### Creation, lineage, and resume (updated 2026-04-25)

`name=` is a cosmetic label, not a resume key. Passing the same name on a later session does not prove identity.

Default rules:

1. Fresh first run: call `onboard(force_new=true)`.
2. New process continuing prior work: call `onboard(force_new=true, parent_agent_id="<prior-uuid>", spawn_reason="new_session")`.
3. Same live process or explicit ownership rebind: call `identity(agent_uuid="<uuid>", continuity_token="<token>", resume=true)`.
4. Ordinary check-ins: pass `continuity_token` when available, otherwise rely on the active session binding.

Avoid these patterns:

- Bare `identity(agent_uuid=X, resume=true)`: UUID alone is an unsigned claim. It currently logs/emits hijack-suspected telemetry and is strict-mode rejected when `UNITARES_IDENTITY_STRICT=strict`.
- `onboard(continuity_token=...)` as cross-process resume: S1-a accepts this only during the deprecation window and returns a warning. Declare lineage with `parent_agent_id` instead.
- Bare `onboard()`: older code may still pin-resume by weak session/IP:UA evidence. Use `force_new=true` when creating a new process identity.

`continuity_token` is now intentionally narrow: 1-hour TTL, rolling, and retained as possession proof for anti-hijack gates. It does not establish process-instance continuity by itself.

## Check-ins

Call `process_agent_update()` after meaningful work:

~~~text
process_agent_update(
  response_text: "Brief summary of what you did",
  complexity: 0.0-1.0,   # Task difficulty estimate
  confidence: 0.0-1.0    # How confident you are (be honest)
)
~~~

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

First run the Machine R.A.I.N. loop (`docs/operations/machine-rain-protocol.md`) when the trigger involves contradiction, stale data, failed validation, weak identity assurance, or surface conflict:

1. Register the concrete signal.
2. Allow the evidence to remain visible.
3. Investigate the canonical source.
4. Next: choose the smallest stabilizing action.

| Situation | Tool | Notes |
|-----------|------|-------|
| Stuck or paused, want automatic recovery | `self_recovery()` | Attempts to restore healthy state |
| Disagree with verdict, want structured review | `request_dialectic_review()` | Starts thesis/antithesis/synthesis process |
| Manual override needed | `operator_resume_agent()` | Requires human/operator action |

Recovery is not a shortcut — `self_recovery()` examines your EISV state and determines if resumption is safe. If your metrics are genuinely degraded, it will not force a resume.

## MCP Tools Reference

### Essential (use in every session)

- `onboard(force_new=true, parent_agent_id=...)` — Create a fresh process identity, optionally declaring lineage
- `process_agent_update()` — Check in with work summary, complexity, confidence
- `get_governance_metrics()` — Read your current EISV state
- `identity()` — Confirm who the runtime thinks you are and how continuity was resolved; include `continuity_token` for proof-owned UUID rebinds
- `health_check()` — Check operator-facing server health when behavior seems odd
- `knowledge(action="search", ...)` — Find existing knowledge before creating new entries
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

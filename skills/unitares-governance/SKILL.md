---
name: unitares-governance
description: >
  Compatibility umbrella skill for the UNITARES governance framework. Use this
  as the entrypoint when you need the overall model and route into the split
  governance skills.
last_verified: "2026-08-17"
freshness_days: 35
source_files:
  - unitares/src/mcp_handlers/core.py
  - unitares/src/mcp_handlers/identity/handlers.py
  - unitares/src/mcp_handlers/tool_stability.py
  - unitares/src/mcp_handlers/middleware/envelope_step.py
  - unitares/src/monitor_metrics.py
  - unitares/skills/governance-lifecycle/SKILL.md
  - unitares/skills/governance-fundamentals/SKILL.md
  - unitares/skills/knowledge-graph/SKILL.md
  - unitares/skills/dialectic-reasoning/SKILL.md
  - unitares/skills/discord-bridge/SKILL.md
---

# UNITARES Governance

This umbrella skill exists for backward compatibility and as a stable top-level
entrypoint into the UNITARES framework.

## Core Model

UNITARES evaluates agent state with the **EISV** model:

- `E`: effective energy / execution drive
- `I`: information integrity / calibration
- `S`: entropy / drift / instability
- `V`: valence (signed E-I imbalance)

EISV is proprioceptive state estimation, not an outcome oracle. The public
`coherence` field is producer-dependent; the deployed `legacy_tanh_v` value is
ODE control feedback. It still participates in configured check-in
compatibility backstops, but it is not behavioral health evidence and no longer
authorizes recovery.

Agents typically call `start_session(force_new=true)` once for a fresh process
identity, then continue that same running process with `sync_state()` as their
main check-in loop. A new user message is not a reason to call
`start_session(force_new=true)` again; that mints another process identity.
These are the primary workflow tools; raw implementation tools such as
`onboard(...)` and
`process_agent_update(...)` remain available for compatibility. The full raw
payload remains available under `raw_governance`; the read aliases
`check_working_state` and `search_shared_memory` default compact and require
their documented full-mode option to include it.

## Session Continuity

Use `start_session(force_new=true)` to register a fresh process identity once.
If the process is a deliberate handoff continuing an exited predecessor's work,
declare that with `parent_agent_id=<prior uuid>` and
`spawn_reason="explicit"`.
Use raw `onboard(...)` instead for older servers or raw response shape.

Use `identity(agent_uuid=..., continuity_token=..., resume=true)` only when
rebinding the same live owner to an existing UUID. The `continuity_token` is
short-lived ownership proof for anti-hijack gates, not indefinite
cross-process continuity. A bare `onboard()` or bare
`identity(agent_uuid=..., resume=true)` can rely on weak evidence or an
unsigned UUID claim; do not teach those as normal flow.

In-process tool calls thread the response's `client_session_id` through
subsequent invocations to maintain transport continuity within a single
process. Use that for ordinary `sync_state()` / `check_working_state()` calls.
`client_session_id` is in-session continuity only — weak across processes, not
identity proof on its own. Do not pass `continuity_token` on every call; reserve
it for explicit same-live-owner `identity(..., resume=true)` rebinds.

Use `sync_state()` after meaningful work to record progress and complexity, then
read `next_action`, `memory_suggestions`, and `recovery_hint` when present. Pass
`confidence` only when it is a real forecast; if the response returns a
`prediction_id`, thread that exact ID into `record_result(...)` when the outcome
lands. Use raw `process_agent_update()` when you need the unwrapped handler
response.

## Knowledge Layer

The governance system is coupled to the **knowledge graph**. Use
`search_shared_memory()` before duplicating work, `store_finding()` for durable
discoveries/root causes/corrections, and `update_finding()` to close the loop.
Task/tool/test outcomes belong in `record_result()`, not in the finding store.

## Split Skills

The old monolithic skill was split into focused skills:

- `skills/governance-lifecycle/SKILL.md` for onboarding, check-ins, and recovery
- `skills/governance-fundamentals/SKILL.md` for EISV, basins, coherence, and verdicts
- `skills/knowledge-graph/SKILL.md` for knowledge graph search and contribution
- `skills/dialectic-reasoning/SKILL.md` for thesis/antithesis/synthesis workflows
- `skills/discord-bridge/SKILL.md` for the Discord governance bridge
- `skills/unitares-dashboard/SKILL.md` for the buildless operator dashboard

If you need the full mental model, start here. If you know the task shape,
prefer the focused skill directly.

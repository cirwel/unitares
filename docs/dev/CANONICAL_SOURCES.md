# Canonical Sources

**Last Updated:** 2026-06-28 (re-verified: all listed runtime sources and thin-doc targets resolve)

Use this page to resolve architecture disputes and doc drift.

Status: canonical authority map for active docs.

## Trust Order

When prose and code disagree, use this order:

1. Runtime code that computes or returns the behavior
2. Prose docs explicitly marked live or canonical
3. Historical docs and archived analyses

This ordering exists because the codebase has accumulated multiple eras of explanation. Some older docs accurately describe earlier phases of the system but no longer describe the runtime that agents interact with today. If an agent reads archived prose before it reads runtime code, it can form a coherent but outdated model of the system. This page exists to prevent that failure mode and give both humans and agents a compact rule for resolving contradictions without guesswork.

## Current Architecture Truth

These files are the canonical runtime sources for current behavior:

| Topic | Canonical source | Why it matters |
|------|-------------------|----------------|
| Shared runtime state and monitor access | `src/agent_state.py`, `src/mcp_handlers/shared.py` | Defines the live singleton/facade that many handlers dereference |
| Core governance runtime | `src/governance_monitor.py` | Initializes dual-log grounding, behavioral state, ODE diagnostics, calibration hooks, and verdict flow |
| Dual-log grounding | `src/dual_log/continuity.py` | Cross-checks reflective inputs against operational signals and tool-derived complexity |
| Behavioral EISV state | `src/behavioral_state.py` | Defines warmup, bootstrap confidence, baselining, and self-relative assessment |
| Behavioral sensor inputs | `src/behavioral_sensor.py` | Shows which observable signals feed behavioral EISV |
| Public semantics returned to operators/agents | `src/services/runtime_queries.py` | Declares behavioral EISV primary, ODE diagnostic, and the surfaced state hierarchy |
| Calibration and objective outcomes | `src/calibration.py`, `src/auto_ground_truth.py` | Defines objective/exogenous calibration signals and confidence correction |

## How To Use This Page

Use this page differently depending on the task:

- If you are summarizing the system, read `README.md`, then `docs/UNIFIED_ARCHITECTURE.md`, then confirm the relevant claims in the runtime files listed above.
- If you are debugging a discrepancy between docs and behavior, skip straight to the runtime files and treat prose as secondary evidence.
- If you are changing architecture docs, update the relevant live doc and then verify that the runtime source still supports the wording.
- If you are changing runtime semantics, update this file only if the authority map or doc classifications have changed.

This page is not intended to duplicate the full architecture narrative. It is the index that tells you where truth lives and which docs are allowed to summarize that truth.

## Active Docs

These docs should stay aligned with the runtime sources above:

| Doc | Status | Intended use |
|-----|--------|--------------|
| `README.md` | live overview | Public-facing summary and top-level framing |
| `docs/UNIFIED_ARCHITECTURE.md` | canonical prose summary | Human-readable architecture explanation |
| `docs/guides/TROUBLESHOOTING.md` | live troubleshooting guide | Failure diagnosis and practical remediation |
| `docs/operations/OPERATOR_RUNBOOK.md` | live operator guide | Startup, health checks, and operator procedures |
| `docs/guides/START_HERE.md` | thin compatibility entrypoint | Minimal workflow and links outward; should stay short |
| `docs/operations/database_architecture.md` | thin infrastructure reference | Storage/backend facts only; should not restate runtime semantics |
| `docs/operations/DEFINITIVE_PORTS.md` | thin operational registry | Port assignments only; should stay small and factual |

## Specialized Active Docs

These are live but intentionally specialized. They should not be treated as general onboarding or architecture truth:

| Doc | Status | Intended use |
|-----|--------|--------------|
| `docs/guides/CIRS_PROTOCOL.md` | specialized protocol reference | CIRS-specific coordination flows |
| `docs/dev/CIRCUIT_BREAKER_DIALECTIC.md` | specialized recovery reference | Circuit-breaker and dialectic recovery flow |
| `docs/dev/KNOWLEDGE_GRAPH_SEMANTICS.md` | specialized developer reference | Shared-memory write/read, link, and audit semantics |
| `docs/dev/SESSION_KEY_DERIVATION.md` | specialized developer reference | Session-key resolution priority and proof-origin trust model |
| `docs/dev/TOOL_REGISTRATION.md` | specialized developer reference | MCP/tool registration work |

## Supporting Non-Canonical Artifacts

These are useful, but they are not runtime authority:

| Artifact | Status | Intended use |
|-----|--------|--------------|
| Paper / preprint snapshots | versioned research framing | Explain a dated architecture and deployment snapshot; not the live canonical system description |
| `docs/CHANGELOG.md` | release history | Track what changed over time; not a substitute for current runtime semantics |

## Known Stale-Risk Patterns

If you see these in active docs, treat them as drift candidates:

- "system operates on agent-reported inputs"
- descriptions implying self-report is the sole or primary substrate
- descriptions implying ODE state directly drives verdicts
- descriptions that omit dual-log grounding from the live architecture

Additional stale-risk patterns:

- long onboarding docs that quietly become second architecture manuals
- operational docs that start restating runtime semantics
- niche deployment guides that read like the default local path
- references to archived design language without an explicit historical label

## Maintenance Rules

Use these rules when deciding whether to edit, shrink, or add a doc:

1. If the content is broad and user-facing, prefer updating an existing live doc rather than creating a new one.
2. If the content is narrow and task-specific, mark it as specialized so agents do not treat it as default guidance.
3. If a doc mainly points to other docs, keep it thin and add an explicit status line saying so.
4. If a statement describes runtime behavior, verify it against the canonical source files before merging.
5. If a doc becomes historical, delete it or note its status inline. Do not preserve stale docs.

The goal is not to minimize documentation at all costs. The goal is to keep the active docs set small enough that agents can form the right model quickly, while still preserving specialized references for the narrower workflows that genuinely need them.

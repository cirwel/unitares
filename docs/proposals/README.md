# Proposals — RFC Index

Active and resolved RFCs that don't (yet) belong in [`docs/ontology/`](../ontology/README.md). **Each doc's body carries its own resolution status and is canonical — this index is a map.** Dated docs are point-in-time records and deliberately preserve references as they were at writing (the doc-health dead-ref check exempts this folder for that reason).

Several of these are **single-writer surfaces** (see the shared contract in `AGENTS.md` / `CLAUDE.md`): the hot Plexus / lease-plane / BEAM thread gets restructured in flight. If another session has an open PR touching one, branch from its head rather than starting a parallel edit.

## Active threads

### Plexus / surface lease plane

| Doc | Status (as of 2026-06-11) |
|---|---|
| [`plexus-scope.md`](plexus-scope.md) | Active boundary name over the live Surface Lease Plane; Plexus Zero retained as manual fallback |
| [`surface-lease-plane-v0.md`](surface-lease-plane-v0.md) | The lease-plane RFC, v0.11+. Phase A shipped 2026-05-03 (PR #305); Phase B promotion window opened 2026-05-16; `resident` enforcement shipped (PR #476) |
| [`surface-lease-plane-phase-a-plan.md`](surface-lease-plane-phase-a-plan.md) | COMPLETE — Phase A execution plan, shipped with PR #305 |
| [`worktree-isolation-vs-lease-default.md`](worktree-isolation-vs-lease-default.md) | v0.2 counter-note / companion to the lease-plane RFC (not a replacement) |
| [`lease-plane-phase-a-latency-2026-05-20.md`](lease-plane-phase-a-latency-2026-05-20.md) | First latency measurement anchoring the substrate-tax gate from the BEAM roadmap |

### BEAM footprint (substrate migration waves)

| Doc | Status (as of 2026-06-11) |
|---|---|
| [`beam-footprint-roadmap-v0.md`](beam-footprint-roadmap-v0.md) | v0.3 — destination A′ committed (operator decision 2026-05-05). Read the V0.3 RESOLUTION block first |
| [`beam-wave-1-sentinel.md`](beam-wave-1-sentinel.md) | v0.1.3 — Wave 1 Surface 1 cycle worker shipped (PR #376). Read the v0.1.3 amendment first |
| [`beam-wave-3-handler-dispatch.md`](beam-wave-3-handler-dispatch.md) | v0.3.2 — active redraft; supersedes v0.2/v0.1.x |
| [`beam-wave-3a-read-only-handlers.md`](beam-wave-3a-read-only-handlers.md) | v0.2 — council-fold complete; operator review pending |
| [`agent-orchestrator-beam-v0.md`](agent-orchestrator-beam-v0.md) | v0 thin slice — council-reviewed library + smoke, not merged to any running surface |
| [`wave-3-section-5-2-boundary-audit-summary.md`](wave-3-section-5-2-boundary-audit-summary.md) | CI-checkable §5.2 boundary-cost audit summary (2026-06-10), required before `elixir/handler_dispatch/` commits |

### Other active

| Doc | Status (as of 2026-06-14) |
|---|---|
| [`behavioral-running-hot-detector-v0.md`](behavioral-running-hot-detector-v0.md) | v0.1 plan, parked — pending council; unbuilt, blocked on the behavioral-EISV arm emitting signal |

## Shipped / resolved

| Doc | Resolution |
|---|---|
| [`onboard-bootstrap-checkin.md`](onboard-bootstrap-checkin.md) | SHIPPED — Phase 5 landed via PR #188 |
| [`onboard-bootstrap-checkin.filter-audit.md`](onboard-bootstrap-checkin.filter-audit.md) | SHIPPED — retained as historical control surface for the parent doc |
| [`refined-phase-5-evidence-contract.md`](refined-phase-5-evidence-contract.md) | SHIPPED — paired with `onboard-bootstrap-checkin.md` (PR #188) |
| [`path1-sync-fingerprint-check.md`](path1-sync-fingerprint-check.md) | SHIPPED — `sync_fingerprint` lives in `src/mcp_handlers/identity/shared.py` |
| [`s19-attestation-mechanism.md`](s19-attestation-mechanism.md) | Mechanism selection council-passed 2026-04-25; implementation correctness gated separately |
| [`section-129-measurement-fix-2026-06-03.md`](section-129-measurement-fix-2026-06-03.md) | Council-passed fix restoring the Wave 1 condition-1 measurement gate |

## Dated evaluation / measurement records

Point-in-time records; superseded analysis is preserved in place by design.

| Doc | What it captured |
|---|---|
| [`wave-0-step-2-call-site-scoping.md`](wave-0-step-2-call-site-scoping.md) | Coordination-failure call-site scoping (v0.3, post-2A-pivot; earlier prescriptions superseded by PR #345) |
| [`wave-1-window-evaluation-2026-05-18.md`](wave-1-window-evaluation-2026-05-18.md) | Wave 1 exit-condition evaluation of the T+0=2026-05-05 → T+13 window |
| [`wave-1-window-evaluation-T0-2026-05-19.md`](wave-1-window-evaluation-T0-2026-05-19.md) | Sibling re-anchor: next evaluation window under the prior doc's falsifier |
| [`ode-profile-decomposition-2026-05-20.md`](ode-profile-decomposition-2026-05-20.md) | ODE profile decomposition + persistence — the BEAM roadmap's load-bearing unknown |

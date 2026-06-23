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
| [`2026-06-24-wave-3-gate-framing.md`](2026-06-24-wave-3-gate-framing.md) | **Read first for the gate.** Framing note (2026-06-22) for the 2026-06-24 read — frames the one yes/no (real consumer for the inert orchestrator?) that resolves most of this cluster; records that the latency case is conceded dead |
| [`beam-footprint-roadmap-v0.md`](beam-footprint-roadmap-v0.md) | v0.3 — destination A′ committed (operator decision 2026-05-05). Read the V0.3 RESOLUTION block first |
| [`beam-wave-1-sentinel.md`](beam-wave-1-sentinel.md) | v0.1.3 — Wave 1 Surface 1 cycle worker shipped (PR #376). Read the v0.1.3 amendment first |
| [`beam-wave-3-handler-dispatch.md`](beam-wave-3-handler-dispatch.md) | v0.3.2 — active redraft; supersedes v0.2/v0.1.x |
| [`beam-wave-3a-read-only-handlers.md`](beam-wave-3a-read-only-handlers.md) | v0.2 — council-fold complete; operator review pending |
| [`agent-orchestrator-beam-v0.md`](agent-orchestrator-beam-v0.md) | v0 thin slice — council-reviewed library + smoke, not merged to any running surface |
| [`beam-governed-effects-dossier-2026-06-18.md`](beam-governed-effects-dossier-2026-06-18.md) | Draft dossier + phased plan — narrows current evidence to BEAM as dual-mode record/execute governed-effect runtime custody, not whole-governance rewrite |
| [`governed-effect-plane-v0.md`](governed-effect-plane-v0.md) | Draft v0.1 — Phase 2 protocol contract for the dossier (dual `custody_mode`, effect envelope, typed errors, idempotency/custody-TTL/payload holes closed); council-revised, first effect class = both (record_only shadow built in PR #866) |
| [`wave-3-section-5-2-boundary-audit-summary.md`](wave-3-section-5-2-boundary-audit-summary.md) | CI-checkable §5.2 boundary-cost audit summary (2026-06-10), required before `elixir/handler_dispatch/` commits |
| [`beam-proprioception-case-v0.md`](beam-proprioception-case-v0.md) | Draft v0.2 — conceptual companion behind A′ (council-folded). Epistemic claim: honest, provenance-tagged runtime introspection is privileged self-evidence (`external_signal`→`externally_verified`; #846 `harness_lane`); build governance on the layer that introspects honestly. Orthogonal to latency; non-relitigating; moves no boundary |

### Operator-vision delegation / identity hardening

The ADR-001 thread: do not enable operator-vision delegation as first proposed; instead land Track A (strict-identity hardening) before Track B (scoped `operator_delegate` disclosure). Read [`ADR-001`](ADR-001-operator-vision-delegation.md) first — it frames the other docs.

| Doc | Status |
|---|---|
| [`ADR-001-operator-vision-delegation.md`](ADR-001-operator-vision-delegation.md) | Accepted (2026-06-16) — do not enable as proposed; pursue Track A + Track B |
| [`track-a-strict-identity-hardening-runbook.md`](track-a-strict-identity-hardening-runbook.md) | Ready to execute — close the fingerprint-pin resume hole; prerequisite for any delegation |
| [`track-b-operator-delegate-design.md`](track-b-operator-delegate-design.md) | Proposal (design-first) — scoped `operator_delegate` read-only disclosure; do not implement before Track A is enforced |
| [`track-b-implementation-blueprint.md`](track-b-implementation-blueprint.md) | Ready to apply once Track A is enforced — implementation blueprint for the `operator_delegate` scope |
| [`lineage-causal-only-semantics.md`](lineage-causal-only-semantics.md) | DRAFT (operator-decided 2026-06-14) — parent-liveness discriminator; cited from `src/mcp_handlers/lifecycle/helpers.py` |
| [`uuid-keyed-identity-migration-v0.md`](uuid-keyed-identity-migration-v0.md) | v0 proposal / design-only (2026-06-14) — make the UUID the sole identity key, reconciling schema with the ontology |
| [`discord-thread-identity-resume-v0.md`](discord-thread-identity-resume-v0.md) | Reference decision record — Discord BEAM thread resume-per-thread plumbing; orchestrator + reference-hook side merged (#834), fail-closed/cross-repo follow-ups tracked separately |
| [`principal-rollup-v0.md`](principal-rollup-v0.md) | v0 proposal (2026-06-18) — count the **principal** (logical worker) not the process-instance; first-class form of identity.md research #3 ("identity as integral, not point-value"). Measurement shipped (`scripts/dev/octopus_rollup.py`); count/mint changes operator-gated. Sits atop `uuid-keyed-identity-migration` |
| [`orchestrator-vouched-identity-v0.md`](orchestrator-vouched-identity-v0.md) | DESIGN-FIRST RFC, council-reviewed 2026-06-17 — earn a genuine `strong` tier for orchestrated headless children (the deferred follow-on to resume-per-thread). Gate artifact for the 2026-06-24 Wave-3 read; no live cutover |

### Other active

| Doc | Status (as of 2026-06-14) |
|---|---|
| [`behavioral-running-hot-detector-v0.md`](behavioral-running-hot-detector-v0.md) | v0.1 plan, parked — pending council; unbuilt, blocked on the behavioral-EISV arm emitting signal |
| [`operator-decision-packet-v0.md`](operator-decision-packet-v0.md) | v1 design — making load-bearing taste/authority/irreversible calls cheap to answer (decision-packet output contract; council pass live, dialectic `ESCALATE`/`design_review` are latent unwired scaffolds). Council-passed to v1 2026-06-17; design-first, no code |
| [`mirror-effectiveness-measurement-v0.md`](mirror-effectiveness-measurement-v0.md) | Phases 0–1 landed (Phase 2 proposed) — deterministic, operator-funded-free measurement of whether a surfaced mirror signal changes agent behavior |
| [`hosted-multi-tenant-endpoint-v0.md`](hosted-multi-tenant-endpoint-v0.md) | Scoping / not committed — hosted governance endpoint decision doc; recommends isolated-per-adopter hosting first and defers true multi-tenant SaaS |
| [`harness-event-safety-policy-v0.md`](harness-event-safety-policy-v0.md) | Draft (2026-06-20) — cross-harness event envelope and fail-closed policy for synthetic/replayed/duplicate events before harness-specific implementation PRs |
| [`beam-event-adapter-design-v0.md`](beam-event-adapter-design-v0.md) | Design note (2026-06-20) — how BEAM residents/supervisors would populate the harness-event-safety envelope (PR #957); design-only, deferred to the 2026-06-24 Wave-3 gate read |
| [`monitor-delegated-liveness-v0.md`](monitor-delegated-liveness-v0.md) | v0 (2026-06-21) — design-only, **DO NOT BUILD YET.** Delegate process-liveness to the owning runtime monitor (OTP supervisor / `:DOWN`) instead of self-report heartbeat. Build-trigger = the agent-orchestrator de-inerting to become the live spawn path; zero live consumers today (`feasible ≠ needed`) |
| [`eisv-distributional-signal-probe-v0.md`](eisv-distributional-signal-probe-v0.md) | **Probe A run — KILL (2026-06-22).** Cheap falsifiable gate on the "make EISV distributional" work; dispersion shows no lift over the previous-outcome baseline (negative AUC delta), so the larger dynamics change is not greenlit. See the Run result block |

## Shipped / resolved

| Doc | Resolution |
|---|---|
| [`onboard-bootstrap-checkin.md`](onboard-bootstrap-checkin.md) | SHIPPED — Phase 5 landed via PR #188 |
| [`onboard-bootstrap-checkin.filter-audit.md`](onboard-bootstrap-checkin.filter-audit.md) | SHIPPED — retained as historical control surface for the parent doc |
| [`refined-phase-5-evidence-contract.md`](refined-phase-5-evidence-contract.md) | SHIPPED — paired with `onboard-bootstrap-checkin.md` (PR #188) |
| [`path1-sync-fingerprint-check.md`](path1-sync-fingerprint-check.md) | SHIPPED — `sync_fingerprint` lives in `src/mcp_handlers/identity/shared.py` |
| [`s19-attestation-mechanism.md`](s19-attestation-mechanism.md) | Mechanism selection council-passed 2026-04-25; implementation correctness gated separately |
| [`section-129-measurement-fix-2026-06-03.md`](section-129-measurement-fix-2026-06-03.md) | Council-passed fix restoring the Wave 1 condition-1 measurement gate |
| [`eisv-basin-health-gating-v0.md`](eisv-basin-health-gating-v0.md) | SHIPPED — PR #696 (issue #689), 2026-06-14; absolute-basin-health gating for self-relative risk, refined by #699 |
| [`dashboard-hero-severity-rollup.md`](dashboard-hero-severity-rollup.md) | SHIPPED (Phase 1) — PR #875; hero reflects all severity sources + "needs attention" band; `computeFleetSeverity` + 12 tests; verified live 2026-06-22 |

## Dated evaluation / measurement records

Point-in-time records; superseded analysis is preserved in place by design.

| Doc | What it captured |
|---|---|
| [`wave-0-step-2-call-site-scoping.md`](wave-0-step-2-call-site-scoping.md) | Coordination-failure call-site scoping (v0.3, post-2A-pivot; earlier prescriptions superseded by PR #345) |
| [`wave-1-window-evaluation-2026-05-18.md`](wave-1-window-evaluation-2026-05-18.md) | Wave 1 exit-condition evaluation of the T+0=2026-05-05 → T+13 window |
| [`wave-1-window-evaluation-T0-2026-05-19.md`](wave-1-window-evaluation-T0-2026-05-19.md) | Sibling re-anchor: next evaluation window under the prior doc's falsifier |
| [`ode-profile-decomposition-2026-05-20.md`](ode-profile-decomposition-2026-05-20.md) | ODE profile decomposition + persistence — the BEAM roadmap's load-bearing unknown |
| [`wave-1-completion-status-2026-06-14.md`](wave-1-completion-status-2026-06-14.md) | Read-only status roll-up across the Wave 1 surfaces + four exit conditions, consolidating the close decision into one ledger |
| [`wave-1-condition-2-alarm-parity-audit-2026-06-14.md`](wave-1-condition-2-alarm-parity-audit-2026-06-14.md) | Alarm-rule parity audit (BEAM vs Python Sentinel) for Wave 1 exit condition 2 |

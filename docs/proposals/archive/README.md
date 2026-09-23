# Proposal archive

Completed, parked, superseded, negative-result and dated records. **Archive placement does not reject an idea, retire a capability, or override a status or stop rule.** Parked work remains available to resume under its stated gates.

[Proposal guide](../README.md) · [Migration inventory](../../dev/proposals-layout-2347.json)

## Plexus / surface lease plane

| Document | Status |
|---|---|
| [`lease-lifecycle-declaration-v0.md`](lease-lifecycle-declaration-v0.md) | **Closed** · **REFUTED as written.** The permanent-strand diagnosis stands; the proposed TTL-only fix would break execution exclusion. Retained as a negative design record and prerequisite warning for any fence/lifecycle follow-up |
| [`lease-plane-phase-a-latency-2026-05-20.md`](lease-plane-phase-a-latency-2026-05-20.md) | **Closed** · First latency measurement anchoring the substrate-tax gate from the BEAM roadmap |

## BEAM footprint (substrate migration waves)

| Document | Status |
|---|---|
| [`2026-06-24-wave-3-gate-framing.md`](2026-06-24-wave-3-gate-framing.md) | **Closed** · **Read first for the gate.** Framing note (2026-06-22) — two separate decisions: (A) freeze the orchestrator cluster (demand empty) vs (B) Wave-3 dispatch on its own merits. Measured 2026-06-22: p50 floor closed, but p99 coordination tail LIVE (`process_agent_update` p99 4740ms, math ~1.3%) |
| [`beam-governed-effects-dossier-2026-06-18.md`](beam-governed-effects-dossier-2026-06-18.md) | **Parked (since 2026-06-19)** · Draft dossier + phased plan — narrows current evidence to BEAM as dual-mode record/execute governed-effect runtime custody, not whole-governance rewrite |
| [`beam-proprioception-case-v0.md`](beam-proprioception-case-v0.md) | **Parked (since 2026-06-19)** · Draft v0.2 — conceptual companion behind A′ (council-folded). Epistemic claim: honest, provenance-tagged runtime introspection is privileged self-evidence (`external_signal`→`externally_verified`; #846 `harness_lane`); build governance on the layer that introspects honestly. Orthogonal to latency; non-relitigating; moves no boundary |
| [`wave-3-go-decision-2026-08-16.md`](wave-3-go-decision-2026-08-16.md) | **Closed** · SIGNED 2026-08-22 — GO-WITH-REDUCED-SCOPE. The decision record under which Wave 3 now proceeds |
| [`beam-wave-3-gamma-hybrid-v0.md`](beam-wave-3-gamma-hybrid-v0.md) | **Closed** · v0 wide cut REJECTED (§0a); the (γ) narrow cut was set aside 2026-06-28 per the handler-dispatch RFC. Retained as a negative design record |

## Operator-vision delegation / identity hardening

| Document | Status |
|---|---|
| [`ADR-001-operator-vision-delegation.md`](ADR-001-operator-vision-delegation.md) | **Closed** · Accepted (2026-06-16) — do not enable as proposed; pursue Track A + Track B |
| [`track-a-strict-identity-hardening-runbook.md`](track-a-strict-identity-hardening-runbook.md) | **Parked (since 2026-06-17)** · Ready to execute — close the fingerprint-pin resume hole; prerequisite for any delegation. Re-read 2026-09-23: the maintainer deployment runs `UNITARES_IDENTITY_STRICT=strict` with the fingerprint gate deliberately left at `log`; code defaults remain `log`, so the runbook's "confirmed current state" is stale for that deployment |
| [`track-b-operator-delegate-design.md`](track-b-operator-delegate-design.md) | **Parked (since 2026-06-16)** · Proposal (design-first) — scoped `operator_delegate` read-only disclosure; do not implement before Track A is enforced |
| [`track-b-implementation-blueprint.md`](track-b-implementation-blueprint.md) | **Parked (since 2026-06-28)** · Ready to apply once Track A is enforced — implementation blueprint for the `operator_delegate` scope |
| [`uuid-keyed-identity-migration-v0.md`](uuid-keyed-identity-migration-v0.md) | **Parked (since 2026-06-30)** · v0 proposal / design-only (2026-06-14; council amendment 2026-06-30) — make the UUID the sole identity key, reconciling schema with the ontology. The 2026-06-30 simplification council ranked it the single architectural lever (root cause of the resolver band-aids) but **lowered urgency**: near-zero write-accountability blast radius today, BEAM may re-key it for free, Wave-3 gate still closed — hold at Phase 0, don't race BEAM |
| [`discord-thread-identity-resume-v0.md`](discord-thread-identity-resume-v0.md) | **Built** · Reference decision record — Discord BEAM thread resume-per-thread plumbing; orchestrator + reference-hook side merged (#834), fail-closed/cross-repo follow-ups tracked separately |
| [`orchestrator-vouched-identity-v0.md`](orchestrator-vouched-identity-v0.md) | **Parked (since 2026-06-28)** · DESIGN-FIRST RFC, council-reviewed 2026-06-17 — earn a genuine `strong` tier for orchestrated headless children (the deferred follow-on to resume-per-thread). Gate artifact for the 2026-06-24 Wave-3 read; no live cutover |
| [`genesis-baseline-aging-v0.md`](genesis-baseline-aging-v0.md) | **Parked (since 2026-06-29)** · Open question / design sketch (2026-06-30) — **no decision, no code change.** Surfaces template-aging risk against immutable-genesis-at-tier-2 (`store_genesis_signature`); recommends measure-first via R1 shadow-mode, then spike dual-anchor (immutable origin + bounded rolling reference) only if decay is real. Anti-laundering tension stated explicitly. From `docs/ontology/trajectory-identity-prior-art-2026-06.md` |
| [`agent-identity-credential-aic-v0.md`](agent-identity-credential-aic-v0.md) | **Parked (since 2026-06-24)** · Prototype + design draft (2026-06-24); not wired into the live identity path |

## Other records

| Document | Status |
|---|---|
| [`behavioral-running-hot-detector-v0.md`](behavioral-running-hot-detector-v0.md) | **Parked (since 2026-06-14)** · v0.1 plan, parked — pending council; unbuilt, blocked on the behavioral-EISV arm emitting signal |
| [`continuous-verdict-blending-v0.md`](continuous-verdict-blending-v0.md) | **Parked (since 2026-06-27)** · v0.2 council-corrected design note — do not implement v0 blend as written; primary fix is verdict-gate hysteresis/dead-band |
| [`operator-decision-packet-v0.md`](operator-decision-packet-v0.md) | **Parked (since 2026-07-01)** · v1 design — making load-bearing taste/authority/irreversible calls cheap to answer (decision-packet output contract; review pass live, dialectic `ESCALATE`/`design_review` are latent unwired scaffolds). Reviewed to v1 2026-06-17; design-first, no code |
| [`hosted-multi-tenant-endpoint-v0.md`](hosted-multi-tenant-endpoint-v0.md) | **Parked (since 2026-06-18)** · Scoping / not committed — hosted governance endpoint decision doc; recommends isolated-per-adopter hosting first and defers true multi-tenant SaaS |
| [`harness-event-safety-policy-v0.md`](harness-event-safety-policy-v0.md) | **Parked (since 2026-06-20)** · Draft (2026-06-20) — cross-harness event envelope and fail-closed policy for synthetic/replayed/duplicate events before harness-specific implementation PRs |
| [`beam-event-adapter-design-v0.md`](beam-event-adapter-design-v0.md) | **Parked (since 2026-06-28)** · Design note (2026-06-20) — how BEAM residents/supervisors would populate the harness-event-safety envelope (PR #957); design-only, deferred to the 2026-06-24 Wave-3 gate read |
| [`monitor-delegated-liveness-v0.md`](monitor-delegated-liveness-v0.md) | **Parked (since 2026-06-21)** · v0 (2026-06-21) — design-only, **DO NOT BUILD YET.** Delegate process-liveness to the owning runtime monitor (OTP supervisor / `:DOWN`) instead of self-report heartbeat. Build-trigger = the agent-orchestrator de-inerting to become the live spawn path; zero live consumers today (`feasible ≠ needed`). Re-read 2026-09-23: the orchestrator now runs as a service and orchestrated review is on in the maintainer deployment, so the build trigger should be re-read; nobody has re-read it yet |
| [`harness-registry-v0.md`](harness-registry-v0.md) | **Parked (since 2026-06-28)** · v0 (2026-06-28) — design-only, **DO NOT BUILD YET.** Authoritative catalog of harness *types* (not identity; instances stay observed in the census). Resolves the type-vs-instance open question by splitting declared-type authority from observed-instance telemetry. Build-trigger = harness-census evidence (PR #1153) crosses the §6 promotion thresholds; conforms to plan.md Track D |
| [`bridge-dispatch-v0.md`](bridge-dispatch-v0.md) | **Parked (since 2026-08-01)** · v0 draft (2026-08-01), pre-review and not an implementation gate — move the operator from transport bottleneck to evidence-backed exception handler |
| [`thread-trajectory-stitching-v0.md`](thread-trajectory-stitching-v0.md) | **Parked (since 2026-06-29)** · v0 proposal, demoted to a metrics-layer backstop — keep genuine cross-instance deaths legible without forging identity continuity |
| [`relational-calibration-maturity-capacity-v0.md`](relational-calibration-maturity-capacity-v0.md) | **Closed** · Immutable v0 capacity preregistration, superseded for protocol v0.2; retained as design history and not a current implementation gate |
| [`accountable-testbed-preliminary-trace.md`](accountable-testbed-preliminary-trace.md) | **Closed** · Preliminary deployed-system trace exercising the federation primitives; explicitly not a multi-host or multi-organization result |
| [`eisv-effort-profile-channel-v0.md`](eisv-effort-profile-channel-v0.md) | **Closed** · SEPARATED AND REFUTED 2026-08-26 (see the doc's status block) — written as a reopening premise for the outcome-grounding stop rule; retained as a negative design record |
| [`outcome-fixture-conflation-decision-packet-v0.md`](outcome-fixture-conflation-decision-packet-v0.md) | **Closed** · **Decision packet (2026-09-02), resolved by delegated selection.** A row whose confidence the server had to scrape is stamped `calibration_excluded`, and that flag is also a standalone fixture marker, so the discrimination instruments (ablation matrix, skeptic report, coherence dependency shadow) dropped every instrument-visible trusted `external_signal` row written after the frozen 2026-08-09 cutoff (951 of 951 at the 2026-09-02 read; rows posted with a confidence, or through `record_result` with a resolvable prediction, are not stamped). One fork for the operator: what the registered 2026-12-01 read does with those rows (run as registered with a pre-declared sensitivity cohort, correct prospectively, correct retroactively, or re-register), plus two engineering items that need no decision. Council- and Codex-reviewed; **R1 selected 2026-09-02** under the operator's delegation ("best for federation"); E1/E2 and the pre-declared sensitivity cohort shipped in PR #2062; the follow-ups (corrected default for non-protocol instruments, protocol manifest, coherence-shadow v0.1) were decided in governed session `e4ebf589a1c79b9d` |
| [`governed-effect-convergence-v0.md`](governed-effect-convergence-v0.md) | **Closed** · DECISION RECORDED 2026-06-28 — unite the governed-effect tracks; supersedes the split design |
| [`stakes-keyed-gating-775.md`](stakes-keyed-gating-775.md) | **Built (dormant)** · Classification artifact landed + inert; the gate mechanism is parked |
| [`redis-retirement-v0.md`](redis-retirement-v0.md) | **Closed** · Scoping draft whose central claim was REFUTED by live verification 2026-06-27 and corrected in place; Redis remains the de-facto primary session store (Stack section of the shared contract) |

## EISV maths, coherence, and outcome grounding

| Document | Status |
|---|---|
| [`substrate-portability-checkin-v0.md`](substrate-portability-checkin-v0.md) | **Built** · Canaries only; changes no math |
| [`eisv-individuality-v2-preregistration.md`](eisv-individuality-v2-preregistration.md) | **Closed** · PRE-REGISTERED 2026-07-02 and executed on schedule; consumed by the result row below |
| [`eisv-individuality-v2-result.md`](eisv-individuality-v2-result.md) | **Closed** · Registered verdict FAIL; inference status UNTESTED AS DEPLOYED. The individuality axiom is retired for raw behavioral EISV as currently measured; a further attempt must change the measurement and pre-register before any of its data exists |

## Shipped / resolved

| Document | Status |
|---|---|
| [`onboard-bootstrap-checkin.md`](onboard-bootstrap-checkin.md) | SHIPPED — Phase 5 landed via PR #188 |
| [`onboard-bootstrap-checkin.filter-audit.md`](onboard-bootstrap-checkin.filter-audit.md) | SHIPPED — retained as historical control surface for the parent doc |
| [`refined-phase-5-evidence-contract.md`](refined-phase-5-evidence-contract.md) | SHIPPED — paired with `onboard-bootstrap-checkin.md` (PR #188) |
| [`path1-sync-fingerprint-check.md`](path1-sync-fingerprint-check.md) | SHIPPED — `sync_fingerprint` lives in `src/mcp_handlers/identity/shared.py` |
| [`s19-attestation-mechanism.md`](s19-attestation-mechanism.md) | Mechanism selection council-passed 2026-04-25; implementation correctness gated separately |
| [`section-129-measurement-fix-2026-06-03.md`](section-129-measurement-fix-2026-06-03.md) | Council-passed fix restoring the Wave 1 condition-1 measurement gate |
| [`eisv-basin-health-gating-v0.md`](eisv-basin-health-gating-v0.md) | SHIPPED — PR #696 (issue #689), 2026-06-14; absolute-basin-health gating for self-relative risk, refined by #699 |
| [`dashboard-hero-severity-rollup.md`](dashboard-hero-severity-rollup.md) | SHIPPED (Phase 1) — PR #875; hero reflects all severity sources + "needs attention" band; `computeFleetSeverity` + 12 tests; verified live 2026-06-22. The code was removed the next day when #1012 retired the classic dashboard |
| [`docs-consolidation-v0.md`](docs-consolidation-v0.md) | SHIPPED — contested-claim lint, audience-split index, shorter README, and thin compatibility/manual routes landed by 2026-08-11 |
| [`beam-wave-1-sentinel.md`](beam-wave-1-sentinel.md) | SHIPPED — Wave 1 executed RFC; active follow-on work belongs to later waves; compatibility stub retained at the old path |
| [`beam-wave-3a-read-only-handlers.md`](beam-wave-3a-read-only-handlers.md) | DEPLOYED — Wave 3a read-only listener execution record; compatibility stub retained at the old path |

## Closed by negative result

| Document | Status |
|---|---|
| [`eisv-distributional-signal-probe-v0.md`](eisv-distributional-signal-probe-v0.md) | **Probe A did not greenlight the build (2026-06-22); KILL inference withdrawn 2026-08-22.** The objective scope could not exercise the probe and the task-scope point estimate does not identify the observation-versus-representation bottleneck. See the correction and Run result blocks. |

## Dated evaluation / measurement / lifecycle records

| Document | Status |
|---|---|
| [`wave-0-step-2-call-site-scoping.md`](wave-0-step-2-call-site-scoping.md) | Coordination-failure call-site scoping (v0.3, post-2A-pivot; earlier prescriptions superseded by PR #345) |
| [`wave-1-window-evaluation-2026-05-18.md`](wave-1-window-evaluation-2026-05-18.md) | Wave 1 exit-condition evaluation of the T+0=2026-05-05 → T+13 window |
| [`wave-1-window-evaluation-T0-2026-05-19.md`](wave-1-window-evaluation-T0-2026-05-19.md) | Sibling re-anchor: next evaluation window under the prior doc's falsifier |
| [`ode-profile-decomposition-2026-05-20.md`](ode-profile-decomposition-2026-05-20.md) | ODE profile decomposition + persistence — the BEAM roadmap's load-bearing unknown |
| [`wave-1-completion-status-2026-06-14.md`](wave-1-completion-status-2026-06-14.md) | Read-only status roll-up across the Wave 1 surfaces + four exit conditions, consolidating the close decision into one ledger |
| [`wave-1-condition-2-alarm-parity-audit-2026-06-14.md`](wave-1-condition-2-alarm-parity-audit-2026-06-14.md) | Alarm-rule parity audit (BEAM vs Python Sentinel) for Wave 1 exit condition 2 |
| [`demotion-review-2026-08-16.md`](demotion-review-2026-08-16.md) | Lifecycle review of all 20 issue #1605 advisory candidates, including explicit reasons for every retained current contract or active proposal |

## Supporting evidence

| Artifact | What it preserves |
|---|---|
| [`accountable-testbed-federation-trace.json`](accountable-testbed-federation-trace.json) | Machine-readable companion to the preliminary testbed trace. |

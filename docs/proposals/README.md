# Proposals — RFC Index

Active and resolved RFCs that don't (yet) belong in [`docs/ontology/`](../ontology/README.md). **Each doc's body carries its own resolution status and is canonical — this index is a map.** Dated docs are point-in-time records and deliberately preserve references as they were at writing (the doc-health dead-ref check exempts this folder for that reason).

Several of these are **single-writer surfaces** (see the shared contract in `AGENTS.md` / `CLAUDE.md`): the hot Plexus / lease-plane / BEAM thread gets restructured in flight. If another session has an open PR touching one, branch from its head rather than starting a parallel edit.

## Disposition at a glance (2026-09-03)

Every row in the thread tables below now leads with one of five dispositions, so
"what is alive here" has a one-word answer per doc. The doc's own status line stays
canonical; the tag is a reading of it under the rule stated here, and a wrong tag is
fixed by editing the row. The thread tables keep their grouping by subject, so a
thread can hold Closed and Parked rows beside Active ones.

| Tag | Rule |
|---|---|
| **Built** | The status line says shipped, implemented, landed, or wired, in whole or in part. *(dormant)* = built but flag-off or unwired; *(partial)* = a named phase shipped and the rest did not. |
| **Registered** | A frozen or pre-registered protocol. Its stop rule binds the analyst, not the feature; it is never re-run, refreshed, or weakened. |
| **Active** | Design or measurement work touched by a commit in the 30 days before 2026-09-03, or named by a signed gate as in progress. |
| **Parked** | Design-only or deferred by its own status line and untouched since before 2026-08-04. The date is the last commit that touched the doc. |
| **Closed** | A recorded decision, a refutation, a superseded draft, a negative result, or a dated record. Retained as provenance. |

The 30-day line is a choice, not a measurement: it separates "someone is working
this" from "nobody has touched this" and claims nothing else. Counts at tagging:
Built 21 · Registered 7 · Active 21 · Parked 24 · Closed 14
(top-level docs; the `resolved/` subfolder is not re-tagged).

## Active threads

### Plexus / surface lease plane

| Doc | Status |
|---|---|
| [`plexus-scope.md`](plexus-scope.md) | **Built** · Active boundary name over the live Surface Lease Plane; Plexus Zero retained as manual fallback |
| [`surface-lease-plane-v0.md`](surface-lease-plane-v0.md) | **Built (partial)** · The lease-plane RFC, v0.11+. Phase A shipped 2026-05-03 (PR #305); Phase B promotion window opened 2026-05-16; `resident` enforcement shipped (PR #476) |
| [`surface-lease-plane-phase-a-plan.md`](surface-lease-plane-phase-a-plan.md) | **Built** · COMPLETE — Phase A execution plan, shipped with PR #305; retained here as the current PR-by-PR sequencing ledger cited by the BEAM roadmap and coordination kernel |
| [`worktree-isolation-vs-lease-default.md`](worktree-isolation-vs-lease-default.md) | **Parked (since 2026-06-28)** · v0.2 counter-note / companion to the lease-plane RFC (not a replacement) |
| [`lease-lifecycle-declaration-v0.md`](lease-lifecycle-declaration-v0.md) | **Closed** · **REFUTED as written.** The permanent-strand diagnosis stands; the proposed TTL-only fix would break execution exclusion. Retained as a negative design record and prerequisite warning for any fence/lifecycle follow-up |
| [`lease-plane-phase-a-latency-2026-05-20.md`](lease-plane-phase-a-latency-2026-05-20.md) | **Closed** · First latency measurement anchoring the substrate-tax gate from the BEAM roadmap |

### BEAM footprint (substrate migration waves)

| Doc | Status |
|---|---|
| [`2026-06-24-wave-3-gate-framing.md`](2026-06-24-wave-3-gate-framing.md) | **Closed** · **Read first for the gate.** Framing note (2026-06-22) — two separate decisions: (A) freeze the orchestrator cluster (demand empty) vs (B) Wave-3 dispatch on its own merits. Measured 2026-06-22: p50 floor closed, but p99 coordination tail LIVE (`process_agent_update` p99 4740ms, math ~1.3%) |
| [`beam-footprint-roadmap-v0.md`](beam-footprint-roadmap-v0.md) | **Active** · v0.4 — destination A′ committed (operator decision 2026-05-05); Wave 3 committed to proceed 2026-06-25 and signed GO-WITH-REDUCED-SCOPE 2026-08-22 (go-decision row below). Read the V0.4 RESOLUTION block first |
| [`beam-wave-3-handler-dispatch.md`](beam-wave-3-handler-dispatch.md) | **Active** · COMMITTED AND OPEN, no active implementation — the commitment stands (V0.4, 2026-06-25); the (γ) narrow cut at `process_agent_update` was set aside 2026-06-28. Read the V0.5 STATUS CORRECTION and V0.6 SCOPE blocks before resuming |
| [`agent-channel-wake-gate-v0.md`](agent-channel-wake-gate-v0.md) | **Active** · v0 — UNSIGNED disconfirmer gate for cross-vendor agent-channel wake; explicitly NOT under Wave 3's authorization |
| [`agent-orchestrator-beam-v0.md`](agent-orchestrator-beam-v0.md) | **Built (dormant)** · v0 thin slice — council-reviewed library + smoke, not merged to any running surface |
| [`beam-governed-effects-dossier-2026-06-18.md`](beam-governed-effects-dossier-2026-06-18.md) | **Parked (since 2026-06-19)** · Draft dossier + phased plan — narrows current evidence to BEAM as dual-mode record/execute governed-effect runtime custody, not whole-governance rewrite |
| [`governed-effect-plane-v0.md`](governed-effect-plane-v0.md) | **Built (partial)** · Draft v0.3 — Phase-4 readiness; the Phase 2 protocol contract for the dossier (dual `custody_mode`, effect envelope, typed errors, idempotency/custody-TTL/payload holes closed). The record_only shadow is built (PR #866); the execute half is not cleared |
| [`wave-3-section-5-2-boundary-audit-summary.md`](wave-3-section-5-2-boundary-audit-summary.md) | **Built** · CI-checkable §5.2 boundary-cost audit summary (2026-06-10), required before `elixir/handler_dispatch/` commits |
| [`beam-proprioception-case-v0.md`](beam-proprioception-case-v0.md) | **Parked (since 2026-06-19)** · Draft v0.2 — conceptual companion behind A′ (council-folded). Epistemic claim: honest, provenance-tagged runtime introspection is privileged self-evidence (`external_signal`→`externally_verified`; #846 `harness_lane`); build governance on the layer that introspects honestly. Orthogonal to latency; non-relitigating; moves no boundary |
| [`beam-verbs-as-contract-capabilities-v0.md`](beam-verbs-as-contract-capabilities-v0.md) | **Active** · Draft v0 (2026-08-28), design-only, two adversarial passes folded, no peer review — which lease-plane verbs should become agent-facing capabilities in `unitares.interface-contract.v1` and which must not. Candidates: `msg/send` first, `msg/inbox` after ack/redelivery, `lease/status` after response scoping, lease mutations conditional; effects + force-release never. **Operator decisions 2026-08-29:** tool-usage accounting settled (capability calls get their own key, never the tool aggregate); `msg` proceeds now, `lease` deferred not cancelled. **Still blocked on:** `msg` idempotency/ack/retry semantics (§8, now the largest open item; §8a records a concurrency partition neither original candidate fixes, §8b adds a pure-read cursor candidate), plus strict BEAM-side lease ownership + status redaction and activity provenance for the deferred lease slice |
| [`attestation-issuance-scope-v0.md`](attestation-issuance-scope-v0.md) | **Active** · Draft v0 (2026-08-28), design-only, **unreviewed**; §4 model **ratified by operator 2026-08-29** — companion to the BEAM-verbs RFC, closing its §9 issuance-scope prerequisite. Finds that `validate_path` is the sole containment on a deliberate assurance exemption (`_mint_presence_attestation` skips `recertify_strong_tier` to avoid an onboarding bootstrap loop), so a global widening would extend that exemption to `/v1/msg/*`. Proposes per-mint-site scope as exact `(method, path)` pairs. No code |
| [`wave-3-go-decision-2026-08-16.md`](wave-3-go-decision-2026-08-16.md) | **Closed** · SIGNED 2026-08-22 — GO-WITH-REDUCED-SCOPE. The decision record under which Wave 3 now proceeds |
| [`wave-3-reduced-scope-gate-v0.md`](wave-3-reduced-scope-gate-v0.md) | **Active** · PROPOSED, unratified as a gate — the reduced-scope conditions under which the signed GO proceeds; §6.3 names the calendar items whose slip halts Wave 3 outright |
| [`beam-wave-3-gamma-hybrid-v0.md`](beam-wave-3-gamma-hybrid-v0.md) | **Closed** · v0 wide cut REJECTED (§0a); the (γ) narrow cut was set aside 2026-06-28 per the handler-dispatch RFC. Retained as a negative design record |

### Operator-vision delegation / identity hardening

The ADR-001 thread: do not enable operator-vision delegation as first proposed; instead land Track A (strict-identity hardening) before Track B (scoped `operator_delegate` disclosure). Read [`ADR-001`](ADR-001-operator-vision-delegation.md) first — it frames the other docs.

| Doc | Status |
|---|---|
| [`ADR-001-operator-vision-delegation.md`](ADR-001-operator-vision-delegation.md) | **Closed** · Accepted (2026-06-16) — do not enable as proposed; pursue Track A + Track B |
| [`fleet-workload-identity-auth-audit-v0.md`](fleet-workload-identity-auth-audit-v0.md) | **Active** · Draft v0.2 (2026-08-24) - council-ratified and Claude-reviewed threat model/Lease Plane pilot specification; v0.2 adds OS-isolated workload bootstrap, canonical proofs, fixed replay/audit semantics, off-state parity tests, availability/capacity gates, and break-glass recovery; live auth remains blocked |
| [`cedar-delegation-authz-v0.md`](cedar-delegation-authz-v0.md) | **Active** · Draft v0 (2026-08-21) — decision pending, deferred-by-default. Cedar as the family's shared policy engine (pre-dispatch authorize step + the governed-effect plane's unbuilt §6 veto), principals from explicit delegation carriers only (operator token, vouched bindings — never lineage), #1387 `(tool, action)` as the action vocabulary, `stakes_table.py` as seed policy, shadow-mode first |
| [`track-a-strict-identity-hardening-runbook.md`](track-a-strict-identity-hardening-runbook.md) | **Parked (since 2026-06-17)** · Ready to execute — close the fingerprint-pin resume hole; prerequisite for any delegation |
| [`track-b-operator-delegate-design.md`](track-b-operator-delegate-design.md) | **Parked (since 2026-06-16)** · Proposal (design-first) — scoped `operator_delegate` read-only disclosure; do not implement before Track A is enforced |
| [`track-b-implementation-blueprint.md`](track-b-implementation-blueprint.md) | **Parked (since 2026-06-28)** · Ready to apply once Track A is enforced — implementation blueprint for the `operator_delegate` scope |
| [`lineage-causal-only-semantics.md`](lineage-causal-only-semantics.md) | **Built** · IMPLEMENTED — the declaration-time parent-liveness gate shipped (see the doc's As-built section); cited from `src/mcp_handlers/lifecycle/helpers.py` |
| [`uuid-keyed-identity-migration-v0.md`](uuid-keyed-identity-migration-v0.md) | **Parked (since 2026-06-30)** · v0 proposal / design-only (2026-06-14; council amendment 2026-06-30) — make the UUID the sole identity key, reconciling schema with the ontology. The 2026-06-30 simplification council ranked it the single architectural lever (root cause of the resolver band-aids) but **lowered urgency**: near-zero write-accountability blast radius today, BEAM may re-key it for free, Wave-3 gate still closed — hold at Phase 0, don't race BEAM |
| [`discord-thread-identity-resume-v0.md`](discord-thread-identity-resume-v0.md) | **Built** · Reference decision record — Discord BEAM thread resume-per-thread plumbing; orchestrator + reference-hook side merged (#834), fail-closed/cross-repo follow-ups tracked separately |
| [`principal-rollup-v0.md`](principal-rollup-v0.md) | **Built (partial)** · v0 proposal (2026-06-18) — count the **principal** (logical worker) not the process-instance; first-class form of identity.md research #3 ("identity as integral, not point-value"). Measurement shipped (`scripts/dev/octopus_rollup.py`); count/mint changes operator-gated. Sits atop `uuid-keyed-identity-migration` |
| [`orchestrator-vouched-identity-v0.md`](orchestrator-vouched-identity-v0.md) | **Parked (since 2026-06-28)** · DESIGN-FIRST RFC, council-reviewed 2026-06-17 — earn a genuine `strong` tier for orchestrated headless children (the deferred follow-on to resume-per-thread). Gate artifact for the 2026-06-24 Wave-3 read; no live cutover |
| [`genesis-baseline-aging-v0.md`](genesis-baseline-aging-v0.md) | **Parked (since 2026-06-29)** · Open question / design sketch (2026-06-30) — **no decision, no code change.** Surfaces template-aging risk against immutable-genesis-at-tier-2 (`store_genesis_signature`); recommends measure-first via R1 shadow-mode, then spike dual-anchor (immutable origin + bounded rolling reference) only if decay is real. Anti-laundering tension stated explicitly. From `docs/ontology/trajectory-identity-prior-art-2026-06.md` |
| [`agent-identity-credential-aic-v0.md`](agent-identity-credential-aic-v0.md) | **Parked (since 2026-06-24)** · Prototype + design draft (2026-06-24); not wired into the live identity path |

### Other active

| Doc | Status |
|---|---|
| [`behavioral-running-hot-detector-v0.md`](behavioral-running-hot-detector-v0.md) | **Parked (since 2026-06-14)** · v0.1 plan, parked — pending council; unbuilt, blocked on the behavioral-EISV arm emitting signal |
| [`continuous-verdict-blending-v0.md`](continuous-verdict-blending-v0.md) | **Parked (since 2026-06-27)** · v0.2 council-corrected design note — do not implement v0 blend as written; primary fix is verdict-gate hysteresis/dead-band |
| [`operator-decision-packet-v0.md`](operator-decision-packet-v0.md) | **Parked (since 2026-07-01)** · v1 design — making load-bearing taste/authority/irreversible calls cheap to answer (decision-packet output contract; review pass live, dialectic `ESCALATE`/`design_review` are latent unwired scaffolds). Reviewed to v1 2026-06-17; design-first, no code |
| [`mirror-effectiveness-measurement-v0.md`](mirror-effectiveness-measurement-v0.md) | **Built (partial)** · Phases 0–1 landed (Phase 2 proposed) — deterministic, operator-funded-free measurement of whether a surfaced mirror signal changes agent behavior |
| [`kg-agent-adoption-pilot-v0.md`](kg-agent-adoption-pilot-v0.md) | **Active** · DRAFT / HOLD — offline fixture independently reviewed; production-plugin probe found the pinned root outside top five for five of six frozen queries, the one audit row required read-only decoder recovery, delayed auto-checkin falsified durable canary isolation, and live parity, scored runs, orchestration promotion, and live actuators remain unauthorized |
| [`hosted-multi-tenant-endpoint-v0.md`](hosted-multi-tenant-endpoint-v0.md) | **Parked (since 2026-06-18)** · Scoping / not committed — hosted governance endpoint decision doc; recommends isolated-per-adopter hosting first and defers true multi-tenant SaaS |
| [`inference-delegation-capability-registry-v0.md`](inference-delegation-capability-registry-v0.md) | **Parked (since 2026-06-29)** · v0 scoping proposal - design-first capability registry + provenance envelope for local, hosted, and operator-authorized subscription-backed inference delegation; recommends Phase 1 registry/provenance before Codex/Claude adapters |
| [`harness-event-safety-policy-v0.md`](harness-event-safety-policy-v0.md) | **Parked (since 2026-06-20)** · Draft (2026-06-20) — cross-harness event envelope and fail-closed policy for synthetic/replayed/duplicate events before harness-specific implementation PRs |
| [`beam-event-adapter-design-v0.md`](beam-event-adapter-design-v0.md) | **Parked (since 2026-06-28)** · Design note (2026-06-20) — how BEAM residents/supervisors would populate the harness-event-safety envelope (PR #957); design-only, deferred to the 2026-06-24 Wave-3 gate read |
| [`monitor-delegated-liveness-v0.md`](monitor-delegated-liveness-v0.md) | **Parked (since 2026-06-21)** · v0 (2026-06-21) — design-only, **DO NOT BUILD YET.** Delegate process-liveness to the owning runtime monitor (OTP supervisor / `:DOWN`) instead of self-report heartbeat. Build-trigger = the agent-orchestrator de-inerting to become the live spawn path; zero live consumers today (`feasible ≠ needed`) |
| [`verification-weighted-verdict-v0.md`](verification-weighted-verdict-v0.md) | **Built (dormant)** · v0 (2026-06-28) — Phases 1/1.5/2 landed: deterministic escalate-only detector (`governance_core/verification.py`) + local-model/Ollama backend (`src/verification_backend.py`) + opt-in eval harness + **default-off** actuator wiring (`apply_verification_floor`, `GOVERNANCE_VERIFICATION_FLOOR`); separates the self-report-dependence worked example 0.0 vs 0.96 and flips flag-on sabotage to pause. **Enabling the flag is council-gated.** Honors the one-sided Φ-floor constraint |
| [`governed-effect-s7-strong-tier-recert.md`](governed-effect-s7-strong-tier-recert.md) | **Built** · Design v0.2, council-folded — strong-tier re-certification gate for governed-effect `execute agent_spawn`; implementation landed separately in the governed-effect track |
| [`tool-surface-legibility-v0.md`](tool-surface-legibility-v0.md) | **Active** · Draft v0 (2026-08-29), design-only, unreviewed — what an agent sees at selection time: a lintable description contract (routing first line; deep lore moves to `describe_tool` detail), discriminator lines for the inference and shared-memory clusters, and a session-start orientation map. Builds on the friendly-verb promotion + #1994 and the consult facade; no removals, no renames, identity-surface edits deferred to that coupled surface |
| [`harness-registry-v0.md`](harness-registry-v0.md) | **Parked (since 2026-06-28)** · v0 (2026-06-28) — design-only, **DO NOT BUILD YET.** Authoritative catalog of harness *types* (not identity; instances stay observed in the census). Resolves the type-vs-instance open question by splitting declared-type authority from observed-instance telemetry. Build-trigger = harness-census evidence (PR #1153) crosses the §6 promotion thresholds; conforms to plan.md Track D |
| [`orchestrated-dialectic-reviewer-v0.md`](orchestrated-dialectic-reviewer-v0.md) | **Built (dormant)** · Implemented behind opt-in gates — standalone reviewer, governed-first spawn path, model-derived `agrees` including `False`, local/Codex/Claude backend routing, fallback behavior and provenance tests are present; operator rollout is a separate step from merge |
| [`bridge-dispatch-v0.md`](bridge-dispatch-v0.md) | **Parked (since 2026-08-01)** · v0 draft (2026-08-01), pre-review and not an implementation gate — move the operator from transport bottleneck to evidence-backed exception handler |
| [`thread-trajectory-stitching-v0.md`](thread-trajectory-stitching-v0.md) | **Parked (since 2026-06-29)** · v0 proposal, demoted to a metrics-layer backstop — keep genuine cross-instance deaths legible without forging identity continuity |
| [`relational-calibration-pilot-v0.md`](relational-calibration-pilot-v0.md) | **Registered** · v0.2 specification and adversarial threat model only — adds temporal/instrument validity, experimental-principal accounting, dyadic inference, and a frozen exposure/horizon contract; runtime collection remains explicitly blocked |
| [`relational-calibration-maturity-capacity-v0.md`](relational-calibration-maturity-capacity-v0.md) | **Closed** · Immutable v0 capacity preregistration, superseded for protocol v0.2; retained as design history and not a current implementation gate |
| [`relational-calibration-maturity-capacity-v1.md`](relational-calibration-maturity-capacity-v1.md) | **Registered** · Frozen, one-time aggregate instrument-supply read with temporal and same-row consistency gates; process UUID counts explicitly do not establish participant or federation capacity |
| [`accountable-testbed-metrics-preregistration-v0.md`](accountable-testbed-metrics-preregistration-v0.md) | **Registered** · Frozen evaluation pre-registration for the accountable multi-principal testbed |
| [`accountable-testbed-metrics-preregistration-v1.md`](accountable-testbed-metrics-preregistration-v1.md) | **Registered** · Frozen v1.1 evaluation contract for future headline, ablation, and scale-sweep runs; the document merge did not execute those runs |
| [`accountable-testbed-preliminary-trace.md`](accountable-testbed-preliminary-trace.md) | **Closed** · Preliminary deployed-system trace exercising the federation primitives; explicitly not a multi-host or multi-organization result |
| [`dialectic-resolution-receipt-v0.md`](dialectic-resolution-receipt-v0.md) | **Built (dormant)** · Wired and dormant: deployment-countersigned dialectic resolution record (Ed25519, `drr.v1`) a peer verifies offline with the pinned public key; mints only when an attestation key exists, and names the custody, key-history and second-principal preconditions before enabling is honest; issuer-level, never party-level, non-repudiation |
| [`orientation-constraint-set-preregistration-v0.md`](orientation-constraint-set-preregistration-v0.md) | **Registered** · Frozen protocol candidate for a paired, information-matched test of a temporary read-only diagnostic constraint set; no durable self-schema or runtime surface is authorized |
| [`eisv-effort-profile-channel-v0.md`](eisv-effort-profile-channel-v0.md) | **Closed** · SEPARATED AND REFUTED 2026-08-26 (see the doc's status block) — written as a reopening premise for the outcome-grounding stop rule; retained as a negative design record |
| [`outcome-fixture-conflation-decision-packet-v0.md`](outcome-fixture-conflation-decision-packet-v0.md) | **Closed** · **Decision packet (2026-09-02), resolved by delegated selection.** A row whose confidence the server had to scrape is stamped `calibration_excluded`, and that flag is also a standalone fixture marker, so the discrimination instruments (ablation matrix, skeptic report, coherence dependency shadow) dropped every instrument-visible trusted `external_signal` row written after the frozen 2026-08-09 cutoff (951 of 951 at the 2026-09-02 read; rows posted with a confidence, or through `record_result` with a resolvable prediction, are not stamped). One fork for the operator: what the registered 2026-12-01 read does with those rows (run as registered with a pre-declared sensitivity cohort, correct prospectively, correct retroactively, or re-register), plus two engineering items that need no decision. Council- and Codex-reviewed; **R1 selected 2026-09-02** under the operator's delegation ("best for federation"); E1/E2 and the pre-declared sensitivity cohort shipped in PR #2062; the follow-ups (corrected default for non-protocol instruments, protocol manifest, coherence-shadow v0.1) were decided in governed session `e4ebf589a1c79b9d` |
| [`agent-message-transport-v0.md`](agent-message-transport-v0.md) | **Built (dormant)** · Implemented, not deployed — migration 069 is unapplied on the maintainer deployment |
| [`consult-advisory-facade-v1.md`](consult-advisory-facade-v1.md) | **Active** · Implementation candidate for the consult advisory facade (2026-08-29) |
| [`governed-effect-convergence-v0.md`](governed-effect-convergence-v0.md) | **Closed** · DECISION RECORDED 2026-06-28 — unite the governed-effect tracks; supersedes the split design |
| [`governed-effect-unitares-profile-v0.md`](governed-effect-unitares-profile-v0.md) | **Built** · Profile + runtime mapper implemented; companion to the governed-effect plane contract |
| [`governed-effect-effect-binding-v0.md`](governed-effect-effect-binding-v0.md) | **Parked (since 2026-06-28)** · Design v0.2 — per-effect authorization, successor to the §7 strong-tier re-certification; demand-gated, build only when its §8 trigger fires |
| [`governed-reviewer-spawn-v0.md`](governed-reviewer-spawn-v0.md) | **Built (dormant)** · Built, inert (flag off); activation is an operator step (§Activation) |
| [`stakes-keyed-gating-775.md`](stakes-keyed-gating-775.md) | **Built (dormant)** · Classification artifact landed + inert; the gate mechanism is parked |
| [`redis-retirement-v0.md`](redis-retirement-v0.md) | **Closed** · Scoping draft whose central claim was REFUTED by live verification 2026-06-27 and corrected in place; Redis remains the de-facto primary session store (Stack section of the shared contract) |
| [`redis-retirement-phase-1-plan.md`](redis-retirement-phase-1-plan.md) | **Parked (since 2026-07-01)** · Implementation plan v1.1 (revised 2026-06-27); not applied |

### EISV maths, coherence, and outcome grounding

The measurement thread: derivations, ablations, and the registered reads. The stop-rule and shadow-contract rows are pre-registered instruments and are exempt from the usage-count rules in the shared contract; `unitares-eisv-maths` is the working discipline for touching any of them.

| Doc | Status |
|---|---|
| [`eisv-maths-roadmap-v0.md`](eisv-maths-roadmap-v0.md) | **Active** · Design-intent roadmap (not a change); captures the direction, each step lands separately |
| [`eisv-grounding-next-move-v0.md`](eisv-grounding-next-move-v0.md) | **Active** · Design-intent roadmap for the next grounding move (not a change) |
| [`eisv-general-solution-v0.md`](eisv-general-solution-v0.md) | **Active** · Derivation + numerical verification; no deployed behaviour or flag |
| [`eisv-grounded-coherence-rederivation-v0.md`](eisv-grounded-coherence-rederivation-v0.md) | **Active** · Design proposal, whiteboard candidate; not deployed |
| [`eisv-fixed-point-calibration-gap-v0.md`](eisv-fixed-point-calibration-gap-v0.md) | **Parked (since 2026-06-25)** · Finding / proposal, not yet a change |
| [`coherence-proprioceptive-thresholds-v0.md`](coherence-proprioceptive-thresholds-v0.md) | **Active** · Proposal; changes no deployed behaviour. Blocked: the coherence signal is frozen (#1572) and nothing here is actionable until that is repaired |
| [`exponential-growth-dynamics-v0.md`](exponential-growth-dynamics-v0.md) | **Built** · Site B (cohort priors) fully wired — the pure primitive merged in PR #1334 |
| [`eisv-stage0-bridge-b-label-routing.md`](eisv-stage0-bridge-b-label-routing.md) | **Built (partial)** · Half (a) shipped in PR #1210; half (b) remains an active routing and population specification |
| [`substrate-portability-checkin-v0.md`](substrate-portability-checkin-v0.md) | **Built** · Canaries only; changes no math |
| [`eisv-outcome-grounding-stop-rule-v0.md`](eisv-outcome-grounding-stop-rule-v0.md) | **Registered** · Registered 2026-12-01 read (proposed 2026-07-31; evidence-scope correction 2026-08-17). The fixture-rule decision packet above governs what the read does with post-cutoff rows. Never re-run or refreshed |
| [`eisv-individuality-v2-preregistration.md`](eisv-individuality-v2-preregistration.md) | **Closed** · PRE-REGISTERED 2026-07-02 and executed on schedule; consumed by the result row below |
| [`eisv-individuality-v2-result.md`](eisv-individuality-v2-result.md) | **Closed** · Registered verdict FAIL; inference status UNTESTED AS DEPLOYED. The individuality axiom is retired for raw behavioral EISV as currently measured; a further attempt must change the measurement and pre-register before any of its data exists |
| [`eisv-incremental-value-ablation-v1.md`](eisv-incremental-value-ablation-v1.md) | **Active** · Draft preregistration (protocol 0.3.0); no cohort enrolled and no experiment scheduled |
| [`legacy-coherence-dependency-ablation-v0.md`](legacy-coherence-dependency-ablation-v0.md) | **Registered** · Prospective shadow contract, 2026-08-12; a distinct v0.1 shadow with the corrected fixture rule was registered beside it 2026-09-02 |
| [`legacy-coherence-identity-ablation-v0.md`](legacy-coherence-identity-ablation-v0.md) | **Active** · Measurement-only proposal |
| [`independent-operator-cohort-preregistration-v0.md`](independent-operator-cohort-preregistration-v0.md) | **Active** · DRAFT protocol that registers at the merge commit of its PR; amended 2026-09-02 before any enrollment |
| [`independent-operator-cohort-enrollments.md`](independent-operator-cohort-enrollments.md) | **Active** · Append-only enrollment ledger for the cohort protocol; entries are added by PR and never edited |
| [`self-improvement-loop-evaluation-v0.md`](self-improvement-loop-evaluation-v0.md) | **Active** · DRAFT protocol that registers at the merge commit of its PR |

## Resolved — relocated to [`resolved/`](resolved/)

Shipped, council-passed, closed-by-result, and dated point-in-time records live in
the [`resolved/`](resolved/) subfolder, keeping this index focused on active
threads. Each doc still carries its own status in its body; the links below point
into `resolved/`. (The subfolder is still under `proposals/`, so the doc-health
dead-ref exemption continues to apply to these point-in-time records.)

### Shipped / resolved

| Doc | Resolution |
|---|---|
| [`onboard-bootstrap-checkin.md`](resolved/onboard-bootstrap-checkin.md) | SHIPPED — Phase 5 landed via PR #188 |
| [`onboard-bootstrap-checkin.filter-audit.md`](resolved/onboard-bootstrap-checkin.filter-audit.md) | SHIPPED — retained as historical control surface for the parent doc |
| [`refined-phase-5-evidence-contract.md`](resolved/refined-phase-5-evidence-contract.md) | SHIPPED — paired with `onboard-bootstrap-checkin.md` (PR #188) |
| [`path1-sync-fingerprint-check.md`](resolved/path1-sync-fingerprint-check.md) | SHIPPED — `sync_fingerprint` lives in `src/mcp_handlers/identity/shared.py` |
| [`s19-attestation-mechanism.md`](resolved/s19-attestation-mechanism.md) | Mechanism selection council-passed 2026-04-25; implementation correctness gated separately |
| [`section-129-measurement-fix-2026-06-03.md`](resolved/section-129-measurement-fix-2026-06-03.md) | Council-passed fix restoring the Wave 1 condition-1 measurement gate |
| [`eisv-basin-health-gating-v0.md`](resolved/eisv-basin-health-gating-v0.md) | SHIPPED — PR #696 (issue #689), 2026-06-14; absolute-basin-health gating for self-relative risk, refined by #699 |
| [`dashboard-hero-severity-rollup.md`](resolved/dashboard-hero-severity-rollup.md) | SHIPPED (Phase 1) — PR #875; hero reflects all severity sources + "needs attention" band; `computeFleetSeverity` + 12 tests; verified live 2026-06-22 |
| [`docs-consolidation-v0.md`](resolved/docs-consolidation-v0.md) | SHIPPED — contested-claim lint, audience-split index, shorter README, and thin compatibility/manual routes landed by 2026-08-11 |
| [`beam-wave-1-sentinel.md`](resolved/beam-wave-1-sentinel.md) | SHIPPED — Wave 1 executed RFC; active follow-on work belongs to later waves; compatibility stub retained at the old path |
| [`beam-wave-3a-read-only-handlers.md`](resolved/beam-wave-3a-read-only-handlers.md) | DEPLOYED — Wave 3a read-only listener execution record; compatibility stub retained at the old path |

### Closed by negative result

| Doc | Resolution |
|---|---|
| [`eisv-distributional-signal-probe-v0.md`](resolved/eisv-distributional-signal-probe-v0.md) | **Probe A did not greenlight the build (2026-06-22); KILL inference withdrawn 2026-08-22.** The objective scope could not exercise the probe and the task-scope point estimate does not identify the observation-versus-representation bottleneck. See the correction and Run result blocks. |

### Dated evaluation / measurement / lifecycle records

Point-in-time records (now under `resolved/`); superseded analysis is preserved
as-written by design.

| Doc | What it captured |
|---|---|
| [`wave-0-step-2-call-site-scoping.md`](resolved/wave-0-step-2-call-site-scoping.md) | Coordination-failure call-site scoping (v0.3, post-2A-pivot; earlier prescriptions superseded by PR #345) |
| [`wave-1-window-evaluation-2026-05-18.md`](resolved/wave-1-window-evaluation-2026-05-18.md) | Wave 1 exit-condition evaluation of the T+0=2026-05-05 → T+13 window |
| [`wave-1-window-evaluation-T0-2026-05-19.md`](resolved/wave-1-window-evaluation-T0-2026-05-19.md) | Sibling re-anchor: next evaluation window under the prior doc's falsifier |
| [`ode-profile-decomposition-2026-05-20.md`](resolved/ode-profile-decomposition-2026-05-20.md) | ODE profile decomposition + persistence — the BEAM roadmap's load-bearing unknown |
| [`wave-1-completion-status-2026-06-14.md`](resolved/wave-1-completion-status-2026-06-14.md) | Read-only status roll-up across the Wave 1 surfaces + four exit conditions, consolidating the close decision into one ledger |
| [`wave-1-condition-2-alarm-parity-audit-2026-06-14.md`](resolved/wave-1-condition-2-alarm-parity-audit-2026-06-14.md) | Alarm-rule parity audit (BEAM vs Python Sentinel) for Wave 1 exit condition 2 |
| [`demotion-review-2026-08-16.md`](resolved/demotion-review-2026-08-16.md) | Lifecycle review of all 20 issue #1605 advisory candidates, including explicit reasons for every retained current contract or active proposal |

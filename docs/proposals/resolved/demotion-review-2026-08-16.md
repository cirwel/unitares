# Documentation lifecycle review — 2026-08-16

**Status:** Completed review of the 20 candidates reported by
`check_doc_health.py --strict --demotion-candidates` at `origin/master`
`4759ea8f`. Issue #1605 was opened against 18 candidates; two later documents
made the same broad header heuristic fire before this review.

The review distinguishes an executed record from a current contract. A document
was relocated only when its own work was complete. A shipped phase inside a
still-operative protocol is not, by itself, grounds for archival.

| Candidate | Decision | Reason |
|---|---|---|
| [`r2-honest-memory-integration.md`](../../ontology/r2-honest-memory-integration.md) | Retain in ontology | Phase 1 is live, but this remains the canonical lineage-integration protocol while Phase 2 downstream consumers and the telemetry gate are open. |
| [`accountable-testbed-metrics-preregistration-v1.md`](../accountable-testbed-metrics-preregistration-v1.md) | Retain active | The merged object was the frozen preregistration itself; it remains the binding contract for future headline, ablation, and scale-sweep runs. |
| [`agent-orchestrator-beam-v0.md`](../agent-orchestrator-beam-v0.md) | Retain active | The library/control thin slice exists, but it is not a running agent surface and its lifecycle and lineage follow-ups remain open. |
| [`beam-wave-1-sentinel.md`](beam-wave-1-sentinel.md) | Relocate; leave stub | Wave 1 is an executed record. Its sole named remainder belongs to Wave 2, so the completed RFC moved here and its old path became a compatibility stub. |
| [`beam-wave-3a-read-only-handlers.md`](beam-wave-3a-read-only-handlers.md) | Relocate; leave stub | The listener is deployed and the RFC's scope is complete. Active Wave 3 dispatch work lives in a separate document. |
| [`bridge-dispatch-v0.md`](../bridge-dispatch-v0.md) | Retain active | This is an unreviewed draft; “merged” describes a related Discord-bridge dependency, not this proposal. |
| [`coherence-proprioceptive-thresholds-v0.md`](../coherence-proprioceptive-thresholds-v0.md) | Retain active | It explicitly changes no deployed behavior and remains blocked on repairing the coherence signal. |
| [`eisv-general-solution-v0.md`](../eisv-general-solution-v0.md) | Retain active | The derivation is verified, but the replacement signal and its gates are not deployed; the document remains design input. |
| [`eisv-grounded-coherence-rederivation-v0.md`](../eisv-grounded-coherence-rederivation-v0.md) | Retain active | This is explicitly a whiteboard candidate with no deployment or outcome validation. |
| [`eisv-stage0-bridge-b-label-routing.md`](../eisv-stage0-bridge-b-label-routing.md) | Retain active | Half (a) shipped elsewhere; half (b), the subject of this document, remains the active routing and population spec. |
| [`exponential-growth-dynamics-v0.md`](../exponential-growth-dynamics-v0.md) | Retain current contract | Site B code is live, but the observe-to-apply calibration gate remains open and runtime code cites this document for its semantics. |
| [`governed-effect-plane-v0.md`](../governed-effect-plane-v0.md) | Retain current contract | The record-only lane is live while the execute lane remains blocked on veto and recovery prerequisites; both lanes share this safety contract. |
| [`legacy-coherence-dependency-ablation-v0.md`](../legacy-coherence-dependency-ablation-v0.md) | Retain active | It is a prospective shadow contract and changes no live weight, threshold, verdict, or actuator. |
| [`mirror-effectiveness-measurement-v0.md`](../mirror-effectiveness-measurement-v0.md) | Retain current contract | Phase 2 remains operator-gated and depends directly on the Phase 0–1 instrumentation and estimator definitions recorded here. |
| [`orchestrator-vouched-identity-v0.md`](../orchestrator-vouched-identity-v0.md) | Retain active | This is a design-first identity gate with no live cutover; the resolved path in its header names a dependency. |
| [`principal-rollup-v0.md`](../principal-rollup-v0.md) | Retain current contract | Measurement shipped, but count/mint behavior remains operator-gated and runtime code cites this document for principal semantics. |
| [`relational-calibration-pilot-v0.md`](../relational-calibration-pilot-v0.md) | Retain active | It is specification-only and explicitly prohibits runtime collection; “complete instrument lock” describes protocol rigor, not delivery. |
| [`surface-lease-plane-phase-a-plan.md`](../surface-lease-plane-phase-a-plan.md) | Retain current ledger | The completed plan is still a load-bearing sequencing source referenced by the BEAM roadmap and coordination kernel, and the shared repository contract treats it as a hot single-writer surface. |
| [`verification-weighted-verdict-v0.md`](../verification-weighted-verdict-v0.md) | Retain current safety contract | Wiring landed default-off; enabling remains council-gated, so the phase definitions and safety envelope are still operative. |
| [`worktree-isolation-vs-lease-default.md`](../worktree-isolation-vs-lease-default.md) | Retain current design record | Its own status is an active counter-note; “SHIPPED” describes the related lease-plane contract, not this document. |

## Checker follow-up

The advisory now reads the document's explicit `Status:` value before falling
back to the broader header, ignores negated lifecycle claims such as “NOT
deployed,” and accepts a reasoned `Demotion review` retention banner. This keeps
dependency metadata and negative statements from masquerading as shipment while
preserving the conservative fallback for older status-less documents.

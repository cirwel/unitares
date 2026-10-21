# Competitive Analysis: Runtime-Governance Neighbors (2026-06)

**Prompted by:** triage of two arXiv papers against UNITARES — Auton ([arXiv:2602.23720](https://arxiv.org/abs/2602.23720), Feb 2026) and MI9 ([arXiv:2508.03858](https://arxiv.org/abs/2508.03858), Aug 2025).
**For:** v7 §10 Related Work spine (see `docs/ontology/paper-positioning.md` §"four-anchor spine").
**Verdict:** Auton is adjacent, not competitive (different layer). MI9 is the one to engage head-on — same banner, overlapping vocabulary, but a different mathematical commitment.

---

## TL;DR

- **Auton — not a competitor.** It is a declarative specification / deployment architecture (Cognitive Blueprint vs Runtime Engine, infrastructure-as-code, MCP as tool transport). Its "governance" means schema conformance, versioning, and auditability of the agent *artifact*. It does not model behavioral state. UNITARES sits downstream of where Auton stops. Cite as a neighbor on the spec/deployment plane; no overlap on EISV, coherence, dialectic, KG, or identity ontology.
- **MI9 — genuinely adjacent, partially competitive, must-cite.** It claims "the first fully integrated runtime governance framework" for agentic AI (Aug 2025, predates the v7 work). It overlaps UNITARES on the runtime-governance banner and on drift / telemetry / containment vocabulary. But it is a discrete control plane (FSM conformance + risk index + graduated containment), not a continuous behavioral-state estimator. No identity ontology, no class-conditional calibration, no shared-memory / dialectic layer.

---

## Auton (arXiv:2602.23720) — different layer

| Axis | Auton | UNITARES |
|---|---|---|
| Core unit | Cognitive Blueprint (declarative spec) / Runtime Engine (substrate) | EISV state vector / coherence / CIRS+PID verdict |
| "Governance" means | Schema conformance, versioning, auditability of the agent *artifact* | Continuous behavioral state estimation at runtime |
| Paradigm | Infrastructure-as-code (Kubernetes / Terraform analogy) | Dynamical-systems state estimation + control |
| MCP role | Tool-integration transport | Delivery surface for governance tools |
| Behavioral state | Absent | Central |

Auton solves the impedance mismatch between stochastic LLM output and deterministic backends by making the agent a versionable, language-agnostic data artifact. That is upstream of, and orthogonal to, behavioral governance. A Cognitive Blueprint is the kind of declarative identity layer that could sit *above* a UNITARES runtime — they compose rather than compete.

**Action:** one-line Related-Work mention as a spec/deployment-plane neighbor. No defensive framing needed.

---

## MI9 (arXiv:2508.03858) — the real head-to-head

MI9's six components: agency-risk index, agent-semantic telemetry capture, continuous authorization monitoring, FSM-based conformance engines, goal-conditioned drift detection, graduated containment. It operates model- and infrastructure-agnostically via a unified planner-action-tool lifecycle abstraction; framework-specific adapters translate SDK events into a standardized Agent Telemetry Stream (ATS). Animating frame: safety and alignment for production deployment.

### Where it overlaps UNITARES (the competitive surface)

1. **Runtime, not pre-deployment.** MI9 explicitly claims the "first fully integrated runtime governance framework." UNITARES makes the same runtime-governance claim. This is a priority-claim collision the paper must engage, not route around.
2. **Drift detection.** MI9 "goal-conditioned drift detection" vs UNITARES drift vector / EISV trajectory drift. Direct terminology collision.
3. **Telemetry normalization.** MI9's ATS + planner-action-tool lifecycle abstraction vs UNITARES's `observe` / `sync_state` normalize-heterogeneous-agents pipeline. Both reduce arbitrary agent stacks to a normalized stream.
4. **Action gating / containment.** MI9 graduated containment + continuous authorization monitoring vs UNITARES verdicts, trust tiers, and strict-identity write gates. Both mediate actions at runtime.

### Where they diverge (UNITARES differentiators)

1. **Control plane vs state estimator.** MI9 is FSM conformance + thresholds + a risk index — a guardrail/control architecture. UNITARES's EISV is a continuous nonlinear state-space estimator with contraction/stability results and a PID governor. Different mathematical object: MI9 decides *conforms / doesn't-conform* against a spec; UNITARES estimates *where in behavioral state space* an agent is and produces a continuous verdict. This is the cleanest line to draw.
2. **Drift means different things.** MI9 drift = deviation from a goal specification (FSM/spec conformance). UNITARES drift = displacement in EISV state space. Same word, different referent — must be defined explicitly to prevent reviewer conflation.
3. **No identity ontology.** MI9 has no lineage, no class-conditional calibration, no earned-vs-performative continuity distinction, no heterogeneity-as-differentiator. The entire v7 animating thesis is absent from MI9.
4. **No shared memory / dialectic.** MI9 governs agents (and fleets) for safety; it has no cross-agent knowledge graph, no dialectic resolution, no multi-agent coherence/synchronization layer.
5. **Frame.** MI9's animating goal is safety/alignment/risk-containment (a security posture). UNITARES's is behavioral coherence as state estimation, with safety a downstream consequence.
6. **Maturity asymmetry (double-edged).** MI9 is a protocol/paper proposal. UNITARES is a deployed MCP server with production telemetry — a strength for the empirical contribution, but it does not blunt MI9's *priority* claim on the runtime-governance banner.

### Side-by-side

| Axis | MI9 | UNITARES |
|---|---|---|
| Governance mechanism | FSM conformance + agency-risk index + graduated containment | EISV state estimation + CIRS + PID governor |
| Mathematical object | Discrete state machines + thresholds | Continuous nonlinear dynamical system |
| Drift | Deviation from goal/spec (conformance) | Displacement in EISV state space |
| Telemetry | ATS via planner-action-tool adapters | `observe`/`sync_state` normalized pipeline |
| Identity / lineage | None | Class-conditional calibration + lineage + v7 ontology |
| Shared memory / dialectic | None | KG + dialectic resolution |
| Animating frame | Safety / alignment / containment | Behavioral coherence as state estimation |
| Maturity | Protocol/paper proposal | Deployed MCP server + production telemetry |

---

## What the v7 paper should do

1. **Cite MI9 as the anchor runtime-governance neighbor in §10** and assert the distinction precisely: MI9 governs via discrete conformance + containment (a control/guardrail plane); UNITARES governs via continuous behavioral state estimation (a dynamical-systems estimator). Both are runtime; the difference is the mathematical commitment, not the deployment phase. Engaging MI9 head-on pre-empts the "this is just MI9 re-skinned" reviewer.
2. **Disambiguate "drift" on first use.** Distinguish UNITARES's EISV-state-space drift from MI9's goal/spec-conformance drift in the same paragraph that introduces the drift vector.
3. **Name the telemetry-layer prior art.** Position MI9's ATS / planner-action-tool abstraction as the telemetry-normalization neighbor, then differentiate on *what is done with* the normalized stream — FSM conformance vs state estimation.
4. **Lean on the differentiators MI9 cannot replicate cheaply:** the identity ontology (class-conditional calibration, lineage, earned-vs-performative continuity) and the shared-memory/dialectic layer. These are exactly the non-empirical contributions `paper-positioning.md` already recommends foregrounding for v7, and none of them appear in MI9.

**Bottom line:** MI9 does not pre-empt UNITARES's core contributions, but it owns the runtime-governance banner first and shares enough vocabulary that silence reads as ignorance of prior art. It is the single most important Related-Work citation for the runtime claim. Auton is a one-line spec/deployment-plane neighbor. Neither is a substitute for UNITARES.

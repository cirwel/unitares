# UNITARES competitive survival audit — 2026-09-16

**Last updated:** 2026-09-17

**Decision:** KEEP the accountability hypothesis; INTERFACE with standards and
vendor runtimes; FREEZE new platform expansion while the narrow claim is tested.

**Confidence:** medium. The overlap findings are grounded in current product
documentation and the repository. The proposed wedge is not validated or shown
necessary: the
independent-operator enrollment ledger is empty, comparative reconstruction is
unmeasured, and no causal protection benefit has been established.

**Review status:** revised after the cross-agent review in
[#2258](https://github.com/cirwel/unitares/pull/2258). That review was broader
and more code-grounded than the first audit. It was still produced by a governed
agent prompted by the same operator, so it is adversarial review, not external
independence.

This is a product survival audit, not a priority claim for the paper. It asks
which parts of UNITARES remain worth investing in after comparing the deployed
system with the standards and platforms an adopter can choose now.

## Executive verdict

Do not abandon UNITARES today. Stop using *federation kernel* as the primary
market category: although the reader-facing documents already restrict it to one
authority domain and describe cross-operator federation as research, the phrase
collides with A2A and hides the narrower accountability hypothesis.

The current product sentence combines too many categories:

> self-hosted federation kernel for agent identity, claims and evidence, review,
> outcomes, and reconstruction

Much of that bundle is not distinctive as a product category:

- A2A owns the open agent-to-agent interoperability protocol category.
- NeMo Relay substantially overlaps runtime scopes, parentage, lifecycle events,
  policy middleware, plugins, and cross-harness observability.
- SPIFFE and cloud IAM address workload authentication; UNITARES's process-instance
  identity and declared causal lineage are a different, narrower contract.
- AgentCore, Google Agent Gateway, and Microsoft Agent 365 provide enterprise
  identity, authorization, registry, monitoring, and governance control planes.
- OpenTelemetry/OpenInference plus self-hostable systems such as Phoenix make
  generic traces, evaluations, datasets, and replay crowded layers. The OTel
  agent conventions remain in development, so adapters must preserve a stable
  internal shape rather than treat today's convention as settled.

UNITARES should be tested as a narrower product:

> **An operator-owned accountability record for long-running work performed by
> heterogeneous agents: who claimed what, what evidence supported it, who
> objected, what conditions were imposed, what happened, and what a successor
> can reconstruct.**

That is a candidate wedge, not an established unique advantage. Its strongest
coding-work neighbor is the pull-request review thread, which already preserves
objections and can enforce conditions through branch protection. The residual
wedge is cross-harness accountability before a PR exists, outside code, and
between agents. Its survival depends on beating the real baseline: Git, PR and
CI artifacts plus the harness transcript and ordinary traces—not an artisanal
handoff written for the experiment.

## What was knowable when UNITARES started

UNITARES's first repository commit is dated 2025-11-20; its first identity record
is dated 2025-11-28. The chronology matters because it separates missed prior art
from products that emerged during development.

| Event | Date | Consequence for this audit |
|---|---:|---|
| NeMo Agent Toolkit public repository created | 2025-03-06 | Agent workflow construction and optimization already had a substantial open-source entrant. |
| A2A repository created | 2025-03-25 | The protocol work predated UNITARES. |
| Google publicly announced A2A | 2025-04-09 | Heterogeneous agent interoperability was a visible category seven months before UNITARES began. |
| A2A moved to the Linux Foundation with support from more than 100 companies | 2025-06-23 | By UNITARES's start, A2A was not an obscure experiment. |
| Microsoft announced Agent 365 as “the control plane for agents” | 2025-11-18 | Enterprise agent registry, access, monitoring and governance were public two days before the preserved UNITARES build record begins. |
| UNITARES first repository commit | 2025-11-20 | Start of the currently preserved build record. |
| AgentCore Policy became generally available | 2026-03-03 | External, fine-grained agent-tool policy matured during the same period. |
| NeMo Relay public repository created | 2026-03-31 | The closest runtime overlap emerged while UNITARES was already operating. |
| A2A reported more than 150 participating organizations and production use | 2026-04-09 | Protocol distribution and adoption became a structural advantage no small project should try to reproduce. |
| Google announced Agent Identity and Agent Gateway at Cloud Next | 2026-04-22 | Cryptographic agent identity and governed agent connectivity matured after UNITARES began; later May posts describe the same announcement. |

The failure was not starting after a settled market. It was failing to maintain
a continuous product-and-standards map while the category consolidated. The June
2026 competitive memo considered only MI9 and Auton. It did not cover A2A,
SPIFFE, OpenTelemetry, NeMo Relay, AgentCore, Agent 365, Google Agent Identity,
or the established observability/evaluation products. It also omitted the
harness vendors' own transcripts, audit events and session histories, the
pull-request thread, revisioned memory services, annotation queues, SCITT-style
signed-statement infrastructure, and demand-side analysis.

## Capability collision matrix

The labels mean:

- **Crowded/commodity:** adopters can obtain most of the capability from a
  standard or mature adjacent product; UNITARES should integrate rather than
  lead with it.
- **Overlap:** UNITARES has a real implementation, but another system owns a
  stronger distribution or execution position.
- **Candidate wedge:** the particular combination may be valuable, but no
  comparative evidence yet establishes it.
- **Blocked:** the architecture does not yet support the stated future path.

| UNITARES capability or claim | Strongest neighbors | Finding | Disposition |
|---|---|---|---|
| Heterogeneous agent interoperability | A2A, MCP | **Commodity.** A2A covers discovery and agent-to-agent task/message/artifact exchange; MCP covers tools and context. | Adopt A2A for transport and retire redundant in-house message/wake designs only after their scope is compared explicitly. |
| Runtime scopes, lifecycle and parent-child work | NeMo Relay, OTel GenAI spans, harness transcripts | **Strong overlap.** Relay has explicit run/turn/tool/LLM/subagent scopes, ownership, cleanup and maintained integrations; harnesses already retain sessions. | Emit and ingest through adapters. Relay is one optional adapter, not the substrate; OTel is a moving target, not yet a settled schema. |
| Tool and model interception | NeMo Relay middleware, AgentCore Policy/Gateway, Google Agent Gateway/Model Armor | **Strong overlap.** These systems can block, sanitize, transform, route or default-deny calls at the execution boundary. | Keep UNITARES-specific decisions separate from enforcement adapters. Do not extend an unvalidated verdict merely because a new gate exists. |
| Process-instance and workload identity | SPIFFE, cloud IAM, AgentCore Identity, Entra Agent ID, Google Agent Identity | **Partial overlap.** The neighbors authenticate workloads. UNITARES additionally models one running process and declared causal lineage, but that is not general security identity. | Optionally bind external principals to UNITARES identities. SPIFFE does not solve the symmetric-resolution-attestation blocker; the dormant Ed25519 receipt is the nearer mechanism. |
| Agent registry and lifecycle | Agent 365, cloud agent platforms | **Overlap with major distribution disadvantage.** | Treat registry data as imported context, not a product wedge. |
| Tracing and trajectory export | OpenTelemetry GenAI, OpenInference, NeMo Relay ATOF/ATIF, Phoenix | **Crowded.** OTel's agent conventions still carry development status, and Phoenix is self-hostable under ELv2 rather than OSI-open-source. | Preserve one stable internal event shape and map it bidirectionally through adapters. Do not treat normalized telemetry as differentiation. |
| Evaluation, annotations and experiments | Phoenix, NeMo Agent Toolkit, cloud evaluation products | **Crowded/overlap.** UNITARES outcome binding can remain a specialized consumer. | Do not become a general evaluation platform. |
| Shared memory / knowledge graph | Harness memory, revisioned memory services, graph/RAG products | **Crowded.** Attribution and correction semantics may help, but graph storage and retrieval are not unique; current search also mixes findings with channel messages. | Keep records required by accountability, enforce relevance, and evaluate revision/correction behavior against existing memory services. |
| Structured objection, conditions and resolution | Pull-request review, annotation queues, workflow engines, UNITARES dialectic | **Candidate wedge, narrower than first stated.** UNITARES binds standing objections and resolution conditions to later checkpoints, but reviewers are not bundled and benefit is unmeasured. | Test the pre-PR, non-code and cross-agent cases against ordinary PR/review workflows. |
| Claim → evidence → challenge → outcome chain | Audit systems, tracing/eval products, Git/CI, UNITARES | **Candidate wedge.** The cross-artifact relationship is more specific than a trace, but UNITARES lacks a unified claim/evidence object and reconstruction is currently client-assembled. | Make this the only product experiment. |
| Provenance labels (`measured`, `derived`, `prior`, `unknown`) | Data lineage and evidence systems | **Useful implementation discipline, not unique as a category.** CI enforcement is locally distinctive. | Retain as a trust contract inside the accountability record. |
| Behavioral state estimation / EISV | MI9 and drift/risk monitoring systems | **Unvalidated estimator on the write-gating path.** The deployed verdict is behavioral assessment; the ODE is a parallel research cross-check. Predictive lift and prevention remain unestablished. | Keep the check-in as automatic capture, demote the verdict to advisory-by-default for new installs after the registered 2026-12-01 read, and isolate the ODE as research. Do not rewire the active instrument during its registered window. |
| Cross-operator federation | A2A plus federated workload identity/PKI and SCITT-style receipts | **Architecturally blocked, already qualified in public copy.** UNITARES has one trusted issuer and symmetric agent-level resolution attestations; another operator cannot independently verify them. | Keep the limitation. Evaluate enabling the dormant Ed25519 deployment receipt; do not claim the public copy currently promises cross-operator federation. |
| Self-hosted, operator-controlled record | Self-hostable trace/eval stacks, OTel stacks, SPIFFE and local databases | **Valuable constraint, not unique alone.** | Retain; combine with cross-harness accountability semantics. Cloud planes are optional integration targets, never required foundations. |

## The closest competitor: NeMo Relay

NeMo Relay is not a complete substitute for UNITARES, but it invalidates a broad
runtime-federation position. Its documented surface includes:

- wrappers for Codex and Claude Code plus integrations for LangChain, LangGraph,
  Deep Agents, OpenClaw, and Hermes Agent;
- hierarchical scopes for runs, requests, tools, LLM calls and subagents;
- canonical lifecycle events and parentage;
- middleware that can block, sanitize, transform, route, retry or replace tool
  and model execution;
- plugins and subscribers;
- ATOF, ATIF, OpenTelemetry and OpenInference export.

Relay is a stronger execution-boundary implementation than UNITARES for the
harnesses it supports, but it is pre-1.0, young and controlled by one vendor. It
should be one optional adapter behind a stable UNITARES boundary, not the required
substrate. The viable relationship is:

**A2A/MCP or host runtime** → **host hooks or an adapter such as NeMo Relay**
(scopes, events, enforcement) → **stable internal events with OTel/ATOF/ATIF
maps** → **UNITARES** (claims, evidence, objections, conditions, outcomes,
reconstruction)

This is complementary only if UNITARES can show that its upper layer produces a
better accountability result than storing the Relay trajectory beside Git and
CI records.

## What UNITARES actually has

The repository demonstrates substantial implementation, not product-market
validation:

- 42 advertised tools, 13,614 test functions, 67 migrations, and 524 Python
  modules at the repository's frozen/recounted snapshots;
- six Elixir applications containing 42,397 tracked source lines, including the
  lease plane, sentinel, agent orchestrator and Wave 3 handlers;
- 169 documented environment flags, a 70-entry compatibility alias table, and
  multiple registered protocols whose cohorts or collection paths have not run;
- continuous single-maintainer deployment and millions of audit/telemetry rows;
- exercised process-bound writes, findings, outcome records, policy responses,
  review records, and pause actuation;
- a public trust contract and unusually explicit evidence limitations.

But the same repository records the decisive deficits:

- every deployment number comes from one operator and mostly co-development
  dogfood;
- the independent-operator protocol exists, but its enrollment ledger is empty;
- incident prevention and benefit from pausing are untested;
- benefit from review and coordination is untested;
- comparative reconstruction against Git, PR, CI, the harness transcript and
  ordinary traces is unmeasured;
- the existing equal-facts accountability-journey rehearsal tied 1.000 to 1.000,
  so any next test must separate capture from retrieval rather than repeat only
  the tied stage;
- the original `sync_state` report is transient, the accountability data spans
  multiple APIs and retention boundaries, and there is no unified claim/evidence
  object;
- enforcement is advisory outside surfaces UNITARES owns;
- multi-principal federation is blocked by the current trust model.

The estimator is a separate structural risk. Every `sync_state` executes the
state update, the primary persisted checkpoint row is an EISV row, and pause can
gate check-ins plus knowledge `store` and `note`. No general flag removes EISV
from that path. The weakness is not simply that agents self-report: server and
substrate signals also enter the model. The weakness is an unvalidated,
in-band estimator coupled unconditionally to the checkpoint and partially to
write authority, with a sparse independent-outcome channel. Feeding it more
runtime telemetry or adding a stronger tool gate does not validate it.

The build is therefore evidence of engineering persistence and exercised
mechanisms. Overgrowth is verified. Necessity remains untested; those are not
opposite verdicts.

## Keep, interface, freeze

### Keep and concentrate

1. Attributed claims and corrections.
2. Evidence provenance and explicit missing-evidence states.
3. Durable objections, reviewer conditions and resolution state.
4. Binding predictions or claims to independently sourced outcomes.
5. A reconstruction query/report that preserves disagreement and uncertainty.
6. Self-hosted operation and exportability.
7. The provenance-label trust contract.
8. Process-instance identity and declared causal lineage as an accountability
   boundary, optionally bound to external security principals.
9. The check-in as the automatic checkpoint capture path, decoupled conceptually
   from whether an EISV verdict is authoritative.

The narrow product has five workflows: **identity/checkpoint**, **claims and
evidence**, **challenge and correction**, **outcomes**, and **reconstruction**.
Retrieval is a required operation across claims and reconstruction, not a sixth
workflow. The missing artifact is one linked object/export spanning the five;
renaming the current tools does not create it.

### Interface with external layers

1. A2A for agent-to-agent interoperability.
2. MCP for tool/context connectivity.
3. OTel/OpenInference emission and ingestion, while their agent conventions
   remain versioned adapters rather than the internal source of truth.
4. NeMo Relay as one optional runtime/enforcement adapter alongside direct host
   hooks.
5. Optional SPIFFE/OIDC/IAM principal binding without replacing process-instance
   semantics.
6. Cedar, cloud gateways or Relay middleware as optional enforcement adapters.
7. Phoenix or another evaluation system for generic traces, labels, datasets and
   experiments.

The cloud control planes are integration targets, never foundations. The core
must remain usable locally without a required vendor or metered service.

### Freeze pending explicit operator disposition

1. BEAM Wave 3 handler dispatch, subject to reaffirming or parking the signed
   go-decision rather than silently overriding it.
2. The governed-effect execute half, dormant agent orchestrator, hosted
   multi-tenant endpoint, and resident runtime.
3. The in-house agent-message transport, wake gate, and send/inbox verbs now
   overlapped by A2A.
4. General model routing, inference hosting, and agent-framework features.
5. A general workload-identity or enterprise-registry product.
6. Generic tracing dashboards and generic evaluation workflows.
7. New EISV product features before the registered 2026-12-01 read. Keep the
   current instrument stable through that read; decide default advisory posture
   and structural decoupling afterward.
8. New preregistrations until at least one existing runnable protocol collects
   its intended cohort.
9. Tool-count growth. The next milestone is fewer concepts and one end-to-end
   accountability workflow, not a larger catalog.

These are decisions for the operator, informed by the re-layering packet in
[#2255](https://github.com/cirwel/unitares/pull/2255), which merged on
2026-09-16; this audit does not revoke previous go-decisions or propose rolling
back its optional Relay adapter. Freezing investment is a portfolio decision,
not a conclusion from zero usage. It does not require deleting deployed
capabilities. Existing compatibility paths can remain while the product boundary
is tested.

## Reconciliation of PRs #2255–#2258

The four pull requests answer different questions and should not be collapsed
into a synthetic consensus. The claims map supplies evidence, the independent
review preserves disagreement, this audit makes a portfolio recommendation, and
the Relay pull request changes runtime behavior. Their reconciled dispositions
are:

| PR | Role | Reconciled disposition |
|---|---|---|
| [#2255](https://github.com/cirwel/unitares/pull/2255) | Optional NeMo Relay adapter plus a Relay tool-policy gate | **Already merged. Retain the adapter; correct the authority default in a dedicated follow-up.** `RelayPluginConfig.enforce` is currently `true`, so installing the component enables EISV-derived tool blocking unless the operator opts out. Change that default to `false`, document enforcement as experimental opt-in, and do not treat its in-band tool failures as independent outcome evidence. Do not remove the exporter or make Relay a required substrate. |
| [#2256](https://github.com/cirwel/unitares/pull/2256) | Claims falsification audit, market map, and proposed reader-facing wording | **Evidence and positioning owner.** Land when its checks pass, before this audit. It owns the shared positioning rewrite; later product documents should cite it rather than fork another description. |
| [#2258](https://github.com/cirwel/unitares/pull/2258) | Point-in-time adversarial review of this audit | **Durable dissent record.** Land when its author declares it ready, before this audit. Preserve the disagreements as written; this audit's disposition table records which recommendations changed the synthesis. |
| [#2257](https://github.com/cirwel/unitares/pull/2257) | Competitive-survival synthesis and investment recommendation | **Merge last.** It depends conceptually on #2256 and #2258 and records the post-merge correction required by #2255. It remains a recommendation until the operator ratifies each reaffirm-or-freeze decision. |

This produces one decision sequence without pretending the documents agree on
every diagnosis:

1. preserve the evidence and the dissent (#2256 and #2258, in either order);
2. correct #2255's authority-expanding default without discarding the adapter;
3. merge this synthesis only after those records are durable; and
4. run the capture-plus-retrieval pilot before funding another platform surface.

The short-term code correction is deliberately narrow. It does not rewire the
EISV instrument during the registered read window, change server policy, or
assert that Relay observations validate the estimator. Any later move from
substrate check-ins to a raw accountability-event seam belongs with the portable
bundle design and must be evaluated separately.

## Demand risk

Overlap is only half the survival question. No evidence yet shows demand for the
narrow record outside the maintainer's fleet. General AI governance frameworks
and regulation require logging, risk controls and traceability in particular
contexts; they do not establish demand from ordinary coding-agent operators for
a claim/challenge/outcome server. The immediate demand test is therefore local
and external at once:

- does the maintainer's fleet recover a costly fact or correction that its
  transcript, PR and CI record lose; and
- will any non-maintainer accept the capture burden after seeing that result?

Decision-rationale systems have a persistent incentive problem: the process that
records context pays now while a future successor benefits later. UNITARES must
return immediate value to the current agent—relevant prior findings, a binding
review condition, or a corrected decision—or its durable record will remain
sparse regardless of theoretical value.

## Proposed 30-day product-discovery pilot

The thresholds below are a **proposed operator decision standard**. They have no
authority until the operator ratifies them before the test starts. They do not
replace the registered efficacy, independent-operator, or accountable-coordination
protocols. It should be filed as a pilot beneath the latter rather than as a new
competing preregistration.

### Question

For real multi-agent work, does the narrow UNITARES record capture or retrieve a
decision-relevant claim, objection, condition, correction, or outcome more
faithfully than the ordinary artifacts available to the successor?

### Compared systems

- **Control:** Git/PR/CI artifacts + the harness transcript + ordinary available
  traces. No handoff is authored specially for the experiment.
- **Candidate:** the same artifacts + the narrow UNITARES accountability record.

This isolates UNITARES's contribution. Comparing UNITARES against no
instrumentation would test the value of observability, not UNITARES.

### Two stages

1. **Capture:** let the ordinary and UNITARES paths record naturally during real
   work, then compare both with an incident oracle assembled afterward. This asks
   whether a material fact entered the record at all.
2. **Retrieval:** give both arms the same underlying facts through neutral
   renderings and compare what a cold reader recovers. This asks whether UNITARES
   structures existing facts usefully. The existing rehearsal tied at 1.000 in
   this equal-facts stage, so another retrieval-only run cannot answer capture.

The portable accountability bundle is a prerequisite for a credible neutral
rendering. Perfect blinding may remain impossible; arm recognizability must be
reported rather than hidden.

### Test units

- At least three real incidents from work that would happen anyway.
- Prefer at least two operators other than the maintainer. If recruitment fails,
  use independent cold readers who did not participate in the incidents, drawn
  from a different model/vendor family where practical, with a human spot check.
- Frozen capture packets plus equal-facts retrieval packets.
- Randomized arm order where possible and a scoring rubric fixed before readers
  see either arm.

Three incidents are a product-discovery gate, not a statistical efficacy study.
Results authorize the next investment decision, not a general scientific claim.

### Measures

1. Material facts recovered correctly.
2. Unsupported facts asserted.
3. Unresolved disagreement preserved rather than flattened.
4. Evidence source and authority classified correctly.
5. Time to reach an actionable reconstruction.
6. Setup and recording burden on the incident's agents and operator.
7. Whether a correction reaches the successor without manual oral context.
8. Whether a standing reviewer condition changes later work.
9. Whether a recorded outcome changes the successor's treatment of the earlier
   claim.

### Proposed decision rule

Continue focused investment in the current bundle if all are true:

1. The candidate arm improves reconstruction on at least two of the three
   incidents without increasing unsupported assertions.
2. At least one non-maintainer reader identifies a decision-relevant fact,
   objection, or correction from UNITARES that the control packet fails to
   preserve clearly.
3. The marginal recording burden is acceptable to the participating operators,
   recorded as a stated judgment with the observed time/call burden beside it.
4. The result can be reproduced from exported records without access to the
   maintainer's live database or private explanation.

If these conditions fail, diagnose capture, retrieval, and burden separately and
try the next cheaper lever: repair capture, simplify the bundle, or reduce
recording burden. Freeze the server as a product only after two consecutive,
predeclared lever failures or an explicit operator opportunity-cost decision.
Failure retires a lever, not the accountability goal.

### Integrity conditions

- Confirm every producer is surfaced, reachable and recorded before interpreting
  missing usage.
- Do not count automated check-ins, telemetry volume, repository size or stars as
  evidence of user value.
- Publish friction and failures alongside positive reconstructions.
- Keep EISV scores out of the primary comparison. The candidate wedge must
  survive without an unvalidated behavioral oracle.
- Do not modify the scoring rubric after either arm has been read.
- Report the transcript, PR review thread, and every specially authored artifact
  present in the control; do not call an artisanal control the standard stack.

## Immediate 7-day actions

1. Open a focused follow-up to #2255 that changes the Relay integration's
   `enforce` default from `true` to `false` and describes the gate as an
   experimental opt-in. Runtime telemetry and a stronger enforcement point do
   not validate EISV.
2. Let #2256 own any reader-facing positioning rewrite; do not start a competing
   PR on that single-writer surface.
3. Specify one portable accountability bundle linking claims, evidence,
   objections, conditions, outcomes and source artifact IDs.
4. Design the bundle against the pull-request thread, harness transcript,
   revisioned memory, annotation queues, and a standard signed-statement or audit
   export—not only against Relay.
5. Run one capture-plus-retrieval pilot on an existing real incident and its
   matched control; do not call the result an efficacy test.
6. Recruit cold readers before polishing the workflow. The empty enrollment
   ledger is a higher-priority risk than another internal feature.
7. Re-run this competitor map monthly and record additions by date, source and
   changed disposition.

## Final answer

UNITARES is not presently a strong cross-operator federation in the market or
cryptographic sense—and its public documents already say that. It is one
operator's deeply built accountability and behavioral-governance system with an
untested path to broader adoption.

It can still survive if it becomes smaller. The only claim worth funding next is
that a durable, operator-owned claim/evidence/challenge/outcome record makes
heterogeneous agent work more accountable and reconstructable than the standard
stack. Platform work outside that hypothesis should be explicitly reaffirmed or
frozen, not allowed to continue by momentum.

## Disposition of the 2026-09-17 review

The full cross-agent review is preserved in
[#2258](https://github.com/cirwel/unitares/pull/2258). This revision accepts its
central corrections:

| Review finding | Disposition here |
|---|---|
| Overgrowth and necessity are not alternatives | **Accepted.** Overgrowth is verified; necessity is untested. |
| The weak component is the unvalidated estimator on the gating path, not self-report by itself | **Accepted.** The diagnosis and the resulting EISV recommendation were changed. |
| Pull-request review is the strongest coding-work neighbor | **Accepted.** The candidate wedge is narrowed to pre-PR, non-code and cross-agent accountability. |
| A2A, OTel, Relay, SPIFFE/IAM and cloud planes require different integration decisions | **Accepted.** “Underlie” was removed; Relay and cloud systems are optional adapters/targets. |
| No reader-facing present-tense cross-operator promise exists | **Accepted.** The audit now criticizes category collision and inconsistent framing rather than a nonexistent claim. |
| The platform investment is in Elixir, Wave 3, transport and execute-plane work | **Accepted.** Those surfaces are named for explicit reaffirm-or-freeze decisions. |
| Outcomes must remain a workflow; retrieval is cross-cutting | **Accepted.** The five-workflow description was corrected. |
| The control must include the harness transcript and separate capture from retrieval | **Accepted.** The pilot was redesigned and the prior tied rehearsal named. |
| Demand and harness vendors were missing | **Accepted.** Both are now explicit risks. |
| Three same-fleet audits are independent evidence | **Rejected, consistently with the review itself.** Cross-agent disagreement is useful; independence remains absent. |

One chronology sentence in the review is itself corrected here: the verified
Agent 365 announcement on 2025-11-18 is **two days before**, not four months
after, the preserved UNITARES first commit on 2025-11-20.

## Sources

External product claims were checked against primary project or vendor
documentation on 2026-09-16 and rechecked for this revision on 2026-09-17.

- [A2A public announcement, 2025-04-09](https://developers.googleblog.com/a2a-a-new-era-of-agent-interoperability/)
- [A2A Linux Foundation launch, 2025-06-23](https://www.linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project-to-enable-secure-intelligent-communication-between-ai-agents)
- [A2A first-year adoption statement](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
- [A2A specification repository](https://github.com/a2aproject/A2A)
- [NeMo Relay overview](https://docs.nvidia.com/nemo/relay/about-nemo-relay/overview)
- [NeMo Relay repository and support matrix](https://github.com/NVIDIA/NeMo-Relay)
- [NeMo Agent Toolkit repository](https://github.com/NVIDIA/NeMo-Agent-Toolkit)
- [Amazon Bedrock AgentCore policy concepts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-core-concepts.html)
- [AgentCore Policy general availability, 2026-03-03](https://aws.amazon.com/about-aws/whats-new/2026/03/policy-amazon-bedrock-agentcore-generally-available/)
- [Microsoft Agent 365 announcement, 2025-11-18](https://www.microsoft.com/en-us/microsoft-365/blog/2025/11/18/microsoft-ignite-2025-copilot-and-agents-built-to-power-the-frontier-firm/)
- [Microsoft Agent 365 general availability, 2026-05-01](https://www.microsoft.com/en-us/security/blog/2026/05/01/microsoft-agent-365-now-generally-available-expands-capabilities-and-integrations/)
- [Microsoft Entra support for Agent 365](https://learn.microsoft.com/en-us/microsoft-agent-365/leadership/entra-agent-365)
- [Google Cloud Next Agent Identity and Agent Gateway announcement, 2026-04-22](https://cloud.google.com/blog/topics/google-cloud-next/welcome-to-google-cloud-next26)
- [Google agent identity and runtime-defense announcement, 2026-05-06](https://cloud.google.com/blog/products/identity-security/whats-new-in-iam-security-governance-and-runtime-defense)
- [Google Agent Gateway ecosystem announcement, 2026-05-05](https://cloud.google.com/blog/products/identity-security/introducing-agent-gateway-isv-ecosystem-for-security-and-governance)
- [Google Model Armor](https://cloud.google.com/security/products/model-armor)
- [SPIFFE documentation](https://spiffe.io/docs/latest/spiffe-about/overview/)
- [OpenTelemetry GenAI agent-span conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)
- [Arize Phoenix documentation](https://arize.com/docs/phoenix/)
- [Arize Phoenix ELv2 license](https://github.com/Arize-ai/phoenix/blob/main/LICENSE)
- [SCITT architecture draft](https://datatracker.ietf.org/doc/draft-ietf-scitt-architecture/)

Repository evidence:

- [`PRODUCT_DEFINITION.md`](../PRODUCT_DEFINITION.md)
- [`EVIDENCE_AND_LIMITS.md`](../EVIDENCE_AND_LIMITS.md)
- [`SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md)
- [`CAPABILITIES_AND_DEPLOYMENT.md`](../CAPABILITIES_AND_DEPLOYMENT.md)
- [`competitive-analysis-2026-06.md`](competitive-analysis-2026-06.md)
- [Competitive claims map, PR #2256](https://github.com/cirwel/unitares/pull/2256)
- [Cross-agent review of this audit, PR #2258](https://github.com/cirwel/unitares/pull/2258)
- [`independent-operator-cohort-preregistration-v0.md`](../proposals/registered/independent-operator-cohort-preregistration-v0.md)
- [`independent-operator-cohort-enrollments.md`](../proposals/active/independent-operator-cohort-enrollments.md)

# UNITARES competitive survival audit — 2026-09-16

**Last updated:** 2026-09-16

**Decision:** KEEP the accountability experiment; MERGE with commodity runtime,
identity, policy, and telemetry layers; STOP the broad federation-platform story.

**Confidence:** medium. The overlap findings are grounded in current product
documentation and the repository. The proposed wedge is not validated: the
independent-operator enrollment ledger is empty, comparative reconstruction is
unmeasured, and no causal protection benefit has been established.

This is a product survival audit, not a priority claim for the paper. It asks
which parts of UNITARES remain worth investing in after comparing the deployed
system with the standards and platforms an adopter can choose now.

## Executive verdict

Do not abandon UNITARES today. Abandon the claim that it is a general federation
kernel in the ordinary market sense.

The current product sentence combines too many categories:

> self-hosted federation kernel for agent identity, claims and evidence, review,
> outcomes, and reconstruction

Most of that bundle is no longer distinctive:

- A2A owns the open agent-to-agent interoperability protocol category.
- NeMo Relay substantially overlaps runtime scopes, parentage, lifecycle events,
  policy middleware, plugins, and cross-harness observability.
- SPIFFE and the cloud identity planes own workload identity and federation.
- AgentCore, Google Agent Gateway, and Microsoft Agent 365 provide enterprise
  identity, authorization, registry, monitoring, and governance control planes.
- OpenTelemetry/OpenInference plus Phoenix and similar systems make generic
  traces, evaluations, annotations, datasets, and replay commodity layers.

UNITARES should be tested as a narrower product:

> **An operator-owned accountability record for long-running work performed by
> heterogeneous agents: who claimed what, what evidence supported it, who
> objected, what conditions were imposed, what happened, and what a successor
> can reconstruct.**

That is a candidate wedge, not an established unique advantage. Its survival
depends on beating a much simpler baseline: Git and CI artifacts, standardized
traces, and a structured handoff document.

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
| UNITARES first repository commit | 2025-11-20 | Start of the currently preserved build record. |
| NeMo Relay public repository created | 2026-03-31 | The closest runtime overlap emerged while UNITARES was already operating. |
| AgentCore Policy became generally available | 2026-03-03 | External, fine-grained agent-tool policy matured during the same period. |
| Microsoft announced Agent 365 as an agent control plane | 2026-03 | Central registry, governance, security, and observability became an explicit enterprise product category. |
| Google announced Agent Identity and Agent Gateway | 2026-05 | Strongly attested identity and governed agent connectivity matured after UNITARES began. |
| A2A reported more than 150 participating organizations and production use | 2026-04-09 | Protocol distribution and adoption became a structural advantage no small project should try to reproduce. |

The failure was not starting after a settled market. It was failing to maintain
a continuous product-and-standards map while the category consolidated. The June
2026 competitive memo considered only MI9 and Auton. It did not cover A2A,
SPIFFE, OpenTelemetry, NeMo Relay, AgentCore, Agent 365, Google Agent Identity,
or the established observability/evaluation products.

## Capability collision matrix

The labels mean:

- **Commodity:** adopters can obtain the capability from a standard or mature
  adjacent product; UNITARES should integrate rather than lead with it.
- **Overlap:** UNITARES has a real implementation, but another system owns a
  stronger distribution or execution position.
- **Candidate wedge:** the particular combination may be valuable, but no
  comparative evidence yet establishes it.
- **Absent/blocked:** the public wording reaches beyond deployed evidence.

| UNITARES capability or claim | Strongest neighbors | Finding | Disposition |
|---|---|---|---|
| Heterogeneous agent interoperability | A2A, MCP | **Commodity.** A2A covers discovery and agent-to-agent task/message/artifact exchange; MCP covers tools and context. | Adopt, do not compete. |
| Runtime scopes, lifecycle and parent-child work | NeMo Relay, OTel GenAI spans | **Strong overlap.** Relay has explicit run/turn/tool/LLM/subagent scopes, ownership, cleanup and maintained integrations. | Export to or ingest from Relay/OTel. Stop building a parallel generic event vocabulary. |
| Tool and model interception | NeMo Relay middleware, AgentCore Policy/Gateway, Google Agent Gateway/Model Armor | **Strong overlap.** These systems can block, sanitize, transform, route or default-deny calls at the execution boundary. | Retain only UNITARES-specific policy decisions; delegate enforcement. |
| Process/workload identity | SPIFFE, cloud IAM, AgentCore Identity, Entra Agent ID, Google Agent Identity | **Commodity at the security layer.** UNITARES's process-instance semantics are useful bookkeeping, but not a stronger workload identity system. | Bind to external principals and attestations. Do not market as general identity infrastructure. |
| Agent registry and lifecycle | Agent 365, cloud agent platforms | **Overlap with major distribution disadvantage.** | Treat registry data as imported context, not a product wedge. |
| Tracing and trajectory export | OpenTelemetry GenAI, OpenInference, NeMo Relay ATOF/ATIF, Phoenix | **Commodity.** | Standardize adapters; stop treating normalized telemetry as differentiation. |
| Evaluation, annotations and experiments | Phoenix, NeMo Agent Toolkit, cloud evaluation products | **Commodity/overlap.** UNITARES outcome binding and calibration can remain a specialized consumer. | Do not become a general evaluation platform. |
| Shared memory / knowledge graph | Agent frameworks, memory services, graph/RAG products | **Crowded.** Attribution and correction semantics may help, but graph storage and retrieval are not unique. | Keep only records needed by accountability and reconstruction. |
| Structured objection, conditions and resolution | Human review systems, workflow engines, UNITARES dialectic | **Candidate wedge.** UNITARES binds disagreement and resolution conditions to an agent lifecycle, but reviewers are not bundled and benefit is unmeasured. | Keep and test against ordinary PR/review workflows. |
| Claim → evidence → challenge → outcome chain | Audit systems, tracing/eval products, Git/CI, UNITARES | **Candidate wedge.** The cross-artifact relationship is more specific than a trace, but UNITARES lacks a unified claim/evidence object and reconstruction is currently client-assembled. | Make this the only product experiment. |
| Provenance labels (`measured`, `derived`, `prior`, `unknown`) | Data lineage and evidence systems | **Useful implementation discipline, not unique as a category.** CI enforcement is locally distinctive. | Retain as a trust contract inside the accountability record. |
| Behavioral state estimation / EISV | MI9 and drift/risk monitoring systems | **Research differentiator, product value unproven.** The deployed verdict is behavioral assessment; the ODE is a parallel research cross-check. Predictive lift and prevention remain unestablished. | Isolate as an optional research module. Never make the accountability product depend on it. |
| Cross-operator federation | A2A plus federated workload identity/PKI | **Absent/blocked.** UNITARES currently has one trusted issuer and symmetric resolution attestations; another operator cannot independently verify the record. | Remove from present-tense product positioning until a multi-principal experiment succeeds. |
| Self-hosted, operator-controlled record | Phoenix OSS, OTel stacks, SPIFFE and local databases | **Valuable constraint, not unique alone.** | Retain; combine with the accountability semantics. |

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

Relay is therefore the better substrate for observing and controlling execution.
UNITARES should not reproduce that runtime. The viable relationship is:

**A2A/MCP or host runtime** → **NeMo Relay** (scopes, events, enforcement) →
**OTel/ATOF/ATIF stream** → **UNITARES** (claims, evidence, objections,
conditions, outcomes, reconstruction)

This is complementary only if UNITARES can show that its upper layer produces a
better accountability result than storing the Relay trajectory beside Git and
CI records.

## What UNITARES actually has

The repository demonstrates substantial implementation, not product-market
validation:

- 42 advertised tools, 13,614 test functions, 67 migrations, and 524 Python
  modules at the repository's frozen/recounted snapshots;
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
- comparative reconstruction against Git plus structured handoff is unmeasured;
- the original `sync_state` report is transient, the accountability data spans
  multiple APIs and retention boundaries, and there is no unified claim/evidence
  object;
- enforcement is advisory outside surfaces UNITARES owns;
- multi-principal federation is blocked by the current trust model.

The build is therefore evidence of engineering persistence and exercised
mechanisms. It is not evidence that an adopter needs the product.

## Keep, merge, stop

### Keep and concentrate

1. Attributed claims and corrections.
2. Evidence provenance and explicit missing-evidence states.
3. Durable objections, reviewer conditions and resolution state.
4. Binding predictions or claims to independently sourced outcomes.
5. A reconstruction query/report that preserves disagreement and uncertainty.
6. Self-hosted operation and exportability.
7. The provenance-label trust contract.

### Merge with external layers

1. A2A for agent-to-agent interoperability.
2. MCP for tool/context connectivity.
3. NeMo Relay and OTel/OpenInference for runtime events and trajectories.
4. SPIFFE or the operator's IAM for authenticated workload identity.
5. Cedar/cloud gateways or Relay middleware for action enforcement.
6. Phoenix or another evaluation system for generic traces, labels, datasets and
   experiments.

“Merge” means adapters and compatible schemas, not a dependency on one vendor.
The core must remain usable locally without a required metered service.

### Stop investing in

1. A proprietary generic runtime-event model where a standard event suffices.
2. General agent orchestration, model routing, inference hosting, or agent
   framework features.
3. A general workload-identity or enterprise registry product.
4. Generic tracing dashboards and generic evaluation workflows.
5. Present-tense “cross-operator federation” claims.
6. New EISV product features before the existing evidence gates resolve.
7. Tool-count growth. The next milestone is fewer concepts and one end-to-end
   accountability workflow, not a larger catalog.

Stopping investment in these surfaces is a portfolio decision, not a conclusion
from zero usage. It does not require deleting deployed capabilities. Existing
compatibility paths can remain while the product boundary is tested.

## Proposed 30-day survival test

The thresholds below are a **proposed operator decision standard**. They have no
authority until the operator ratifies them before the test starts. They do not
replace the registered efficacy or independent-operator protocols.

### Question

For a real multi-agent work incident, does the UNITARES accountability record
let a cold successor or operator reconstruct the material history more accurately
or efficiently than a standard stack alone?

### Compared systems

- **Control:** Git/PR/CI artifacts + Relay/OTel trajectory + a structured handoff.
- **Candidate:** the same artifacts + the narrow UNITARES accountability record.

This isolates UNITARES's contribution. Comparing UNITARES against no
instrumentation would test the value of observability, not UNITARES.

### Test units

- At least three real incidents from work that would happen anyway.
- At least two operators other than the maintainer, or, if recruitment fails,
  independent cold readers who did not participate in the incidents.
- Frozen incident packets with the same underlying source artifacts in both arms.
- Randomized arm order and a scoring rubric fixed before readers see either arm.

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

### Proposed decision rule

Continue focused investment only if all are true:

1. The candidate arm improves reconstruction on at least two of the three
   incidents without increasing unsupported assertions.
2. At least one non-maintainer reader identifies a decision-relevant fact,
   objection, or correction from UNITARES that the control packet fails to
   preserve clearly.
3. The marginal recording burden is acceptable to the participating operators,
   recorded as a stated judgment with the observed time/call burden beside it.
4. The result can be reproduced from exported records without access to the
   maintainer's live database or private explanation.

If these conditions fail, freeze the server as a research artifact and extract
only reusable schemas, provenance linting, or adapters. Failure retires the
current product-investment lever; it does not prove that accountability is an
unnecessary goal.

### Integrity conditions

- Confirm every producer is surfaced, reachable and recorded before interpreting
  missing usage.
- Do not count automated check-ins, telemetry volume, repository size or stars as
  evidence of user value.
- Publish friction and failures alongside positive reconstructions.
- Keep EISV scores out of the primary comparison. The candidate wedge must
  survive without an unvalidated behavioral oracle.
- Do not modify the scoring rubric after either arm has been read.

## Immediate 7-day actions

1. Freeze net-new feature proposals unless they repair a blocker in the narrow
   accountability path.
2. Replace the broad product sentence in a separate positioning PR after the
   operator accepts this audit.
3. Specify one portable accountability bundle linking claims, evidence,
   objections, conditions, outcomes and source artifact IDs.
4. Build a read-only Relay/OTel ingestion spike; do not build another execution
   wrapper.
5. Produce one reconstruction report from an existing real incident and the
   matched control packet.
6. Recruit cold readers before polishing the workflow. The empty enrollment
   ledger is a higher-priority risk than another internal feature.
7. Re-run this competitor map monthly and record additions by date, source and
   changed disposition.

## Final answer

UNITARES is not presently a strong federation in the market or cryptographic
sense. It is one operator's deeply built accountability and behavioral-governance
system with an untested path to broader adoption.

It can still survive if it becomes smaller. The only claim worth funding next is
that a durable, operator-owned claim/evidence/challenge/outcome record makes
heterogeneous agent work more accountable and reconstructable than the standard
stack. Everything else should be integrated, isolated as research, or frozen.

## Sources

All external product claims were checked against primary project or vendor
documentation on 2026-09-16.

- [A2A public announcement, 2025-04-09](https://developers.googleblog.com/a2a-a-new-era-of-agent-interoperability/)
- [A2A Linux Foundation launch, 2025-06-23](https://www.linuxfoundation.org/press/linux-foundation-launches-the-agent2agent-protocol-project-to-enable-secure-intelligent-communication-between-ai-agents)
- [A2A first-year adoption statement](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
- [A2A specification repository](https://github.com/a2aproject/A2A)
- [NeMo Relay overview](https://docs.nvidia.com/nemo/relay/about-nemo-relay/overview)
- [NeMo Relay repository and support matrix](https://github.com/NVIDIA/NeMo-Relay)
- [NeMo Agent Toolkit repository](https://github.com/NVIDIA/NeMo-Agent-Toolkit)
- [Amazon Bedrock AgentCore policy concepts](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-core-concepts.html)
- [AgentCore Policy general availability, 2026-03-03](https://aws.amazon.com/about-aws/whats-new/2026/03/policy-amazon-bedrock-agentcore-generally-available/)
- [Microsoft Agent 365 control plane](https://www.microsoft.com/microsoft-agent-365)
- [Microsoft Entra support for Agent 365](https://learn.microsoft.com/en-us/microsoft-agent-365/leadership/entra-agent-365)
- [Google agent identity and runtime-defense announcement, 2026-05-06](https://cloud.google.com/blog/products/identity-security/whats-new-in-iam-security-governance-and-runtime-defense)
- [Google Agent Gateway ecosystem announcement, 2026-05-05](https://cloud.google.com/blog/products/identity-security/introducing-agent-gateway-isv-ecosystem-for-security-and-governance)
- [Google Model Armor](https://cloud.google.com/security/products/model-armor)
- [SPIFFE documentation](https://spiffe.io/docs/latest/spiffe-about/overview/)
- [OpenTelemetry GenAI agent-span conventions](https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/gen-ai-agent-spans.md)
- [Arize Phoenix documentation](https://arize.com/docs/phoenix/)

Repository evidence:

- [`PRODUCT_DEFINITION.md`](../PRODUCT_DEFINITION.md)
- [`EVIDENCE_AND_LIMITS.md`](../EVIDENCE_AND_LIMITS.md)
- [`SCOPE_AND_THREAT_MODEL.md`](../SCOPE_AND_THREAT_MODEL.md)
- [`CAPABILITIES_AND_DEPLOYMENT.md`](../CAPABILITIES_AND_DEPLOYMENT.md)
- [`competitive-analysis-2026-06.md`](competitive-analysis-2026-06.md)
- [`independent-operator-cohort-preregistration-v0.md`](../proposals/independent-operator-cohort-preregistration-v0.md)
- [`independent-operator-cohort-enrollments.md`](../proposals/independent-operator-cohort-enrollments.md)

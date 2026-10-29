# What UNITARES is

## The product in one sentence

**UNITARES is a self-hosted, single-operator federation kernel for agent
identity, claims and evidence, review, outcomes, and reconstruction.** Agents
carry findings, corrections, and review decisions across sessions and vendors;
a successor or operator can retrieve the record and inspect its sources.

“Federation” here means independent runtimes sharing one operator-controlled
server and authority domain. Each runtime keeps its own models, tools, and
execution loop, and communicates with peers through its own transport or A2A.
Nothing in UNITARES replicates state across independent servers.

The shared record answers five questions:

| Question | Deployed mechanism |
|---|---|
| **Who said it?** | Process identity and explicit lineage attribute work and handoffs. Platform workload identity supplies credentials; lineage grants no authority. |
| **What supports it?** | Durable findings retain claims, corrections, and provenance. Each `sync_state` check-in keeps a bounded excerpt of its report text, the agent's own words rather than a verified claim; durable claims require a finding. |
| **Who challenged it?** | Structured review preserves positions, objections, conditions, and resolution; a review completes when its session resolves. |
| **What happened?** | Typed outcome events preserve reported results and evidence grades, with prediction binding when supplied. |
| **What can a successor recover?** | Knowledge search, review reads, history export, and authorized outcome-evidence reads let a client reconstruct the work from available records. |

Git and Markdown handoffs fit alongside this record: a finding can reference
code, logs, test results, or a handoff document while retaining attribution and
later corrections. See [capabilities and deployment](CAPABILITIES_AND_DEPLOYMENT.md)
for tool names, service prerequisites, and retention and authorization boundaries.

## Interfaces and integration

MCP is the primary agent interface. REST, the public SDK, host adapters, and
the dashboard expose the same Core. Claude Code, Codex, Hermes, custom runtimes,
and residents retain their own reasoning loops and interaction surfaces.

The [interface contract](INTERFACE_CONTRACT.md) defines callable capabilities
across MCP, REST discovery, and local stdio. A connected MCP client reaches every
capability; a host integration (hooks or an adapter) adds automatic onboarding
and check-ins. An integration may honor an explicitly configured policy
actuator; an EISV recommendation alone carries no authority by default.

## Core and Resident are different products

This repository ships **UNITARES Core** and specialized reference residents.
Core owns identity, shared knowledge, review, outcomes, audit, coordination,
and the optional behavioral-state instrument. Agents own their reasoning and
execution; an operator owns every authority boundary.

**UNITARES Resident** is a separate first-party agent project for persistent
conversations, provider and tool adapters, scheduling, task queues, and operator
interfaces. Its repository and runtime skeleton ship today; the agent
itself has not shipped yet. It uses the same public MCP/SDK contract as any
other harness, with no direct database access, Core-internal imports, or
privileged measurement or policy path.

## One task across agents

A coding agent investigates a failed test, requests review, and hands work to
a new process:

1. **Bind and search.** Call `start_session(force_new=true)`, thread the returned
   `client_session_id` on later calls, and search earlier findings before
   repeating work. Authorized reads can also precede binding.
2. **Keep the evidence.** Store the diagnosis as a finding with code and test
   references. Use `sync_state` after meaningful work.
3. **Join the review.** Request review, read the participating reviewer's
   response, and address objections. Preserve unresolved conditions.
4. **Record the result.** Use `record_result` with outcome provenance, adding the
   intended `prediction_id` when grading a prediction. Confidence is optional;
   a reported result retains its evidence grade.
5. **Close and hand off.** Record the finding's closure and evidence. A successor
   starts fresh, declares lineage only for a real handoff from an exited
   predecessor, and retrieves the findings, review, and available outcomes.

Check-ins return an EISV behavioral state estimate and preserve the policy
engine's raw recommendation under `policy_evaluation`. The default
`UNITARES_POLICY_ACTUATION=observe` posture projects a recommended pause into
non-coercive guidance: it creates no lifecycle hold and blocks no write.
Operators that still need the historical circuit breaker can opt into
`UNITARES_POLICY_ACTUATION=enforce`. Existing durable pauses remain real state
until their normal recovery, review, operator, or expiry path lifts them. EISV
ships in the core path today and moves to an optional module under the
[staged plan](proposals/active/eisv-core-boundary-v0.md). The
[computation reference](EISV_COMPUTATION.md) defines the deployed instrument.

## Who it is for, and what "done" looks like

Agents are direct users. One operator running several long-lived agents
controls the deployment and chooses their runtimes and infrastructure. The
goal is a retrievable record of claims, evidence, reviews, outcomes, and state
interpretations that lets a later process continue the work. UNITARES connects those
records alongside the outputs of evals, guardrails, and sandboxes.

## Provenance

The [trust contract](trust-contract.md) distinguishes measured observations,
derived values, priors, and unknowns. CI checks selected governance-metrics
fields for provenance labels, uninitialized assessments, and fleet-calibration
scope. The contract's [enforcement status](trust-contract.md#7-enforcement)
defines that coverage and the remaining work.

## Evidence

The [captured two-process walkthrough](guides/AGENT_SESSION_WALKTHROUGH.md)
shows attribution, outcome binding, closure, and successor retrieval with real
server responses. The
[review retrospective](evaluations/review-correction-traces-2026-09-21.md)
traces findings to the fixes they produced: evidence grades preserved,
formatting repair kept from manufacturing approval, unresolved findings kept
visible, and a review condition carried into an implementation ten days after
its review opened. Historical deployments also delivered 370+ policy pauses
between April and September 2026; that is evidence that the actuator operated,
not that the pauses improved outcomes. The actuator is now explicit opt-in.

Comparative benefit and task-outcome improvement remain unmeasured. The
[evidence record](EVIDENCE_AND_LIMITS.md#evaluation-boundaries) holds the
evaluation results and registered read conditions, the
[scope and threat model](SCOPE_AND_THREAT_MODEL.md) covers agents concealing or
gaming signals, and the [Reviewer Guide](REVIEWER_GUIDE.md) exercises the
workflow.

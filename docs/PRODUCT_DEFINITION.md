# What UNITARES is

One page, plain language. The architecture docs describe the parts; this page
states the product. When wording here and in a canonical reference disagree,
the canonical reference wins — this page trades precision for clarity on
purpose and links to the precise version at every step.

## The product in one sentence

**UNITARES is a self-hosted, single-operator federation kernel for agent
identity, claims and evidence, review, outcomes, and reconstruction.** Different
harnesses retain their own execution loops while sharing an attributed record
through MCP/HTTP.

Agents use that record to carry findings, corrections, and review decisions
across sessions and vendors. A successor can search earlier work before acting;
the human working with it can inspect what was established and what still needs
review.

A Markdown handoff may be all a task needs. An agent can also reference that
document in a UNITARES finding alongside code, logs, and test results. The
document gives a successor a concise starting point; the shared record keeps
attribution, corrections, review decisions, and reported outcomes available
around it. Git, handoff documents, and UNITARES can support the same workflow.

Here federation means many independent runtimes and harnesses sharing one
operator-controlled server and authority domain; that is what single-operator
means. The sense with several operators or authority domains is
cross-operator or multi-principal. “Kernel” describes that shared
accountability boundary. Runtimes interoperate with each other over their own
transports or an agent-to-agent protocol such as A2A; UNITARES is the record
behind them, not the transport between them. Its current mechanisms map to
five questions:

| Question | Deployed mechanism | Boundary |
|---|---|---|
| **Who said it?** | Fresh process identity and explicit lineage. | Lineage links work; it does not confer authority or make two processes one identity. A process identity is an attribution record, not a credential; workload-identity systems remain the credential layer. |
| **What supports it?** | Derived check-in state, durable attributed findings, corrections, and provenance. | The original `sync_state` report text is transient; durable claims require a finding. This wording introduces no new object schema or automatic evidence verification. |
| **Who challenged it?** | Structured review records, disagreement, conditions, and resolution. | A reviewer must participate; requesting review is not completing review. |
| **What happened?** | Typed outcome events and prediction binding. | Caller reports need independent evidence to establish correctness. |
| **What can a successor recover?** | Shared-memory search, knowledge reads, review records, history export, and authorized outcome-evidence reads. | Reconstruction is assembled by clients across different retention and authorization boundaries; complete reconstruction and reduced operator effort remain unmeasured. |

What the word does not claim: UNITARES does not own execution, and nothing in it
replicates state across independent servers.

See [capabilities and deployment](CAPABILITIES_AND_DEPLOYMENT.md) for actual
tool names and service prerequisites. Behavioral state estimation, policy,
recovery, and calibration remain part of the system. They interpret the record
and gate governed operations; they are not substitutes for evidence.

## MCP-native, not MCP-only

MCP is the primary agent-facing contract: it is the narrow waist through which
different agent runtimes can use the same identity, state, policy, review, and
memory capabilities. It is not the product's outer boundary. REST, the public
SDK, host adapters, and the dashboard expose the same Core for clients whose
lifecycle cannot be expressed by a direct MCP connection alone.

The versioned [interface contract](INTERFACE_CONTRACT.md) names that narrow
waist precisely. It guarantees advertised, callable capability names across
MCP, REST discovery, and local stdio. Lifecycle hooks and policy actuation are
separate host-integration capabilities; connecting an MCP client does not imply
that its host will onboard, check in, or stop automatically.

This makes UNITARES **harness-agnostic**. Claude Code, Codex, Hermes, custom
runtimes, and resident agents keep their own reasoning loops, model choices,
tools, and interaction surfaces. UNITARES observes and governs their declared
checkpoints without requiring them to become one kind of agent.

## Core and Resident are different products

This repository ships UNITARES Core and several specialized reference
residents. The separate UNITARES Resident repository ships an early runtime
skeleton, not yet a usable general-purpose conversational agent.

| Product surface | Owns | Boundary |
|---|---|---|
| **UNITARES Core** | Identity, provenance, state estimation, policy and recovery, audit, shared knowledge, dialectic review, and coordination. | Does not own the agent's reasoning or tool-execution loop. |
| **UNITARES Resident** | A first-party agent experience: persistent conversations, provider and tool adapters, scheduling, task queues, memory participation, and operator-facing interfaces. | Lives outside Core and uses the same public MCP/SDK contract as any other harness. Its repository and runtime skeleton are shipped; the usable agent product is not yet shipped. |

Keeping that boundary prevents the system from grading an execution loop it
privately controls. UNITARES Resident may become the easiest way to run a
governed agent, but it must remain ordinary userland: no direct database access,
no imports from Core internals, and no privileged measurement or policy path.

## One task across agents

A coding agent investigates a failed test, asks for review, and hands the work
to a new process. The record connects the parts of that task:

1. **Bind the process.** The agent calls `start_session(force_new=true)` and
   passes the returned `client_session_id` on later writes, so each is
   attributable to that process.
2. **Search earlier findings.** Before changing the code, it searches shared
   memory and reads relevant findings, including corrections and their sources.
3. **Keep the finding and its evidence.** It stores the diagnosis with references
   to the code and test results. Important claims need an explicit durable
   finding; a `sync_state` report alone does not preserve its original text.
4. **Ask for review and join the result.** A participating reviewer records an
   objection or a resolution. The agent addresses the objection, and the review
   preserves the disagreement and any conditions rather than treating the
   request itself as approval.
5. **Record what happened.** The agent reports the bounded test outcome through
   `record_result`. If it previously made a prediction, the outcome can bind to
   that prediction for calibration; confidence is optional.
6. **Hand off the work.** The successor starts with its own process identity and
   declares lineage for a real handoff from an exited predecessor. It searches
   the stored findings and reads the review and available outcome evidence.
   The operator can inspect those same records. Recovery depends on what was
   captured and what the caller is authorized to read.

Meaningful `sync_state` check-ins also return an EISV behavioral state estimate
and a policy action: proceed, guide, or pause. A pause holds governed writes
until recovery, review, an operator, or expiry lifts it; it does not stop the
agent's execution loop. EISV currently ships in the core path. Moving it to an
optional module is the adopted direction, with implementation governed by the
[staged plan](proposals/active/eisv-core-boundary-v0.md). The estimator's
predictive validity and whether pausing improves outcomes remain research
questions; the [computation reference](EISV_COMPUTATION.md) describes the
current instrument.

## Who it is for, and what "done" looks like

Agents are direct users of the shared record. **One operator running several
long-lived agents** controls the deployment and its authority boundary. The
agents use tools and work through multi-step tasks over hours or days; the
operator chooses their runtimes and infrastructure.

The operational goal is an attributable record of claims, reported outcomes,
reviews, and policy decisions that a later process or operator can inspect.
Configured identity gates enforce accountable writes, and policy can interrupt
governed operations. Detecting a confidently failing agent before damage, or
improving outcomes by pausing it, has not been established.

UNITARES runs alongside **evals**, **guardrails**, and **sandboxes**, preserving
attribution, evidence, review, and reported outcomes across the work they
assess. Its shared record lets agents and operators inspect those outputs
together over time.

## The distinctive guarantee

Every value the system emits carries a provenance label — `measured`,
`derived`, `prior`, or `unknown` — and that labeling is lint-enforced in CI
(`src/trust_contract_lint.py`). A number can be wrong; it cannot silently
pretend to be a measurement. The full guarantee, including what honest
failure looks like, is the [trust contract](trust-contract.md).

## Evidence and limits

The deployed product provides an attributed record of findings, review, and
reported outcomes. Its usefulness depends on agents and integrations capturing
the relevant evidence; it does not verify every claim automatically or establish
complete reconstruction. Its effect on coordination effort and task outcomes
has not yet been measured, and agent preference has not been established.

The policy instrument has separate limits. Apart from the pause gate on
governed write surfaces, enforcement is advisory: the server returns an action
and the client decides whether to honor it. The frozen 2026-08-09 outcome-lift
evaluation detected no predictive lift and demonstrates no prevention — a
non-detection whose preserved record cannot establish read-specific power
([power audit](operations/falsifiability-power-audit-2026-08-23.md)), so weak
predictive lift is unresolved rather than ruled out — and calibrated
concealment by a motivated agent is a documented structural blind spot. The
pause gate demonstrably actuates; protection remains untested. Producing a pause
and delivering it are separate events: at the 2026-08-06 audit a cadence
window suppressed 195 of 218 recorded pauses (89.4%) before they reached the
agent, and while delivery is live (a governed pause landed 2026-08-09), the rate
has not been re-measured since (as of 2026-10-11 the 89.4% figure is the
2026-08-06 reading, not a current rate, and has not been re-read against the live
database). The contract ledger records the protection claim
as untested. The precise boundary is
the [scope and threat model](SCOPE_AND_THREAT_MODEL.md); the evidence status
is the [claim ledger](EVIDENCE_AND_LIMITS.md#claim-ledger) and the
[Reviewer Guide](REVIEWER_GUIDE.md).

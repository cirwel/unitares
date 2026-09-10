# What UNITARES is

One page, plain language. The architecture docs describe the parts; this page
states the product. When wording here and in a canonical reference disagree,
the canonical reference wins — this page trades precision for clarity on
purpose and links to the precise version at every step.

## The product in one sentence

**UNITARES is a self-hosted federation kernel for agent identity, claims and
evidence, review, outcomes, and reconstruction.** Different harnesses retain
their own execution loops while sharing an attributed record through MCP/HTTP.

Here federation means many independent runtimes and harnesses sharing one
operator-controlled server and authority domain. “Kernel” describes that shared
accountability boundary. It is not a claim that UNITARES owns execution,
replicates state across independent servers, or has proved better outcomes than
a structured handoff. Its current mechanisms map to five questions:

| Question | Deployed mechanism | Boundary |
|---|---|---|
| **Who said it?** | Fresh process identity and explicit lineage. | Lineage links work; it does not confer authority or make two processes one identity. |
| **What supports it?** | Derived check-in state, durable attributed findings, corrections, and provenance. | The original `sync_state` report text is transient; durable claims require a finding. This wording introduces no new object schema or automatic evidence verification. |
| **Who challenged it?** | Structured review records, disagreement, conditions, and resolution. | A reviewer must participate; requesting review is not completing review. |
| **What happened?** | Typed outcome events and prediction binding. | Caller reports need independent evidence to establish correctness. |
| **What can a successor recover?** | Shared-memory search, knowledge reads, review records, history export, and authorized outcome-evidence reads. | Reconstruction is assembled by clients across different retention and authorization boundaries; complete reconstruction and superiority over Git plus handoff remain unmeasured. |

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

## One governed incident, end to end

The chain below illustrates the deployed checkpoint and recovery workflow.
It is one use of the shared record, not a requirement to invoke every capability.

1. **An agent onboards** and receives a process identity. From here on, every
   write it makes is attributable to that specific process, not to a display
   name (`src/mcp_handlers/identity/`).
2. **It checks in after each meaningful unit of work** — what it did and how
   confident it is (`sync_state`, canonically `process_agent_update`).
   External outcomes such as test results and exit codes arrive via
   `record_result` and are compared against the confidence the agent claimed.
   That gap is the calibration signal.
3. **The server scores the check-in** into the four EISV coordinates,
   smoothed over time; after enough check-ins the agent is graded against its
   own history rather than a universal threshold
   ([computation reference](EISV_COMPUTATION.md)).
4. **A decision ladder returns one action** — proceed, guide, or pause —
   always with a named reason and a next step (`src/monitor_decision.py`).
5. **A paused agent is actually stopped at the governed surface:** further
   check-ins are refused with `AGENT_PAUSED`. To resume it must submit a real
   reflection through `self_recovery`, which is quality-checked, and may be
   routed into a dialectic review by another healthy agent. A review
   resolution can impose conditions — complexity limits, monitoring windows —
   enforced on the agent's subsequent check-ins
   (`src/mcp_handlers/dialectic/enforcement.py`).
6. **The finding lands in the shared knowledge graph** with writer
   attribution, where the next agent searches before acting. The whole chain
   is replayable from the audit trail.

## Who it is for, and what "done" looks like

The customer is **one operator running several long-lived autonomous agents**
— tool-using, multi-step, working over hours or days — on infrastructure that
operator controls. It is a self-hosted, MCP-native operating layer, not an
agent framework and not a hosted platform.

The operational goal is an attributable record of claims, reported outcomes,
reviews, and policy decisions that a later process or operator can inspect.
Configured identity gates enforce accountable writes, and policy can interrupt
governed operations. Detecting a confidently failing agent before damage, or
improving outcomes by pausing it, has not been established.

It occupies the gap the other layers leave open: **evals** judge the model
before deployment, **guardrails and sandboxes** judge each action as it
happens, **UNITARES** judges the running process over time. It replaces none
of them.

## The distinctive guarantee

Every value the system emits carries a provenance label — `measured`,
`derived`, `prior`, or `unknown` — and that labeling is lint-enforced in CI
(`src/trust_contract_lint.py`). A number can be wrong; it cannot silently
pretend to be a measurement. The full guarantee, including what honest
failure looks like, is the [trust contract](trust-contract.md).

## Limits, in one paragraph

Apart from the pause gate on governed write surfaces, enforcement is advisory:
the server returns an action and the client decides whether to honor it. The
frozen 2026-08-09 outcome-lift evaluation detected no predictive lift and
demonstrates no prevention — a non-detection whose preserved record cannot
establish read-specific power
([power audit](operations/falsifiability-power-audit-2026-08-23.md)), so weak
predictive lift is unresolved rather than ruled out — and calibrated
concealment by a motivated agent is a documented structural blind spot. The
defensible claim today is therefore **an accountability instrument whose circuit
breaker demonstrably actuates** — not one shown to protect anything. Producing a
pause and delivering it are separate events: at the 2026-08-06 audit a cadence
window suppressed 195 of 218 recorded pauses (89.4%) before they reached the
agent, and while delivery is live (a governed pause landed 2026-08-09), the rate
has not been re-measured since. The contract ledger records the protection claim
as untested. The precise boundary is
the [scope and threat model](SCOPE_AND_THREAT_MODEL.md); the evidence status
is the README's [Evidence and limits](../README.md#evidence-and-limits)
section and the [Reviewer Guide](REVIEWER_GUIDE.md).

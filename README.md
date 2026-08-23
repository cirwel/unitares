<div align="center">

<img alt="UNITARES — runtime governance for AI-agent fleets" src="docs/assets/hero-v2.png" width="100%">

### Runtime state, accountability, and recovery for long-lived AI-agent fleets.

</div>

An agent forty turns into a task reports high confidence while its tests are
failing. Every individual tool call was allowed, so an action-level guardrail has
nothing to object to. What is missing is a longitudinal record that compares what
the agent claims with what actually happened.

**UNITARES is that record.** Agents check in after meaningful units of work;
the server keeps a longitudinal score of whether each agent's claims match its
recorded results, pauses an agent whose behavior drifts, gates resumption on a
recovery step, and leaves an audit trail plus a shared memory every other agent
can search — record, score, interrupt, remember. Plain-language version:
[What UNITARES is](docs/PRODUCT_DEFINITION.md). It is a self-hosted MCP/HTTP
service you run yourself — not an agent framework, not a hosted platform.

**Status:** v2.19.0. Continuously operated since November 2025 under a single
operator: 71,141 stored EISV state rows and six long-running resident agents,
one of them on separate hardware. 4,573,890 audit/telemetry events were logged
over the same period, but 91.4% of those are session-resolution and
cross-device-call records, not independent policy decisions. A frozen
falsifiability eval against a permutation null did **not detect** predictive
lift (selective p = 0.070–0.567). Read that as a non-detection on a 224-row
cohort, not as a measured ceiling: on a cohort of that shape the harness cannot
resolve a weak effect, which the
[power audit](docs/operations/falsifiability-power-audit-2026-08-23.md)
quantifies. Either way it says nothing about the accountability mechanism the
score sits inside: identity-bound writes, claim-vs-recorded-evidence
comparison, and an enforced pause until recovery all run independent of it. The
defensible claim today is an accountability instrument with one working circuit
breaker, not incident prevention. [Evidence and limits](#evidence-and-limits)
scopes every number on this page, including that non-detection.

<div align="center">

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.12+-2f7d72?style=flat-square&labelColor=0f171f)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-2f7d72?style=flat-square&labelColor=0f171f)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

[![Quickstart](https://img.shields.io/badge/▶-quickstart-5eead4?style=for-the-badge&labelColor=0f171f)](#quickstart)
[![Evidence](https://img.shields.io/badge/evidence-check_it-f5a623?style=for-the-badge&labelColor=0f171f)](#evidence-and-limits)
[![Docs](https://img.shields.io/badge/docs-read-7d8f97?style=for-the-badge&labelColor=0f171f)](docs/README.md)

*The DOI above identifies a public preprint, not peer-reviewed validation.*

</div>

---

## What it does

One governed incident, end to end — every step is deployed behavior:

1. An agent **onboards** and gets a process identity; every later write is
   attributable to that specific process.
2. It **checks in** after each unit of work with what it did and its stated
   confidence; recorded outcomes (tests, exit codes, reviews) are compared
   against that claim.
3. The server **scores** the check-in into four state coordinates and runs a
   decision ladder that returns **proceed or pause** — with graduated `guide`
   sub-actions on the proceed side — always with a named reason and a next
   step.
4. A paused agent's further check-ins are **refused** until it recovers: a
   written reflection while its state remains unhealthy, or a quick resume
   once the state has settled back to safe. A reflection can be reviewed by
   another healthy agent whose resolution can attach conditions to its next
   check-ins; stale pauses expire rather than wedging an abandoned agent.
5. The recovering agent stores the finding in **shared memory** with writer
   attribution, where the next agent searches before repeating the mistake. The whole chain is
   replayable from the audit trail.

The surfaces below are that chain broken into its parts:

| Core surface | What the operator gets |
|---|---|
| **Accountable identity** | Bind writes to a process instance and retain what it did, claimed, and observed. Reads can remain open; every write is bound to an agent identity — strictly proof-carrying when `STRICT_IDENTITY_REQUIRED` is set, auto-minted otherwise (see the [trust contract](docs/trust-contract.md)). |
| **Evidence-linked calibration** | Compare stated confidence with tests, exit codes, tool results, review labels, and other recorded outcomes. |
| **Policy and recovery** | Return a named action, reason, and next step; enforce pauses on governed write surfaces; support a `reflect → validate → resume` path. |
| **Operator visibility** | Inspect lifecycle, health, state, evidence, and policy history through MCP/HTTP APIs and a self-hosted dashboard. |

Selected state values carry an explicit provenance label, so a number can be
wrong but cannot silently pretend to be a measurement. A mechanical lint
enforces label presence on the metrics read surface today; the full
`measured` / `derived` / `prior` / `unknown` vocabulary and wider coverage are
the staged target tracked in the [trust contract](docs/trust-contract.md).

The core loop is deliberately small. Four further surfaces build **on** that
record rather than beside it. Each is independently usable and none is required
to run the check-in loop.

| Surface | What it adds |
|---|---|
| **Shared knowledge graph** | A provenance-aware store agents search before acting and write findings back to, with tagging, supersede, and archival lifecycle. |
| **Structured review** | Agents request review of each other's work; theses, disagreement, and resolution are recorded rather than resolved in chat. |
| **Reference resident agents** | Long-running sweep, audit, triage, and narration agents shipped as working examples, not as a framework to subclass. |
| **Elixir/OTP coordination** | Leases, handoffs, dispatch, and supervision for agents that outlive a single process. |

The identity binding is what connects them: an accountable write is what makes a
stored finding, a review, or a held lease attributable to a specific process
rather than to a label.

The public [`unitares-sdk`](agents/sdk/README.md) handles connection, identity,
check-ins, heartbeats, and knowledge participation for resident agents. Its
README carries the current install command and server compatibility guidance.

## Quickstart

```bash
git clone --branch v2.19.0 --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
make demo
```

This release-tagged Docker Compose flow is the supported install path for a
local, single-operator deployment. It brings up PostgreSQL/AGE/pgvector, Redis,
and the server on loopback without requiring manual database initialization.
The source-based macOS playbook is an advanced bare-metal path, not a second
default installer.

`make demo` onboards a fresh process and sends six check-ins over the real API,
printing the response shape, decision reason, state detail, and warmup position.
In about a minute you have the full loop running against your own stack.

The dashboard is at `http://localhost:8767/dashboard`; MCP clients connect to
`http://localhost:8767/mcp/`.

Use the surface that matches the question:

- **Does the signal beat a simple baseline?** Run the
  [falsifiability harness](docs/REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc)
  against your own data — the demo shows the loop works, not that the signal predicts.
- **What does sustained operation look like?** Read the
  [maintainer-deployment snapshot](docs/PRODUCTION_SNAPSHOT.md) and its
  [data caveat](docs/operations/DEPLOYMENT_DATA_CAVEAT.md).
- **How do I operate or integrate it?** Follow the
  [user manual](docs/manual/README.md).

## Integrate an MCP client

`start_session` and `sync_state` are tools exposed by the connected UNITARES
server. A fresh process creates its own identity, then includes the returned
session binding on later writes:

```python
session = start_session(force_new=True)

result = sync_state(
    response_text=output,
    complexity=0.6,
    confidence=0.8,
    client_session_id=session["client_session_id"],
)

if result.get("success") is False:  # refused write, e.g. the agent is paused
    return_to_operator(result.get("recovery"))
elif result.get("state_summary", {}).get("action") == "pause":
    return_to_operator(result.get("next_action"))  # application-defined boundary
```

For a durable resident, preserve its identity anchor rather than minting a new
identity on every run; the [SDK lifecycle example](agents/sdk/README.md) handles
that continuity. Pair self-reported confidence with verifiable evidence whenever
possible:

| Need | Tool |
|---|---|
| Search shared memory before writing | `search_shared_memory(query=...)` |
| Record a test, task, or external outcome | `record_result(...)` |
| Request structured review | `request_review(issue_description=...)` |
| Read state without writing | `check_working_state()` |

When recording an outcome for a specific check-in, pass the `prediction_id`
from that check-in's response so the outcome grades that claim rather than an
unrelated earlier one.

`list_tools()` enumerates the complete live surface and
`describe_tool(tool_name=...)` explains any one tool. MCP, REST, the SDK, and host adapters use the same server;
Claude Code and Codex are supported clients, not server-side assumptions.

## How the runtime loop works

<div align="center">
  <img src="docs/assets/flow.png" width="100%" alt="agent acts, checks in, receives state and policy, self-regulates, and leaves an audit trail">
</div>

At each checkpoint, the agent reports a meaningful unit of work and its stated
confidence. The server resolves the process identity, associates available
outcomes, updates longitudinal state, and returns a policy action with a named
reason. The retained record lets an operator audit both the claim and the basis
for the response.

Clients can treat the policy action, reason, and next step as the stable
contract. Operators can optionally inspect four EISV coordinates covering work
progress, evidence alignment, behavioral drift, and their balance. EISV is
proprioceptive state estimation: a read of how the process is working, drawn
from auditable, published heuristics. The
[computation reference](docs/EISV_COMPUTATION.md) documents formulas, warmup,
thresholds, and source code; the
[interpretation contract](docs/ontology/eisv-proprioception-contract.md) records
which readings are supported and which are not.

## Where it fits

UNITARES runs **alongside** evals, guardrails, and sandboxes; it replaces none of
them.

| Layer | Question | Timing |
|---|---|---|
| **Evals** | Is this model good enough for a defined task? | Before or between deployments. |
| **Guardrails / sandbox** | Is this action allowed and contained? | Per action. |
| **UNITARES** | What has this running process been doing, what evidence supports its claims, and what state is it in now? | Continuously, mid-run. |

It is designed for long-lived coding, research, operations, monitoring, and
multi-agent processes that can instrument a check-in loop. It is usually not
worth the overhead for short-lived chat turns.

UNITARES is a state instrument, not an outcome oracle: it
does not decide whether an output is correct or ethical, and it
cannot detect deliberate concealment without independent evidence. The
[scope and threat model](docs/SCOPE_AND_THREAT_MODEL.md) draws that boundary
precisely. The mathematical formulation in the companion paper is a parallel
research path, not the deployed policy mechanism.

## Local control and future federation

UNITARES is self-hosted and single-operator by design: identity, telemetry,
evidence, and policy history stay on infrastructure you control, with no
outbound dependency on a vendor service.

The architecture also exposes the seams a later federation experiment would
need: process-bound identity, evidence provenance, a
[versioned telemetry envelope](docs/ontology/eisv-telemetry-envelope-v1.md), and
policy decisions with named reasons. Whether those records suffice to exchange
cross-operator attestations without centralizing raw telemetry is an open
question on the **multi-principal trust** track in the [roadmap](ROADMAP.md);
cross-governor trust, consensus, and enforcement are not deployed guarantees
today.

## Evidence and limits

Everything above is scoped by what has actually been measured. At the
[2026-08-11 frozen snapshot](docs/PRODUCTION_SNAPSHOT.md), the maintainer
deployment provided operational evidence — the system runs, at length, under
real load — not an independent efficacy study:

| Evidence | Scope |
|---|---|
| **4,573,890 audit/telemetry events** | Continuous maintainer-run operation since 2025-11-28, the first identity record. Session-resolution observations and cross-device-call records make up 91.4%; this is infrastructure/load evidence, not 4.6M independent policy decisions. |
| **71,141 stored EISV state rows** | Longitudinal state observations in `core.agent_state`; rows are not independent agents or trials. |
| **15 recorded self-recovery events** | Of 21 canonical, non-automatic lifecycle-resume records. Shows the path was exercised; not 15 independent trials or proof that pauses improved outcomes. |
| **32,181 labeled EISV windows** | [20,655 overlapping real windows from one 39-day Raspberry Pi run plus 11,526 synthetic windows](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories). Window parameters, the real/synthetic split (a per-row `provenance` column), and the generating pipeline are documented on the dataset card and indexed in the [evaluation catalog](docs/EVALUATION_INDEX.md#labelled-sets). These are windows, not independent agents or customer trajectories. |
| **6 long-running resident agents** | Configured and operating in the maintainer deployment at the snapshot date; one runs on separate hardware, the same Raspberry Pi that produced the labeled-window dataset. The same single-operator fleet as every number above, not external adopters. |

The maintainer deployment is **single-operator and co-development dogfood**: most
agents governed by the system are also building the system. Read
[`DEPLOYMENT_DATA_CAVEAT.md`](docs/operations/DEPLOYMENT_DATA_CAVEAT.md) before
citing a fleet number.

What is not yet established:

- **Predictive lift.** Unresolved — not demonstrated, and not disproved. In
  the frozen 2026-08-09 outcome-lift evaluation no overall slice separated from
  the permutation null after model selection (selective p = 0.070–0.567, none
  at or below the 0.05 threshold); some unadjusted metrics improved, none
  cleared the selection-aware threshold. That is a non-detection. Its power was
  unmeasured until the
  [power audit](docs/operations/falsifiability-power-audit-2026-08-23.md),
  which finds the instrument cannot resolve a weak effect on a cohort of this
  shape — so the snapshot sets no ceiling on EISV's forecasting power, as the
  [Reviewer Guide](docs/REVIEWER_GUIDE.md) and the
  [stop rule](docs/proposals/eisv-outcome-grounding-stop-rule-v0.md) both
  already stated. The question is settled by the pre-registered 2026-12-01
  read, not by this snapshot. There is no demonstrated prevention. The Reviewer
  Guide gives the frozen command and interpretation; compact output is
  preserved in the
  [dated ablation snapshot](docs/operations/eisv-ablation-frozen-2026-08-09.md).
- **Behavior change from review and coordination.** The system records that the
  review and coordination surfaces ran and what they concluded; whether they
  change an agent's subsequent behavior is unmeasured.
- **Robustness against a motivated attacker** optimizing the monitored proxy.
  Calibrated capability concealment is a documented structural blind spot — see
  the [scope and threat model](docs/SCOPE_AND_THREAT_MODEL.md).
- **Benefit from pausing.** The pause and recovery path runs and is recorded;
  whether pausing improved any outcome is unmeasured.

The instrument-frame validation the system does claim — reliability,
faithfulness under intervention, calibration — is scoped and partly built; the
[roadmap](ROADMAP.md) tracks it. The companion DOI identifies a
[public preprint](https://doi.org/10.5281/zenodo.19647159), not peer-reviewed
validation.

## Architecture, setup, and documentation

**Python 3.12+ · PostgreSQL + AGE + pgvector · Redis · optional Elixir/OTP
coordination.** Redis holds session and identity state, and the Docker
quickstart brings it up. The server starts without it in a degraded local-only
mode, which is not the supported path.

| Reader | Start here |
|---|---|
| Evaluator or grant reviewer | [Reviewer Guide](docs/REVIEWER_GUIDE.md) → [computation](docs/EISV_COMPUTATION.md) → [scope and threat model](docs/SCOPE_AND_THREAT_MODEL.md) |
| Integrator | [Manual](docs/manual/README.md) → [MCP clients](docs/integration/MCP_CLIENTS.md) |
| Operator | [Docker quickstart](#quickstart) → [operator runbook](docs/operations/OPERATOR_RUNBOOK.md); [bare-metal playbook](docs/install/PLAYBOOK.md) for advanced macOS installs |
| Contributor | [AGENTS.md](AGENTS.md) → [architecture](docs/UNIFIED_ARCHITECTURE.md) → [canonical sources](docs/dev/CANONICAL_SOURCES.md) |
| Research/provenance reader | [Evaluation index](docs/EVALUATION_INDEX.md) → [ontology](docs/ontology/README.md) → [proposals](docs/proposals/README.md) |

The complete documentation map is [`docs/README.md`](docs/README.md). Optional
analogies and philosophical readings are isolated under
[`docs/essays/`](docs/essays/README.md); they are not specifications or evidence.

Project operation is explicit: see the [roadmap](ROADMAP.md),
[compatibility and naming map](COMPATIBILITY.md), [governance](GOVERNANCE.md),
[contributing guide](CONTRIBUTING.md), [security policy](SECURITY.md),
[support policy](SUPPORT.md), and [release process](docs/operations/RELEASE_PROCESS.md).

## Ecosystem repositories

These are adjacent integrations, testbeds, and research projects; the core
quickstart does not require them.

| Project | Role |
|---|---|
| [anima-mcp](https://github.com/cirwel/anima-mcp) | Raspberry Pi longitudinal testbed. |
| [unitares-governance-plugin](https://github.com/cirwel/unitares-governance-plugin) | Codex and Claude Code lifecycle/hook packaging. |
| [unitares-host-adapter](https://github.com/cirwel/unitares-host-adapter) | Thin bindings for additional clients and model hosts. |
| [fermata](https://github.com/cirwel/fermata) | Governed-effect runtime research seed. |
| [eisv-lumen](https://github.com/cirwel/eisv-lumen) | Dataset generation and labeling pipeline. |
| [unitares-paper-v6](https://github.com/cirwel/unitares-paper-v6) | Companion preprint and research formulation. |

## Citation and license

Kenny Wang ([ORCID 0009-0006-7544-2374](https://orcid.org/0009-0006-7544-2374)),
CIRWEL Systems. See [`CITATION.cff`](CITATION.cff) for the versioned citation.
The DOI below identifies a public preprint, not peer-reviewed validation — see
[Evidence and limits](#evidence-and-limits).

```bibtex
@misc{wang2026unitares,
  author = {Wang, Kenny},
  title  = {{UNITARES}: Information-Theoretic Governance of Heterogeneous Agent Fleets},
  year   = {2026},
  doi    = {10.5281/zenodo.19647159}
}
```

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

<div align="center">

<img alt="UNITARES — runtime governance for AI-agent fleets" src="docs/assets/hero-v2.png" width="100%">

### Runtime state, accountability, and recovery for long-lived AI-agent fleets.

</div>

**Status:** v2.18.0. Sustained operation is documented in one long-running
maintainer deployment. External adoption remains unvalidated, and the current
outcome-lift evaluation found no result beyond a selection-aware null.

An agent forty turns into a task reports high confidence while its tests are
failing. Every individual tool call was allowed, so an action-level guardrail may
have nothing to object to. What is missing is a longitudinal record that compares
what the agent claims with what actually happened.

**UNITARES keeps that record.** Agents check in after meaningful units of work.
The server binds writes to a process identity, stores claims and outcomes, derives
a four-score state estimate, and returns a policy decision with a named reason.
It is a self-hosted MCP/HTTP service, not an agent framework or hosted platform.

<div align="center">

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.12+-2f7d72?style=flat-square&labelColor=0f171f)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-2f7d72?style=flat-square&labelColor=0f171f)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

**Maintainer dogfood since November 2025 · 4.5M+ recorded audit/telemetry events.**

That count is evidence of sustained operation in one maintainer-run environment,
not external adoption or governance efficacy.

[![Quickstart](https://img.shields.io/badge/▶-quickstart-5eead4?style=for-the-badge&labelColor=0f171f)](#quickstart)
[![Evidence](https://img.shields.io/badge/evidence-check_it-f5a623?style=for-the-badge&labelColor=0f171f)](#evidence-and-limits)
[![Docs](https://img.shields.io/badge/docs-read-7d8f97?style=for-the-badge&labelColor=0f171f)](docs/README.md)

</div>

---

## What it does

| Core surface | What the operator gets |
|---|---|
| **Accountable identity** | Bind writes to a process instance and retain what it did, claimed, and observed. Reads can remain open; writes are attributable. |
| **Evidence-linked calibration** | Compare stated confidence with tests, exit codes, tool results, review labels, and other recorded outcomes. |
| **Policy and recovery** | Return a named action, reason, and next step; enforce pauses on governed write surfaces; support a `reflect → validate → resume` path. |
| **Operator visibility** | Inspect lifecycle, health, state, evidence, and policy history through MCP/HTTP APIs and a self-hosted dashboard. |

The core loop is deliberately small. Optional modules add a provenance-aware
knowledge graph, structured review, reference resident agents, and Elixir/OTP
coordination for leases, handoffs, dispatch, and supervision. They can be
used independently of the basic check-in loop.

The public [`unitares-sdk`](agents/sdk/README.md) handles connection, identity,
check-ins, heartbeats, and knowledge participation for resident agents. Its
README carries the current install command and server compatibility guidance.

## Quickstart

```bash
git clone --branch v2.18.0 --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
make demo
```

This release-tagged Docker Compose flow is the **Tier-1 install contract** for a
local, single-operator deployment. It brings up PostgreSQL/AGE/pgvector, Redis,
and the server on loopback without requiring manual database initialization.
The source-based macOS playbook is an advanced bare-metal path, not a second
default installer.

`make demo` onboards a fresh process and sends six check-ins over the real API.
It prints the response shape, decision reason, state detail, and warmup position.
The demo answers **“is my stack wired?”** It does not establish predictive value.

The dashboard is at `http://localhost:8767/dashboard`; MCP clients connect to
`http://localhost:8767/mcp/`.

Use the surface that matches the question:

- **Does the signal beat a simple baseline?** Run the
  [falsifiability harness](docs/REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc).
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

action = result.get("state_summary", {}).get("action")
if action in ("pause", "reject"):
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

`list_tools()` enumerates the complete live surface and `describe_tool(name)`
explains any one tool. MCP, REST, the SDK, and host adapters use the same server;
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
progress, evidence alignment, behavioral drift, and their balance. These are
auditable heuristics, not literal thermodynamic quantities or universal labels.
The [computation reference](docs/EISV_COMPUTATION.md) documents formulas,
warmup, thresholds, and source code; the
[interpretation contract](docs/ontology/eisv-proprioception-contract.md) records
permitted readings, refuted claims, and open evaluation gaps.

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

It is not an outcome oracle: it does not decide whether an output is correct or
ethical, and it cannot detect deliberate concealment without independent
evidence. The information-theoretic and ODE formulation in the companion paper
remains a research target and parallel diagnostic path, not the deployed policy
mechanism.

## Local control and future federation

UNITARES starts as a self-hosted governor under one operator's control. The
architecture exposes the seams that later federation experiments would need:
process-bound identity, evidence provenance, a
[versioned telemetry envelope](docs/ontology/eisv-telemetry-envelope-v1.md), and
policy decisions with named reasons.

Future experiments can test whether those records are sufficient for exchanging
cross-operator attestations without centralizing raw telemetry. Cross-governor
trust, consensus, and enforcement are not deployed guarantees. Experiments
between mutually distrustful governors remain gated on independent-operator
validation in the [roadmap](ROADMAP.md).

## Evidence and limits

At the [2026-08-11 frozen snapshot](docs/PRODUCTION_SNAPSHOT.md), the maintainer
deployment provided operational evidence, not an independent efficacy study:

| Evidence | Scope |
|---|---|
| **4,573,890 audit/telemetry events** | Continuous maintainer-run operation since 2025-11-28. Session-resolution observations and cross-device-call records make up 91.4%; this is infrastructure/load evidence, not 4.6M independent policy decisions. |
| **71,141 stored EISV state rows** | Longitudinal state observations in `core.agent_state`; rows are not independent agents or trials. |
| **15 recorded self-recovery events** | Of 21 canonical, non-automatic lifecycle-resume records. Shows the path was exercised; not 15 independent trials or proof that pauses improved outcomes. |
| **32,181 labeled EISV windows** | [20,655 overlapping real windows from one 39-day Raspberry Pi run plus 11,526 synthetic windows](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories). These are windows, not independent agents or customer trajectories. |

The maintainer deployment is **single-operator and co-development dogfood**: most
agents governed by the system are also building the system. Read
[`DEPLOYMENT_DATA_CAVEAT.md`](docs/operations/DEPLOYMENT_DATA_CAVEAT.md) before
citing a fleet number.

The frozen 2026-08-09 outcome-lift evaluation is a negative result: after model
selection, no overall slice separated from the permutation null (selective
p = 0.070–0.567). Some unadjusted metrics improved, but none cleared the
selection-aware threshold. There is **no demonstrated prevention**. The
[Reviewer Guide](docs/REVIEWER_GUIDE.md) gives the frozen command and
interpretation; compact output is preserved in the
[dated ablation snapshot](docs/operations/eisv-ablation-frozen-2026-08-09.md).

Robustness against a motivated attacker optimizing the monitored proxy remains
unproven. Calibrated capability concealment is a documented structural blind
spot. See the [scope and threat model](docs/SCOPE_AND_THREAT_MODEL.md).

The companion DOI identifies a
[public preprint](https://doi.org/10.5281/zenodo.19647159), not peer-reviewed
validation.

## Architecture, setup, and documentation

**Python 3.12+ · PostgreSQL + AGE + pgvector · Redis · optional Elixir/OTP
coordination.** Redis is the de-facto session store in the long-running
maintainer deployment; without it the server starts in degraded local-only mode
suitable for the demo.

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

```bibtex
@misc{wang2026unitares,
  author = {Wang, Kenny},
  title  = {{UNITARES}: Information-Theoretic Governance of Heterogeneous Agent Fleets},
  year   = {2026},
  doi    = {10.5281/zenodo.19647159}
}
```

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

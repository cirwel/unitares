<div align="center">

### A federation kernel for accountable AI agents.

Identity, claims and evidence, review, outcomes, and reconstruction across agent runtimes.

</div>

UNITARES is a self-hosted MCP server that gives agents a shared, attributed
record while they keep their own reasoning loops and tools. A new process can
recover durable claims earlier processes stored, inspect the retained evidence
and disagreement, and continue the work with its own identity.

Here, **federation kernel** means many independent agent runtimes and harnesses
sharing one operator-controlled server and authority domain. It does not promise
autonomous cross-server replication or a new agent runtime. The deployed building blocks are process identities, check-ins,
attributed findings, structured reviews, outcome events, and history retrieval.
Reconstruction uses those records; its fidelity and benefit over Git plus a
structured handoff still need comparative evaluation.

**Status:** v2.22.0. Running continuously since November 2025.

<div align="center">

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.12+-2f7d72?style=flat-square&labelColor=0f171f)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-2f7d72?style=flat-square&labelColor=0f171f)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

[![Quickstart](https://img.shields.io/badge/▶-quickstart-5eead4?style=for-the-badge&labelColor=0f171f)](#quickstart)
[![Evidence](https://img.shields.io/badge/evidence-check_it-f5a623?style=for-the-badge&labelColor=0f171f)](#evidence-and-limits)
[![Docs](https://img.shields.io/badge/docs-read-7d8f97?style=for-the-badge&labelColor=0f171f)](docs/README.md)

</div>

---

## What it does

| What you get | The mechanism |
|---|---|
| **Identity** — who made the claim? | `start_session(force_new=true)` binds a fresh process; retain `client_session_id` for later calls. Real lineage links inherited work, not authority or sameness. Write enforcement depends on the configured identity gates. |
| **Claims and evidence** — what was asserted, and what supports it? | `sync_state` submits a work report from which durable state is derived; it does not retain the original report text. `store_finding` and `update_finding` retain durable attributed claims and corrections. Supply evidence and provenance explicitly. |
| **Review** — what was challenged, and on what terms? | `request_review` opens a structured review; `dialectic` records positions, disagreement, conditions, and resolution. A request alone is not a completed review. |
| **Outcomes** — what actually happened? | `record_result` records typed outcomes; an explicit `prediction_id` links a result to the prediction it grades. A stored outcome does not independently verify the caller's report. |
| **Reconstruction** — what should the next process recover? | `search_shared_memory`, knowledge reads, review history, `export`, and operator-gated outcome-evidence reads expose different retained records. Clients assemble them; there is no single reconstruction tool or guarantee that the original check-in text survives. |

<div align="center">
  <img src="docs/assets/flow.png" width="100%" alt="agent acts, checks in, receives state and policy, self-regulates, and leaves an audit trail">
</div>

The core record is retained in your deployment and accessible through MCP,
HTTP, and the self-hosted dashboard. Core storage needs no external model
provider. Optional cloud consultation and configured integrations can send
submitted content outside the deployment; their privacy and authorization
settings apply.

Clients can treat the policy action, reason, and next step as the stable
contract; the enforcement record rides alongside it. Operators can additionally
read four EISV coordinates — work progress, evidence alignment, behavioral
drift, and their balance. Those are published heuristics, documented in the
[computation reference](docs/EISV_COMPUTATION.md).

## Scope

UNITARES is a state instrument, not an outcome oracle. It
does not decide whether an output is correct or ethical, and it
cannot detect deliberate concealment without independent evidence.
Whether pausing an agent prevents anything is untested;
[Evidence and limits](#evidence-and-limits) is the measured record.

## Quickstart

```bash
git clone --branch v2.21.0 --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait   # PostgreSQL/AGE/pgvector, Redis, lease plane, server on loopback
```

MCP clients connect to `http://localhost:8767/mcp/`; the dashboard is at
`http://localhost:8767/dashboard`. The loop, from any client:

```python
session = start_session(force_new=True)
sid = session["client_session_id"]

result = sync_state(
    response_text=output,
    complexity=0.6,
    confidence=0.8,
    client_session_id=sid,
)
refused = (
    result.get("success") is False              # error-shaped refusal, e.g. paused
    or result.get("tool_class") == "required"   # identity refusal: success-shaped
)
if refused:
    return_to_operator(result.get("recovery") or result.get("next_step"))
elif result.get("state_summary", {}).get("action") == "pause":
    return_to_operator(result.get("next_action"))   # your boundary to honor

record_result(
    outcome_type="test_passed" if tests_passed else "test_failed",
    prediction_id=result.get("prediction_id"),      # grades this check-in's claim
    client_session_id=sid,
)
state = check_working_state(client_session_id=sid)
```

Two demos run this against the live server:

- **`make demo`** onboards a process and sends six check-ins over the real API.
- **`make coordination-demo`** shows the identity guarantees biting: two agents
  onboard, governance exchanges their continuity credentials for single-use,
  request-bound Ed25519 attestations, A's attestation is refused when it claims
  B's UUID, a captured attestation is refused on replay, and a governed
  `maintenance:/` surface moves through an identity-checked handoff before
  release.

This release-tagged Docker Compose flow is the supported install path for a
local, single-operator deployment; after cloning, the one-command install/start
is `docker compose up -d --wait`. The tag is the latest verified public release
and may trail the source version in **Status** — newer releases are on the
[releases page](https://github.com/cirwel/unitares/releases).

Evaluating rather than installing? Start with
[Evidence and limits](#evidence-and-limits) and the
[Reviewer Guide](docs/REVIEWER_GUIDE.md). Deploying? Use the
[user manual](docs/manual/README.md).

## Where it fits

UNITARES runs **alongside** evals, guardrails, and sandboxes. It replaces none of
them.

| Layer | Question | Timing |
|---|---|---|
| **Evals** | Is this model good enough for a defined task? | Before or between deployments. |
| **Guardrails / sandbox** | Is this action allowed and contained? | Per action. |
| **UNITARES** | What has this running process been doing, what evidence supports its claims, and what state is it in now? | Continuously, mid-run. |

It is for **one operator running several long-lived agents** — coding, research,
operations, monitoring — on infrastructure they control. It is usually not worth
the overhead for short-lived chat turns.

It governs the agent's loop from outside rather than owning it, so Claude Code,
Codex, custom runtimes, and resident agents stay different userlands while
sharing one accountable record. Plain-language definition:
[What UNITARES is](docs/PRODUCT_DEFINITION.md).

## Tools

Start with the five-part workflow above. Behavioral state estimation, policy
and recovery remain deployed capabilities; inference, diagnostics, calibration,
configuration, exports, and administration support more specialized workflows.
The [capability and deployment guide](docs/CAPABILITIES_AND_DEPLOYMENT.md) maps
the core workflow to real tools and explains which services each profile needs.

One complete catalog advertises every registered-and-mounted public tool,
including primary workflow aliases. **Core and advanced are reading paths, not visibility or permission
modes.** No tool mode is needed; legacy `GOVERNANCE_TOOL_MODE` settings are
ignored. `list_tools(category=...)` browses the catalog, and
`describe_tool(tool_name=..., action=...)` returns full parameter details.
Primary workflow names are preferred; raw names remain available for
compatibility. Each action retains its authorization requirements.

The public [unitares-sdk](agents/sdk/README.md) supports client
integration. Ordinary fresh processes onboard fresh; dedicated resident
substrate identities follow their adapter's explicit contract. A display name,
UUID alone, or old continuity token does not establish cross-process identity.

## Deployment profiles

- **Local operator:** the release-tagged Compose quickstart provisions PostgreSQL
  with AGE/pgvector, Redis, the lease plane, and the server.
- **Private Glama installation:** the separate storage-backed bundle provides
  those backing services behind stdio. Persist `/data`; see the
  [Glama guide](docs/deployment/glama.md) for build, validation, and upgrade limits.
- **Client of an existing deployment:** connect over MCP/HTTP using that
  operator's access policy; installing a client does not provision the server.

A bare Python process with missing storage dependencies can expose discovery
without usable durable operations. It is not a supported lightweight core
installation. Neither bundle includes an inference provider, automated reviewer
fleet, or resident fleet by default. Catalog visibility does not establish
service readiness or successful execution.

## Evidence and limits

At the [2026-08-11 frozen snapshot](docs/PRODUCTION_SNAPSHOT.md): 4,573,890
audit/telemetry events, 71,141 stored EISV state rows, 6 long-running resident
agents, 15 recorded self-recovery events, and
[32,181 labeled EISV windows](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories)
published as a dataset. Those numbers and the rest of the build record are in
[Evidence and limits, in full](docs/EVIDENCE_AND_LIMITS.md).

They come from one operator, since 2025-11-20: a single deployment, and
co-development dogfood, since most agents governed by the system are also
building it. Read the
[deployment data caveat](docs/operations/DEPLOYMENT_DATA_CAVEAT.md) before citing
any fleet number.

Every claim below carries an evidence class. A class says what a result
supports; it is not a positive or negative judgement about the project. A
registered operational `FAIL` can close a scheduled line of work without
scientifically refuting the underlying capability, and a claim earns `REFUTED`
only when target, counterfactual, independent unit, power, decision rule, and
read protocol all support it — see the
[inference-status contract](docs/ontology/falsification-inference-containment-2026-08-22.md).

| Evidence class | What it licenses |
|---|---|
| **Operational observation** | A named mechanism ran in the stated deployment. Not benefit, correctness, or generality. |
| **Exercised path** | A code path ran and left countable, replayable records. Execution, not benefit. |
| **Non-detection** | The test did not separate the candidate from its comparison. Without power, that is not absence. |
| **Structural limit** | A boundary that follows from the design itself. More data does not move it. |
| **Untested** | No suitable measurement has been made. |

Three further classes — **Benchmark pass / fail**, **Unidentified /
inconclusive**, and **Mismatch / path bound** — are available for result types
this table does not currently hold.

### Current claim status

| Question | Status | What the record supports |
|---|---|---|
| Sustained operation | **Operational observation** | The maintainer deployment has run continuously under real load. The counts above are rows, events, and configured residents. |
| Identity and audit trail | **Exercised path** | Process-bound writes, evidence records, policy responses, and replayable audit history are deployed. This establishes mechanism execution. |
| Pause actuation and delivery | **Event reconciled; protection untested** | A governed pause landed on 2026-08-09. At the 2026-08-06 audit, a cadence window had downgraded 195 of 218 recorded pauses (89.4%) before delivery; the current rate has not been re-measured. See [ledger rows 24, 27, and 28](docs/ontology/eisv-proprioception-contract.md). |
| Predictive lift | **Non-detection; inconclusive for weak effects** | In the frozen 2026-08-09 cohort, no slice cleared the selection-aware null (selective p = 0.070–0.567). The first power characterisation was withdrawn for corrupted synthetic pairing and uncontrolled class-balance drift, and the preserved record omits the cluster geometry needed to reconstruct read-specific power, so the corrected [power audit](docs/operations/falsifiability-power-audit-2026-08-23.md) sets no standing AUC ceiling. |
| Incident prevention or benefit from pausing | **Untested** | No governed-versus-ungoverned comparison has shown an incident prevented or an outcome improved by pausing. |
| Review binds on the reviewed agent | **Exercised path** | A paused agent cannot resolve its own session over a standing reviewer objection: the submission is recorded, the session is not resolved, and it waits for facilitation (`src/dialectic_protocol.py`). 33 non-canary sessions carry such a submission after a reviewer's rejection. Since the 2026-07-02 reviewer-label split, 81 non-canary verdicts are recorded and 77 carry the reviewer's conditions; 42 dissent, one of which is a parse failure recorded as disagreement rather than a reviewer's judgement. |
| Benefit from review and coordination | **Untested** | These records cover dialectic-mediated review only: review run through subagent councils or external models leaves no row unless filed through `reviewer_provenance`, a field no caller has yet populated, so that gap is in recording rather than occurrence. Benefit is separate and unmeasured — every change went through the reviewed path, leaving zero untreated cases to compare against. |
| Robustness to a motivated attacker | **Structural limit** | A capable process can optimize or conceal behavior outside the monitored proxy; independent evidence remains necessary. See the [scope and threat model](docs/SCOPE_AND_THREAT_MODEL.md). |

The outcome read carries a protocol qualification. After the frozen cutoff,
recurring automation exposed live discrimination output: the ablation watchdog
completed 42 of 51 executions and the dogfood guard completed 43 of 52. Those
jobs are paused. The fixed
[2026-12-01 gate](docs/proposals/eisv-outcome-grounding-stop-rule-v0.md) remains
an operational decision rule, but it is not the only post-registration read and
cannot be described as clean single-read blinding; its report must disclose the
interim access and read-specific power.

The validation the system does claim — reliability, faithfulness under
intervention, and calibration — is scoped and partly built; the
[roadmap](ROADMAP.md) tracks it. The DOI identifies a
[public preprint](https://doi.org/10.5281/zenodo.19647159), not peer-reviewed
validation.

## Documentation

**Python 3.12+ · PostgreSQL + AGE + pgvector · Redis · optional Elixir/OTP
coordination.** Redis holds session and identity state and the Docker quickstart
brings it up; the server starts without it in a degraded local-only mode, which
is not the supported path.

| Reader | Start here |
|---|---|
| Evaluator or grant reviewer | [Reviewer Guide](docs/REVIEWER_GUIDE.md) → [computation](docs/EISV_COMPUTATION.md) → [scope and threat model](docs/SCOPE_AND_THREAT_MODEL.md) |
| Integrator | [Manual](docs/manual/README.md) → [MCP clients](docs/integration/MCP_CLIENTS.md) |
| Operator | [Docker quickstart](#quickstart) → [operator runbook](docs/operations/OPERATOR_RUNBOOK.md); [bare-metal playbook](docs/install/PLAYBOOK.md) |
| Contributor | [AGENTS.md](AGENTS.md) → [architecture](docs/UNIFIED_ARCHITECTURE.md) → [canonical sources](docs/dev/CANONICAL_SOURCES.md) |
| Research reader | [Evaluation index](docs/EVALUATION_INDEX.md) → [ontology](docs/ontology/README.md) → [proposals](docs/proposals/README.md) |

Complete map: [`docs/README.md`](docs/README.md). Analogies and philosophical
readings are isolated under [`docs/essays/`](docs/essays/README.md) — not
specifications, not evidence. Project operation: [roadmap](ROADMAP.md),
[compatibility](COMPATIBILITY.md), [governance](GOVERNANCE.md),
[contributing](CONTRIBUTING.md), [security](SECURITY.md), [support](SUPPORT.md),
[releases](docs/operations/RELEASE_PROCESS.md).

## Ecosystem repositories

Adjacent integrations, testbeds, and research projects; the quickstart needs none
of them.

| Project | Role |
|---|---|
| [unitares-resident](https://github.com/cirwel/unitares-resident) | First-party agent userland built on the public SDK contract. Early skeleton, not yet a usable general-purpose agent. |
| [anima-mcp](https://github.com/cirwel/anima-mcp) | Raspberry Pi longitudinal testbed. |
| [unitares-governance-plugin](https://github.com/cirwel/unitares-governance-plugin) | Codex and Claude Code lifecycle/hook packaging. |
| [unitares-host-adapter](https://github.com/cirwel/unitares-host-adapter) | Thin bindings for additional clients and model hosts. |
| [fermata](https://github.com/cirwel/fermata) | Governed-effect runtime research seed. |
| [eisv-lumen](https://github.com/cirwel/eisv-lumen) | Dataset generation and labeling pipeline. |
| [unitares-paper-v6](https://github.com/cirwel/unitares-paper-v6) | Companion preprint and research formulation. |

The lowercase residents under [`agents/`](agents/README.md) are reference clients
and operational examples, not a framework to subclass.

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

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

<div align="center">

<img alt="UNITARES — runtime governance for AI-agent fleets" src="docs/assets/hero-v2.png" width="100%">

### Runtime state, accountability, and recovery for long-lived AI-agent fleets.

</div>

**Status:** v2.17.0 public overview. Production data below is maintainer
dogfood; current outcome-lift evidence is negative, and external adoption
remains unvalidated.

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

| Surface | Purpose |
|---|---|
| **Identity and audit** | Bind writes to a process instance and retain what it did, claimed, and observed. Reads can remain open; writes are accountable. |
| **Runtime state** | Estimate four behavioral coordinates—Energy, Integrity, drift (`S`), and derived Valence—using auditable heuristic blends. |
| **Calibration** | Compare claimed confidence with external outcomes such as tests, exit codes, tool results, or review labels. |
| **Policy and recovery** | Return a policy decision, enforce pauses on governed write surfaces, and provide a `reflect → validate → resume` recovery path. |
| **Knowledge graph** | Store typed discoveries with provenance, semantic search, status, and supersession rather than accumulating duplicate memory. |
| **Dialectic review** | Route disputed decisions through thesis, antithesis, and synthesis, peer-to-peer or with an LLM reviewer. |
| **Fleet coordination** | Ship reference residents plus an Elixir/OTP coordination layer for leases, handoffs, dispatch, and supervision. |

The public [`unitares-sdk`](agents/sdk/README.md) handles connection, identity,
check-ins, heartbeats, and knowledge participation for resident agents. It ships
in-tree and installs with `pip install ./agents/sdk`; it is not yet on PyPI.

## The loop

<div align="center">
  <img src="docs/assets/flow.png" width="100%" alt="agent acts, checks in, receives state and policy, self-regulates, and leaves an audit trail">
</div>

An agent completes a unit of work, calls `sync_state()`, and reads the returned
decision. Four coordinates provide optional detail:

| Coordinate | Operational question | Deployed meaning |
|---|---|---|
| **E · Energy** | Is work advancing? | Blend of progress, coherence, complexity calibration, and available outcomes. |
| **I · Integrity** | Do claims match evidence? | Calibration and outcome consistency. |
| **S · Drift** | Is behavior moving away from its reference? | Heuristic drift, regime-instability, and complexity-divergence blend—not literal entropy. |
| **V · Valence** | Is motion outrunning integrity, or the reverse? | EMA-smoothed `E - I`; derived, not independent. |

The current implementation has three distinct maturity stages:

1. **Check-ins 1–2:** the Φ cold-start prior owns the verdict because behavioral
   confidence is below 0.3.
2. **Check-ins 3–24:** behavioral assessment is authoritative but uses fixed
   universal thresholds.
3. **From check-in 25:** `baseline_confidence >= 0.8` against the 30-update
   target, enabling self-relative z-score scoring.

Absolute safety floors and basin gates remain active throughout. The exact
formulas and source references are in
[`EISV_COMPUTATION.md`](docs/EISV_COMPUTATION.md).
The [EISV proprioception contract](docs/ontology/eisv-proprioception-contract.md)
records the permitted interpretation, refuted claims, and open evaluation gaps.

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

It is not an output validator or universal ethics classifier. It is not an outcome oracle,
grand jury, bad-result detector, or deliberate-concealment detector. The deployed
EISV values are behavioral heuristics; the information-theoretic and ODE
formulation in the companion paper is a research target and parallel diagnostic
path, not the post-warmup verdict mechanism.

For AI clients, the stable contract is the returned policy action and named
reason; EISV is inspectable state telemetry, not a label the client must invent
meaning for.

## Quickstart

```bash
git clone --branch v2.17.0 --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
make demo
```

`make demo` onboards a fresh process and sends six check-ins over the real API.
It prints the response shape, decision reason, four coordinates, and warmup
position. The demo answers **“is my stack wired?”** It stays below the
self-relative threshold and therefore does not establish predictive value.

Use separate surfaces for separate questions:

- **Does the signal beat a simple baseline?** Run the
  [falsifiability harness](docs/REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc).
- **What does sustained operation look like?** Read the
  [production snapshot](docs/PRODUCTION_SNAPSHOT.md) and its
  [deployment-data caveat](docs/operations/DEPLOYMENT_DATA_CAVEAT.md).
- **How do I operate or integrate it?** Follow the
  [user manual](docs/manual/README.md).

The dashboard is at `http://localhost:8767/dashboard`; MCP clients connect to
`http://localhost:8767/mcp/`.

## Integrate in two calls

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
    agent.require_human_review(result.get("next_action"))
```

Pair self-reported confidence with verifiable evidence whenever possible:

| Need | Tool |
|---|---|
| Search shared memory before writing | `search_shared_memory(query=...)` |
| Record a test, task, or external outcome | `record_result(...)` |
| Request structured review | `request_review(issue_description=...)` |
| Read state without writing | `check_working_state()` |

`list_tools()` enumerates the complete live surface and `describe_tool(name)`
explains any one tool. MCP, REST, the SDK, and host adapters all use the same
server; Claude Code and Codex are supported clients, not server-side assumptions.

## Evidence and limits

At the [2026-08-11 frozen snapshot](docs/PRODUCTION_SNAPSHOT.md), the maintainer
deployment provided operational evidence, not an independent efficacy study:

| Evidence | Scope |
|---|---|
| **4,573,890 audit/telemetry events** | Continuous maintainer-run operation since 2025-11-28. Session-resolution observations and cross-device-call records make up 91.4%; this is infrastructure/load evidence, not 4.6M independent policy decisions. |
| **71,141 stored EISV state rows** | Longitudinal state observations in `core.agent_state`; rows are not independent agents or trials. |
| **15 recorded self-recovery events** | Of 21 canonical, non-automatic lifecycle-resume records. Shows the path was exercised; not 15 independent trials or proof that pauses improved outcomes. |
| **32,181 labeled EISV windows** | [20,655 overlapping real windows from one 39-day Raspberry Pi run plus 11,526 synthetic windows](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories). These are windows, not independent agents or customer trajectories. |

The public deployment is **single-operator and co-development dogfood**: most
agents governed by the system are also building the system. Read
[`DEPLOYMENT_DATA_CAVEAT.md`](docs/operations/DEPLOYMENT_DATA_CAVEAT.md) before
citing a fleet number.

The frozen 2026-08-09 trusted-anchor matrix is a negative current result. Across
strict/task scope, 30/90-day windows, and 0/5/30-minute leads, every overall
slice is `NOISE-LEVEL` after comparing the selected best candidate with the
best-of-candidates permutation null (selective p = 0.070–0.567). Some unadjusted
models improve both AUC and Brier, often through `prior_risk`, `prior_s`, or
dispersion, but none separates from that selection-aware null at p < 0.05. There
is **no demonstrated prevention**. The [Reviewer Guide](docs/REVIEWER_GUIDE.md)
gives the frozen command and interpretation.
The compact output is preserved in the
[dated ablation snapshot](docs/operations/eisv-ablation-frozen-2026-08-09.md).

Robustness against a motivated attacker optimizing the monitored proxy remains
unproven. Calibrated capability concealment is a documented structural blind
spot. See the [scope and threat model](docs/SCOPE_AND_THREAT_MODEL.md).

The companion DOI identifies a [public preprint](https://doi.org/10.5281/zenodo.19647159),
not peer-reviewed validation. Federation across mutually distrustful governors
is a research direction; the current deployment is one governor operated by one
principal.

## Architecture, setup, and documentation

**Python 3.12+ · PostgreSQL + AGE + pgvector · Redis · optional Elixir/OTP
coordination.** Redis is the de-facto session store in production; without it the
server starts in degraded local-only mode suitable for the demo.

| Reader | Start here |
|---|---|
| Evaluator or grant reviewer | [Reviewer Guide](docs/REVIEWER_GUIDE.md) → [computation](docs/EISV_COMPUTATION.md) → [scope and threat model](docs/SCOPE_AND_THREAT_MODEL.md) |
| Integrator | [Manual](docs/manual/README.md) → [MCP clients](docs/integration/MCP_CLIENTS.md) |
| Operator | [Install playbook](docs/install/PLAYBOOK.md) → [operator runbook](docs/operations/OPERATOR_RUNBOOK.md) |
| Contributor | [AGENTS.md](AGENTS.md) → [architecture](docs/UNIFIED_ARCHITECTURE.md) → [canonical sources](docs/dev/CANONICAL_SOURCES.md) |
| Research/provenance reader | [Evaluation index](docs/EVALUATION_INDEX.md) → [ontology](docs/ontology/README.md) → [proposals](docs/proposals/README.md) |

The complete documentation map is [`docs/README.md`](docs/README.md). Optional
analogies and philosophical readings are isolated under
[`docs/essays/`](docs/essays/README.md); they are not specifications or evidence.

Project operation is explicit: see the [roadmap](ROADMAP.md),
[compatibility and naming map](COMPATIBILITY.md), [governance](GOVERNANCE.md),
[support policy](SUPPORT.md), and [release process](docs/operations/RELEASE_PROCESS.md).

## Related CIRWEL projects

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

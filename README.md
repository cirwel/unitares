<div align="center">

<img alt="UNITARES — runtime governance for AI-agent fleets" src="docs/assets/hero-v2.png" width="100%">

### A stateful governance runtime for fleets of autonomous AI agents.

**Each agent is judged against its own history and calibration — not a fixed per-action rule — and gets back a verdict it can act on, mid-run.**<br/>
Most controls are stateless: they check one action against one rule. UNITARES carries each agent's trajectory, calibration, and recent verdicts into the next decision — so an agent *drifting* surfaces while its output still looks fine, and it self-corrects (`proceed` / `guide` / `pause` / `reject`) before an external guardrail has to fire.

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.12+-2f7d72?style=flat-square&labelColor=0f171f)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-2f7d72?style=flat-square&labelColor=0f171f)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

*Status: live. First public commit 2025-12-04 · 3.7M+ governance events in production · dogfooded.*

[![Quickstart](https://img.shields.io/badge/▶-quickstart-5eead4?style=for-the-badge&labelColor=0f171f)](#try-the-demo-locally)
[![Docs](https://img.shields.io/badge/docs-read-7d8f97?style=for-the-badge&labelColor=0f171f)](docs/README.md)
[![Paper v6](https://img.shields.io/badge/paper-v6-8957e5?style=for-the-badge&labelColor=0f171f)](https://github.com/cirwel/unitares-paper-v6)
[![Verify it yourself](https://img.shields.io/badge/verify-it_yourself-f5a623?style=for-the-badge&labelColor=0f171f)](docs/REVIEWER_GUIDE.md)

One layer of the **[CIRWEL stack](https://cirwel.github.io)** — runtime safety infrastructure for autonomous agents, *after* deployment. UNITARES is the governed fleet; [Anima](https://github.com/cirwel/anima-mcp) is its physical edge testbed. [Full index ↗](https://cirwel.github.io)

</div>

---

- **Stateful, state-aware verdicts.** Each agent is judged against its *own* ~30-check-in baseline and recent history — not a fixed per-action rule — so slow degradation surfaces while the output still looks fine, and the verdict reflects context, not just the current action.
- **Confidence grounded in results.** Self-reported `confidence` is scored against real evidence — test exit codes, tool output, file ops. An agent can inflate the number; it can't inflate its success rate, and that calibration feeds back into future verdicts.
- **Peer review that becomes a runtime constraint.** On a disputed verdict, an authority-weighted peer agent from the fleet reviews (self-review blocked); the synthesized conditions *persist* and gate that agent's future decisions — a runtime constraint, not just debate text.
- **One signal the agent acts on.** Every check-in returns a plain verdict — `proceed` / `guide` / `pause` / `reject` — plus the agent's full health vector (`EISV`) for finer per-dimension policies. Humans watch the same fleet through the optional dashboard.

## Use UNITARES if

- you run autonomous or semi-autonomous coding, research, operations, or resident agents;
- you want mid-run health signals, not only pre-deploy evals or post-hoc logs;
- you need agents to check their own state before continuing; and
- you want an audit trail of confidence, evidence, drift, and recovery.

UNITARES is **not** an output validator, sandbox, hosted agent platform, or grand jury. EISV is **not an outcome oracle**; it is proprioceptive telemetry for the running agent. External outcome evidence and policy/review layers own labels such as task-negative, contract violation, or authority/harm.

## Try the demo locally

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up -d --wait && make demo
```

`make demo` drives a synthetic agent through seven check-ins — clean work, then confidence drifting from results, then confusion — and prints the verdict at each step. First run can spend a few minutes building Docker images; later runs are the fast path. Then point any MCP client at `http://localhost:8767/mcp/`.

For a human operator view, open the optional dashboard at `http://localhost:8767/dashboard`. Dashboard implementation details live in [`dashboard/README.md`](dashboard/README.md); public deployment screenshots live in [`docs/PRODUCTION_SNAPSHOT.md`](docs/PRODUCTION_SNAPSHOT.md).

> **Running continuously since November 2025 · 3.7M+ governance events under sustained load · dogfooded** — the agents building UNITARES run under it. Every number is verifiable on a fresh clone. ([Production snapshot →](docs/PRODUCTION_SNAPSHOT.md))

## Where it fits

UNITARES runs **alongside** your evals and guardrails — it doesn't replace either. They answer different questions at different times:

| | Question it answers | When it acts |
|---|---|---|
| **Evals** | Is this model good enough to ship? | before deploy |
| **Guardrails** | Is this *action* allowed right now? | per action |
| **UNITARES** | Is this agent *still healthy* as it works? | continuously, mid-run |

## Mechanisms

The engine behind the verdict — what makes the decision *stateful* rather than a per-action rule:

- **State-aware verdict engine.** Each verdict is a function of the agent's own baseline, calibration, and recent verdict history — not the current action in isolation. Auditable behavioral model, not a black box ([`behavioral_assessment.py`](src/behavioral_assessment.py)).
- **Outcome-grounded calibration.** Self-reported `confidence` is scored against objective evidence — test exit codes, tool output, file ops — and the resulting calibration feeds back into future verdicts. The number is gameable; the success rate isn't.
- **Dialectic peer review → runtime constraints.** A disputed verdict is reviewed by an authority-weighted peer agent from the fleet (self-review blocked; supermajority quorum on round exhaustion); the synthesized conditions *persist* and gate that agent's later verdicts — a runtime constraint, not debate text.
- **Per-instance identity isolation.** Each process-instance is a distinct governed identity with its own state. Reads are open; writes are accountable to a bound caller. No cross-instance state bleed by default.
- **Durable audit trail + shared knowledge.** Every confidence, evidence, verdict, drift, and recovery is recorded — the basis for "verify it yourself." The same store (Postgres + pgvector, with an Apache AGE graph view) also holds the fleet's shared knowledge graph: agents contribute discoveries that are linked into a graph of cross-agent relations.

<div align="center">

[Architecture](docs/UNIFIED_ARCHITECTURE.md) · [Scope & threat model](docs/SCOPE_AND_THREAT_MODEL.md)

</div>

## How it works

<div align="center">
  <img src="docs/assets/flow.png" width="100%" alt="agent acts → checks in (sync_state) → graded vs its own baseline → state + action → self-regulates → durable audit trail"/>
</div>

After each unit of work, the agent checks in with `sync_state()` — passing its self-reported confidence plus verifiable evidence (test results, exit codes, tool output). It gets back one plain policy action:

<div align="center">

**`proceed`** &nbsp;·&nbsp; **`guide`** &nbsp;·&nbsp; **`pause`** &nbsp;·&nbsp; **`reject`**

</div>

That's the whole contract: the agent reads the policy action and course-corrects *before* an external guardrail has to fire. No new vocabulary required to use it.

<details>
<summary><strong>The four numbers behind the policy action (EISV)</strong></summary>

<br/>

Want to act on *why*, not just the policy action? Each check-in also returns four scores per agent, each graded against that agent's *own* ~30-check-in baseline — so slow drift surfaces even while output still looks fine:

| | | Goes wrong when… |
|---|---|---|
| **E** · Energy | is the work advancing? | thrashing, retries, no progress |
| **I** · Integrity | do claims match results? | high confidence, low actual success |
| **S** · Entropy | drifting from its own normal? | erratic, divergent behavior |
| **V** · Valence | derived: energy vs integrity | motion without coherence (or vice-versa) |

</details>

<div align="center">

[EISV proprioception contract](docs/ontology/eisv-proprioception-contract.md) · [How EISV is computed](docs/EISV_COMPUTATION.md) · [Architecture](docs/UNIFIED_ARCHITECTURE.md) · [Who it's for & threat model](docs/SCOPE_AND_THREAT_MODEL.md)

</div>

## Integrate in two calls

```python
# 1. Start a governance session for this process.
session = start_session(force_new=True)
client_session_id = session["client_session_id"]

# 2. Check in after meaningful work.
result = sync_state(
    response_text=output,
    complexity=0.6,
    confidence=0.8,
    client_session_id=client_session_id,
)

action = result.get("state_summary", {}).get("action")
if action is None:
    raw = result.get("raw_governance", result)
    action = raw.get("decision", {}).get("action", raw.get("action", "proceed"))

if action in ("pause", "reject"):
    agent.require_human_review(result.get("next_action", "Governance requested review"))
```

The agent reads the action and acts — that's the whole loop. Self-reported `confidence` is strongest when paired with real outcomes, so include tool results or call `record_result(...)` when your client has evidence such as test status, exit codes, or deployment checks. UNITARES isn't an output validator or a sandbox; it's a state layer the agent itself can read, *before* external controls fire.

<details>
<summary><strong>Finer control: branch on the EISV components</strong></summary>

<br/>

For per-dimension policies, read the four scores instead of the single verdict:

```python
raw = result.get("raw_governance", result)
eisv = raw.get("primary_eisv") or raw.get("metrics", {})

if eisv.get("I", 1) < 0.4:
    agent.require_human_review("integrity low — pausing autonomous actions")
elif eisv.get("S", 0) > 0.7:
    agent.narrow_scope()        # fewer tools, tighter search
elif eisv.get("E", 1) < 0.2:
    agent.stop_and_summarize()  # avoid thrashing
```

</details>

For long-running or scheduled agents, the [SDK](agents/sdk/README.md) handles connection, identity, check-ins, and heartbeats. ([Getting started](docs/guides/START_HERE.md) · [MCP client config](docs/integration/MCP_CLIENTS.md))

## Don't trust this README — verify it

**Evaluating with an agent?** Don't take the prose. On a fresh clone, the [falsifiability harness](docs/REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc) scores EISV/prior-state telemetry against a deliberately dumb baseline (AUC, Brier) using external outcome labels, and self-labels each slice `INCONCLUSIVE` / `SKEPTICAL` / `WEAK SIGNAL` / `KEEP TESTING` rather than asserting. The harness is the part you run yourself.

**Honest about what fires.** Policy actions come from an auditable behavioral model ([`behavioral_assessment.py`](src/behavioral_assessment.py)), not a black box — the information-theoretic / free-energy formulation is the research *target*, not the live policy-action path ([Paper v6](https://github.com/cirwel/unitares-paper-v6) · [how EISV is computed](docs/EISV_COMPUTATION.md)).

Human evaluators start with the [Reviewer Guide](docs/REVIEWER_GUIDE.md).

---

## Stack & setup

**Python 3.12+ · PostgreSQL + AGE + pgvector · Redis (optional).** Transports: MCP on `/mcp/` (Streamable HTTP) · REST on `/v1/tools/call` · Dashboard on `/dashboard`.

<details>
<summary><strong>Alternate ports, bare-metal, and thin clients</strong></summary>

If `5432`, `6379`, or `8767` is already allocated, pick alternate host ports:

```bash
POSTGRES_HOST_PORT=15432 REDIS_HOST_PORT=16379 GOVERNANCE_HOST_PORT=18767 docker compose up -d --wait
UNITARES_DEMO_PORT=18767 make demo
```

**Bare-metal** (lower overhead, what the maintainer runs in production): PostgreSQL 16+ with Apache AGE + pgvector compiled and installed (examples use PG 17), Redis optional.

```bash
pip install -r requirements-full.txt
export DB_BACKEND=postgres
export DB_POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/governance
export DB_AGE_GRAPH=governance_graph
export UNITARES_KNOWLEDGE_BACKEND=age
python src/mcp_server.py --port 8767
```

`requirements-full.txt` is the default (server, tests, handler dev); `requirements-core.txt` is a 2-package subset (`mcp` + `numpy`) for thin stdio/proxy clients. DB bring-up: [db/postgres/README.md](db/postgres/README.md). Run signal-only without the math model: `export UNITARES_DISABLE_ODE=1`. Full port map: [`docs/operations/DEFINITIVE_PORTS.md`](docs/operations/DEFINITIVE_PORTS.md).

</details>

## Documentation

| Guide | Purpose |
|-------|---------|
| [Getting Started](docs/guides/START_HERE.md) | Setup, workflows, tool modes |
| [How EISV is computed](docs/EISV_COMPUTATION.md) | Deployed formulas vs. target semantics |
| [Reviewer Guide](docs/REVIEWER_GUIDE.md) | Cold-evaluator path + falsifiability harness |
| [Scope & threat model](docs/SCOPE_AND_THREAT_MODEL.md) | Who it's for, why agents can't game it, what's unproven |
| [Architecture](docs/UNIFIED_ARCHITECTURE.md) | Pipeline, verdicts, recovery, storage |
| [Glossary](docs/ontology/glossary.md) | Terms keyed by the question they answer — published at [cirwel.github.io/unitares](https://cirwel.github.io/unitares/) |
| [Production snapshot](docs/PRODUCTION_SNAPSHOT.md) | Live metrics + dashboard views |
| [MCP Clients](docs/integration/MCP_CLIENTS.md) | Cursor / Claude Code / Claude Desktop config |
| [Troubleshooting](docs/guides/TROUBLESHOOTING.md) | Common issues |
| [Changelog](docs/CHANGELOG.md) | Releases |

> Three files at the repo root — [`CLAUDE.md`](CLAUDE.md), [`AGENTS.md`](AGENTS.md), [`CODEX_START.md`](CODEX_START.md) — orient AI CLIs (Claude Code, Codex). Human readers can skip them.

## The CIRWEL stack

UNITARES is the governance runtime at the center of a larger body of work. The full index — papers, systems, datasets, and decks — lives at **[cirwel.github.io](https://cirwel.github.io)**.

| | What it is |
|---|---|
| [**unitares-governance-plugin**](https://github.com/cirwel/unitares-governance-plugin) | Mount any agent into governance — Claude Code / Codex plugin that wires check-ins, dialectic review, and verdicts into the loop via hooks |
| [**unitares-host-adapter**](https://github.com/cirwel/unitares-host-adapter) | Thin client bindings — Hermes, Claude Code, Goose, and arbitrary OpenAI-compatible clients |
| [**anima-mcp**](https://github.com/cirwel/anima-mcp) | Physical longitudinal testbed — the same EISV model mapped from Raspberry Pi sensor/system telemetry; the source cited in the papers |
| [**fermata**](https://github.com/cirwel/fermata) | Governed-effect runtime seed — agents *propose* effects; only governed effects *commit* |
| [**unitares-discord-bridge**](https://github.com/cirwel/unitares-discord-bridge) | Governance events, agent presence, and system health as a live Discord server |
| [**eisv-lumen**](https://github.com/cirwel/eisv-lumen) | Governance benchmark dataset — [32,181 labeled EISV trajectories](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories) (20,655 real) |
| [**unitares-paper-v6**](https://github.com/cirwel/unitares-paper-v6) | Companion paper — *Information-Theoretic Governance of Heterogeneous Agent Fleets* (Wang, 2026); concept DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159) |

## Citation

Kenny Wang ([ORCID 0009-0006-7544-2374](https://orcid.org/0009-0006-7544-2374)), CIRWEL Systems. If you build on this work, please cite — see [`CITATION.cff`](CITATION.cff).

```bibtex
@misc{wang2026unitares,
  author       = {Wang, Kenny},
  title        = {{UNITARES}: Information-Theoretic Governance of Heterogeneous Agent Fleets},
  year         = {2026},
  doi          = {10.5281/zenodo.19647159},
  url          = {https://doi.org/10.5281/zenodo.19647159},
  note         = {Concept DOI; resolves to latest version. ORCID: 0009-0006-7544-2374}
}
```

---

<div align="center">

**Apache License 2.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE).<br/>
Built by [@cirwel](https://github.com/cirwel) · [CIRWEL Systems](https://cirwel.github.io)

</div>

<div align="center">

<img alt="UNITARES — runtime governance for AI-agent fleets" src="docs/assets/hero-v2.png" width="100%">

### Runtime governance for heterogeneous AI-agent fleets.

</div>

An agent forty turns into a task reports high confidence. Its tests are failing. Every individual action it took was allowed, so nothing in your stack objects — no single call was wrong, and nothing is comparing what the agent *says* against what actually happened.

**UNITARES keeps that comparison.** Agents check in while they work. Each one gets an accountable identity, a durable record of what it did and claimed, and a four-score state estimate it can read mid-run. Each check-in returns one action: `proceed`, `guide`, `pause`, or `reject`.

<div align="center">

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.12+-2f7d72?style=flat-square&labelColor=0f171f)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-2f7d72?style=flat-square&labelColor=0f171f)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

**Running continuously since November 2025 · 4.5M+ governance events · the agents that build UNITARES run under it.**

[![Quickstart](https://img.shields.io/badge/▶-quickstart-5eead4?style=for-the-badge&labelColor=0f171f)](#quickstart)
[![What's in the box](https://img.shields.io/badge/what's-in_the_box-5eead4?style=for-the-badge&labelColor=0f171f)](#whats-in-the-box)
[![What's running](https://img.shields.io/badge/what's-running-f5a623?style=for-the-badge&labelColor=0f171f)](#whats-running)
[![Docs](https://img.shields.io/badge/docs-read-7d8f97?style=for-the-badge&labelColor=0f171f)](docs/README.md)

</div>

---

## What's in the box

`docker compose up` gives you a server. The repo gives you a working fleet — this is a system, not a library.

| | What you get |
|---|---|
| **Governance server** | The check-in loop, per-process identity, calibration, policy actions. MCP on `/mcp/`, REST on `/v1/tools/call`, operator dashboard on `/dashboard`. |
| **Knowledge graph** | Shared cross-agent memory, not a side feature. Typed discoveries (`bug_found`, `architectural_decision`, `cleanup`, …) with severity, semantic search over BGE-M3 embeddings, and a status lifecycle so a later agent supersedes a wrong entry instead of duplicating it. PostgreSQL + Apache AGE. |
| **Resident agents** | A pattern, not a fixed set. A resident is any long-running or scheduled process that checks in, carries state, and participates in the knowledge graph — **model-agnostic and env-configurable, with no paid API key on the default path** (the code-review resident posts to a local Ollama endpoint out of the box). Four references ship in [`agents/`](agents/README.md), and they are the ones monitoring the maintainer's own fleet: **Vigil** (scheduled health sweeps) · **Sentinel** (continuous fleet monitor) · **Chronicler** (daily archive capture) · **Watcher** (code review on a post-edit hook). Copy one, or subclass the SDK and write your own. |
| **`unitares-sdk`** | The public agent-to-governance contract — connection, identity, check-ins, heartbeats, and KG participation for your own residents. Ships in-tree; install with `pip install ./agents/sdk` (not yet published to PyPI). |
| **Dialectic orchestrator** | When an action is disputed, agents argue it out: a session opens, reviewers are assigned, and participants submit **thesis → antithesis → synthesis** until it resolves to a durable constraint rather than a one-off override. Runs peer-to-peer or LLM-assisted, and resolved sessions feed back into calibration ground truth. |
| **Recovery** | A paused agent is not dead-ended. `reflect → validate → resume`: the agent reviews its own state, the server checks whether resuming is safe, and it either resumes or gets specific guidance on what to fix. Operator override exists for the cases that need one. |
| **BEAM coordination** | In-tree Elixir/OTP for surface leases, handoffs, dispatch, and supervision alongside the Python server. |
| **Benchmark dataset** | [32,181 labeled EISV trajectories](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories) (20,655 real) for evaluating state models against something other than your own logs. |

## The loop

<div align="center">
  <img src="docs/assets/flow.png" width="100%" alt="agent acts → checks in (sync_state) → graded against its own baseline → state + action returned → agent self-regulates → durable audit trail"/>
</div>

Everything hangs off one call. The agent finishes a unit of work, calls `sync_state()` with what it did and how confident it is, and reads back an action. Four scores come with it, each graded against that agent's own expanding baseline:

| | | Reads badly when… |
|---|---|---|
| **E** · Energy | is the work advancing? | thrashing, retries, no progress |
| **I** · Integrity | do claims match results? | high confidence, low actual success |
| **S** · Entropy | drifting from its own normal? | erratic, divergent behavior |
| **V** · Valence | derived: energy vs integrity | motion without coherence, or the reverse |

Everything else in the box answers a question that loop raises about the agent doing the work:

| Question | Answered by | Status |
|---|---|---|
| Who is acting? | per-process **identity** — reads open, writes accountable | enforced |
| What did it do and claim? | durable **audit record**, queryable per agent | 4.5M events |
| Did its confidence match real evidence? | evidence-grounded **calibration** | live |
| How is it tracking against its own baseline? | the four-score **state** — *[how it's graded](docs/EISV_COMPUTATION.md)* | live on every resident |
| Has this been learned or corrected before? | the **knowledge graph** | live |
| Is a disputed action defensible? | **dialectic** peer review → durable constraints | live |
| When another model produced the output, what evidence is that? | **`call_model`** provenance | live |

## Where it fits

UNITARES runs **alongside** your evals and guardrails. It replaces neither.

| | Question it answers | When it acts |
|---|---|---|
| **Evals** | Is this model good enough to ship? | before deploy |
| **Guardrails** | Is this *action* allowed right now? | per action |
| **UNITARES** | What has this agent been doing, and is its account of it accurate? | continuously, mid-run |

**Reach for it when** you run autonomous or semi-autonomous coding, research, operations, resident, or local-model agents; when you need an accountable record of who did what; when agents should read their own state before continuing; and when confidence, evidence, and recovery belong on one audit trail.

**It is not** an output validator, sandbox, hosted agent platform, agent framework, or chat interface. Your client provides the hands — prompts, tools, files, terminals, scheduled work, operator UX. UNITARES provides the accountable continuity underneath. The state reading is **not an outcome oracle**, a bad-result detector, or a grand jury; it is runtime telemetry about the agent, and external evidence is what calibrates it. Policy and review layers own labels such as task-negative, contract violation, or authority/harm.

**Client-neutral by design.** Claude Code, Codex, Hermes, Goose, Cursor, dispatch agents, local models, and frontier providers such as Mistral all use the same server over MCP, REST, the SDK, or a host adapter. Claude is one client family, not a server-side assumption.

## Quickstart

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up -d --wait && make demo
```

`make demo` onboards an agent and drives six check-ins over the real API, printing the action, the reason, and the four scores at each step — the exact response shape your client will parse. Sixty seconds, no DB queries, everything on screen came back in a check-in response.

Each step also prints its warmup position (`baseline: 4/25`, `5/25`, …). Scoring runs on fixed thresholds until an agent has 25 check-ins of its own history, then switches to that agent's baseline — so a fresh install shows you the warmup phase, which is where every agent starts and where most short-lived ones stay. The [residents](agents/README.md) operate past it.

**Three surfaces, three questions.** *Is my stack wired* — this demo. *Does the signal beat a dumb baseline* — the [falsifiability harness](docs/REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc), on your own clone. *What does it look like deployed* — the [production snapshot](docs/PRODUCTION_SNAPSHOT.md) and the dashboard.

First run spends a few minutes building images; later runs are fast. Then point any MCP client at `http://localhost:8767/mcp/`.

For an operator view, open the dashboard at `http://localhost:8767/dashboard` ([implementation](dashboard/README.md) · [deployment screenshots](docs/PRODUCTION_SNAPSHOT.md)).

## Integrate in two calls

For AI clients, the stable contract is: start a session, pass the returned `client_session_id` into each check-in, and obey the returned action. The four-score state is optional context for finer control.

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

Self-reported `confidence` is worth most when paired with verifiable evidence, so include tool results or call `record_result(...)` when your client has test status, exit codes, or deployment checks. That evidence is what makes calibration a measurement rather than self-grading.

| Need | Tool |
|---|---|
| Search the shared knowledge graph | `search_shared_memory(query=...)` |
| Record verified external evidence | `record_result(...)` |
| Ask for structured peer review | `request_review(issue_description=...)` |
| Read current state without writing | `check_working_state()` |

<details>
<summary><strong>Warmup behavior, enforcement, and per-dimension policies</strong></summary>

<br/>

The baseline takes ~30 check-ins. Until then the action falls back to a cold-start prior built mostly from server-derived signals (complexity divergence, coherence, calibration — self-reported drift is capped at a ≤30% blend), so during warmup it is *not* discriminative of absolute drift magnitude: a worsening drift vector will not on its own move the action. After baselining, the per-agent behavioral assessment feeds the action and can escalate it. A pause is enforced — the runtime boundary marks the agent `paused` and blocks writes — not advisory. It is also not a dead end: see [recovery](#more-of-the-surface) for the `reflect → validate → resume` path out.

For per-dimension policies, read the scores directly. The payload field is still `primary_eisv` for API compatibility:

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

For long-running or scheduled agents, the [SDK](agents/sdk/README.md) handles connection, identity, check-ins, and heartbeats. Any MCP client accepting Streamable HTTP can connect to `/mcp/`; REST serves non-MCP clients, bridges, local-model hosts, and adapters. ([Getting started](docs/guides/START_HERE.md) · [MCP client config](docs/integration/MCP_CLIENTS.md))

## More of the surface

The four tools above cover the common path. The rest is there when you need it — `list_tools()` enumerates the surface live, and `describe_tool(name)` explains any one of them.

**What a running agent can do for itself**

| | | |
|---|---|---|
| **Recover from a pause** | Reflect on why it happened, check whether resuming is safe, then resume or get specific guidance. The way out of an enforced pause. | `self_recovery_review` · `check_recovery_options` · `direct_resume_if_safe` |
| **Dry-run a check-in** | See the action a check-in would produce without writing anything. | `simulate_update` |
| **Compare against peers** | Ask how it's doing relative to structurally similar agents rather than against an absolute threshold. | `compare_me_to_similar` · `compare_agents` |
| **Manage its calibration** | Check calibration, submit ground truth when an outcome lands, rebuild from history. | `check_calibration` · `update_calibration_ground_truth` · `record_result` |
| **Reach another model** | Call a different model through governance so its output carries provenance; enumerate available inference hosts. | `call_model` · `list_inference_hosts` · `describe_inference_host` |

**What agents do with each other**

| | | |
|---|---|---|
| **Argue to resolution** | Open a dispute, submit thesis / antithesis / synthesis, reassign a reviewer, read the transcript. Peer-to-peer or LLM-assisted. | `request_review` · `submit_thesis` · `submit_antithesis` · `submit_synthesis` · `list_dialectic_sessions` |
| **Signal across the fleet (CIRS)** | A multi-agent resonance layer: announce state, raise an alert, publish a coherence report, declare a boundary contract about what you will and won't trust. | `cirs_protocol` · `state_announce` · `coherence_report` · `boundary_contract` |
| **Build on shared memory** | Beyond search: synthesize across entries, supersede a wrong one, audit the graph, follow a discovery's relations. | `synthesize_knowledge_graph` · `supersede_discovery` · `audit_knowledge_graph` |

**What you can see across the fleet**

| | | |
|---|---|---|
| **Health & anomalies** | Find stuck agents, detect anomalies fleet-wide, aggregate metrics, read overall workspace health. | `detect_stuck_agents` · `detect_anomalies` · `aggregate_metrics` · `get_workspace_health` |
| **Behavioral identity** | Verify that a process claiming continuity actually matches the trajectory it claims. | `verify_trajectory_identity` · `get_trajectory_status` |
| **Audit & export** | Query the event log, correlate outcomes against prior state, pull system history, export to file. | `audit_events_query` · `outcome_correlation` · `get_system_history` · `export_to_file` |

## What's running

Not a prototype and not a demo. The numbers below come from the deployment that governs the agents building this repo.

| | |
|---|---|
| **4,563,981 governance events** | continuous since November 2025 |
| **Six mechanisms in production** | identity, audit record, calibration, knowledge graph, dialectic review, recovery — each doing what it says, every day |
| **15 agent-initiated recoveries** | of 21 total resumes; a paused agent reflected, validated, and resumed itself rather than waiting for an operator |
| **~205,000 resident check-ins** | Lumen 139,843 · Sentinel 26,741 · Steward 24,513 · Watcher 8,762 · Vigil 4,975 |
| **Four residents at `verified` trust tier** | substrate-earned, not assigned — three-condition pass on declared role, sustained behavior, dedicated substrate |
| **Per-agent baseline scoring, live** | every resident is thousands of check-ins past the 25 that switch scoring from fixed thresholds to its own history |
| **32,181 labeled EISV trajectories** | [published](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories), 20,655 real — evaluate a state model against something other than your own logs |
| **12,434 tests, 82.6% coverage** | green on every merge |

**The maintainer's fleet is the deployment.** Vigil, Sentinel, Chronicler, Watcher, and Steward run under governance continuously, on the machine that develops the server they check in to. Lumen adds a physical testbed on a Raspberry Pi. When something breaks here, it breaks the fleet first.

**Auditable.** Once a baseline exists, actions come from an inspectable behavioral model ([`behavioral_assessment.py`](src/behavioral_assessment.py)); before that, from a mostly server-derived cold-start prior. The information-theoretic formulation in [Paper v6](https://github.com/cirwel/unitares-paper-v6) is the research roadmap, not a description of the post-warmup decision path.

**Grade the state estimate yourself, on a fresh clone.** The [falsifiability harness](docs/REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc) scores the four-score telemetry against deliberately dumb baselines on externally labeled task evidence, reporting each slice as it finds it.

Every claim this project has tested — including the ones that came back negative, what each was measured against, and the wording held to as a result — is in the [agent-state contract](docs/ontology/eisv-proprioception-contract.md), with the full catalog in [`docs/EVALUATION_INDEX.md`](docs/EVALUATION_INDEX.md). Read those before citing a number from this page.

Human evaluators start with the [Reviewer Guide](docs/REVIEWER_GUIDE.md) · [Scope & threat model](docs/SCOPE_AND_THREAT_MODEL.md) · [Architecture](docs/UNIFIED_ARCHITECTURE.md).

## Federation: accountability without a trusted center

Everything above describes a **single governor with a single operator**. The architectural commitment is that this generalizes without a central authority: each principal runs their own governor, and cross-principal interaction is mediated by verifiable attestation rather than by anyone's administrative root.

The **primitives for that already exist in the deployed system** — identity is per-process, credentials structurally refuse cross-principal resume, and declared lineage is recorded as *provisional* rather than trusted on assertion. A preliminary trace exercised them end to end without new code.

What does **not** exist yet is the multi-host, adversarial-governor, benchmark-scale build: mutually-distrusting governors, cross-principal delegation, shared-infrastructure effects under attestation. That is the research direction, not a shipped capability, and a testbed-and-benchmark paper is in preparation.

---

## Stack & setup

**Python 3.12+ · PostgreSQL + AGE + pgvector · Redis.**

<details>
<summary><strong>Alternate ports, bare-metal, and thin clients</strong></summary>

If `5432`, `6379`, or `8767` is taken, pick alternate host ports:

```bash
POSTGRES_HOST_PORT=15432 REDIS_HOST_PORT=16379 GOVERNANCE_HOST_PORT=18767 docker compose up -d --wait
UNITARES_DEMO_PORT=18767 make demo
```

**Bare-metal** (lower overhead, what the maintainer runs in production): PostgreSQL 16+ with Apache AGE and pgvector compiled in (examples use PG 17). Redis: the server boots in degraded local-only mode without it, but production uses it as the primary session store.

```bash
pip install -r requirements-full.txt
export DB_BACKEND=postgres
export DB_POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/governance
export DB_AGE_GRAPH=governance_graph
export UNITARES_KNOWLEDGE_BACKEND=age
python src/mcp_server.py --port 8767
```

`requirements-full.txt` is the default (server, tests, handler dev); `requirements-core.txt` is a minimal runtime subset for thin stdio/proxy clients. DB bring-up: [db/postgres/README.md](db/postgres/README.md). Run signal-only without the math model: `export UNITARES_DISABLE_ODE=1`. Full port map: [`docs/operations/DEFINITIVE_PORTS.md`](docs/operations/DEFINITIVE_PORTS.md).

</details>

## Documentation

| Guide | Purpose |
|-------|---------|
| [Getting Started](docs/guides/START_HERE.md) | Setup, workflows, tool modes |
| [Build a resident agent](agents/README.md) | The four reference residents and the SDK pattern |
| [Reviewer Guide](docs/REVIEWER_GUIDE.md) | Cold-evaluator path + falsifiability harness |
| [Agent-state contract](docs/ontology/eisv-proprioception-contract.md) | Tested-claim ledger, validation rule, preferred wording |
| [Evaluation index](docs/EVALUATION_INDEX.md) | Catalog of evaluations and what each covers |
| [How the four scores are computed](docs/EISV_COMPUTATION.md) | Deployed formulas vs. target semantics |
| [Scope & threat model](docs/SCOPE_AND_THREAT_MODEL.md) | Who it's for, why agents can't game it, what's unproven |
| [Architecture](docs/UNIFIED_ARCHITECTURE.md) | Pipeline, actions, recovery, storage |
| [Glossary](docs/ontology/glossary.md) | Terms keyed by the question they answer — published at [cirwel.github.io/unitares](https://cirwel.github.io/unitares/) |
| [Production snapshot](docs/PRODUCTION_SNAPSHOT.md) | Live metrics + dashboard views |
| [MCP Clients](docs/integration/MCP_CLIENTS.md) | Streamable HTTP, stdio bridges, hosted connectors |
| [Troubleshooting](docs/guides/TROUBLESHOOTING.md) | Common issues |
| [Changelog](docs/CHANGELOG.md) | Releases |

> Root files such as [`CLAUDE.md`](CLAUDE.md), [`AGENTS.md`](AGENTS.md), and [`CODEX_START.md`](CODEX_START.md) are client-specific operating notes for AI CLIs. They do not limit the server: UNITARES is client-neutral over MCP/REST.

## The CIRWEL stack

UNITARES is the governance runtime at the center of a larger body of work — runtime safety infrastructure for autonomous agents, *after* deployment. Full index at **[cirwel.github.io](https://cirwel.github.io)**.

| | What it is |
|---|---|
| [**anima-mcp**](https://github.com/cirwel/anima-mcp) | Physical longitudinal testbed — the same four-score state model mapped from Raspberry Pi sensor and system telemetry; the source cited in the papers |
| [**unitares-governance-plugin**](https://github.com/cirwel/unitares-governance-plugin) | Hook/sidecar packaging for clients such as Codex and Claude Code; useful for lifecycle automation, not required for direct MCP/REST use |
| [**unitares-host-adapter**](https://github.com/cirwel/unitares-host-adapter) | Thin client bindings — Hermes, Goose, Claude Code, OpenAI-compatible hosts, local models, frontier providers such as Mistral, and arbitrary REST clients |
| [**fermata**](https://github.com/cirwel/fermata) | Governed-effect runtime seed — agents *propose* effects; only governed effects *commit* |
| [**unitares-discord-bridge**](https://github.com/cirwel/unitares-discord-bridge) | Governance events, dispatch/presence, and system health as a live Discord surface |
| [**eisv-lumen**](https://github.com/cirwel/eisv-lumen) | The benchmark dataset above, with its generation and labeling pipeline |
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

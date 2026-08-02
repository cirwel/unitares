<div align="center">

<img alt="UNITARES — runtime governance for AI-agent fleets" src="docs/assets/hero-v2.png" width="100%">

### Runtime governance for heterogeneous AI-agent fleets.

**Agents check in while they work. UNITARES gives each one an accountable identity, a durable record of what it did and claimed, and a state estimate it can read mid-run — then returns a single action: `proceed`, `guide`, `pause`, or `reject`.**

Most controls check one action against one rule and forget it. UNITARES keeps per-process history, so the question it answers is not *is this call allowed* but *what has this agent been doing, and does its account of that match the evidence.*

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.12+-2f7d72?style=flat-square&labelColor=0f171f)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-2f7d72?style=flat-square&labelColor=0f171f)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

*Running continuously since November 2025 — 4.4M+ governance events. The agents that build UNITARES run under it.*

[![Quickstart](https://img.shields.io/badge/▶-quickstart-5eead4?style=for-the-badge&labelColor=0f171f)](#quickstart)
[![What's proven](https://img.shields.io/badge/what's-proven-f5a623?style=for-the-badge&labelColor=0f171f)](#whats-measured-and-what-isnt)
[![Docs](https://img.shields.io/badge/docs-read-7d8f97?style=for-the-badge&labelColor=0f171f)](docs/README.md)

</div>

---

## What an agent gets

One loop: the agent finishes a unit of work, calls `sync_state()`, and reads back an action. Everything else is a question that loop raises about the agent doing the work.

| Question | What answers it | Status |
|---|---|---|
| Who is acting? | per-process **identity** — reads open, writes accountable | enforced |
| What did it do and claim? | durable **audit record**, queryable per agent | 4.4M events |
| Did its confidence match real evidence? | evidence-grounded **calibration** | live |
| How is it tracking against its own baseline? | four-score **state** (EISV) — *[how it's graded](docs/EISV_COMPUTATION.md)* | live, [validation open](#whats-measured-and-what-isnt) |
| Has this been learned or corrected before? | governed **shared memory** (knowledge graph) | live |
| Is a disputed action defensible? | **dialectic** peer review → durable constraints | live |
| When another model produced the output, what evidence is that? | **`call_model`** provenance | live |

Fleet infrastructure sits beside that loop for work that is multi-agent or side-effectful: surface **leases**, **resident monitors** (scheduled agents that run the loop themselves), **BEAM/Elixir coordination**, and **governed effects** — agents propose, only governed effects commit.

Transports: MCP on `/mcp/`, REST on `/v1/tools/call`, an optional dashboard on `/dashboard`, and an SDK for resident or scheduled agents. Take the loop alone for a quick start; reach for the rest when you need it.

## Where it fits

UNITARES runs **alongside** your evals and guardrails. It replaces neither.

| | Question it answers | When it acts |
|---|---|---|
| **Evals** | Is this model good enough to ship? | before deploy |
| **Guardrails** | Is this *action* allowed right now? | per action |
| **UNITARES** | What has this agent been doing, and is its account of it accurate? | continuously, mid-run |

**Reach for it when** you run autonomous or semi-autonomous coding, research, operations, resident, or local-model agents; when you need an accountable record of who did what; when agents should read their own state before continuing; and when you want confidence, evidence, and recovery on one audit trail.

**It is not** an output validator, sandbox, hosted agent platform, agent framework, or chat interface. Your client provides the hands — prompts, tools, files, terminals, scheduled work, operator UX. UNITARES provides the accountable continuity underneath. The state reading is **not an outcome oracle** or bad-result detector; it is runtime telemetry about the agent, and external evidence is what calibrates it. Policy and review layers own labels such as task-negative, contract violation, or authority/harm.

**Client-neutral by design.** Claude Code, Codex, Hermes, Goose, Cursor, dispatch agents, local models, and frontier providers such as Mistral all use the same server over MCP, REST, the SDK, or a host adapter. Claude is one client family, not a server-side assumption.

## Quickstart

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up -d --wait && make demo
```

`make demo` drives a synthetic agent through seven check-ins — clean work, then confidence drifting away from results, then confusion — printing the action at each step. The first run spends a few minutes building images; later runs are fast. Then point any MCP client at `http://localhost:8767/mcp/`.

For an operator view, open the dashboard at `http://localhost:8767/dashboard` ([implementation](dashboard/README.md) · [deployment screenshots](docs/PRODUCTION_SNAPSHOT.md)).

## Integrate in two calls

Start a session, pass the returned `client_session_id` into each check-in, obey the returned action. The four-score state is optional context for finer control.

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

That's the loop. Self-reported `confidence` is worth most when paired with verifiable evidence, so include tool results or call `record_result(...)` when your client has test status, exit codes, or deployment checks — that evidence is what makes calibration meaningful rather than self-graded.

| Need | Tool |
|---|---|
| Search the shared knowledge graph | `search_shared_memory(query=...)` |
| Record verified external evidence | `record_result(...)` |
| Ask for structured peer review | `request_review(issue_description=...)` |
| Read current state without writing | `check_working_state()` |

<details>
<summary><strong>The four scores</strong></summary>

<br/>

Each check-in also returns four scores, graded against that agent's own expanding baseline. The docs and payloads call this vector **EISV**: Energy, Integrity, Entropy, Valence.

| | | Reads low/high when… |
|---|---|---|
| **E** · Energy | is the work advancing? | thrashing, retries, no progress |
| **I** · Integrity | do claims match results? | high confidence, low actual success |
| **S** · Entropy / drift | moving away from its own normal? | erratic, divergent behavior |
| **V** · Valence | derived: energy vs integrity | motion without coherence, or the reverse |

The baseline takes ~30 check-ins. Until then the action falls back to a cold-start prior built mostly from server-derived signals (complexity divergence, coherence, calibration — self-reported drift is capped at a ≤30% blend), so during warmup it is *not* discriminative of absolute drift magnitude: a worsening drift vector will not on its own move the action. After baselining, the per-agent behavioral assessment feeds the action and can escalate it. A pause is enforced — the runtime boundary marks the agent `paused` and blocks writes until recovery — not advisory.

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

## What's measured, and what isn't

The identity, record, calibration, and review layers do what they say — they are mechanisms, and you can watch them work in the demo. The **state estimate** is the part still earning its keep, and this project would rather say so than let you discover it.

**"Does the telemetry work?" is two questions with two different answers.** Conflating them is how a governance project talks itself into believing its own instruments.

| Axis | The question | Where it stands |
|---|---|---|
| **Outcome prediction** | Does prior-state telemetry predict a bad result better than a dumb baseline? | **It does not.** On externally labeled evidence, no EISV/prior-state feature beats a plain previous-outcome baseline. The decision path is moving toward the simpler signal accordingly. |
| **Self-predictability** | Does an agent's state estimate track *that agent*, label-free? | **Open, and under-powered.** Agents are distinguishable and non-stationary, but a pre-registered test did not clear its own bar against a persistence/AR(1) null. Untested as deployed — not refuted — on roughly four effective agents. |

The binding constraint on the first row is external bad-label supply, not the model: sparse labels are a data-quality limit, not a philosophical failure. The second row's test was pre-registered and frozen before its data cutoff, and its kill criterion was honored when it fired.

The ledger of every tested claim — what was measured, what it showed, and the wording this project holds itself to — is the [agent-state contract](docs/ontology/eisv-proprioception-contract.md). The catalog of evaluations is [`docs/EVALUATION_INDEX.md`](docs/EVALUATION_INDEX.md).

**Grade it yourself on a fresh clone.** The [falsifiability harness](docs/REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc) scores the four-score telemetry against deliberately dumb baselines on externally labeled task evidence, reporting each slice as it finds it. It is wired to be able to disagree with this README.

**Auditable, not a black box.** Once a baseline exists, actions come from an inspectable behavioral model ([`behavioral_assessment.py`](src/behavioral_assessment.py)); before that, from a mostly server-derived cold-start prior. The information-theoretic formulation in [Paper v6](https://github.com/cirwel/unitares-paper-v6) is the research roadmap, not a description of the post-warmup decision path.

Human evaluators start with the [Reviewer Guide](docs/REVIEWER_GUIDE.md) · [Scope & threat model](docs/SCOPE_AND_THREAT_MODEL.md) · [Architecture](docs/UNIFIED_ARCHITECTURE.md).

## Where it's going

Everything above describes the deployed system: **one governor, one operator**. The identity layer already holds the posture a multi-party world needs — identity is per-process, credentials structurally refuse cross-principal resume, and declared lineage is recorded as *provisional* rather than trusted on assertion.

The research direction extends this to **multi-principal** deployments: mutually-distrusting principals each running their own governor, with cross-principal delegation and shared-infrastructure effects mediated by verifiable attestation between governors rather than authorized by a central party. No multi-host, multi-party deployment exists yet — that is the research, not a shipped claim. A testbed-and-benchmark paper is in preparation.

---

## Stack & setup

**Python 3.12+ · PostgreSQL + AGE + pgvector · Redis.** MCP on `/mcp/` (Streamable HTTP) · REST on `/v1/tools/call` · Dashboard on `/dashboard`.

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
| [**anima-mcp**](https://github.com/cirwel/anima-mcp) | Physical longitudinal testbed — the same four-score state model mapped from Raspberry Pi sensor/system telemetry; the source cited in the papers |
| [**unitares-governance-plugin**](https://github.com/cirwel/unitares-governance-plugin) | Hook/sidecar packaging for clients such as Codex and Claude Code; useful for lifecycle automation, not required for direct MCP/REST use |
| [**unitares-host-adapter**](https://github.com/cirwel/unitares-host-adapter) | Thin client bindings — Hermes, Goose, Claude Code, OpenAI-compatible hosts, local models, frontier providers such as Mistral, and arbitrary REST clients |
| [**fermata**](https://github.com/cirwel/fermata) | Governed-effect runtime seed — agents *propose* effects; only governed effects *commit* |
| [**unitares-discord-bridge**](https://github.com/cirwel/unitares-discord-bridge) | Governance events, dispatch/presence, and system health as a live Discord surface |
| [**BEAM coordination kernel**](docs/ontology/beam-coordination-kernel.md) | In-tree Elixir/OTP coordination for live surface leases, handoffs, dispatch, and supervision beside the Python server |
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

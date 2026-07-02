<div align="center">

<img alt="UNITARES — runtime governance for AI-agent fleets" src="docs/assets/hero-v2.png" width="100%">

### Runtime health checks for autonomous-agent fleets.

**UNITARES is a check-in server for agents while they are working: an agent reports what it did, attaches evidence when it has any, and gets back one action: `proceed`, `guide`, `pause`, or `reject`.**<br/>
Most controls inspect one action against one rule. UNITARES keeps history for each agent process, compares the current run with that agent's own baseline, and makes drift visible to the agent and the human operator while the output may still look fine.

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.12+-2f7d72?style=flat-square&labelColor=0f171f)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-2f7d72?style=flat-square&labelColor=0f171f)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

*Status: live since November 2025 — 3.7M+ governance events (public snapshot frozen 2026-06-16).*

[![Quickstart](https://img.shields.io/badge/▶-quickstart-5eead4?style=for-the-badge&labelColor=0f171f)](#try-the-demo-locally)
[![Docs](https://img.shields.io/badge/docs-read-7d8f97?style=for-the-badge&labelColor=0f171f)](docs/README.md)
[![Paper v6](https://img.shields.io/badge/paper-v6-8957e5?style=for-the-badge&labelColor=0f171f)](https://github.com/cirwel/unitares-paper-v6)
[![Verify it yourself](https://img.shields.io/badge/verify-it_yourself-f5a623?style=for-the-badge&labelColor=0f171f)](docs/REVIEWER_GUIDE.md)

One layer of the **[CIRWEL stack](https://cirwel.github.io)** — runtime safety infrastructure for autonomous agents, *after* deployment. UNITARES is the governed fleet; [Anima](https://github.com/cirwel/anima-mcp) is its physical edge testbed. [Full index ↗](https://cirwel.github.io)

**Client-neutral by design:** Claude Code, Codex, Hermes, Goose, Cursor, Discord/dispatch agents, local models, and frontier providers such as Mistral can all use the same governance server through MCP, REST, the SDK, or a host adapter. Claude is one client family, not a server-side assumption.

</div>

---

## The loop, and the organs around it

Everything in UNITARES hangs off one per-agent loop: an agent checks in after meaningful work and gets back one action — `proceed` / `guide` / `pause` / `reject`. The other subsystems are answers to questions that loop raises about the agent doing the work:

| Question about the running agent | Answered by |
|---|---|
| Who is acting? | per-process **identity** — reads open, writes accountable |
| How is it doing, versus its own baseline? | the four-score **state** (EISV) — *[how it's graded](docs/EISV_COMPUTATION.md)* |
| Did its confidence match real evidence? | evidence-grounded **calibration** |
| Has this been learned or corrected before? | governed **shared memory** (knowledge graph) |
| Is a disputed action defensible? | **dialectic** peer review → durable constraints |
| When another model produced the output, what evidence is that? | **`call_model`** provenance |

Around that per-agent loop sits fleet infrastructure you reach for only when work is multi-agent or side-effectful — surface **leases**, **resident monitors** (scheduled agents that run the loop themselves), **BEAM/Elixir coordination**, and **governed effects** (agents propose; only governed effects commit). The [CIRWEL stack](#the-cirwel-stack) table maps these and their maturity.

The transports are MCP on `/mcp/`, REST on `/v1/tools/call`, an optional dashboard on `/dashboard`, and an SDK for resident or scheduled agents. Pick up only the loop for a quick start; the organs are there when you want to act on *why*, and the infrastructure when persistent or side-effectful work needs it.

## Use UNITARES if

- you run autonomous or semi-autonomous coding, research, operations, resident, Discord, or local-model agents;
- you want mid-run health signals, not only pre-deploy evals or post-hoc logs;
- you need agents to check their own state before continuing; and
- you want an audit trail of confidence, evidence, drift, and recovery.

UNITARES is **not** an output validator, sandbox, hosted agent platform, or grand jury. Its state reading is **not an outcome oracle** or bad-result detector; it is runtime telemetry for the running agent. External evidence calibrates the signal, and policy/review layers own labels such as task-negative, contract violation, or authority/harm.

## Try the demo locally

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up -d --wait && make demo
```

`make demo` drives a synthetic agent through seven check-ins — clean work, then confidence drifting from results, then confusion — and prints the policy action at each step. First run can spend a few minutes building Docker images; later runs are the fast path. Then point any MCP client at `http://localhost:8767/mcp/`.

For a human operator view, open the optional dashboard at `http://localhost:8767/dashboard`. Dashboard implementation details live in [`dashboard/README.md`](dashboard/README.md); public deployment screenshots live in [`docs/PRODUCTION_SNAPSHOT.md`](docs/PRODUCTION_SNAPSHOT.md).

> **Running continuously since November 2025 · 3.7M+ governance events** — the agents building UNITARES run under it. ([Production snapshot →](docs/PRODUCTION_SNAPSHOT.md) · [verify the numbers →](docs/REVIEWER_GUIDE.md))

## Where it fits

UNITARES runs **alongside** your evals and guardrails — it doesn't replace either. They answer different questions at different times:

| | Question it answers | When it acts |
|---|---|---|
| **Evals** | Is this model good enough to ship? | before deploy |
| **Guardrails** | Is this *action* allowed right now? | per action |
| **UNITARES** | Is this agent *still healthy* as it works? | continuously, mid-run |

### How it relates to agent clients

UNITARES is not an agent framework or chat interface. Hermes, Claude Code, Codex, Goose, Discord dispatchers, SDK residents, and local-model hosts provide the hands: prompts, tools, files, terminals, browsers, scheduled work, and operator UX. UNITARES provides the governed continuity underneath — the loop and organs above. For one-off chat or local coding, skip the governance loop; for persistent, multi-agent, high-side-effect, or resident work, mount the client through MCP/REST/SDK or a lifecycle adapter.

### Where it's going: accountability without a trusted center

Everything above describes the deployed system: **one governor, one operator**. The identity layer already enforces the posture a multi-party world needs — identity is per-process, credentials structurally refuse cross-principal resume, and declared lineage is recorded as *provisional* rather than trusted on assertion. The active research direction extends this to genuinely **multi-principal** deployments: mutually-distrusting principals each running their own governor, with cross-principal delegation and shared-infrastructure effects mediated by verifiable attestation between governors rather than authorized by any central party. No multi-host, multi-party deployment exists yet — that is the research, not a shipped claim. A testbed-and-benchmark paper is in preparation (arXiv, expected August 2026).

## How it works

<div align="center">
  <img src="docs/assets/flow.png" width="100%" alt="agent acts → checks in (sync_state) → graded vs its own baseline → state + action → self-regulates → durable audit trail"/>
</div>

After each unit of work, the agent checks in with `sync_state()` — passing self-reported confidence plus verifiable evidence when available (test results, exit codes, tool output). It gets back one plain policy action:

<div align="center">

**`proceed`** &nbsp;·&nbsp; **`guide`** &nbsp;·&nbsp; **`pause`** &nbsp;·&nbsp; **`reject`**

</div>

That's the whole contract: the agent reads the policy action and course-corrects using its own state estimate, without waiting for an external guardrail to catch it. Once a baseline exists, the central signal is a residual — current state minus this agent's own operating reference — so deviation is treated first as information, not as guilt or punishment. No special vocabulary is required to use the loop.

<details>
<summary><strong>The four scores behind the policy action</strong></summary>

<br/>

Want to act on *why*, not just the policy action? Each check-in also returns four scores per agent, each graded against that agent's *own* ~30-check-in baseline, so slow drift can surface while output still looks fine. The research docs and payloads call this vector **EISV**: Energy, Integrity, Entropy, Valence.

| | | Goes wrong when… |
|---|---|---|
| **E** · Energy | is the work advancing? | thrashing, retries, no progress |
| **I** · Integrity | do claims match results? | high confidence, low actual success |
| **S** · Entropy / drift | drifting from its own normal? | erratic, divergent behavior |
| **V** · Valence | derived: energy vs integrity | motion without coherence (or vice-versa) |

The baseline takes ~30 check-ins to establish. Until then the policy action falls back to self-reported signals and fixed thresholds, so it is *not yet* discriminative of absolute drift magnitude — a worsening drift vector will not, on its own, move the action during warmup. After baselining, the per-agent behavioral assessment is combined into the action and can escalate it. A pause is enforced (the runtime boundary marks the agent `paused` and blocks further writes until recovery), not merely advisory.

</details>

<div align="center">

[Agent-state contract](docs/ontology/eisv-proprioception-contract.md) · [How the four scores are computed](docs/EISV_COMPUTATION.md) · [Architecture](docs/UNIFIED_ARCHITECTURE.md) · [Who it's for & threat model](docs/SCOPE_AND_THREAT_MODEL.md)

</div>

## Integrate in two calls

For AI clients, the stable contract is: start a session, pass the returned `client_session_id` into each check-in, obey the returned action, and treat the four-score state as optional context for finer control.

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

The agent reads the action and acts — that's the whole loop. Self-reported `confidence` is strongest when paired with verifiable evidence, so include tool results or call `record_result(...)` when your client has evidence such as test status, exit codes, or deployment checks. UNITARES is not an output validator or sandbox; it is an agent-facing state-estimation layer while external controls remain separate.

The same primary tool surface also gives agents a few optional moves:

| Need | Tool |
|---|---|
| Search the shared knowledge graph | `search_shared_memory(query=...)` |
| Record verified external evidence | `record_result(...)` |
| Ask for structured peer review | `request_review(issue_description=...)` |
| Read current state without writing | `check_working_state()` |

<details>
<summary><strong>Finer control: branch on the four scores</strong></summary>

<br/>

For per-dimension policies, read the four scores instead of only the single policy action. The raw payload field is still named `primary_eisv` for API compatibility:

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

For long-running or scheduled agents, the [SDK](agents/sdk/README.md) handles connection, identity, check-ins, and heartbeats. Any MCP client that accepts Streamable HTTP can connect to `/mcp/`; REST is available for non-MCP clients, Discord/dispatch bridges, local-model hosts, and adapters. ([Getting started](docs/guides/START_HERE.md) · [MCP client config](docs/integration/MCP_CLIENTS.md))

## Verify every claim yourself

**Evaluating with an agent?** On a fresh clone, the [falsifiability harness](docs/REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc) grades whether the four-score state telemetry beats deliberately dumb baselines (AUC, Brier) on externally labeled task/result evidence, reporting each slice honestly rather than asserting it. Most projects don't ship the means to disprove them; this one does. ([Reviewer Guide →](docs/REVIEWER_GUIDE.md))

**Auditable, not a black box.** Policy actions come from an inspectable behavioral model ([`behavioral_assessment.py`](src/behavioral_assessment.py)); the information-theoretic formulation in [Paper v6](https://github.com/cirwel/unitares-paper-v6) is the research roadmap, not a claim about the current decision path ([how the four scores are computed](docs/EISV_COMPUTATION.md)).

Human evaluators start with the [Reviewer Guide](docs/REVIEWER_GUIDE.md).

---

## Stack & setup

**Python 3.12+ · PostgreSQL + AGE + pgvector · Redis.** Transports: MCP on `/mcp/` (Streamable HTTP) · REST on `/v1/tools/call` · Dashboard on `/dashboard`.

<details>
<summary><strong>Alternate ports, bare-metal, and thin clients</strong></summary>

If `5432`, `6379`, or `8767` is already allocated, pick alternate host ports:

```bash
POSTGRES_HOST_PORT=15432 REDIS_HOST_PORT=16379 GOVERNANCE_HOST_PORT=18767 docker compose up -d --wait
UNITARES_DEMO_PORT=18767 make demo
```

**Bare-metal** (lower overhead, what the maintainer runs in production): PostgreSQL 16+ with Apache AGE + pgvector compiled and installed (examples use PG 17). Redis: the server boots in degraded local-only mode without it, but production uses it as the primary session store.

```bash
pip install -r requirements-full.txt
export DB_BACKEND=postgres
export DB_POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/governance
export DB_AGE_GRAPH=governance_graph
export UNITARES_KNOWLEDGE_BACKEND=age
python src/mcp_server.py --port 8767
```

`requirements-full.txt` is the default (server, tests, handler dev); `requirements-core.txt` is a minimal runtime subset (see the file) for thin stdio/proxy clients. DB bring-up: [db/postgres/README.md](db/postgres/README.md). Run signal-only without the math model: `export UNITARES_DISABLE_ODE=1`. Full port map: [`docs/operations/DEFINITIVE_PORTS.md`](docs/operations/DEFINITIVE_PORTS.md).

</details>

## Documentation

| Guide | Purpose |
|-------|---------|
| [Getting Started](docs/guides/START_HERE.md) | Setup, workflows, tool modes |
| [How the four scores are computed](docs/EISV_COMPUTATION.md) | Deployed formulas vs. target semantics |
| [Reviewer Guide](docs/REVIEWER_GUIDE.md) | Cold-evaluator path + falsifiability harness |
| [Scope & threat model](docs/SCOPE_AND_THREAT_MODEL.md) | Who it's for, why agents can't game it, what's unproven |
| [Architecture](docs/UNIFIED_ARCHITECTURE.md) | Pipeline, policy actions, recovery, storage |
| [Glossary](docs/ontology/glossary.md) | Terms keyed by the question they answer — published at [cirwel.github.io/unitares](https://cirwel.github.io/unitares/) |
| [Production snapshot](docs/PRODUCTION_SNAPSHOT.md) | Live metrics + dashboard views |
| [MCP Clients](docs/integration/MCP_CLIENTS.md) | Client-neutral MCP setup: Streamable HTTP, stdio bridges, hosted connectors |
| [Troubleshooting](docs/guides/TROUBLESHOOTING.md) | Common issues |
| [Changelog](docs/CHANGELOG.md) | Releases |

> Root files such as [`CLAUDE.md`](CLAUDE.md), [`AGENTS.md`](AGENTS.md), and [`CODEX_START.md`](CODEX_START.md) are client-specific operating notes for AI CLIs. They do not limit the server: UNITARES itself is client-neutral over MCP/REST.

## The CIRWEL stack

UNITARES is the governance runtime at the center of a larger body of work. The full index — papers, systems, datasets, and decks — lives at **[cirwel.github.io](https://cirwel.github.io)**.

| | What it is |
|---|---|
| [**unitares-governance-plugin**](https://github.com/cirwel/unitares-governance-plugin) | Hook/sidecar packaging for clients such as Codex and Claude Code; useful for lifecycle automation, not required for direct MCP/REST use |
| [**unitares-host-adapter**](https://github.com/cirwel/unitares-host-adapter) | Thin client bindings — Hermes, Goose, Claude Code, OpenAI-compatible hosts, local models, frontier providers such as Mistral, and arbitrary REST clients |
| [**anima-mcp**](https://github.com/cirwel/anima-mcp) | Physical longitudinal testbed — the same four-score state model mapped from Raspberry Pi sensor/system telemetry; the source cited in the papers |
| [**fermata**](https://github.com/cirwel/fermata) | Governed-effect runtime seed — agents *propose* effects; only governed effects *commit* |
| [**unitares-discord-bridge**](https://github.com/cirwel/unitares-discord-bridge) | Governance events, dispatch/presence, and system health as a live Discord surface |
| [**BEAM coordination kernel**](docs/ontology/beam-coordination-kernel.md) | In-tree Elixir/OTP coordination work for live surface leases, handoffs, dispatch, and supervision beside the Python governance server |
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

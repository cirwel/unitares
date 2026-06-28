<div align="center">

<img alt="UNITARES — runtime governance for AI-agent fleets" src="docs/assets/hero-v2.png" width="100%">

### Runtime governance for autonomous-agent fleets.

**UNITARES gives each running agent online state estimation: EISV, calibration, evidence, and drift over its own trajectory, surfaced as a policy action it can use mid-run.**<br/>
Most controls inspect one action against one rule. UNITARES carries trajectory into the next check-in, so drift is measurable while output still looks fine and the agent can course-correct — not a hidden detector trying to catch agents after the fact.

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.12+-2f7d72?style=flat-square&labelColor=0f171f)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-2f7d72?style=flat-square&labelColor=0f171f)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

*Status: live and dogfooded. Public snapshot: 3.7M+ governance events from a single-operator deployment, frozen 2026-06-16.*

[![Quickstart](https://img.shields.io/badge/▶-quickstart-5eead4?style=for-the-badge&labelColor=0f171f)](#try-the-demo-locally)
[![Docs](https://img.shields.io/badge/docs-read-7d8f97?style=for-the-badge&labelColor=0f171f)](docs/README.md)
[![Paper v6](https://img.shields.io/badge/paper-v6-8957e5?style=for-the-badge&labelColor=0f171f)](https://github.com/cirwel/unitares-paper-v6)
[![Verify it yourself](https://img.shields.io/badge/verify-it_yourself-f5a623?style=for-the-badge&labelColor=0f171f)](docs/REVIEWER_GUIDE.md)

One layer of the **[CIRWEL stack](https://cirwel.github.io)** — runtime safety infrastructure for autonomous agents, *after* deployment. UNITARES is the governed fleet; [Anima](https://github.com/cirwel/anima-mcp) is its physical edge testbed. [Full index ↗](https://cirwel.github.io)

**Client-neutral by design:** Claude Code, Codex, Hermes, Goose, Cursor, Discord/dispatch agents, local models, and frontier providers such as Mistral can all use the same governance server through MCP, REST, the SDK, or a host adapter. Claude is one client family, not a server-side assumption.

</div>

---

## What you get after install

- **A governance server for heterogeneous agents.** MCP on `/mcp/`, REST on `/v1/tools/call`, an optional dashboard on `/dashboard`, and an SDK for resident or scheduled agents.
- **Online agent-state estimation.** Each process identity gets EISV state readings against its *own* baseline and recent history, so slow degradation surfaces while output still looks fine. (During the baseline warmup the reading leans on self-reported signals and is not yet drift-discriminative — the payload flags this explicitly.)
- **Outcome-grounded calibration.** Self-reported `confidence` is scored against real evidence — tests, exit codes, tool output, file ops, deployments, and task results — and that calibration feeds future state readings and policy actions.
- **Governed shared memory.** A Postgres + pgvector + Apache AGE knowledge graph lets agents search and contribute durable discoveries, corrections, supersessions, and cross-agent relations with provenance. It is sediment, not a transcript dump.
- **Dialectic review and durable constraints.** Disputed policy actions can be reviewed by authority-weighted peers; synthesized conditions persist and can gate that agent's future decisions.
- **One action the agent can obey.** Every check-in returns `proceed` / `guide` / `pause` / `reject`, plus the full EISV health vector for finer policies. Humans watch the same fleet through the optional dashboard.

## Use UNITARES if

- you run autonomous or semi-autonomous coding, research, operations, resident, Discord, or local-model agents;
- you want mid-run health signals, not only pre-deploy evals or post-hoc logs;
- you need agents to check their own state before continuing; and
- you want an audit trail of confidence, evidence, drift, and recovery.

UNITARES is **not** an output validator, sandbox, hosted agent platform, or grand jury. EISV is **not an outcome oracle** or bad-verdict dispenser; it is proprioceptive telemetry for the running agent. External outcome evidence and policy/review layers own labels such as task-negative, contract violation, or authority/harm.

## Try the demo locally

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up -d --wait && make demo
```

`make demo` drives a synthetic agent through seven check-ins — clean work, then confidence drifting from results, then confusion — and prints the policy action at each step. First run can spend a few minutes building Docker images; later runs are the fast path. Then point any MCP client at `http://localhost:8767/mcp/`.

For a human operator view, open the optional dashboard at `http://localhost:8767/dashboard`. Dashboard implementation details live in [`dashboard/README.md`](dashboard/README.md); public deployment screenshots live in [`docs/PRODUCTION_SNAPSHOT.md`](docs/PRODUCTION_SNAPSHOT.md).

> **Running continuously since November 2025 · 3.7M+ governance events under sustained single-operator load · dogfooded** — the agents building UNITARES run under it. The snapshot documents deployment totals; the reviewer harness documents what can be regenerated from a clone or deployment data. ([Production snapshot →](docs/PRODUCTION_SNAPSHOT.md))

## Where it fits

UNITARES runs **alongside** your evals and guardrails — it doesn't replace either. They answer different questions at different times:

| | Question it answers | When it acts |
|---|---|---|
| **Evals** | Is this model good enough to ship? | before deploy |
| **Guardrails** | Is this *action* allowed right now? | per action |
| **UNITARES** | Is this agent *still healthy* as it works? | continuously, mid-run |

### How it relates to agent clients

UNITARES is not an agent framework or chat interface. Hermes, Claude Code, Codex, Goose, Discord dispatchers, SDK residents, and local-model hosts provide the hands: prompts, tools, files, terminals, browsers, scheduled work, and operator UX. UNITARES provides governed continuity underneath them: process identity, check-ins, online agent-state estimation, calibration against outcomes, shared-memory provenance, dialectic review, and auditable policy actions. For one-off chat or local coding, skip the governance loop; for persistent, multi-agent, high-side-effect, or resident work, mount the client through MCP/REST/SDK or a lifecycle adapter.

<details>
<summary><strong>Mechanisms behind the state reading</strong></summary>

- **State-aware policy engine** — baseline, calibration, and recent history; not the current action alone ([`behavioral_assessment.py`](src/behavioral_assessment.py)).
- **Online state-estimation loop** — EISV, confidence, evidence provenance, and policy margin are fed back to the agent as runtime telemetry.
- **Outcome-grounded calibration** — self-reported `confidence` is scored against objective evidence when available.
- **Dialectic review → constraints** — disputed policy actions can become durable gating conditions after peer review.
- **Per-instance identity isolation** — each process has its own governed state; reads are open, writes are accountable.
- **Audit trail + KG** — confidence, evidence, policy actions, drift, recovery, and shared-memory contributions remain inspectable.

[Architecture](docs/UNIFIED_ARCHITECTURE.md) · [Scope & threat model](docs/SCOPE_AND_THREAT_MODEL.md)

</details>

## How it works

<div align="center">
  <img src="docs/assets/flow.png" width="100%" alt="agent acts → checks in (sync_state) → graded vs its own baseline → state + action → self-regulates → durable audit trail"/>
</div>

After each unit of work, the agent checks in with `sync_state()` — passing self-reported confidence plus verifiable evidence when available (test results, exit codes, tool output). It gets back one plain policy action:

<div align="center">

**`proceed`** &nbsp;·&nbsp; **`guide`** &nbsp;·&nbsp; **`pause`** &nbsp;·&nbsp; **`reject`**

</div>

That's the whole contract: the agent reads the policy action and course-corrects using its own state estimate, without waiting for an external guardrail to catch it. No new vocabulary required to use it.

<details>
<summary><strong>The four numbers behind the policy action (EISV)</strong></summary>

<br/>

Want to act on *why*, not just the policy action? Each check-in also returns four scores per agent, each graded against that agent's *own* ~30-check-in baseline, so slow drift can surface while output still looks fine:

| | | Goes wrong when… |
|---|---|---|
| **E** · Energy | is the work advancing? | thrashing, retries, no progress |
| **I** · Integrity | do claims match results? | high confidence, low actual success |
| **S** · Entropy / drift | drifting from its own normal? | erratic, divergent behavior |
| **V** · Valence | derived: energy vs integrity | motion without coherence (or vice-versa) |

The baseline takes ~30 check-ins to establish. Until then the verdict falls back to self-reported signals and fixed thresholds, so it is *not yet* discriminative of absolute drift magnitude — a worsening drift vector will not, on its own, move the verdict during warmup. After baselining, the per-agent behavioral assessment is combined into the verdict and can escalate it. A pause is enforced (the runtime boundary marks the agent `paused` and blocks further writes until recovery), not merely advisory.

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

The agent reads the action and acts — that's the whole loop. Self-reported `confidence` is strongest when paired with real outcomes, so include tool results or call `record_result(...)` when your client has evidence such as test status, exit codes, or deployment checks. UNITARES is not an output validator or sandbox; it is an agent-facing state-estimation layer while external controls remain separate.

The same primary tool surface also gives agents a few optional moves:

| Need | Tool |
|---|---|
| Search the shared knowledge graph | `search_shared_memory(query=...)` |
| Record verified external evidence | `record_result(...)` |
| Ask for structured peer review | `request_review(issue_description=...)` |
| Read current state without writing | `check_working_state()` |

<details>
<summary><strong>Finer control: branch on the EISV components</strong></summary>

<br/>

For per-dimension policies, read the four scores instead of only the single policy action:

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
| [**anima-mcp**](https://github.com/cirwel/anima-mcp) | Physical longitudinal testbed — the same EISV model mapped from Raspberry Pi sensor/system telemetry; the source cited in the papers |
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

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/hero.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/hero.svg">
  <img alt="UNITARES — Self-regulating AI agents" src="docs/assets/hero.svg" width="100%">
</picture>

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

Status: live. First public commit 2025-12-04. Cold evaluators can start with the [Reviewer Guide](docs/REVIEWER_GUIDE.md); architecture details are in [docs/UNIFIED_ARCHITECTURE.md](docs/UNIFIED_ARCHITECTURE.md).

Multi-agent fleets fly blind. The agent-identity layer tells you *who* is calling. The evaluation layer tells you *whether a model is good enough to deploy*. Neither tells you **what the fleet is actually doing right now, whether it's still coherent, or whether it's drifting from its baseline**. That layer is what UNITARES is.

### How it works in one read

An agent calls `process_agent_update()` after a unit of work. It sends a self-reported `confidence`, a self-reported `complexity`, the `response_text`, and any `recent_tool_results` (test outcomes, exit codes, lint output, file modifications — things the system doesn't have to trust the agent about). UNITARES tracks four numbers per agent — **EISV** for short:

- **E (Energy)** — is the work advancing? Tool calls succeeding and decisions resolving raise E; thrashing, retries, no-progress lower it.
- **I (Integrity)** — do claims match outcomes? Confidence calibrated to observed success rate raises I; high confidence with low actual success lowers it.
- **S (Entropy)** — is behavior diverging from the agent's own baseline? Stable trajectory and consistent claims keep S low; drift and divergence push it up.
- **V (Valence)** — derived: a signed E−I imbalance. Positive = energetic-but-incoherent; negative = coherent-but-depleted.

Each check-in returns a verdict — `proceed` / `guide` / `pause` / `reject` — so the agent can self-regulate before external circuit breakers fire. Humans read the same state on a dashboard; peer agents read it over the API.

### Why an agent can't just lie about its confidence

Self-reported confidence is one input. UNITARES also observes **hard exogenous outcomes** — test pass/fail, exit codes, tool results — fed back through the `outcome_event` tool. Over many tasks it tracks whether the agent's claimed confidence matches its actual success rate. An agent that reports `confidence=0.9` while succeeding only 50% of the time accumulates calibration error; integrity drops; the verdict shifts to `guide` or `pause`. The signal is grounded in what actually happened, not what the agent claimed.

After ~30 check-ins the four numbers are graded against the agent's own running baseline, not a universal threshold. Absolute safety floors still apply.

Running continuously since November 2025. State stored in PostgreSQL + AGE. The theory and the dynamical-systems version of this model live in [Paper v6](https://github.com/cirwel/unitares-paper-v6) (DOI 10.5281/zenodo.19647159) — readers who want the full derivation start there.

### Who should integrate this

If you're running **multiple long-lived autonomous agents** — tool-using, multi-step, doing real work over hours or days — and you've had the experience of an agent quietly drifting without anyone noticing until something visible broke, UNITARES is for you. The check-in loop surfaces drift while it's still numerical (integrity slipping, calibration error climbing) instead of at the point a human user complains. It does not replace evals or guardrails; it runs in parallel as a state layer the agent itself can read.

**Integration cost:** one MCP / REST call per agent unit-of-work, plus an `outcome_event` callback for any task with a hard exogenous outcome (tests, exit codes, tool results). Dashboard, knowledge graph, dialectic, and continuity are downstream of that.

**Not yet a good fit for** short-lived chatbot interactions where per-turn governance overhead exceeds the value, or teams without the ability to instrument their agent loop. External adoption is the open question; the [Production snapshot](#production-snapshot) is honest about it.

### Try it

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up -d --wait         # Postgres+AGE+pgvector+Redis+server, bound to 127.0.0.1
make demo                           # 60-second scripted trajectory
```

`make demo` onboards a synthetic agent, drives seven check-ins (clean work → calibration drift → confusion), and prints the verdict + state at each step. Source: [`scripts/demo/quick_demo.py`](scripts/demo/quick_demo.py). Then point any MCP client at `http://localhost:8767/mcp/`.

If you already run UNITARES locally and port `8767` is live, skip `docker compose up` and run `make demo` directly. If Docker reports that `5432`, `6379`, or `8767` is already allocated, pick alternate host ports:

```bash
POSTGRES_HOST_PORT=15432 REDIS_HOST_PORT=16379 GOVERNANCE_HOST_PORT=18767 docker compose up -d --wait
UNITARES_DEMO_PORT=18767 make demo
```

Bare-metal setup (Homebrew Postgres, native install) is in [Installation](#installation).

**Service ports** (bound to `127.0.0.1` by default; override host-side via `.env`):

| Service | Port | Endpoint |
|---|---|---|
| Governance MCP server | `8767` | `http://localhost:8767/mcp/` |
| Postgres + AGE + pgvector | `5432` | `postgresql://postgres:postgres@localhost:5432/governance` |
| Redis (session cache) | `6379` | `redis://localhost:6379/0` |

Additional services (started via launchd, not bundled into `docker compose up`):

| Service | Port | Endpoint |
|---|---|---|
| Gateway MCP (reduced surface) | `8768` | `http://localhost:8768/mcp/` |
| Surface lease plane (bearer-auth) | `8788` | `http://localhost:8788/v1/lease/*` |

**Workflow:** `start_session(force_new=true)` → `sync_state()` → `check_working_state()`. These are first-class aliases for `onboard`, `process_agent_update`, and `get_governance_metrics`. Use `parent_agent_id` for fresh-process lineage — details in [Getting Started](docs/guides/START_HERE.md).

**Resident agents:** for long-running or scheduled agents, start with the SDK in [`agents/sdk/README.md`](agents/sdk/README.md). It handles MCP connection, identity anchors, check-ins, heartbeats, log rotation, state persistence, and pause hooks.

**Transports:** MCP on `/mcp/` (Streamable HTTP) · REST on `/v1/tools/call` · Dashboard on `/dashboard`

**Stack:** Python 3.12+ · PostgreSQL + AGE + pgvector · Redis (optional)

---

## The self-regulation loop

1. **Agent acts** — tool call, response, decision.
2. **Unitares updates state** — four numbers that summarize how it's going.
3. **Agent reads its own state back** in the check-in response.
4. **Agent applies its own policy** — proceed, narrow scope, ask for review, or stop.

```python
# Inside the agent's loop
result = sync_state(response_text=output, complexity=0.6, confidence=0.8)

if result["metrics"]["integrity"] < 0.4:
    agent.require_human_review("integrity low — pausing autonomous actions")
elif result["metrics"]["entropy"] > 0.7:
    agent.narrow_scope()            # fewer tools, tighter search
elif result["metrics"]["energy"] < 0.2:
    agent.stop_and_summarize()      # avoid thrashing
```

The agent reads its own metrics and adjusts *before* external controls have to fire. Humans see the same state on the dashboard; peer agents read it over the API. Unitares isn't an output validator (guardrails, evals) or a behavioral sandbox (permissions, container limits) — it's a state layer the agent itself can read.

## What makes the signal trustworthy

**No ethics oracle.** The four numbers come from things UNITARES already measures — calibration error, complexity divergence, behavioral drift. No hand-labeled "is this ethical?" classifier.

**Trajectory as identity.** Long-run EISV patterns answer continuity questions ("still the same agent across restarts?") and surface drift no single check-in could see.

**Peer review when needed.** When an agent's confidence and the system's assessment disagree, UNITARES runs a short adversarial review with peer agents — or with an LLM when no peers are around — before anything halts. See [dialectic-dataset](https://github.com/cirwel/dialectic-dataset).

---

## Production snapshot

Frozen public snapshot from May 6, 2026 (single-operator deployment — self-traffic, not external adoption). Headline: **351K+ governance events processed · ≈94K in the last 7 days**.

<details>
<summary><strong>Full metrics table</strong></summary>

| Metric | Value |
|--------|-------|
| Agents onboarded | 3,660 total process-instances — overwhelmingly ephemeral CLI sessions from one operator's workstation plus a handful of long-running resident agents (launchd crons) |
| Distinct event-emitting identities (last 21 days) | 1,144 total; mostly ephemeral local CLI sessions, not external adoption |
| Unique agents active (last 7 days) | 135 distinct event emitters |
| Governance events processed | 351,000+ (≈94K in the last 7 days) |
| Knowledge graph discoveries | 860 |
| V operating range | Active agents often within [-0.1, 0.1] |
| Tests | 8,500+ collected · smoke/pre-push subset plus 25% min coverage gate |

</details>

*What these numbers are good for:* a stress test that the pipeline holds up under sustained volume. *What they are not:* evidence of product-market traction. External adoption is the open question.

<p align="center">
  <img src="docs/assets/dashboard.png" width="80%" alt="Unitares dashboard — stats overview with fleet coherence, agent count, discoveries, and system health"/>
</p>

<details>
<summary><strong>More dashboard views</strong> (pulse, EISV charts, agents, dialectic, activity)</summary>

<p align="center">
  <img src="docs/assets/dashboard-pulse.png" width="80%" alt="Pulse — live event feed and EISV time series"/>
</p>
<p align="center"><em>Pulse — live event feed, drift indicators, and EISV time series charts</em></p>

<p align="center">
  <img src="docs/assets/dashboard-agents.png" width="80%" alt="Agents and Discoveries panels"/>
</p>
<p align="center"><em>Agents (sorted by recency, with trust tiers) and Discoveries (filterable by type and time range)</em></p>

<p align="center">
  <img src="docs/assets/dashboard-dialectic.png" width="80%" alt="Dialectic sessions — recovery and review history"/>
</p>
<p align="center"><em>Dialectic sessions — failed, resolved, and active recovery sessions with message counts</em></p>

<p align="center">
  <img src="docs/assets/dashboard-activity.png" width="80%" alt="Activity timeline — check-ins, verdicts, discoveries"/>
</p>
<p align="center"><em>Activity timeline — filterable event log across all agents</em></p>

</details>

> **Integrating an agent?** Jump to [Quick Start](#quick-start).

---

## Quick Start

```
1. start_session(force_new=true) → Get a fresh process identity
2. sync_state()                  → Log your work
3. check_working_state()         → Check your state
```

Example check-in (non-mirror responses include full `metrics`, `decision`, etc.):

```jsonc
sync_state({
  "response_text": "Refactored auth module, added rate limiting",
  "complexity": 0.6,
  "confidence": 0.8,
  "task_type": "refactoring",
  "response_mode": "mirror"  // or: minimal, compact, standard, full, auto
})
```

**`response_mode: "mirror"`** shapes the payload for self-awareness: `mirror` is a **list of strings** (actionable signals), not a nested object. Optional top-level `reflection` and `relevant_prior_work` surface a state reflection and knowledge-graph items when relevant. See `_format_mirror` in [`src/mcp_handlers/response_formatter.py`](src/mcp_handlers/response_formatter.py).

```jsonc
{
  "verdict": {
    "value": "proceed",
    "meaning": "State is healthy.",
    "next_action": "Continue working normally."
  },
  "_mode": "mirror",
  "mirror": [
    "Fleet calibration: 72% accuracy over 12 fleet-wide decisions (high-conf: 0.8, low-conf: 0.5)",
    "Complexity divergence: you reported 0.60 but system derives 0.45 (divergence=0.15)"
  ],
  "reflection": "Complexity estimate is diverging from the output-surface proxy.",
  "relevant_prior_work": [
    { "summary": "Rate limiter bypass in auth …", "by": "agent-abc", "relevance": 0.82 }
  ]
}
```

**Verdict field:** Mirror/compact responses wrap the verdict with `value`, `meaning`, and `next_action`. Governance actions are **`proceed` / `guide` / `pause` / `reject`** ([Architecture](docs/UNIFIED_ARCHITECTURE.md)). If `action` is absent, formatters fall back to **`continue`** — see `response_formatter.py`.

The `start_session()` / `onboard()` response includes `agent_uuid`. Store it as an identity anchor. On a fresh process that continues prior work, call `start_session(force_new=true, parent_agent_id=<prior uuid>, spawn_reason="new_session")`. Use `identity(agent_uuid=..., continuity_token=..., resume=true)` only for same-owner proof-owned rebinds.

### Installation

Two supported paths. Pick one.

#### A. Docker Compose (recommended for evaluation)

Zero host dependencies beyond Docker. Brings up Postgres+AGE+pgvector, Redis, and the governance server in one command.

```bash
git clone https://github.com/cirwel/unitares.git
cd unitares
cp .env.example .env       # optional — defaults work
docker compose up
# server: http://localhost:8767/mcp/
```

To override credentials or host-side ports (e.g. you already have Postgres on `5432`), edit `.env` first. Compose definition: [`docker-compose.yml`](docker-compose.yml). Postgres image: [`db/postgres/Dockerfile.age-vector`](db/postgres/Dockerfile.age-vector).

#### B. Bare-metal (native Postgres + AGE)

Lower overhead, faster iteration, what the maintainer runs in production. Requires PostgreSQL 16+ with Apache AGE + pgvector compiled and installed (examples use PostgreSQL 17). Redis optional (session cache only).

```bash
git clone https://github.com/cirwel/unitares.git
cd unitares
pip install -r requirements-full.txt

export DB_BACKEND=postgres
export DB_POSTGRES_URL=postgresql://postgres:postgres@localhost:5432/governance
export DB_AGE_GRAPH=governance_graph
export UNITARES_KNOWLEDGE_BACKEND=age

python src/mcp_server.py --port 8767
```

`requirements-full.txt` is the default for almost everything — running the local server, running tests (`pytest` is in `full` only), and handler development. `requirements-core.txt` is a 2-package subset (`mcp` + `numpy`) for thin stdio/proxy setups where the governance server runs elsewhere and you only need a local client. Database bring-up details (PostgreSQL 17 + AGE + pgvector compile): [db/postgres/README.md](db/postgres/README.md).

The EISV ODE engine lives in this repo at `governance_core/` (pure Python, no separate install). To skip the ODE entirely and run with behavioral-EISV only: `export UNITARES_DISABLE_ODE=1`.

### MCP configuration

Client-specific JSON (Cursor / Claude Code / Claude Desktop), endpoint table, and bind-address security: [`docs/integration/MCP_CLIENTS.md`](docs/integration/MCP_CLIENTS.md).

Agent identity: save `agent_uuid` from `onboard()` as an anchor; declare fresh-process lineage with `parent_agent_id`; use `continuity_token` only as short-lived ownership proof for explicit UUID rebinds. See [Getting Started](docs/guides/START_HERE.md) and [Operator Runbook](docs/operations/OPERATOR_RUNBOOK.md).

---

## State ranges and pipeline

E, I, S each live in `[0, 1]`; V in `[-1, 1]`. Verdict thresholds and the absolute safety floors are in [`src/behavioral_assessment.py`](src/behavioral_assessment.py). Implementation: EMA-smoothed observations primary (`src/behavioral_state.py`); a coupled ODE in `governance_core/` runs in parallel as a diagnostic fallback. The full pipeline (drift → entropy, calibration, circuit breaker, dialectic) and the ODE derivation are in [Architecture](docs/UNIFIED_ARCHITECTURE.md) and [Paper v6](https://github.com/cirwel/unitares-paper-v6).

---

## Architecture

```mermaid
graph LR
    A[AI Agent] -->|check-in| M["MCP Server :8767"]
    M -->|observations| BS[Behavioral EISV]
    BS -->|"verdict + guidance"| M
    M -->|parallel diagnostic| UC[unitares-core ODE]
    UC -.->|analysis only| M
    M -->|"verdict + guidance"| A
    M <-->|"state, audit, calibration"| PG[("PostgreSQL + AGE")]
    M <-->|knowledge graph| PG
    M -.->|session cache| R[(Redis)]
    M -->|web UI| D[Dashboard]

    style BS fill:#1a5c1a,stroke:#666,color:#fff
    style UC fill:#2d2d2d,stroke:#666,color:#fff
```

**Use cases:** Fleet monitoring and early warning, inter-agent state observation, trajectory-based identity and continuity, outcome-calibrated confidence tracking, dialectic peer review, persistent knowledge graph with staleness awareness.

---

## Documentation

| Guide | Purpose |
|-------|---------|
| [Getting Started](docs/guides/START_HERE.md) | Setup, workflows, tool modes |
| [MCP Clients](docs/integration/MCP_CLIENTS.md) | Cursor / Claude Code / Claude Desktop config |
| [Architecture](docs/UNIFIED_ARCHITECTURE.md) | Pipeline, verdicts, recovery, storage |
| [Troubleshooting](docs/guides/TROUBLESHOOTING.md) | Common issues |
| [Dashboard](dashboard/README.md) | Web UI |
| [Database](docs/operations/database_architecture.md) | PostgreSQL + AGE |
| [Changelog](docs/CHANGELOG.md) | Releases |

### Agent bootstrap files (root)

Three files at the repo root orient different AI CLIs. Human readers can skip them.

| File | For |
|------|-----|
| [`CLAUDE.md`](CLAUDE.md) | Claude Code sessions — hook lifecycle, Watcher resolution, Claude-specific rules |
| [`AGENTS.md`](AGENTS.md) | Codex sessions — machine-facing bootstrap (shares a core contract with `CLAUDE.md`) |
| [`CODEX_START.md`](CODEX_START.md) | Codex users — human-facing quickstart for direct workflow |

---

## Related Projects

- [**anima-mcp**](https://github.com/cirwel/anima-mcp) — reference UNITARES deployment cited as longitudinal validation data in the papers
- [**unitares-governance-plugin**](https://github.com/cirwel/unitares-governance-plugin) — Installable client adapters for Codex and Claude
- [**unitares-discord-bridge**](https://github.com/cirwel/unitares-discord-bridge) — Discord presence and governance events
- [**eisv-lumen**](https://github.com/cirwel/eisv-lumen) — Governance benchmark dataset (21K agent-state trajectories on HuggingFace)
- [**unitares-paper-v6**](https://github.com/cirwel/unitares-paper-v6) — Companion paper *Information-Theoretic Governance of Heterogeneous Agent Fleets* (Wang, 2026); concept DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159)

This `unitares` repo is the governance server/runtime. Plugin-side `.codex-plugin/`, `hooks/`, `skills/`, and `commands/` content belongs to the companion adapter repo, not as canonical copies here.

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

**Apache License 2.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Covers server, dashboard, tooling, and the ODE dynamics engine in `governance_core/`. Attribution requested per the NOTICE file for redistributions and derivative works.

Built by [@cirwel](https://github.com/cirwel)

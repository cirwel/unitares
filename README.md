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

**UNITARES watches a fleet of AI agents while they work and tells you — and each agent — when one is starting to go off the rails, before anything visibly breaks.**

When you run many autonomous agents, you can already tell *who* is calling (identity) and *whether a model is good enough to deploy* (evals). What you usually can't see is what the fleet is doing **right now**: whether each agent is still making real progress, whether its confidence matches its actual results, and whether it's drifting away from how it normally behaves. That live picture is what UNITARES provides. It runs alongside your evals and guardrails — it doesn't replace them.

### How it works in one read

After each unit of work, an agent checks in by calling `sync_state()`. The check-in includes a self-reported `confidence`, a self-reported `complexity`, the `response_text`, and any `recent_tool_results` — test outcomes, exit codes, lint output, file changes — i.e. things the system can verify instead of taking the agent's word for.

> **A note on tool names:** agents should use the primary task-verb tools (`sync_state`, `start_session`, `record_result`). Older raw implementation names (`process_agent_update`, `onboard`, `outcome_event`) remain stable for compatibility and debugging. This README uses the primary names throughout; the full mapping is in [Tool names](#tool-names).

From each check-in, UNITARES tracks four numbers per agent — together called **EISV**:

- **E (Energy)** — is the work advancing? Tool calls succeeding and decisions resolving raise E; thrashing, retries, and no-progress lower it.
- **I (Integrity)** — do the agent's claims match its results? Confidence that lines up with the observed success rate raises I; high confidence with low actual success lowers it.
- **S (Entropy)** — is the agent drifting from its own normal behavior? A steady, consistent trajectory keeps S low; erratic, divergent behavior pushes it up.
- **V (Valence)** — a single summary number derived from the gap between E and I. Positive means *energetic but incoherent* (lots of motion, claims not matching outcomes); negative means *coherent but running low on progress*.

Each check-in returns a plain verdict — `proceed` / `guide` / `pause` / `reject` — so the agent can correct itself before any external safety system has to step in. Humans read the same state on a dashboard; other agents can read it over the API.

### Why an agent can't just inflate its own confidence

Self-reported confidence is only one input. UNITARES also watches **real outcomes it can verify** — test pass/fail, exit codes, tool results — sent back via `record_result()`. Over many tasks it compares the agent's *claimed* confidence against its *actual* success rate. An agent that reports `confidence=0.9` while only succeeding 50% of the time builds up a track record of being overconfident; its Integrity (I) drops, and the verdict shifts to `guide` or `pause`. The signal is anchored to what actually happened, not to what the agent said about itself.

After about 30 check-ins, the four numbers are graded against the agent's *own* running history rather than a one-size-fits-all threshold. Absolute safety floors still apply on top of that.

Running continuously since November 2025. State is stored in PostgreSQL + AGE. The underlying theory and the math (dynamical-systems) version of this model are in [Paper v6](https://github.com/cirwel/unitares-paper-v6) (DOI 10.5281/zenodo.19647159) — start there if you want the full derivation.

### Who should integrate this

UNITARES is for you if you run **multiple long-lived autonomous agents** — tool-using, multi-step, doing real work over hours or days — and you've watched an agent quietly drift without anyone noticing until something visible broke. The check-in loop surfaces that drift while it's still just numbers moving (Integrity slipping, overconfidence climbing) instead of waiting for a user to complain. It runs in parallel with your evals and guardrails as a live state layer the agent itself can read.

**Integration cost:** one MCP/REST `sync_state` call per agent unit-of-work, plus a `record_result` callback for any task with a verifiable outcome (tests, exit codes, tool results). The dashboard, knowledge graph, peer review, and continuity features are all downstream of those two calls.

**The threshold that matters is check-in count, not wall-clock time.** Self-relative grading needs roughly **30 check-ins** to establish an agent's baseline (absolute safety floors apply before that). An agent doing dozens of units of work — over an hour or a week — crosses it; one that does three and exits never does. That's the real line for "is my session long enough to benefit," not a duration.

**Probably not worth it yet for** short-lived chatbot turns, where per-turn overhead outweighs the benefit, or for teams that can't instrument their agent loop. Whether this is useful outside the author's own deployment is still an open question — the [Production snapshot](#production-snapshot) is honest about that.

### Try it

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up -d --wait         # Postgres+AGE+pgvector+Redis+server, bound to 127.0.0.1
make demo                           # 60-second scripted trajectory
```

`make demo` starts a synthetic agent session, drives seven check-ins (clean work → confidence drifting from results → confusion), and prints the verdict + state at each step. Source: [`scripts/demo/quick_demo.py`](scripts/demo/quick_demo.py). Then point any MCP client at `http://localhost:8767/mcp/`.

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

**Workflow:** `start_session(force_new=true)` → `sync_state()` → `check_working_state()`. Use `parent_agent_id` to link a fresh process to prior work — details in [Getting Started](docs/guides/START_HERE.md).

**Resident agents:** for long-running or scheduled agents, start with the SDK in [`agents/sdk/README.md`](agents/sdk/README.md). It handles the MCP connection, identity, check-ins, heartbeats, log rotation, state persistence, and pause hooks.

**Transports:** MCP on `/mcp/` (Streamable HTTP) · REST on `/v1/tools/call` · Dashboard on `/dashboard`

**Stack:** Python 3.12+ · PostgreSQL + AGE + pgvector · Redis (optional)

---

## The self-regulation loop

1. **Agent acts** — tool call, response, decision.
2. **UNITARES updates state** — the four numbers that summarize how it's going.
3. **Agent reads its own state back** in the check-in response.
4. **Agent applies its own policy** — proceed, narrow scope, ask for review, or stop.

```python
# Inside the agent's loop
result = sync_state(response_text=output, complexity=0.6, confidence=0.8)
raw = result.get("raw_governance", result)  # full payload lives here when using sync_state
metrics = raw.get("metrics") or {}
eisv = (
    raw.get("primary_eisv")
    or raw.get("behavioral_eisv")
    or metrics.get("eisv")
    or metrics
)

if eisv.get("I") is not None and eisv["I"] < 0.4:
    agent.require_human_review("integrity low — pausing autonomous actions")
elif eisv.get("S") is not None and eisv["S"] > 0.7:
    agent.narrow_scope()            # fewer tools, tighter search
elif eisv.get("E") is not None and eisv["E"] < 0.2:
    agent.stop_and_summarize()      # avoid thrashing
```

The agent reads its own metrics and adjusts *before* external controls have to fire. Humans see the same state on the dashboard; other agents read it over the API. UNITARES isn't an output validator (guardrails, evals) or a sandbox (permissions, container limits) — it's a state layer the agent itself can read.

## What makes the signal trustworthy

**No "ethics" classifier.** The four numbers come from things UNITARES already measures — how well confidence matches outcomes, how far reported complexity is from observed complexity, how far behavior has drifted. There's no hand-labeled "is this ethical?" model in the loop.

**Trajectory as identity.** An agent's EISV pattern over time answers continuity questions ("is this still the same agent across restarts?") and surfaces slow drift that no single check-in could catch.

**Peer review when it matters.** When an agent's confidence and the system's assessment disagree, UNITARES runs a short back-and-forth review with other agents — or with an LLM when no peers are around — before anything halts. (In the codebase and papers this is called *dialectic*.) See [dialectic-dataset](https://github.com/cirwel/dialectic-dataset).

---

## Scope and threat model

UNITARES is **adversarial-aware, not adversarial-naive** — but its enforcement is deliberately conservative.

**What resists gaming.** The signal is anchored to outcomes an agent can't fake: test pass/fail, exit codes, and tool results sent via `record_result()`. An agent can inflate its self-reported `confidence`, but not its actual success rate — sustained overconfidence drives Integrity (I) down regardless of what the agent claims. Drift (S) is graded against the agent's *own* prior trajectory, so there is no fixed threshold to pre-game, and calibration compounds over many tasks rather than judging any single check-in.

**Why enforcement leans lenient.** Verdicts are biased toward `proceed`/`guide` over `pause`/`reject` by design. A false-positive pause on a productive agent is itself a failure mode — acutely so here, since the agents building UNITARES run under UNITARES. The verdict ladder exists so most signal arrives as information the agent can act on, not as a halt: high-confidence degradation trips a pause, ambiguity is surfaced rather than blocked.

**What "no ethics classifier" does and doesn't mean.** It means there is no hand-labeled "is this ethical?" model in the loop — *not* that the system is value-free. Two consequences worth stating plainly: drift (S) is a *salience flag*, not a verdict — high entropy raises attention, it does not auto-fail, so beneficial exploration surfaces as something to look at rather than something to punish. And Integrity (I) is anchored to *ground truth* (outcomes), not to the agent's own history — an agent drifting toward better results shows *rising* I, not a penalty. The "past behavior was the desirable behavior" assumption applies only to S, not to the model as a whole.

**The genuine open question.** Robustness against a *motivated* attacker deliberately optimizing the EISV proxy, at scale, is unproven. The deployment is single-operator with no red-team. That is the real limitation — not an absence of adversarial design, but an absence of adversarial *testing*.

---

## Production snapshot

Frozen public snapshot from June 16, 2026 (single-operator deployment — the author's own traffic, not external adoption). Headline: **3.7M+ governance events processed · ≈714K in the last 7 days**.

<details>
<summary><strong>Full metrics table</strong></summary>

| Metric | Value |
|--------|-------|
| Agents onboarded | 3,777 total process-instances — overwhelmingly ephemeral CLI sessions from one operator's workstation plus a handful of long-running resident agents (launchd crons) |
| Distinct event-emitting identities (last 21 days) | 510; mostly ephemeral local CLI sessions, not external adoption (lower than earlier snapshots as identity-consolidation work cut phantom per-session identities) |
| Unique agents active (last 7 days) | 369 distinct event emitters |
| Governance events processed | 3,748,000+ (≈714K in the last 7 days) |
| Knowledge graph discoveries | 1,054 |
| V operating range | Active agents often within [-0.1, 0.1] |
| Tests | 8,500+ collected · smoke/pre-push subset plus 25% min coverage gate |

</details>

*What these numbers show:* the pipeline holds up under sustained volume. *What they don't show:* product-market traction. External adoption is the open question.

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
<p align="center"><em>Peer-review sessions — failed, resolved, and active recovery sessions with message counts</em></p>

<p align="center">
  <img src="docs/assets/dashboard-activity.png" width="80%" alt="Activity timeline — check-ins, verdicts, discoveries"/>
</p>
<p align="center"><em>Activity timeline — filterable event log across all agents</em></p>

</details>

> **Integrating an agent?** Jump to [Quick Start](#quick-start).

---

## Quick Start

```
1. start_session(force_new=true)  → Get a fresh process identity
2. sync_state()                   → Log your work
3. check_working_state()          → Check your state
```

Example check-in:

```jsonc
sync_state({
  "response_text": "Refactored auth module, added rate limiting",
  "complexity": 0.6,
  "confidence": 0.8,
  "task_type": "refactoring",
  "response_mode": "mirror"  // or: minimal, compact, standard, full, auto
})
```

`response_mode` controls how much detail comes back. **`mirror`** is built for self-awareness: it returns a short list of plain-text signals the agent can act on (under `raw_governance` when you call through `sync_state`). Optional `reflection` and `relevant_prior_work` fields surface a one-line state read and related knowledge-graph items when there are any. See `_format_mirror` in [`src/mcp_handlers/response_formatter.py`](src/mcp_handlers/response_formatter.py).

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

**Verdicts** are always one of **`proceed` / `guide` / `pause` / `reject`** ([Architecture](docs/UNIFIED_ARCHITECTURE.md)). Mirror/compact responses wrap each one with `value`, `meaning`, and `next_action`. If no verdict is present, formatters fall back to `continue` (see `response_formatter.py`).

The `start_session()` response includes `agent_uuid` and `client_session_id` at the top level. Save `agent_uuid` — it's the agent's identity anchor. When a fresh process continues earlier work, call `start_session(force_new=true, parent_agent_id=<prior uuid>, spawn_reason="new_session")` to record the link. (`identity(agent_uuid=..., continuity_token=..., resume=true)` is only for same-owner rebinds where you can prove ownership.)

### Tool names

Agents should use the primary task-verb tools. The older raw implementation names remain available for compatibility, older clients, and cases where you explicitly want the raw handler response shape. Primary tools return the agent-experience envelope, with the full payload preserved under `raw_governance`.

| Primary workflow tool | Raw implementation tool |
|---|---|
| `start_session` | `onboard` |
| `sync_state` | `process_agent_update` |
| `check_working_state` | `get_governance_metrics` |
| `record_result` | `outcome_event` |
| `search_shared_memory` | `knowledge(action="search")` |
| `request_review` | `dialectic(action="request")` |

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

The EISV math engine lives in this repo at `governance_core/` (pure Python, no separate install). To run with the observed-behavior signal only and skip the math model: `export UNITARES_DISABLE_ODE=1`.

### MCP configuration

Client-specific JSON (Cursor / Claude Code / Claude Desktop), endpoint table, and bind-address security: [`docs/integration/MCP_CLIENTS.md`](docs/integration/MCP_CLIENTS.md).

Agent identity: save `agent_uuid` from `start_session()` as an anchor; link a fresh process to prior work with `parent_agent_id`; use `continuity_token` only as short-lived ownership proof for explicit UUID rebinds. See [Getting Started](docs/guides/START_HERE.md) and [Operator Runbook](docs/operations/OPERATOR_RUNBOOK.md).

---

## State ranges and pipeline

E, I, and S each live in `[0, 1]`; V in `[-1, 1]`. Verdict thresholds and the absolute safety floors are in [`src/behavioral_assessment.py`](src/behavioral_assessment.py). The primary signal is smoothed from observed behavior (`src/behavioral_state.py`); a separate math model in `governance_core/` runs in parallel as a diagnostic cross-check. The full pipeline (drift → entropy, calibration, circuit breaker, peer review) and the math derivation are in [Architecture](docs/UNIFIED_ARCHITECTURE.md) and [Paper v6](https://github.com/cirwel/unitares-paper-v6).

---

## Architecture

```mermaid
graph LR
    A[AI Agent] -->|check-in| M["MCP Server :8767"]
    M -->|observations| BS[Behavioral EISV]
    BS -->|"verdict + guidance"| M
    M -->|parallel diagnostic| UC[governance_core ODE]
    UC -.->|analysis only| M
    M -->|"verdict + guidance"| A
    M <-->|"state, audit, calibration"| PG[("PostgreSQL + AGE")]
    M <-->|knowledge graph| PG
    M -.->|session cache| R[(Redis)]
    M -->|web UI| D[Dashboard]

    style BS fill:#1a5c1a,stroke:#666,color:#fff
    style UC fill:#2d2d2d,stroke:#666,color:#fff
```

**Use cases:** Fleet monitoring and early warning, watching one agent's state from another, trajectory-based identity and continuity, outcome-calibrated confidence tracking, peer review, and a persistent knowledge graph with staleness awareness.

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

**Apache License 2.0** — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Covers server, dashboard, tooling, and the math dynamics engine in `governance_core/`. Attribution requested per the NOTICE file for redistributions and derivative works.

Built by [@cirwel](https://github.com/cirwel)

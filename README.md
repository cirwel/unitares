<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/hero.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/hero.svg">
  <img alt="UNITARES — Self-regulating AI agents" src="docs/assets/hero.svg" width="100%">
</picture>

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

Status: live. First public commit 2025-12-04. For architecture details, see [docs/UNIFIED_ARCHITECTURE.md](docs/UNIFIED_ARCHITECTURE.md).

Multi-agent fleets fly blind. The agent-identity layer tells you *who* is calling. The evaluation layer tells you *whether a model is good enough to deploy*. Neither tells you **what the fleet is actually doing right now, whether it's still coherent, or whether it's drifting from its anchor**. That layer is what Unitares is.

Unitares is a runtime telemetry and coordination layer for heterogeneous AI-agent fleets. Agents check in, the system tracks a four-channel state vector — **energy** (productive capacity), **integrity** (information coherence), **entropy** (disorder), **valence** (signed E/I imbalance) — and each check-in returns a verdict (`proceed` / `guide` / `pause` / `reject`), so agents regulate themselves *before* external circuit breakers fire. Humans read the same state on a dashboard; peer agents read it over the API.

Slow down when disorder spikes, ask for review when integrity drops, hand off when running on fumes. Circuit breakers and kill switches are still there — they're just the last line of defense, not the first.

Running continuously in production since November 2025. Long-run trajectories are stored in PostgreSQL + AGE; the state model is derived from what agents actually do (EMA-smoothed observations, not model predictions). Test counts and coverage gates are in the [Production snapshot](#production-snapshot).

**Try it** — bring the stack up, then feel the loop:

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up                  # Postgres+AGE+pgvector+Redis+server, bound to 127.0.0.1
make demo                          # in another shell: 60-second scripted trajectory
```

`make demo` onboards a synthetic agent, drives seven check-ins (clean work → calibration drift → confusion), and prints the verdict + EISV state at each step — so you can see the loop reading drift before reading the architecture doc. Source: [`scripts/demo/quick_demo.py`](scripts/demo/quick_demo.py). Then point any MCP client at `http://localhost:8767/mcp/`.

Bare-metal setup (Homebrew Postgres, native install) is in [Installation](#installation). The Pi/Lumen embodiment side is **optional** — governance runs standalone.

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

**Workflow:** `onboard(force_new=true)` → `process_agent_update()` → `get_governance_metrics()`. Use `parent_agent_id` for fresh-process lineage — details in [Getting Started](docs/guides/START_HERE.md).

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
result = process_agent_update(response_text=output, complexity=0.6, confidence=0.8)

if result["metrics"]["integrity"] < 0.4:
    agent.require_human_review("integrity low — pausing autonomous actions")
elif result["metrics"]["entropy"] > 0.7:
    agent.narrow_scope()            # fewer tools, tighter search
elif result["metrics"]["energy"] < 0.2:
    agent.stop_and_summarize()      # avoid thrashing
```

The agent reads its own metrics and adjusts *before* external controls have to fire. Humans see the same state on the dashboard; peer agents read it over the API. Unitares isn't an output validator (guardrails, evals) or a behavioral sandbox (permissions, container limits) — it's a state layer the agent itself can read.

## What makes the signal trustworthy

**Self-relative scoring.** After ~30 check-ins, the four numbers are graded against *your* baseline, not a universal threshold. You're flagged when *you* drift.

**Drift from observables, not an ethics oracle.** The drift signal comes from calibration accuracy, complexity divergence, coherence, and stability — things the system already measures. No hand-labeled "is this ethical?" classifier.

**Trajectory as identity.** Long-run EISV patterns answer continuity questions ("still the same agent?") and surface drift no single check-in could see.

**Peer review when needed.** When an agent's confidence and the system's assessment disagree, Unitares can run a short adversarial review with peer agents — or with an LLM when no peers are around — before anything halts.

---

## Production snapshot

As of May 6, 2026 (single-operator deployment — self-traffic, not external adoption). Headline: **351K+ governance events processed · ≈94K in the last 7 days**.

<details>
<summary><strong>Full metrics table</strong></summary>

| Metric | Value |
|--------|-------|
| Agents onboarded | 3,660 total process-instances — overwhelmingly ephemeral CLI sessions from one operator's workstation plus a handful of long-running resident agents (launchd crons, Pi-side Lumen) |
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
1. onboard(force_new=true)      → Get a fresh process identity
2. process_agent_update()       → Log your work
3. get_governance_metrics()     → Check your state
```

Example check-in (non-mirror responses include full `metrics`, `decision`, etc.):

```jsonc
process_agent_update({
  "response_text": "Refactored auth module, added rate limiting",
  "complexity": 0.6,
  "confidence": 0.8,
  "task_type": "refactoring",
  "response_mode": "mirror"  // or: minimal, compact, standard, full, auto
})
```

**`response_mode: "mirror"`** shapes the payload for self-awareness: `mirror` is a **list of strings** (actionable signals), not a nested object. Optional top-level `question` and `relevant_prior_work` surface a targeted nudge and knowledge-graph items when relevant. See `_format_mirror` in [`src/mcp_handlers/response_formatter.py`](src/mcp_handlers/response_formatter.py).

```jsonc
{
  "verdict": "proceed",
  "_mode": "mirror",
  "mirror": [
    "Fleet calibration: 72% accuracy over 12 fleet-wide decisions (high-conf: 0.8, low-conf: 0.5)",
    "Complexity divergence: you reported 0.60 but system derives 0.45 (divergence=0.15)"
  ],
  "question": "What's driving your sense of difficulty?",
  "relevant_prior_work": [
    { "summary": "Rate limiter bypass in auth …", "by": "agent-abc", "relevance": 0.82 }
  ]
}
```

**Verdict field:** Responses expose `verdict` from `decision.action`. Governance actions are **`proceed` / `guide` / `pause` / `reject`** ([Architecture](docs/UNIFIED_ARCHITECTURE.md)). If `action` is absent, formatters fall back to **`continue`** — see `response_formatter.py`.

The `onboard()` response includes `agent_uuid`. Store it as an identity anchor. On a fresh process that continues prior work, call `onboard(force_new=true, parent_agent_id=<prior uuid>, spawn_reason="new_session")`. Use `identity(agent_uuid=..., continuity_token=..., resume=true)` only for same-owner proof-owned rebinds.

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

## How state works

Agents emit text and tool results; they rarely expose a stable notion of internal condition. Unitares exposes four continuous variables any client can report and any observer can read:

| Variable | Range | What it tracks |
|----------|-------|----------------|
| **E** (Energy) | [0, 1] | Productive capacity |
| **I** (Integrity) | [0, 1] | Information coherence |
| **S** (Entropy) | [0, 1] | Disorder and uncertainty |
| **V** (Valence) | [-1, 1] | Signed energy/integrity imbalance: energetic-but-incoherent (positive) vs coherent-but-depleted (negative) |

**Behavioral EISV (primary, verdict-driving)** — Implemented in `src/behavioral_state.py` and `src/behavioral_assessment.py`: EMA-smoothed observations per dimension, no ODE and no universal attractor. After **~30** updates, per-agent **Welford** baselines enable self-relative scoring (z-score vs *your* operating point). Earlier check-ins use bootstrap behavior; absolute safety floors still apply.

**ODE in `governance_core` (secondary, diagnostic/fallback)** — The same four variables also evolve in a coupled ODE with contraction-style stability analysis. That integration runs **in parallel for analysis**; governance verdicts normally follow behavioral EISV once behavioral confidence is established, while ODE remains the fallback when behavioral confidence is still insufficient. See [Architecture](docs/UNIFIED_ARCHITECTURE.md) for the full pipeline (drift → entropy, calibration, circuit breaker, dialectic).

<details>
<summary><strong>Dynamics (for the curious)</strong> — the coupled ODE behind the fallback path</summary>

```
dE/dt = α(I - E) - β·E·S           Energy tracks integrity, dragged by entropy
dI/dt = -k·S + β_I·C(V) - γ_I·I   Integrity boosted by coherence, reduced by entropy
dS/dt = -μ·S + λ₁·‖Δη‖² - λ₂·C   Entropy decays, rises with drift, damped by coherence
dV/dt = κ(E - I) - δ·V             Valence accumulates E-I mismatch, decays toward zero
```

</details>

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

- [**Lumen / anima-mcp**](https://github.com/cirwel/anima-mcp) — Embodied agent on Raspberry Pi
- [**unitares-governance-plugin**](https://github.com/cirwel/unitares-governance-plugin) — Installable client adapters for Codex and Claude
- [**unitares-discord-bridge**](https://github.com/cirwel/unitares-discord-bridge) — Discord presence and governance events
- [**eisv-lumen**](https://github.com/cirwel/eisv-lumen) — Governance benchmark (21K trajectories on HuggingFace)
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

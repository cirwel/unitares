<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/hero.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/hero.svg">
  <img alt="UNITARES — Self-regulating AI agents" src="docs/assets/hero.svg" width="100%">
</picture>

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

Status: live. First public commit 2025-12-04.

**UNITARES watches a fleet of AI agents while they work and tells you — and each agent — when one is starting to go off the rails, before anything visibly breaks.**

When you run many autonomous agents, you can check *whether a model is good enough to deploy* (evals) and *catch bad actions as they happen* (guardrails). What you usually can't see is what the fleet is doing **right now**: whether each agent is still making real progress, whether its confidence matches its actual results, and whether it's drifting away from how it normally behaves. That live picture is what UNITARES provides. It runs alongside your evals and guardrails — it doesn't replace them.

### What you get

- **Drift early-warning** — catch an agent degrading while it's still just numbers moving, before it ships visibly broken output.
- **Outcome-calibrated confidence** — overconfident agents get caught automatically: claimed confidence is scored against verified results, not taken on trust.
- **Self-correction before escalation** — the agent reads its own state and narrows scope or stops *before* an external guardrail has to fire.
- **Cross-agent + human observability** — one agent reads another's live state over the API; you watch the whole fleet on a dashboard.
- **Peer review instead of hard stops** — when an agent's confidence and the system's assessment disagree, agents (or an LLM) reconcile before anything halts.

**In one screen:** running continuously since November 2025 · 3.7M+ governance events under sustained load · dogfooded — the agents building UNITARES run under it · two-call integration (`sync_state` + `record_result`) · `make demo` shows the self-correction loop in 60 seconds. Single-operator so far, not external adoption — and you don't have to take the numbers on faith (see the harness below).

<p align="center">
  <img src="docs/assets/dashboard.png" width="80%" alt="Unitares dashboard — fleet coherence, agent count, discoveries, and system health"/>
</p>

> **Evaluating with an agent?** Don't trust the prose — regenerate the evidence. On a fresh clone the [falsifiability harness](docs/REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc) scores EISV/prior-state features against a deliberately dumb baseline (AUC, Brier) and self-labels each slice `INCONCLUSIVE` / `SKEPTICAL` / `WEAK SIGNAL` / `KEEP TESTING` rather than asserting — the harness is the part you run yourself.

Human evaluators: start with the [Reviewer Guide](docs/REVIEWER_GUIDE.md); architecture is in [docs/UNIFIED_ARCHITECTURE.md](docs/UNIFIED_ARCHITECTURE.md).

**Contents:** [What you get](#what-you-get) · [How it works](#how-it-works-in-one-read) · [Who should integrate](#who-should-integrate-this) · [Quickstart](#quickstart) · [Integrate](#integrate) · [Trustworthy signal](#what-makes-the-signal-trustworthy) · [Scope & threat model](#scope-and-threat-model) · [Production snapshot](#production-snapshot) · [Architecture](#architecture) · [Docs](#documentation)

### How it works in one read

After each unit of work, an agent checks in by calling `sync_state()`. The check-in includes a self-reported `confidence`, a self-reported `complexity`, the `response_text`, and any `recent_tool_results` — test outcomes, exit codes, lint output, file changes — i.e. things the system can verify instead of taking the agent's word for. (This README uses the primary task-verb tool names throughout; older raw names remain stable for compatibility — full mapping under [Tool names](#tool-names).)

From each check-in, UNITARES tracks four numbers per agent — together called **EISV**:

- **E (Energy)** — is the work advancing? Tool calls succeeding and decisions resolving raise E; thrashing, retries, and no-progress lower it.
- **I (Integrity)** — do the agent's claims match its results? Confidence that lines up with the observed success rate raises I; high confidence with low actual success lowers it.
- **S (Entropy)** — is the agent drifting from its own normal behavior? A steady, consistent trajectory keeps S low; erratic, divergent behavior pushes it up.
- **V (Valence)** — a derived readout of the E−I gap, not an independent fourth dimension. Positive means *energetic but incoherent* (lots of motion, claims not matching outcomes); negative means *coherent but running low on progress*.

These four numbers are computed from **auditable heuristic blends over observable behavior** — decision outcomes, calibration error (claimed confidence vs. verified success), drift from the agent's own baseline, tool results — then EMA-smoothed. There is no black box. The *information-theoretic* readings of E/I/S/V (entropy, mutual information, free energy) used in [Paper v6](https://github.com/cirwel/unitares-paper-v6) are **target semantics**, not what the running code computes today; the exact deployed formulas, with provenance tags, are in [How EISV is computed](docs/EISV_COMPUTATION.md).

Each check-in returns a plain verdict — `proceed` / `guide` / `pause` / `reject` — so the agent can correct itself before any external safety system has to step in. Humans read the same state on a dashboard; other agents can read it over the API.

### Why an agent can't just inflate its own confidence

Self-reported confidence is only one input. UNITARES also watches **real outcomes it can verify** — test pass/fail, exit codes, tool results — sent back via `record_result()`. Over many tasks it compares the agent's *claimed* confidence against its *actual* success rate. An agent that reports `confidence=0.9` while only succeeding 50% of the time builds up a track record of being overconfident; its Integrity (I) drops, and the verdict shifts to `guide` or `pause`. The signal is anchored to what actually happened, not to what the agent said about itself.

After about 30 check-ins, the four numbers are graded against the agent's *own* running history rather than a one-size-fits-all threshold. Absolute safety floors still apply on top of that.

Running continuously since November 2025; state in PostgreSQL + AGE. **The verdict path is the auditable behavioral model described above** — component risk plus self-relative z-scores, source in [`src/behavioral_assessment.py`](src/behavioral_assessment.py). A separate dynamical-systems model (`governance_core/`, the thermodynamic / free-energy formulation) runs **in parallel as a research cross-check and does not drive verdicts by default** ([`governance_monitor.py`](src/governance_monitor.py): *"the ODE runs in parallel but does NOT drive verdicts… primary verdicts come from behavioral assessment"*). Its derivation is in [Paper v6](https://github.com/cirwel/unitares-paper-v6) (DOI 10.5281/zenodo.19647159). Want the theory → start with the paper; want what actually fires → [How EISV is computed](docs/EISV_COMPUTATION.md).

### Who should integrate this

UNITARES is for you if you run **multiple long-lived autonomous agents** — tool-using, multi-step, doing real work over hours or days — and you've watched an agent quietly drift without anyone noticing until something visible broke. The check-in loop surfaces that drift while it's still just numbers moving (Integrity slipping, overconfidence climbing) instead of waiting for a user to complain. It runs in parallel with your evals and guardrails as a live state layer the agent itself can read.

**The threshold that matters is check-in count, not wall-clock time.** Self-relative grading needs roughly **30 check-ins** to establish an agent's baseline (absolute safety floors apply before that). An agent doing dozens of units of work — over an hour or a week — crosses it; one that does three and exits never does. That's the real line for "is my session long enough to benefit," not a duration.

**Probably not worth it yet for** short-lived chatbot turns, where per-turn overhead outweighs the benefit, or for teams that can't instrument their agent loop.

---

## Quickstart

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up -d --wait         # Postgres+AGE+pgvector+Redis+server, bound to 127.0.0.1
make demo                           # 60-second scripted trajectory
```

`make demo` drives a synthetic agent through seven check-ins (clean work → confidence drifting from results → confusion) and prints the verdict + state at each step ([`scripts/demo/quick_demo.py`](scripts/demo/quick_demo.py)). Then point any MCP client at `http://localhost:8767/mcp/`.

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

`requirements-full.txt` is the default (server, tests, handler dev); `requirements-core.txt` is a 2-package subset (`mcp` + `numpy`) for thin stdio/proxy clients. DB bring-up: [db/postgres/README.md](db/postgres/README.md). Run signal-only without the math model: `export UNITARES_DISABLE_ODE=1`.

</details>

**Stack:** Python 3.12+ · PostgreSQL + AGE + pgvector · Redis (optional). Transports: MCP on `/mcp/` (Streamable HTTP) · REST on `/v1/tools/call` · Dashboard on `/dashboard`. Full port map, gateway, and lease-plane: [`docs/operations/DEFINITIVE_PORTS.md`](docs/operations/DEFINITIVE_PORTS.md). MCP client config (Cursor / Claude Code / Claude Desktop) and bind-address security: [`docs/integration/MCP_CLIENTS.md`](docs/integration/MCP_CLIENTS.md).

---

## Integrate

The loop is two calls; the dashboard, knowledge graph, peer review, and continuity features are all downstream of them.

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

The agent reads its own metrics and adjusts *before* external controls have to fire. UNITARES isn't an output validator (guardrails, evals) or a sandbox (permissions, container limits) — it's a state layer the agent itself can read.

**Check-in shape.** `response_mode` controls how much detail comes back; **`mirror`** returns a short list of plain-text signals the agent can act on:

```jsonc
sync_state({
  "response_text": "Refactored auth module, added rate limiting",
  "complexity": 0.6,
  "confidence": 0.8,
  "task_type": "refactoring",
  "response_mode": "mirror"  // or: minimal, compact, standard, full, auto
})
```

<details>
<summary><strong>Example <code>mirror</code> response</strong></summary>

```jsonc
{
  "verdict": { "value": "proceed", "meaning": "State is healthy.", "next_action": "Continue working normally." },
  "_mode": "mirror",
  "mirror": [
    "Fleet calibration: 72% accuracy over 12 fleet-wide decisions (high-conf: 0.8, low-conf: 0.5)",
    "Complexity divergence: you reported 0.60 but system derives 0.45 (divergence=0.15)"
  ],
  "reflection": "Complexity estimate is diverging from the output-surface proxy.",
  "relevant_prior_work": [ { "summary": "Rate limiter bypass in auth …", "by": "agent-abc", "relevance": 0.82 } ]
}
```

Optional `reflection` and `relevant_prior_work` surface a one-line state read and related knowledge-graph items. See `_format_mirror` in [`src/mcp_handlers/response_formatter.py`](src/mcp_handlers/response_formatter.py).

</details>

**Verdicts** are always `proceed` / `guide` / `pause` / `reject` ([Architecture](docs/UNIFIED_ARCHITECTURE.md)); mirror/compact responses wrap each with `value`, `meaning`, and `next_action`.

**Identity.** `start_session(force_new=true)` returns `agent_uuid` (save it — the identity anchor) and `client_session_id`. To link a fresh process to prior work: `start_session(force_new=true, parent_agent_id=<prior uuid>, spawn_reason="new_session")`. (`continuity_token` is only short-lived ownership proof for explicit UUID rebinds.) For long-running or scheduled agents, the SDK in [`agents/sdk/README.md`](agents/sdk/README.md) handles connection, identity, check-ins, heartbeats, and pause hooks. More: [Getting Started](docs/guides/START_HERE.md) · [Operator Runbook](docs/operations/OPERATOR_RUNBOOK.md).

### Tool names

Use the primary task-verb tools; the older raw names remain for compatibility and for cases where you want the raw handler response shape. Primary tools return the agent-experience envelope, with the full payload under `raw_governance`.

| Primary workflow tool | Raw implementation tool |
|---|---|
| `start_session` | `onboard` |
| `sync_state` | `process_agent_update` |
| `check_working_state` | `get_governance_metrics` |
| `record_result` | `outcome_event` |
| `search_shared_memory` | `knowledge(action="search")` |
| `request_review` | `dialectic(action="request")` |

---

## What makes the signal trustworthy

**Trajectory as identity.** An agent's EISV pattern over time answers continuity questions ("is this still the same agent across restarts?") and surfaces slow drift that no single check-in could catch.

**Peer review when it matters.** When an agent's confidence and the system's assessment disagree, UNITARES runs a short back-and-forth review with other agents — or with an LLM when no peers are around — before anything halts. (In the codebase and papers this is called *dialectic*.) See [dialectic-dataset](https://github.com/cirwel/dialectic-dataset).

---

## Scope and threat model

UNITARES is **adversarial-aware, not adversarial-naive**, with deliberately conservative enforcement. The signal is anchored to outcomes an agent can't fake — test pass/fail, exit codes, tool results via `record_result()`: an agent can inflate its self-reported `confidence` but not its actual success rate, and drift (S) is graded against its *own* prior trajectory, so there is no fixed threshold to pre-game. Verdicts lean toward `proceed`/`guide` over `pause`/`reject` by design — a false-positive pause on a productive agent is itself a failure mode (acutely so here, since the agents building UNITARES run under it). And "no ethics classifier" means no hand-labeled ethics model, *not* that the system is value-free: drift (S) is a salience flag, not a verdict, and Integrity (I) is anchored to ground-truth outcomes rather than to the agent's own history.

**The genuine open question.** Robustness against a *motivated* attacker deliberately optimizing the EISV proxy, at scale, is unproven — the deployment is single-operator with no red-team. That is the real limitation: an absence of adversarial *testing*, not of adversarial design.

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

<details>
<summary><strong>Dashboard views</strong> (pulse, EISV charts, agents, dialectic, activity)</summary>

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

---

## Architecture

E, I, and S live in `[0, 1]`, V in `[-1, 1]`. The primary signal is EMA-smoothed from observed behavior ([`src/behavioral_state.py`](src/behavioral_state.py)); verdict thresholds and absolute safety floors are in [`src/behavioral_assessment.py`](src/behavioral_assessment.py); the `governance_core/` math model runs in parallel as a diagnostic cross-check. Full pipeline (drift → entropy, calibration, circuit breaker, peer review) and the math derivation: [Architecture](docs/UNIFIED_ARCHITECTURE.md) and [Paper v6](https://github.com/cirwel/unitares-paper-v6).

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

Beyond the core loop, the same state feeds trajectory-based identity and continuity (is this the same agent across restarts?) and a persistent knowledge graph with staleness awareness, so discoveries carry across agents and sessions.

---

## Documentation

| Guide | Purpose |
|-------|---------|
| [Getting Started](docs/guides/START_HERE.md) | Setup, workflows, tool modes |
| [How EISV is computed](docs/EISV_COMPUTATION.md) | Deployed formulas vs. target semantics |
| [Reviewer Guide](docs/REVIEWER_GUIDE.md) | Cold-evaluator path + falsifiability harness |
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

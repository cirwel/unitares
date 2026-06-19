<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/hero.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/assets/hero.svg">
  <img alt="UNITARES — Self-regulating AI agents" src="docs/assets/hero.svg" width="100%">
</picture>

### Catch an AI agent going off the rails — before anything breaks.

**Runtime telemetry & self-governance for fleets of autonomous AI agents.**<br/>
UNITARES watches each agent while it works and tells you — and the agent itself — the moment one starts to drift, while it's still just numbers moving and not yet broken output.

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.19647159.svg)](https://doi.org/10.5281/zenodo.19647159)

*Status: live. First public commit 2025-12-04 · 3.7M+ governance events in production · dogfooded.*

[![▶ Quickstart](https://img.shields.io/badge/▶_Quickstart-2ea44f?style=for-the-badge)](#try-it-in-60-seconds)
[![Read the docs](https://img.shields.io/badge/Docs-1f6feb?style=for-the-badge)](docs/README.md)
[![Paper](https://img.shields.io/badge/Paper_v6-8957e5?style=for-the-badge)](https://github.com/cirwel/unitares-paper-v6)
[![Reviewer Guide](https://img.shields.io/badge/Verify_it_yourself-da7633?style=for-the-badge)](docs/REVIEWER_GUIDE.md)

</div>

---

<table>
<tr>
<td width="33%" valign="top">

### 🩺 See drift early

Each agent is graded against its *own* baseline. Slow degradation shows up as Integrity slipping and entropy rising — while the output still looks fine.

</td>
<td width="33%" valign="top">

### 🔒 Confidence you can't fake

Claims are scored against **real results** — tests, exit codes, tool output — not the agent's word. An agent can inflate `confidence`; it can't inflate its success rate.

</td>
<td width="33%" valign="top">

### 🛰️ The whole fleet, live

One dashboard for humans. Over the API, agents read **each other's** live state to decide whether to trust a handoff — and self-correct before a guardrail has to fire.

</td>
</tr>
</table>

## Try it in 60 seconds

```bash
git clone https://github.com/cirwel/unitares.git && cd unitares
docker compose up -d --wait && make demo
```

`make demo` drives a synthetic agent through seven check-ins — clean work, then confidence drifting from results, then confusion — and prints the verdict at each step. Then point any MCP client at `http://localhost:8767/mcp/`.

<div align="center">
  <img src="docs/assets/dashboard.png" width="85%" alt="Unitares dashboard — fleet coherence, agent count, discoveries, and system health"/>
</div>

> **Running continuously since November 2025 · 3.7M+ governance events under sustained load · dogfooded** — the agents building UNITARES run under it. Every number is verifiable on a fresh clone. ([Production snapshot →](docs/PRODUCTION_SNAPSHOT.md))

## Where it fits

UNITARES runs **alongside** your evals and guardrails — it doesn't replace either. They answer different questions at different times:

| | Question it answers | When it acts |
|---|---|---|
| **Evals** | Is this model good enough to ship? | before deploy |
| **Guardrails** | Is this *action* allowed right now? | per action |
| **UNITARES** | Is this agent *still healthy* as it works? | continuously, mid-run |

## The core idea in 30 seconds

After each unit of work, an agent checks in with `sync_state()` — passing its self-reported confidence plus verifiable evidence (test outcomes, exit codes, tool results). UNITARES turns that into four live numbers per agent, **EISV**:

| | | Goes wrong when… |
|---|---|---|
| **E** · Energy | is the work advancing? | thrashing, retries, no progress |
| **I** · Integrity | do claims match results? | high confidence, low actual success |
| **S** · Entropy | drifting from its own normal? | erratic, divergent behavior |
| **V** · Valence | the E−I gap, derived | motion without coherence (or vice-versa) |

Each check-in returns a plain verdict — **`proceed` / `guide` / `pause` / `reject`** — so the agent can correct itself *before* an external safety system has to step in. After ~30 check-ins, each agent is graded against its *own* baseline, so slow degradation surfaces even while output still looks fine.

<div align="center">

[How EISV is computed](docs/EISV_COMPUTATION.md) · [Architecture](docs/UNIFIED_ARCHITECTURE.md) · [Who it's for & threat model](docs/SCOPE_AND_THREAT_MODEL.md)

</div>

## Integrate in two calls

```python
# Inside the agent's loop
result = sync_state(response_text=output, complexity=0.6, confidence=0.8)
eisv = result.get("raw_governance", result).get("primary_eisv", {})

if eisv.get("I", 1) < 0.4:
    agent.require_human_review("integrity low — pausing autonomous actions")
elif eisv.get("S", 0) > 0.7:
    agent.narrow_scope()        # fewer tools, tighter search
elif eisv.get("E", 1) < 0.2:
    agent.stop_and_summarize()  # avoid thrashing
```

The agent reads its own state and adjusts before external controls fire. UNITARES isn't an output validator or a sandbox — it's a state layer the agent itself can read. For long-running or scheduled agents, the [SDK](agents/sdk/README.md) handles connection, identity, check-ins, and heartbeats.

<div align="center">

[Getting started](docs/guides/START_HERE.md) · [MCP client config](docs/integration/MCP_CLIENTS.md) · [Tool names & raw API](docs/guides/START_HERE.md)

</div>

## Don't trust this README — verify it

**Evaluating with an agent?** Don't take the prose. On a fresh clone, the [falsifiability harness](docs/REVIEWER_GUIDE.md#falsifiability-grade-eisv-yourself-dont-trust-this-doc) scores EISV against a deliberately dumb baseline (AUC, Brier) and self-labels each slice `INCONCLUSIVE` / `SKEPTICAL` / `WEAK SIGNAL` / `KEEP TESTING` rather than asserting. The harness is the part you run yourself.

**Honest about what fires.** The verdict path is the auditable *behavioral* model — component risk plus self-relative z-scores ([`src/behavioral_assessment.py`](src/behavioral_assessment.py)). The information-theoretic / free-energy formulation in `governance_core/` and [Paper v6](https://github.com/cirwel/unitares-paper-v6) is **target semantics**: it runs in parallel as a research cross-check and **does not drive verdicts by default** ([`governance_monitor.py`](src/governance_monitor.py): *"the ODE runs in parallel but does NOT drive verdicts… primary verdicts come from behavioral assessment"*). Want the theory → the paper; want what actually fires → [How EISV is computed](docs/EISV_COMPUTATION.md).

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
| [Production snapshot](docs/PRODUCTION_SNAPSHOT.md) | Live metrics + dashboard views |
| [MCP Clients](docs/integration/MCP_CLIENTS.md) | Cursor / Claude Code / Claude Desktop config |
| [Troubleshooting](docs/guides/TROUBLESHOOTING.md) | Common issues |
| [Changelog](docs/CHANGELOG.md) | Releases |

> Three files at the repo root — [`CLAUDE.md`](CLAUDE.md), [`AGENTS.md`](AGENTS.md), [`CODEX_START.md`](CODEX_START.md) — orient AI CLIs (Claude Code, Codex). Human readers can skip them.

## Related projects

- [**anima-mcp**](https://github.com/cirwel/anima-mcp) — reference UNITARES deployment cited as longitudinal validation data in the papers
- [**unitares-governance-plugin**](https://github.com/cirwel/unitares-governance-plugin) — installable client adapters for Codex and Claude
- [**unitares-discord-bridge**](https://github.com/cirwel/unitares-discord-bridge) — Discord presence and governance events
- [**eisv-lumen**](https://github.com/cirwel/eisv-lumen) — governance benchmark dataset (21K agent-state trajectories on HuggingFace)
- [**unitares-paper-v6**](https://github.com/cirwel/unitares-paper-v6) — companion paper *Information-Theoretic Governance of Heterogeneous Agent Fleets* (Wang, 2026); concept DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159)

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
Built by [@cirwel](https://github.com/cirwel) · CIRWEL Systems

</div>

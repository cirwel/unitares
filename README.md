<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/unitares-lockup-dark.svg">
  <img src="docs/assets/unitares-lockup.svg" width="420" alt="UNITARES">
</picture>

### Accountability infrastructure for long-running AI agents.

**Many agents, one record.**

When an agent restarts, hands off, or loses its context, the next one can see
who claimed what, on what evidence, who challenged it, and what replaced it.

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.14+-5C544A?style=flat-square&labelColor=1A1612)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-5C544A?style=flat-square&labelColor=1A1612)](LICENSE)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.19647159-7A1F1F?style=flat-square&labelColor=1A1612)](https://doi.org/10.5281/zenodo.19647159) [![Glama score](https://glama.ai/mcp/servers/cirwel/unitares/badges/score.svg)](https://glama.ai/mcp/servers/cirwel/unitares)

</div>

Agent work outlives the process that started it. A coding session restarts, a
research agent loses its context, a task moves to another runtime. Logs show
each run, but not the thread between them, so the next agent repeats work,
trusts a claim that was already corrected, or cannot tell who decided what.

UNITARES is a server you run that keeps that thread. Claude Code, Codex, Hermes
Agent, and any MCP or HTTP client write their findings, reviews, and results to
one operator-owned record. A later agent, or you, can read it back and see
where each piece came from.

## What the record answers

| Question | Mechanism |
|---|---|
| **Who said it?** | **Identity and lineage**: every write is bound to the process that made it, and a successor declares which process it continues. |
| **What supports it?** | **Claims and evidence**: findings keep their sources and corrections outside any single context window. A finding that replaces another points to it, so a stale claim carries its correction. |
| **Who challenged it?** | **Governed review**: objections, conditions, and their resolution stay on the work they concern. Asking for review is not the same as passing it. |
| **What happened?** | **Outcome grounding**: test, build, and task results are recorded against the check-ins that predicted them. |
| **What can a successor recover?** | **Reconstruction**: search, review history, and export let a new process pick up the work before it acts. |

Two more pieces sit around the record. **Runtime policy**: each check-in returns
`proceed`, `guide`, or `pause`, with a reason, and a pause holds writes until
review or recovery. **Second opinions**: `consult` asks another connected model
(Claude, Codex, Antigravity, or a local Ollama model) and keeps the answer with
the task.

## See it

Agent A finds a bug, records it, and exits. Agent B starts fresh after a
restart, finds A's finding before touching the code, and continues A's work
under its own identity. If A's finding was later corrected, B gets the
correction, not the stale claim.

```text
# Agent A
start_session   {"force_new": true}                                  -> agent A's id
store_finding   {"summary": "upload retry test is flaky: ...", ...}  -> finding id
record_result   {"outcome_type": "test_passed", ...}

# Agent B, after a restart
search_shared_memory {"query": "flaky upload retry test"}            -> A's finding, and any correction
start_session   {"force_new": true, "parent_agent_id": "<A's id>", "spawn_reason": "explicit"}
```

The [captured two-process walkthrough](docs/guides/AGENT_SESSION_WALKTHROUGH.md)
shows every call and response.

## Install

With Git, curl, and Docker Compose installed, one command starts the latest
verified release of the local stack (the server, PostgreSQL with AGE and
pgvector, Redis, and the coordination plane):

```bash
v=$(curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION) && git clone --branch "v$v" --depth 1 https://github.com/cirwel/unitares.git && cd unitares && docker compose up -d --wait
```

Then connect MCP clients at `http://localhost:8767/mcp/`, or open the dashboard
at `http://localhost:8767/dashboard`. Ports, volumes, models, and upgrades:
[Docker quickstart](docs/manual/02-install.md#21-docker-quickstart).

**Connect your agents** ([Integrating agents](docs/manual/04-integrating-agents.md)):
- **Claude Code and Codex:** the [governance plugin](https://github.com/cirwel/unitares-governance-plugin) wires MCP, lifecycle hooks, and edit leases.
- **Hermes Agent and OpenAI-compatible clients:** the [host adapter](https://github.com/cirwel/unitares-host-adapter).
- **Anything else:** [Streamable HTTP MCP](docs/integration/MCP_CLIENTS.md), the [Python SDK](agents/sdk/README.md) (`pip install unitares-sdk`), or the [resident agent runtime](https://github.com/cirwel/unitares-resident).

## Who it is for

For an operator running several long-lived AI agents, across vendors, on
infrastructure they control, who needs the agents' shared account of the work
to survive restarts and handoffs.

Not a sandbox, an output validator, or a hosted agent platform. It runs beside
your evals and guardrails and replaces neither, and it never takes over an
agent's own execution loop.

## How it fits together

UNITARES is self-hosted accountability infrastructure for operators running
multiple AI agents. Its single-operator federation kernel connects independent
runtimes to one operator-controlled server over MCP or HTTP, where they share a
durable record while keeping their own models, tools, and runtimes. They
interoperate with each other over their own transports or A2A; UNITARES is the
record behind them, not the transport between them. Together this is an
operator-owned accountability layer for coding, research, background, and
resident agents. What it adds to an activity log is adjudication: claims,
challenges, and outcomes stay bound to the process that made the claim, for
review and for reconstruction later.

Storage is PostgreSQL (with AGE for the finding graph and pgvector for semantic
search) and Redis, with no required external model API. The check-in's policy
action comes from a behavioral state estimate, a form of
runtime [proprioception](docs/ontology/eisv-proprioception-contract.md); that
estimator is moving out of the core record into an optional module.

What each claim has been measured to do, and what has not been measured, is in
[Evidence and limits](docs/EVIDENCE_AND_LIMITS.md) and the
[Reviewer Guide](docs/REVIEWER_GUIDE.md).

## Start here

| Goal | Guide |
|---|---|
| Try it | [Install](#install) → [two-process walkthrough](docs/guides/AGENT_SESSION_WALKTHROUGH.md) |
| Connect an agent or application | [MCP integration](docs/integration/MCP_CLIENTS.md) · [Python SDK](agents/sdk/README.md) |
| Operate a deployment | [Operator manual](docs/manual/README.md) |
| Understand the product and architecture | [Product definition](docs/PRODUCT_DEFINITION.md) · [Architecture](docs/UNIFIED_ARCHITECTURE.md) |
| Research behind it | [Evidence and limits](docs/EVIDENCE_AND_LIMITS.md) · [Paper](https://doi.org/10.5281/zenodo.19647159) · [Public dataset](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories) |
| Contribute | [Contributing](.github/CONTRIBUTING.md) · [Development guide](AGENTS.md) |
| Everything else | [Documentation index](docs/README.md) |

## Citation and license

Kenny Wang ([ORCID 0009-0006-7544-2374](https://orcid.org/0009-0006-7544-2374)),
CIRWEL Systems. See [`CITATION.cff`](CITATION.cff) for the versioned citation.

```bibtex
@misc{wang2026unitares,
  author = {Wang, Kenny},
  title  = {{UNITARES}: Information-Theoretic Governance of Heterogeneous Agent Fleets},
  year   = {2026},
  doi    = {10.5281/zenodo.19647159}
}
```

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).

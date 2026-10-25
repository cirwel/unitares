<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/unitares-lockup-dark.svg">
  <img src="docs/assets/unitares-lockup.svg" width="420" alt="UNITARES">
</picture>

### Accountability infrastructure for long-running AI agents.

A self-hosted record your agents write to. After a restart or handoff, the next
agent can look up who claimed what, on what evidence, who disputed it, and what
replaced it.

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.14+-5C544A?style=flat-square&labelColor=1A1612)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-5C544A?style=flat-square&labelColor=1A1612)](LICENSE)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.19647159-7A1F1F?style=flat-square&labelColor=1A1612)](https://doi.org/10.5281/zenodo.19647159) [![Glama score](https://glama.ai/mcp/servers/cirwel/unitares/badges/score.svg)](https://glama.ai/mcp/servers/cirwel/unitares)

</div>

Agent work outlives the process that started it. A coding session restarts, a
research agent loses its context, a task moves to another runtime. Logs show
each run, but not the thread between them. Without that thread, the next agent
can repeat a dead end, act on a claim that was already corrected, or lose the
evidence behind an earlier decision.

UNITARES is a server you run that keeps that thread. Agents connect over MCP or
HTTP (with ready integrations for Claude Code, Codex, and Hermes Agent) and
write their findings, reviews, and results to one operator-owned record. A later
agent, or you, can read it back and see where each piece came from.

## What the record answers

| Question | Mechanism |
|---|---|
| **Who said it?** | **Identity and lineage**: every write to the record is bound to the process that made it, and a successor can declare which process it continues. |
| **What supports it?** | **Claims and evidence**: findings keep their sources and corrections outside any single context window. When a newer finding replaces an older one, the older one links to it. |
| **Who challenged it?** | **Governed review**: objections, conditions, and their resolution stay on the work they concern. Asking for review is not the same as passing it. |
| **What happened?** | **Outcome grounding**: test, build, and task results are recorded against the check-ins that predicted them. |
| **What can a successor recover?** | **Reconstruction**: a new process can search or export prior findings, open review conditions, and outcomes before it acts. |

Two more pieces sit around the record. **Runtime policy**: each check-in returns
`proceed`, `guide`, or `pause`, with a reason; a pause refuses that process's
check-ins and new findings until review or recovery lifts it or it expires.
**Second opinions**: `consult` asks another connected model (Claude, Codex, Antigravity,
or local Ollama) and keeps the answer with the task.

## See it

Agent A (Codex) finds a bug, records it, and exits. Agent B (Claude Code) later
finds the real cause and records a finding that supersedes A's. Agent C (Hermes
Agent) picks up A's task after a restart and reads the record before touching
the code (reading needs no identity): its search returns A's finding marked
superseded, with a link to B's. C then declares that it continues A's task.

```text
# Agent A (Codex)
start_session  {"force_new": true}                                     -> agent A's id
store_finding  {"summary": "upload retry test is flaky", ...}           -> finding a
record_result  {"outcome_type": "test_failed", ...}

# Agent B (Claude Code), in its own session, later
start_session  {"force_new": true}                                     -> agent B's id
store_finding  {"summary": "upload retry fails on clock skew", ...}     -> finding b
use_tool       {"tool_name": "knowledge", "arguments": {"action": "supersede", "discovery_id": "<b>", "supersedes_id": "<a>"}, ...}

# Agent C (Hermes Agent), after A's restart
search_shared_memory {"query": "upload retry test"}   -> finding a (superseded, points to b), finding b
start_session  {"force_new": true, "parent_agent_id": "<A's id>", "spawn_reason": "explicit"}
```

Nothing forces an agent to read the record; the Claude Code and Codex plugin's
session start points it there. A simpler handoff, call by call: the
[captured two-process walkthrough](docs/guides/AGENT_SESSION_WALKTHROUGH.md).

## Install

**Docker Compose**, on any platform: with Git, curl, and Docker Compose installed, one command starts the latest published release of the local stack (the server, PostgreSQL with AGE and pgvector, Redis, and the coordination plane; the [core profile](docs/manual/02-install.md#a-smaller-stack-the-core-profile) runs only the first two):

```bash
v=$(curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION) && git clone --branch "v$v" --depth 1 https://github.com/cirwel/unitares.git && cd unitares && docker compose up -d --wait
```

**Homebrew preview**, on an Apple Silicon Mac without Docker: the [`cirwel/unitares` tap](https://github.com/cirwel/homebrew-unitares) installs the same services natively. Installing the package does not provision the database: follow the tap's [install](https://github.com/cirwel/homebrew-unitares#install) and [first-time setup](https://github.com/cirwel/homebrew-unitares#first-time-setup), then run `unitares start`. Upgrades follow its [backup-and-migrate steps](https://github.com/cirwel/homebrew-unitares#upgrades-and-recovery).

Either way, connect MCP clients at `http://localhost:8767/mcp/`, or open the dashboard at `http://localhost:8767/dashboard`. Pick one path; they keep separate data ([installation choices](docs/install/CHOOSING_INSTALLATION.md)). One stack per machine: container names are fixed, so run `docker compose down` (without `-v`) in an earlier checkout before starting another (`stop` is not enough, because `restart: unless-stopped` revives stopped containers when Docker starts), or use the [managed launcher](docs/manual/02-install.md#managed-checkout-alternative) (`scripts/install/launch.sh`), which checks ownership first. No model connection is configured by default. Ports, volumes, models, and Docker upgrades: [Docker quickstart](docs/manual/02-install.md#21-docker-quickstart).

**Connect your agents** ([Integrating agents](docs/manual/04-integrating-agents.md)):
- **Claude Code and Codex:** [`unitares-governance-plugin`](https://github.com/cirwel/unitares-governance-plugin) wires MCP, lifecycle hooks, and edit leases.
- **Hermes Agent and OpenAI-compatible clients:** the [host adapter](https://github.com/cirwel/unitares-host-adapter).
- **Anything else:** [Streamable HTTP MCP](docs/integration/MCP_CLIENTS.md), the [Python SDK](agents/sdk/README.md) (`pip install unitares-sdk`), or the [resident agent runtime](https://github.com/cirwel/unitares-resident).

## Who it is for

For an operator running several long-lived AI agents, across vendors, on
infrastructure they control, who needs the agents' shared account of the work
to survive restarts and handoffs.

If your agents all work in one repository, git and an issue tracker may be
enough. UNITARES is for work that spans runtimes and outlives any one
repository or session.

Not a sandbox, an output validator, or a hosted agent platform. It runs beside
your evals and guardrails and replaces neither. It does not run the agent's
loop: the plugin's hooks and edit leases coordinate around the agent's work,
and a pause refuses only that process's check-ins and new findings.

## How it fits together

UNITARES is self-hosted accountability infrastructure for operators running
multiple AI agents. Its single-operator federation kernel connects independent
runtimes to one operator-controlled server over MCP or HTTP, where they share a
durable record while keeping their own models, tools, and runtimes. They
interoperate with each other over their own transports or A2A; UNITARES is the
record behind them, not the transport between them. Together this is an
operator-owned accountability layer for coding, research, background, and
resident agents. What it adds to an activity log is lineage and supersession:
claims and evidence, reviews, and outcomes stay bound to the process that
recorded them, and a replaced claim points to its replacement, for review and
for reconstruction later.

Storage is PostgreSQL and Redis, with no required external model API. The
quickstart runs the AGE backend, which stores the links between findings; the
plain `postgres` backend has no supersession links and full-text search only.
The check-in's policy action comes from a behavioral state estimate, a form of
runtime [proprioception](docs/ontology/eisv-proprioception-contract.md).

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

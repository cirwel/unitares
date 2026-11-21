<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/unitares-lockup-dark.svg">
  <img src="docs/assets/unitares-lockup.svg" width="420" alt="UNITARES">
</picture>

### Accountability infrastructure for long-running AI agents.

A self-hosted record your agents write to. Across sessions and runtimes, people
and agents can look up who claimed what, on what evidence, who disputed it, and
what replaced it.

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.14+-5C544A?style=flat-square&labelColor=1A1612)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-5C544A?style=flat-square&labelColor=1A1612)](LICENSE)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.19647159-7A1F1F?style=flat-square&labelColor=1A1612)](https://doi.org/10.5281/zenodo.19647159) [![Glama score](https://glama.ai/mcp/servers/cirwel/unitares/badges/score.svg)](https://glama.ai/mcp/servers/cirwel/unitares)

</div>

Agent work outlives the process that started it. Git, scratchpads, and handoff
notes carry code and in-flight task state; UNITARES keeps the shared record of
claims, reviews, and outcomes across runs. When a finding is corrected or a
change is challenged, the next session reads the updated record instead of
acting on a superseded claim.

Any agent or model that speaks MCP or HTTP can write its findings, reviews, and outcomes to one operator-owned record, with ready integrations for Claude Code, Codex, and Hermes Agent.

**[Walk through a recorded incident →](https://cirwel.github.io/unitares/demo.html)** · [Read the evidence](docs/EVIDENCE_AND_LIMITS.md) · [Install](#install)

## What the record answers

| Question | Mechanism |
|---|---|
| **Who said it?** | **Identity and lineage**: every write is bound to the process that made it, and a successor can declare which process it continues. |
| **What supports it?** | **Claims and evidence**: findings keep their sources and corrections outside any single context window. When a finding is superseded, the older entry links to its replacement. |
| **Who challenged it?** | **Governed review**: objections, conditions, and their resolution stay attached to the work; unresolved reviewer conditions remain open across sessions. |
| **What happened?** | **Outcome grounding**: test, build, and task results are recorded against the check-ins that predicted them. |
| **What can a successor recover?** | **Reconstruction**: a new process can search or export prior findings, open review conditions, and outcomes before it acts. |

**Digital proprioception** runs on each check-in: **EISV** summarizes behavioral
observations over time and returns state feedback and a policy recommendation
([computation contract](docs/EISV_COMPUTATION.md#role-runtime-and-evidence)).
**Dialectic review** records thesis, challenge, and resolution on the work; when
a model endpoint or orchestrator is configured, **`consult`** queries an
independent model.

## See the record before installing

Agent A (Codex) records a bug finding and exits. Agent B (Claude Code) later
identifies the root cause and supersedes A's finding. In a later session, Agent
C (Hermes Agent) searches the record before editing code—reads require no
identity—and receives A's finding marked superseded alongside a link to B's. C
then declares explicit lineage from A.

```text
# Agent A (Codex)
start_session  {"force_new": true}                                     -> agent A's id
store_finding  {"summary": "upload retry test is flaky", ...}           -> finding a
record_result  {"outcome_type": "test_failed", ...}

# Agent B (Claude Code), in its own session, later
start_session  {"force_new": true}                                     -> agent B's id
store_finding  {"summary": "upload retry fails on clock skew", ...}     -> finding b
use_tool       {"tool_name": "knowledge", "arguments": {"action": "supersede", "discovery_id": "<b>", "supersedes_id": "<a>"}, ...}

# Agent C (Hermes Agent), in a later session
search_shared_memory {"query": "upload retry test"}   -> finding a (superseded, points to b), finding b
start_session  {"force_new": true, "parent_agent_id": "<A's id>", "spawn_reason": "explicit"}
```

With the governance plugin enabled in Claude Code or Codex, session-start hooks surface lifecycle guidance and local continuity context. For captured server responses end to end, see the [two-process walkthrough](docs/guides/AGENT_SESSION_WALKTHROUGH.md); to inspect a multi-agent incident in the browser, see the [service-reload fixture demo](https://cirwel.github.io/unitares/demo.html).

## Install

**Homebrew preview** (Apple Silicon macOS): install the native service formulas from [`cirwel/unitares`](https://github.com/cirwel/homebrew-unitares) using its [install](https://github.com/cirwel/homebrew-unitares#install) and [first-time setup](https://github.com/cirwel/homebrew-unitares#first-time-setup) guides. Run `unitares update` for coordinated backups, migrations, and health checks; kegs that predate it use the tap's [one-time manual transition](https://github.com/cirwel/homebrew-unitares#upgrades-and-recovery).

**Docker Compose** (any platform, with Git, `curl`, and Docker Compose): starts the latest published release—the server, PostgreSQL with AGE and pgvector, Redis, and the coordination plane:

```bash
v=$(curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION) && git clone --branch "v$v" --depth 1 https://github.com/cirwel/unitares.git && cd unitares && docker compose up -d --wait
```

- **Stack profiles:** add [`docker-compose.full.yml`](docs/manual/02-install.md#24-quickstart-defaults-and-a-full-setup) for local semantic search and hybrid ranking, or use the [core profile](docs/manual/02-install.md#a-smaller-stack-the-core-profile) to run only the server and PostgreSQL.
- **Endpoints and updates:** connect MCP clients at `http://localhost:8767/mcp/` and open the dashboard at `http://localhost:8767/dashboard` ([Docker quickstart](docs/manual/02-install.md#21-docker-quickstart)). Run `./scripts/unitares update` to back up PostgreSQL, apply migrations, and verify deep health ([updating](docs/manual/02-install.md#updating)).
- **One stack per machine:** Docker and Homebrew keep separate data ([installation choices](docs/install/CHOOSING_INSTALLATION.md)). Because Compose container names are fixed, run `docker compose down` (without `-v`) in an earlier checkout before starting another, or use the [managed launcher](docs/manual/02-install.md#managed-checkout-alternative) (`scripts/install/launch.sh`).

**Connect agents and models** ([Integrating agents](docs/manual/04-integrating-agents.md)):
- **Claude Code, Codex, and ChatGPT:** [`unitares-governance-plugin`](https://github.com/cirwel/unitares-governance-plugin) wires MCP, skills, lifecycle hooks, and per-file edit leases for Claude Code and Codex (short-lived file locks on the coordination plane that prevent concurrent edits in one checkout from overwriting each other), and builds a hook-free portable skill package for ChatGPT ([ChatGPT package guide](https://github.com/cirwel/unitares-governance-plugin/blob/master/docs/chatgpt.md)).
- **Hermes Agent and OpenAI-compatible clients:** [`unitares-host-adapter`](https://github.com/cirwel/unitares-host-adapter).
- **Other MCP clients, hosted connectors, and Python agents:** [Streamable HTTP MCP](docs/integration/MCP_CLIENTS.md) (`npx mcp-remote` for stdio-only clients; Bearer or OAuth on a dedicated listener for hosted connectors such as Claude.ai and ChatGPT), the [Python SDK](agents/sdk/README.md) (`pip install unitares-sdk`), or the [resident agent runtime](https://github.com/cirwel/unitares-resident).
- **Optional server-side models:** the core record runs without model APIs or keys. Run `./scripts/unitares model` to connect a local OpenAI-compatible server (Ollama, vLLM, LM Studio, or llama.cpp) for `consult` and in-process review ([choose a model](docs/manual/02-install.md#choose-a-model-optional)), or enable the [agent orchestrator](docs/manual/02-install.md#24-quickstart-defaults-and-a-full-setup) for cross-family CLI review.

## Who it is for

UNITARES is built for operators running long-lived AI agents across vendors on infrastructure they control, where work spans sessions, repositories, or runtimes. It works alongside Git, issue trackers, handoff notes, evals, and guardrails: each agent keeps its own loop and model, while UNITARES ties their identities, claims, reviews, and outcomes into one queryable record.

## How it fits together

UNITARES is self-hosted accountability infrastructure for operators running
multiple AI agents. Its single-operator federation kernel connects independent
runtimes to one operator-controlled server over MCP or HTTP, where they share a
durable record while keeping their own models, tools, and runtimes. They
interoperate with each other over their own transports or A2A; UNITARES is the
record behind them, not the transport between them.

Together this forms an operator-owned accountability layer for coding, research,
background, and resident agents. Beyond an activity log, it preserves lineage
and supersession: claims and evidence, reviews, and outcomes stay bound to the
process that recorded them, and a superseded claim links forward to its
replacement for later review and reconstruction.

Storage uses PostgreSQL (with Apache AGE for supersession graph edges and
pgvector for embeddings) and Redis, with no required external model API.
Identity, authorization, archive, and coordination boundaries are enforced by
the server. On check-ins, behavioral state estimation provides runtime [proprioception](docs/ontology/eisv-proprioception-contract.md); its policy recommendation is retained in the record and is advisory by default, with optional hard-hold enforcement.

Empirical evaluations, operational measurements, and scope live in
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

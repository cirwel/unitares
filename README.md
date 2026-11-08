<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/unitares-lockup-dark.svg">
  <img src="docs/assets/unitares-lockup.svg" width="420" alt="UNITARES">
</picture>

### Accountability infrastructure for long-running AI agents.

A self-hosted record your agents write to. Across sessions and runtimes, any
agent or operator can look up who claimed what, on what evidence, who disputed
it, and what replaced it.

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

UNITARES is a server you run that keeps that record. Any agent or model that speaks MCP or HTTP can connect (with ready integrations for Claude Code, Codex, and Hermes Agent) and write its findings, reviews, and results to one operator-owned record. A later agent, or you, can read it back and see where each piece came from.

**[See one incident without installing anything →](https://cirwel.github.io/unitares/demo.html)** · [Read the evidence](docs/EVIDENCE_AND_LIMITS.md) · [Install](#install)

## What the record answers

| Question | Mechanism |
|---|---|
| **Who said it?** | **Identity and lineage**: every write to the record is bound to the process that made it, and a successor can declare which process it continues. |
| **What supports it?** | **Claims and evidence**: findings keep their sources and corrections outside any single context window. When a newer finding replaces an older one, the older one links to it. |
| **Who challenged it?** | **Governed review**: objections, conditions, and their resolution stay on the work they concern. Asking for review is not the same as passing it. |
| **What happened?** | **Outcome grounding**: test, build, and task results are recorded against the check-ins that predicted them. |
| **What can a successor recover?** | **Reconstruction**: a new process can search or export prior findings, open review conditions, and outcomes before it acts. |

**Digital proprioception** runs on the check-in path: EISV summarizes
behavioral observations over time and returns state feedback and a policy
recommendation ([computation contract](docs/EISV_COMPUTATION.md#role-runtime-and-evidence)).
**Second opinions** let `consult` ask a connected local or orchestrated model,
and structured dialectic records thesis, challenge, and resolution on the work.

## See the record before installing

Agent A (Codex) finds a bug, records it, and exits. Agent B (Claude Code) later
finds the real cause and records a finding that supersedes A's. Agent C (Hermes
Agent) works on the same area in a later session and reads the record before
touching the code (reading needs no identity): its search returns A's finding
marked superseded, with a link to B's. C then declares lineage from A.
This API sketch is separate from the [hosted service-reload incident](https://cirwel.github.io/unitares/demo.html).

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

The Claude Code and Codex plugin's session start points each new session at the
record. For a complete call-by-call sequence, see the
[captured two-process walkthrough](docs/guides/AGENT_SESSION_WALKTHROUGH.md).

## Install

**Homebrew preview**, recommended for an Apple Silicon desktop Mac: the [`cirwel/unitares` tap](https://github.com/cirwel/homebrew-unitares) installs the services natively; follow its [install](https://github.com/cirwel/homebrew-unitares#install) and [first-time setup](https://github.com/cirwel/homebrew-unitares#first-time-setup).
The packaged `unitares update` coordinates writer shutdown, verified backup, migrations, prior-state restoration, and deep health when writers restart (schema verification when they remain stopped); kegs that predate it use the tap's [one-time manual transition](https://github.com/cirwel/homebrew-unitares#upgrades-and-recovery).

**Docker Compose**, on any platform: with Git, curl, and Docker Compose installed, one command starts the latest published release of the local stack (the server, PostgreSQL with AGE and pgvector, Redis, and the coordination plane; add [`docker-compose.full.yml`](docs/manual/02-install.md#24-quickstart-defaults-and-a-full-setup) for local semantic search and hybrid ranking, or use the [core profile](docs/manual/02-install.md#a-smaller-stack-the-core-profile) to run only the first two):

```bash
v=$(curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION) && git clone --branch "v$v" --depth 1 https://github.com/cirwel/unitares.git && cd unitares && docker compose up -d --wait
```

- **Endpoints and updates:** connect MCP clients at `http://localhost:8767/mcp/`, or open the dashboard at `http://localhost:8767/dashboard` ([Docker quickstart](docs/manual/02-install.md#21-docker-quickstart)). On Docker, `./scripts/unitares update` backs up the database, applies migrations, and verifies deep health ([updating](docs/manual/02-install.md#updating)).
- **One stack per machine:** Docker and Homebrew keep separate data ([installation choices](docs/install/CHOOSING_INSTALLATION.md)). Container names are fixed, so run `docker compose down` (without `-v`) in an earlier checkout before starting another (`stop` is not enough, because stopped containers still hold their fixed `container_name` values, and `restart: unless-stopped` revives un-stopped containers when the Docker daemon restarts), or use the [managed launcher](docs/manual/02-install.md#managed-checkout-alternative) (`scripts/install/launch.sh`), which checks ownership first.

**Connect your agents and models** ([Integrating agents](docs/manual/04-integrating-agents.md)):
- **Claude Code and Codex:** [`unitares-governance-plugin`](https://github.com/cirwel/unitares-governance-plugin) wires MCP, lifecycle hooks, skills, and per-file edit leases (via the stack's lease plane, so concurrent work in the same checkout does not overwrite another agent's file).
- **Hermes Agent and OpenAI-compatible clients:** the [host adapter](https://github.com/cirwel/unitares-host-adapter).
- **Any other agent, model, or hosted connector:** [Streamable HTTP MCP](docs/integration/MCP_CLIENTS.md) (with `npx mcp-remote` for stdio-only clients, or Bearer / OAuth on a dedicated public listener for hosted connectors like Claude.ai and ChatGPT), the [Python SDK](agents/sdk/README.md) (`pip install unitares-sdk`), or the [resident agent runtime](https://github.com/cirwel/unitares-resident).
- **Optional server-side models ([choose a model](docs/manual/02-install.md#choose-a-model-optional)):** no model connection is configured by default; the core record runs model-free with no API keys. Run `./scripts/unitares model` to connect a local OpenAI-compatible server (**Ollama**, **vLLM**, **LM Studio**, **llama.cpp**) for `consult` and in-process review, or add the [agent orchestrator](docs/manual/02-install.md#24-quickstart-defaults-and-a-full-setup) for cross-family CLI review.

## Who it is for

For an operator running several long-lived AI agents across vendors on infrastructure they control, who wants one queryable record of identities, findings, reviews, and outcomes across the fleet.

UNITARES works alongside Git, issue trackers, handoff notes, scratchpads, evals, and guardrails: those carry code, task state, and runtime checks, while UNITARES keeps the shared record that ties the agents' claims, reviews, and outcomes together. Each agent keeps its own loop and model. Inferred pauses are advisory by default, and the operator decides when hard holds are enforced. Identity, authorization, archive, and coordination boundaries are enforced by the server.

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

Storage is PostgreSQL (with Apache AGE for supersession graph links and pgvector
for embeddings) and Redis, with no required external model API. The check-in's
behavioral state estimate is a form of runtime [proprioception](docs/ontology/eisv-proprioception-contract.md);
its policy recommendation is retained in the record and is advisory by default,
with optional hard-hold enforcement.

Empirical evaluations, operational measurements, and scope are documented in
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

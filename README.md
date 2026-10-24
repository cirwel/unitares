<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/unitares-lockup-dark.svg">
  <img src="docs/assets/unitares-lockup.svg" width="420" alt="UNITARES">
</picture>

### Accountability infrastructure for long-running AI agents.

Give every process an identity. Keep claims, evidence, reviews, and outcomes
connected. Recover work across restarts, context loss, and handoffs.

[![Tests](https://github.com/cirwel/unitares/actions/workflows/tests.yml/badge.svg)](https://github.com/cirwel/unitares/actions/workflows/tests.yml)
[![Python](https://img.shields.io/badge/python-3.14+-5C544A?style=flat-square&labelColor=1A1612)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache_2.0-5C544A?style=flat-square&labelColor=1A1612)](LICENSE)
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.19647159-7A1F1F?style=flat-square&labelColor=1A1612)](https://doi.org/10.5281/zenodo.19647159) [![Glama score](https://glama.ai/mcp/servers/cirwel/unitares/badges/score.svg)](https://glama.ai/mcp/servers/cirwel/unitares)

</div>

UNITARES is self-hosted accountability infrastructure for operators running
multiple AI agents. Its single-operator federation kernel connects independent
runtimes to one operator-controlled server over MCP or HTTP, where they share a
durable record while keeping their own models, tools, and runtimes. They
interoperate with each other over their own transports or A2A; UNITARES is the
record behind them, not the transport between them.

Across Claude Code, Codex, Hermes Agent, and custom MCP or REST clients, agent
work remains attributable, reviewable, and recoverable even when the process
that started it is gone.

## What UNITARES gives you

- **Identity and lineage** — bind every write to the process that made it and
  record where inherited work came from. Process identity is an attribution
  record, not a credential; platform workload identity remains the credential
  layer.
- **Claims and evidence** — retain durable findings, corrections, and provenance
  outside any single context window, distinct from transient check-in text.
- **Shared memory as a graph** — with the AGE backend, each finding links to the
  process that wrote it, to findings that share its tags, and to any newer
  finding that supersedes it, so a stale result carries its replacement's id.
  Search is semantic on `age` and full-text on `postgres`; [hybrid and reranked
  search](docs/manual/03-running-the-server.md#search-and-retrieval-flags) are opt-in.
- **Governed review** — preserve objections, conditions, and resolution on the
  work record rather than treating a review request as approval.
- **Cross-vendor second opinions** — ask another connected model (Claude, Codex,
  Antigravity, local Ollama) for advisory `consult` feedback or on-record
  review, and keep the exchange attached to the task.
- **Outcome grounding** — connect bounded test, build, or task results back to
  earlier check-ins and predictions.
- **Runtime policy** — return a checkpoint action (`proceed`, `guide`, or
  `pause`), reason, and next step to gate governed writes when review or
  recovery is needed.
- **Reconstruction** — let a successor process query prior findings, active
  review conditions, and outcomes before continuing earlier work.

Together, these form an operator-owned accountability layer across coding,
research, background, and resident agents. What it adds to an activity log is
adjudication: disagreement, conditions, and outcomes remain bound to the process
that made the claim. The [claim ledger](docs/EVIDENCE_AND_LIMITS.md) gives the
evidence status of each measured result.

## How it works

When a task spans a process restart or handoff, the shared record connects the
work across both processes (full trace in the
[captured two-process walkthrough](docs/guides/AGENT_SESSION_WALKTHROUGH.md)):

```text
# Process A — bind identity, record finding, check in, log outcome, close, and exit
start_session        {"force_new": true, "name": "upload-fixer"}        -> agent_uuid_A, client_session_id_A
store_finding        {"client_session_id": ..., "discovery_type": "bug_found", "summary": "..."} -> discovery_id
sync_state           {"client_session_id": ..., "complexity": 0.4, "confidence": 0.7} -> action: "proceed", prediction_id
record_result        {"client_session_id": ..., "outcome_type": "test_passed", "prediction_id": ...}
use_tool             {"tool_name": "update_finding", "arguments": {"discovery_id": ..., "status": "resolved", "closure_class": "fix_verified", ...}}
use_tool             {"tool_name": "agent", "arguments": {"action": "release_presence", "client_session_id": ...}}

# Process B (after restart) — recover A's record before acting, then declare lineage
search_shared_memory {"query": "flaky upload retry test"}               -> A's resolved finding and agent_uuid_A
start_session        {"force_new": true, "parent_agent_id": agent_uuid_A, "spawn_reason": "explicit"}
```

Alongside findings and outcomes, agents can request structured review
(`request_review`) or cross-vendor advice (`consult`). The server runs beside
evals, sandboxes, and guardrails without taking over the agent's execution loop.
Core storage is self-hosted on PostgreSQL (with AGE and pgvector) and Redis with
no required external model API. Each `sync_state` check-in returns a policy
action derived from an EISV behavioral state estimate, a form of
runtime [proprioception](docs/ontology/eisv-proprioception-contract.md); that
estimator is moving out of the core record into an optional policy module.

## Install

**Apple Silicon Mac:** use the published [Homebrew preview](https://github.com/cirwel/homebrew-unitares) for the server, PostgreSQL with AGE/pgvector, Redis and the lease plane, without Docker.

```sh
brew update && brew tap cirwel/unitares && brew trust cirwel/unitares
brew install cirwel/unitares/unitares && brew pin unitares unitares-lease-plane
```

The published tap includes native setup/service CLI backports; its package revision differs from the server's upstream version.

Complete the tap's [first-time database and configuration steps](https://github.com/cirwel/homebrew-unitares#first-time-setup), then run `unitares start`. Database provisioning and backed-up upgrades remain explicit preview steps; package installation alone does not make the server ready.

**Docker Compose:** for container deployments and other platforms, Git, curl and Docker Compose start the latest verified full stack. An opt-in [core profile](docs/manual/02-install.md#a-smaller-stack-the-core-profile) runs only the server and database:

```bash
v=$(curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION) && git clone --branch "v$v" --depth 1 https://github.com/cirwel/unitares.git && cd unitares && docker compose up -d --wait
```

Both paths serve MCP at `http://localhost:8767/mcp/` and the dashboard at `http://localhost:8767/dashboard`. Choose one primary installation; see [installation choices](docs/install/CHOOSING_INSTALLATION.md) and [Docker ports and setup](docs/manual/02-install.md#21-docker-quickstart).

**Connect your agents** (see [Integrating agents](docs/manual/04-integrating-agents.md)):

- **Claude Code & Codex:** install the [governance plugin](https://github.com/cirwel/unitares-governance-plugin) for MCP wiring, lifecycle hooks, and surface leases.
- **Hermes Agent & OpenAI-compatible clients:** install the [host adapter](https://github.com/cirwel/unitares-host-adapter).
- **Custom runtimes & scripts:** connect directly over [Streamable HTTP MCP](docs/integration/MCP_CLIENTS.md), install the [Python SDK](agents/sdk/README.md) (`pip install unitares-sdk`), or use the [resident agent runtime](https://github.com/cirwel/unitares-resident).

**Data, models, and updates:**

- **Data persistence:** Homebrew uses the configured local PostgreSQL database; Docker uses named volumes. Installing another path does not migrate data or switch clients. See [installation choices](docs/install/CHOOSING_INSTALLATION.md).
- **Models are optional:** no model connection is configured by default. Ollama and other endpoints are explicit opt-ins; see [Choose a model](docs/manual/02-install.md#choose-a-model-optional).
- **Upgrades:** Homebrew uses the tap's [stop/backup/migrate/restart procedure](https://github.com/cirwel/homebrew-unitares#upgrades-and-recovery). `./scripts/unitares update` manages Docker Compose only; see [Updating](docs/manual/02-install.md#updating).

## Where it is going

UNITARES is working toward bringing a heterogeneous fleet under accountable
operation in one step: identities are configured, handoffs are enforceable,
important evidence survives context loss, reviews bind to the work they govern,
and outcomes are recorded where the next decision can use them—so agent systems
can accumulate experience without losing authorship, challenge, or operational
control.

## Start here

| Goal | Guide |
|---|---|
| Operate a deployment | [Operator manual](docs/manual/README.md) |
| Connect an agent or application | [Captured two-process walkthrough](docs/guides/AGENT_SESSION_WALKTHROUGH.md) · [MCP integration](docs/integration/MCP_CLIENTS.md) · [Python SDK](agents/sdk/README.md) |
| Understand the product and architecture | [Product definition](docs/PRODUCT_DEFINITION.md) · [Architecture](docs/UNIFIED_ARCHITECTURE.md) |
| Evaluate the claims | [Evidence and limits](docs/EVIDENCE_AND_LIMITS.md) · [Reviewer Guide](docs/REVIEWER_GUIDE.md) · [Public dataset](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories) |
| Contribute | [Contributing](.github/CONTRIBUTING.md) · [Development guide](AGENTS.md) |
| Everything else | [Documentation index](docs/README.md): deployment profiles, operations, security, compatibility, research, and the full tool surface |

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

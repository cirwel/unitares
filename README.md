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
[![DOI](https://img.shields.io/badge/DOI-10.5281%2Fzenodo.19647159-7A1F1F?style=flat-square&labelColor=1A1612)](https://doi.org/10.5281/zenodo.19647159)

</div>

An agent reports that its fix is done and the tests pass. By morning its
session has restarted, its context is gone, and another process has taken over
the task. Who said it? What supports it? Who challenged it? What happened? Each
run leaves its own log, and the answers scatter across them.

UNITARES is self-hosted accountability infrastructure for operators running
multiple AI agents. Its federation kernel connects independent runtimes to one
operator-controlled server over MCP or HTTP, where they share a durable record
while keeping their own models, tools, and runtimes. They interoperate with
each other over their own transports or A2A; UNITARES is the record behind
them, not the transport between them.

Agent work should remain attributable, reviewable, and
recoverable even when the process that started it is gone.

## What UNITARES gives you

- **Identity and lineage** — know which process acted and where inherited work
  came from. This is a record for attribution, not a credential; platform
  workload identity remains the credential layer.
- **Claims and evidence** — retain important findings, corrections, and their
  provenance outside any one context window.
- **Governed review** — preserve disagreement, conditions, and resolution as
  part of the work record.
- **Outcome grounding** — connect predictions and check-ins to what later
  happened.
- **Runtime policy** — return an action, reason, and next step at meaningful
  checkpoints in an agent's loop.
- **Reconstruction** — give a successor the records needed to understand and
  continue earlier work.

These are mechanisms that run and leave records, which is a different claim from
improving outcomes. The frozen outcome-lift read did not establish predictive
lift — inconclusive rather than ruled out — and prevention or improvement from
pausing remains untested. The [claim ledger](docs/EVIDENCE_AND_LIMITS.md) marks
which side of that line each capability sits on.

Together, these form an operator-owned accountability layer across coding
agents, research agents, background agents, and custom runtimes. What it adds
to a record of what happened is adjudication: disagreement, conditions, and
outcomes bound to the process that made the claim.

## Install

With Git, curl, and Docker Compose installed, one command starts the latest
verified release of the local operator stack:

```bash
v=$(curl -fsSL https://raw.githubusercontent.com/cirwel/unitares/master/PUBLISHED_VERSION) && git clone --branch "v$v" --depth 1 https://github.com/cirwel/unitares.git && cd unitares && docker compose up -d --wait
```

Connect MCP clients at `http://localhost:8767/mcp/` or open the dashboard at
`http://localhost:8767/dashboard`.

This provisions the server, PostgreSQL with AGE and pgvector, Redis, and the
coordination plane.

## How it works

An agent joins the operator's UNITARES deployment and receives a process
identity. During work it can publish selected findings and evidence, request
structured review, report meaningful state transitions, and record outcomes.
UNITARES keeps those records available to the operator and to later authorized
processes.

The server runs alongside evals, sandboxes, and guardrails. It provides the
continuity and accountability layer that connects their outputs over time.
Core storage is self-hosted and runs on its own; the operator chooses which
inference providers and integrations to connect.

Its EISV state model is runtime [proprioception](docs/ontology/eisv-proprioception-contract.md):
a way to make changes in an agent process visible so operators can diagnose and
act on them with evidence.

## Where it is going

UNITARES is working toward an operator experience where a fleet can be brought
under accountable operation in one step: identities are configured, handoffs
are enforceable, important evidence survives, reviews bind to the work they
govern, and outcomes are recorded where the next decision can use them.

The larger aim is infrastructure for agent systems that can accumulate useful
experience without losing authorship, challenge, or operational control as they
grow.

## Start here

| Goal | Guide |
|---|---|
| Operate a deployment | [Operator manual](docs/manual/README.md) |
| Connect an agent or application | [MCP integration](docs/integration/MCP_CLIENTS.md) · [Python SDK](agents/sdk/README.md) |
| Understand the product and architecture | [Product definition](docs/PRODUCT_DEFINITION.md) · [Architecture](docs/UNIFIED_ARCHITECTURE.md) |
| Evaluate the claims | [Evidence and limits](docs/EVIDENCE_AND_LIMITS.md) · [Reviewer Guide](docs/REVIEWER_GUIDE.md) · [Public dataset](https://huggingface.co/datasets/hikewa/unitares-eisv-trajectories) |
| Contribute | [Contributing](.github/CONTRIBUTING.md) · [Development guide](AGENTS.md) |

The [documentation index](docs/README.md) covers deployment profiles,
operations, security, compatibility, research, and the full tool surface.

## Ecosystem

UNITARES works with the
[governance plugin](https://github.com/cirwel/unitares-governance-plugin) for
Codex and Claude Code, the public [Python SDK](agents/sdk/README.md), and the
[resident agent runtime](https://github.com/cirwel/unitares-resident). These are
separate userlands connected by the same operator-owned record.

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

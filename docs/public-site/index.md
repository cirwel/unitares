# Accountability infrastructure for long-running AI agents

*Many agents, one record.*

<div class="hero-actions" role="group" aria-label="Get started">
  <a class="primary" href="demo.html">See it without installing</a>
  <a class="secondary" href="#is-it-for-you">Is it for you?</a>
  <a class="secondary" href="#evaluate-the-project">Evaluate the evidence</a>
</div>

UNITARES is self-hosted accountability infrastructure for operators running
multiple AI agents. Its single-operator federation kernel connects independent
runtimes to one operator-controlled server over MCP or HTTP, where they share a
durable record while keeping their own models, tools, and runtimes. Agent
work remains attributable, reviewable, and recoverable even when the
process that started it is gone. It runs beside evals, guardrails, and
sandboxes and replaces none of them.

Vendor platforms keep agent identity, memory, and review inside their own cloud
or tool. UNITARES keeps one attributed record across them, on infrastructure you
control: Claude Code, Codex, Hermes Agent, and any MCP-capable agent record
findings, advice, and review decisions that stay bound to the work across
handoffs, and a question can be routed to another vendor's model for a second
opinion.

**Current public releases:** [server v3.6.0](https://github.com/cirwel/unitares/releases/tag/v3.6.0)
· [Python SDK 0.6.1](https://pypi.org/project/unitares-sdk/0.6.1/)
· [multi-architecture container](https://github.com/cirwel/unitares/pkgs/container/unitares)
· Apache-2.0

## The operating problem

Agent work fragments when a process restarts, loses context, changes vendors,
or hands a task to another runtime. Git preserves code changes, issue trackers
preserve pull-request and issue threads, and handoff notes summarize a single
session—while UNITARES gives every connected agent one queryable account of who
made a claim, what evidence supported or contradicted it, who challenged it,
what replaced it, and what outcome followed, outside any one model's context
window.

## Is it for you?

**Built for operators running multiple or long-lived AI agents** across sessions,
repositories, or vendors who want one operator-owned record of authorship,
evidence, corrections, review, and outcomes that survives restarts and handoffs.

UNITARES works alongside Git, GitHub issues, and session handoff notes rather
than replacing them: findings can cite commits, PRs, test logs, or handoff docs
while keeping claims attributed and linking superseded conclusions to their
replacements. Start with the [no-install incident walkthrough](demo.html) to
see how a successor process reconstructs an incident from the record.

## What operators get

| Surface | Operational value |
|---|---|
| Accountable identity | Bind writes to a process instance and retain lineage across explicit handoffs. |
| Claims and evidence | Retain check-in claims beside tests, exit codes, tool results, review labels, and recorded outcomes. |
| Corrections and review | Keep objections with the work they concern, and link a replaced claim to its replacement instead of silently rewriting history. |
| Outcome grounding | Record test, build, and task results against the check-ins that predicted them. |
| Reconstruction | Let a later process search the record and recover the chain without sharing the earlier process's context window. |

Tool-boundary enforcement, trace emission, and agent-to-agent transport are not
this server's job: runtime middleware, OpenTelemetry, and A2A own those.
UNITARES keeps the record beside them.

Two optional instruments sit around the record. Digital proprioception returns
an EISV state estimate and retains its raw policy recommendation for inspection;
the recommendation is observational by default and cannot pause an identity or
block writes unless the operator explicitly enables the legacy actuator.
`consult` can ask a connected model for a second opinion, and structured
dialectic is available for durable review. Neither is required to use the
record.

## Run the released server

Inspect the data model in the [hosted incident walkthrough](demo.html), or
stand up the released stack on your own machine.

On an **Apple Silicon Mac**, install the native preview with Homebrew (server,
coordination lease plane, PostgreSQL 17 with AGE/pgvector, and Redis — no Docker
required), then follow the
[Homebrew setup guide](https://github.com/cirwel/unitares/blob/master/docs/install/HOMEBREW_SETUP.md):

```bash
brew update
brew tap cirwel/unitares
brew trust cirwel/unitares
brew install cirwel/unitares/unitares
brew pin unitares unitares-lease-plane apache-age unitares-pgvector
```

Or run the **Docker Compose** stack on macOS or Linux:

```bash
git clone --branch v3.6.0 --depth 1 https://github.com/cirwel/unitares.git
cd unitares
docker compose up -d --wait
```

Connect an MCP client at `http://localhost:8767/mcp/`, open the dashboard at
`http://localhost:8767/dashboard`, or follow the
[captured two-process walkthrough](https://github.com/cirwel/unitares/blob/master/docs/guides/AGENT_SESSION_WALKTHROUGH.md).

To include in-container `sentence-transformers` (`BAAI/bge-m3`) for semantic and
hybrid knowledge-graph search, layer `docker-compose.full.yml` (or use
`docker-compose.core.yml` for the minimal server + PostgreSQL profile; see the
[installation manual](https://github.com/cirwel/unitares/blob/master/docs/manual/02-install.md#24-quickstart-defaults-and-a-full-setup)):

```bash
docker compose -f docker-compose.yml -f docker-compose.full.yml up -d --wait --build
```

Install the resident-agent SDK from PyPI:

```bash
python -m pip install unitares-sdk==0.6.1
```

Or inspect the signed multi-architecture server image:

```bash
docker pull ghcr.io/cirwel/unitares:v3.6.0
```

## Evaluate the project

| Question | Evidence path |
|---|---|
| How do I install, configure, and run the server? | [User Manual](https://github.com/cirwel/unitares/blob/master/docs/manual/README.md) · [Installation guide](https://github.com/cirwel/unitares/blob/master/docs/manual/02-install.md) |
| How do I connect my MCP client or agent harness? | [Integrating agents](https://github.com/cirwel/unitares/blob/master/docs/manual/04-integrating-agents.md) · [MCP client matrix](https://github.com/cirwel/unitares/blob/master/docs/integration/MCP_CLIENTS.md) |
| What is live, proposed, or falsifiable? | [Reviewer Guide](https://github.com/cirwel/unitares/blob/master/docs/REVIEWER_GUIDE.md) |
| What does the runtime compute? | [Computation reference](https://github.com/cirwel/unitares/blob/master/docs/EISV_COMPUTATION.md) |
| What are the threat model and blind spots? | [Scope and threat model](https://github.com/cirwel/unitares/blob/master/docs/SCOPE_AND_THREAT_MODEL.md) |
| What evidence can be regenerated? | [Evaluation catalog](https://github.com/cirwel/unitares/blob/master/docs/EVALUATION_INDEX.md) |
| How is the system operated and released? | [Operations docs](https://github.com/cirwel/unitares/tree/master/docs/operations) |
| What remains on the roadmap? | [Roadmap](https://github.com/cirwel/unitares/blob/master/docs/ROADMAP.md) |

## Evidence

The [evidence ledger](https://github.com/cirwel/unitares/blob/master/docs/EVIDENCE_AND_LIMITS.md)
lists measured results with their evidence status: what has run, what was
measured, and what is still open, with the data behind each.

## Cross-operator trust

Today one operator runs a deployment, and many independent runtimes share its
record. Trust between operators is the research direction. The architecture
already exposes versioned telemetry, provenance, identity, and named policy
decisions, so attestations between deployments can be tested without
centralizing raw telemetry.

## Where to go next

Read the [incident walkthrough](demo.html). To assess the boundary between
deployed mechanisms and research claims, start with the
[Reviewer Guide](https://github.com/cirwel/unitares/blob/master/docs/REVIEWER_GUIDE.md).
For integration or pilot questions, [start a GitHub Discussion](https://github.com/cirwel/unitares/discussions).

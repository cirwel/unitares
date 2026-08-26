# UNITARES Documentation

This is the documentation tree for the
[UNITARES MCP-native accountability and coordination layer](../README.md). Start with the repo README for
the core idea and quickstart, then choose the path below. Canonical references,
operator runbooks, research provenance, and optional essays are kept separate so
an analogy or proposal is not mistaken for a deployed contract.

## Reader's path

| You are… | Read in this order |
|---|---|
| **A reviewer / first-time visitor** | [repo README](../README.md) → [`PRODUCT_DEFINITION.md`](PRODUCT_DEFINITION.md) → [`REVIEWER_GUIDE.md`](REVIEWER_GUIDE.md) → [`EISV_COMPUTATION.md`](EISV_COMPUTATION.md) → [`SCOPE_AND_THREAT_MODEL.md`](SCOPE_AND_THREAT_MODEL.md) → [`PRODUCTION_SNAPSHOT.md`](PRODUCTION_SNAPSHOT.md) |
| **Integrating an MCP client** | [`manual/04-integrating-agents.md`](manual/04-integrating-agents.md) → [`integration/MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) → [`guides/TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) |
| **Installing / deploying** | [`manual/02-install.md`](manual/02-install.md) → [`install/PLAYBOOK.md`](install/PLAYBOOK.md) → [`operations/OPERATOR_RUNBOOK.md`](operations/OPERATOR_RUNBOOK.md) |
| **Contributing to the identity layer** | [`../AGENTS.md`](../AGENTS.md) → [`ontology/README.md`](ontology/README.md) → [`ontology/identity.md`](ontology/identity.md) → [`ontology/plan.md`](ontology/plan.md) |
| **Reading research history** | [`EVALUATION_INDEX.md`](EVALUATION_INDEX.md) → [`ontology/README.md`](ontology/README.md) → [`proposals/README.md`](proposals/README.md) |

Project-level status and participation live at the repository root:
[roadmap](../ROADMAP.md), [compatibility](../COMPATIBILITY.md),
[governance](../GOVERNANCE.md), [support](../SUPPORT.md), and
[contributing](../CONTRIBUTING.md).

## Reader-facing documentation

### `manual/` — the user manual

A cohesive, multi-chapter front door for operators and integrators. Thin chapters that stitch the canonical docs below into one walkthrough; the deep references stay canonical.

→ Start at **[`manual/README.md`](manual/README.md)** (overview · install · running · integrating · reading the signals · operating · troubleshooting).

### Canonical reference

- **[`PRODUCT_DEFINITION.md`](PRODUCT_DEFINITION.md)** — what UNITARES is, in plain language: the one-sentence product, the Core/userland boundary, MCP's place in the stack, the record/score/interrupt/remember loop, and the honest limits. Start here when the architecture nouns aren't landing.
- **[`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md)** — the canonical architecture doc. End-to-end picture of the server, state model, transports, and storage.
- **[`CANONICAL_COMPONENTS.md`](CANONICAL_COMPONENTS.md)** — component/layer map, orthogonal to the check-in pipeline view in `UNIFIED_ARCHITECTURE.md`.
- **[`REVIEWER_GUIDE.md`](REVIEWER_GUIDE.md)** — guided tour for reviewers evaluating the project.
- **[`SCOPE_AND_THREAT_MODEL.md`](SCOPE_AND_THREAT_MODEL.md)** — who this is for, what anchors the signal, and what gaming or robustness remains unproven.
- **[`PRODUCTION_SNAPSHOT.md`](PRODUCTION_SNAPSHOT.md)** — frozen live metrics and dashboard views.
- **[`trust-contract.md`](trust-contract.md)** — what the system guarantees, what it does not, and what honest failure looks like.
- **[`ontology/eisv-telemetry-envelope-v1.md`](ontology/eisv-telemetry-envelope-v1.md)** — versioned measurement → derivation → policy → enforcement provenance stored with each new state row.
- **[`CHANGELOG.md`](CHANGELOG.md)** — release history.

### Subsystem guides

Operating guidance for individual subsystems lives next to the code as Skills, not in `docs/` (this keeps the guide in sync with its `source_files:` and carries its own freshness budget). The map to those lives here:

- **Knowledge graph (KG)** — agent-facing operating manual: search-before-write discipline, the `knowledge()` actions, discovery types/statuses, tagging, and closing the loop → [`skills/knowledge-graph/SKILL.md`](../skills/knowledge-graph/SKILL.md)

### `guides/` — getting started

User- and integrator-facing how-tos. Thin by design — most architecture lives in `UNIFIED_ARCHITECTURE.md` and the repo README.

- [`START_HERE.md`](guides/START_HERE.md) — compatibility redirect to the current audience paths
- [`TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) — canonical symptom-and-recovery guide
- [`CIRS_PROTOCOL.md`](guides/CIRS_PROTOCOL.md) — multi-agent coordination protocol (specialized; not a general architecture overview)

### `install/` — installation

- [`PLAYBOOK.md`](install/PLAYBOOK.md) — bare-metal install playbook (Homebrew Postgres, native Python). Docker path is in the repo README. **Live reference** — keep this current.
- [`cross-machine-surface.md`](install/cross-machine-surface.md) — *point-in-time install-surface audit (2026-04-24), preserved as a record.* Inventory of machine-varying values; useful background for a cross-machine setup, but the install path itself is `PLAYBOOK.md`, not this.

### `integration/` — MCP and client wiring

- [`MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) — Streamable HTTP MCP endpoints, stdio bridges, and hosted/client-neutral setup

### `ontology/` — identity ontology

The system's versioned identity ontology, the resolution ledger, and the working RFCs that evolve them. Has its own reader's guide because the folder is dense. The question-keyed vocabulary glossary also lives here: [`ontology/glossary.md`](ontology/glossary.md), published with an interactive viewer at [cirwel.github.io/unitares](https://cirwel.github.io/unitares/glossary.html).

→ Start at **[`ontology/README.md`](ontology/README.md)**.

## Operator and contributor documentation

### `operations/` — operator-internal runbooks

How to run this in production. Most readers can skip these.

→ Start at **[`operations/README.md`](operations/README.md)**. The primary
documents are the [operator runbook](operations/OPERATOR_RUNBOOK.md),
[port registry](operations/DEFINITIVE_PORTS.md),
[database architecture](operations/database_architecture.md), and
[deployment-data caveat](operations/DEPLOYMENT_DATA_CAVEAT.md).

### `dev/` — developer-internal

For people working on UNITARES itself, not using it.

- [`CANONICAL_SOURCES.md`](dev/CANONICAL_SOURCES.md) — arch-dispute resolution
- [`DRIFT_LEDGER.md`](dev/DRIFT_LEDGER.md) — guard/seam index for drift prevention and known unguarded seams
- [`TOOL_REGISTRATION.md`](dev/TOOL_REGISTRATION.md) — how tools are wired into the MCP server
- [`TOOL_EDGE_INDEX.md`](dev/TOOL_EDGE_INDEX.md) — generated: every tool resolved to its handler, action delegates, and params schema
- [`CIRCUIT_BREAKER_DIALECTIC.md`](dev/CIRCUIT_BREAKER_DIALECTIC.md) — recovery semantics (specialized)

## Research, provenance, and optional interpretation

### `proposals/` — internal RFCs and decision history

These are research and engineering provenance, not a list of shipped features.
Active and resolved RFCs that do not belong in `ontology/` live here. Each doc
carries its own resolution status in the body.

→ Status-grouped index at **[`proposals/README.md`](proposals/README.md)**.

### `essays/` — optional, non-normative interpretation

Analogies and philosophical readings live here so they cannot be mistaken for
canonical architecture, evaluation evidence, or deployed semantics.

→ Start at **[`essays/README.md`](essays/README.md)**.

### `assets/`

Hero SVG and other rendered diagrams referenced from the README.

## Sibling repos

- The paper — [`unitares-paper-v6`](https://github.com/cirwel/unitares-paper-v6) (DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159))
- The plugin (governance-start, governance-lifecycle skills) — [`unitares-governance-plugin`](https://github.com/cirwel/unitares-governance-plugin)
- The Discord bridge — [`unitares-discord-bridge`](https://github.com/cirwel/unitares-discord-bridge)
- The Pi-side embodiment — [`anima-mcp`](https://github.com/cirwel/anima-mcp)

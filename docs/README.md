# UNITARES Documentation

This is the documentation tree for the [UNITARES governance MCP server](../README.md). If you're new to the project, **start with the [repo README](../README.md)** — it has the architecture summary, the `make demo` walkthrough, and the production snapshot. This page is the map for everything under `docs/`.

## Reader's path

| You are… | Read in this order |
|---|---|
| **A reviewer / first-time visitor** | [repo README](../README.md) → [`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md) → [`ontology/identity.md`](ontology/identity.md) → [`ontology/paper-positioning.md`](ontology/paper-positioning.md) |
| **Integrating an MCP client** | [`integration/MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) → [`guides/START_HERE.md`](guides/START_HERE.md) → [`guides/TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) |
| **Installing / deploying** | [`install/PLAYBOOK.md`](install/PLAYBOOK.md) → [`install/cross-machine-surface.md`](install/cross-machine-surface.md) → [`operations/OPERATOR_RUNBOOK.md`](operations/OPERATOR_RUNBOOK.md) |
| **Working on the identity layer** | [`../AGENTS.md`](../AGENTS.md) → [`ontology/README.md`](ontology/README.md) → [`ontology/identity.md`](ontology/identity.md) → [`ontology/plan.md`](ontology/plan.md) |

## Layout

### Canonical reference

- **[`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md)** — the canonical architecture doc. End-to-end picture of the server, state model, transports, and storage.
- **[`REVIEWER_GUIDE.md`](REVIEWER_GUIDE.md)** — guided tour for reviewers evaluating the project.
- **[`trust-contract.md`](trust-contract.md)** — what the system guarantees, what it does not, and what honest failure looks like.
- **[`CHANGELOG.md`](CHANGELOG.md)** — release history.

### `guides/` — getting started

User- and integrator-facing how-tos. Thin by design — most architecture lives in `UNIFIED_ARCHITECTURE.md` and the repo README.

- [`START_HERE.md`](guides/START_HERE.md) — workflow + canonical-sources pointer
- [`TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) — common failures
- [`CIRS_PROTOCOL.md`](guides/CIRS_PROTOCOL.md) — multi-agent coordination protocol (specialized; not a general architecture overview)

### `install/` — installation

- [`PLAYBOOK.md`](install/PLAYBOOK.md) — bare-metal install playbook (Homebrew Postgres, native Python). Docker path is in the repo README.
- [`cross-machine-surface.md`](install/cross-machine-surface.md) — multi-machine surface setup

### `integration/` — MCP and client wiring

- [`MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) — Streamable HTTP MCP endpoints and how to point Claude Code / other clients at them

### `ontology/` — identity ontology

The system's versioned identity ontology, the resolution ledger, and the working RFCs that evolve them. Has its own reader's guide because the folder is dense.

→ Start at **[`ontology/README.md`](ontology/README.md)**.

### `operations/` — operator-internal runbooks

How to run this in production. Most readers can skip these.

- [`OPERATOR_RUNBOOK.md`](operations/OPERATOR_RUNBOOK.md) — primary runbook
- [`DEFINITIVE_PORTS.md`](operations/DEFINITIVE_PORTS.md) — port assignments across services
- [`database_architecture.md`](operations/database_architecture.md) — single-Postgres / schema-isolation model
- [`lease-plane-operator-runbook.md`](operations/lease-plane-operator-runbook.md) — Elixir lease-plane operations
- [`branch-hygiene-runbook.md`](operations/branch-hygiene-runbook.md) — resident branch-hygiene sweep (`agents/vigil_hygiene`)
- [`DATA_NOTES.md`](operations/DATA_NOTES.md) — operational data dictionary for the production governance database
- [`DEPLOYMENT_DATA_CAVEAT.md`](operations/DEPLOYMENT_DATA_CAVEAT.md) — what the cited deployment numbers do and don't mean

### `dev/` — developer-internal

For people working on UNITARES itself, not using it.

- [`CANONICAL_SOURCES.md`](dev/CANONICAL_SOURCES.md) — arch-dispute resolution
- [`TOOL_REGISTRATION.md`](dev/TOOL_REGISTRATION.md) — how tools are wired into the MCP server
- [`CIRCUIT_BREAKER_DIALECTIC.md`](dev/CIRCUIT_BREAKER_DIALECTIC.md) — recovery semantics (specialized)

### `proposals/` — RFCs

Active and resolved RFCs that don't (yet) belong in `ontology/`. The Plexus / lease-plane / BEAM-coordination thread lives here. Each doc carries its own resolution status in the body.

→ Status-grouped index at **[`proposals/README.md`](proposals/README.md)**.

### `assets/`

Hero SVG and other rendered diagrams referenced from the README.

## Sibling repos

- The paper — [`unitares-paper-v6`](https://github.com/cirwel/unitares-paper-v6) (DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159))
- The plugin (governance-start, governance-lifecycle skills) — [`unitares-governance-plugin`](https://github.com/cirwel/unitares-governance-plugin)
- The Discord bridge — [`unitares-discord-bridge`](https://github.com/cirwel/unitares-discord-bridge)
- The Pi-side embodiment — [`anima-mcp`](https://github.com/cirwel/anima-mcp)

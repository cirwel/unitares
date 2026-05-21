# UNITARES Documentation

This is the documentation tree for the [UNITARES governance MCP server](../README.md). If you're new to the project, **start with the [repo README](../README.md)** — it has the architecture summary, the `make demo` walkthrough, and the production snapshot. This page is the map for everything under `docs/`.

## Reader's path

| You are… | Read in this order |
|---|---|
| **A reviewer / first-time visitor** | [repo README](../README.md) → [`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md) |
| **Integrating an MCP client** | [`integration/MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) → [`guides/START_HERE.md`](guides/START_HERE.md) → [`guides/TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) |
| **Installing / deploying** | [`install/PLAYBOOK.md`](install/PLAYBOOK.md) → [`install/cross-machine-surface.md`](install/cross-machine-surface.md) → [`operations/OPERATOR_RUNBOOK.md`](operations/OPERATOR_RUNBOOK.md) |
| **Working on the identity layer** | [`../AGENTS.md`](../AGENTS.md) (the shared agent contract) → `src/mcp_handlers/identity/` |

## Layout

### Canonical reference

- **[`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md)** — the canonical architecture doc. End-to-end picture of the server, state model, transports, and storage.
- **[`CHANGELOG.md`](CHANGELOG.md)** — release history.

### `guides/` — getting started

User- and integrator-facing how-tos. Thin by design — most architecture lives in `UNIFIED_ARCHITECTURE.md` and the repo README.

- [`START_HERE.md`](guides/START_HERE.md) — workflow + canonical-sources pointer
- [`TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) — common failures

### `install/` — installation

- [`PLAYBOOK.md`](install/PLAYBOOK.md) — bare-metal install playbook (Homebrew Postgres, native Python). Docker path is in the repo README.
- [`cross-machine-surface.md`](install/cross-machine-surface.md) — multi-machine surface setup

### `integration/` — MCP and client wiring

- [`MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) — Streamable HTTP MCP endpoints and how to point Claude Code / other clients at them

### `operations/` — operator-internal runbooks

How to run this in production. Most readers can skip these.

- [`OPERATOR_RUNBOOK.md`](operations/OPERATOR_RUNBOOK.md) — primary runbook
- [`DEFINITIVE_PORTS.md`](operations/DEFINITIVE_PORTS.md) — port assignments across services
- [`database_architecture.md`](operations/database_architecture.md) — single-Postgres / schema-isolation model
- [`lease-plane-operator-runbook.md`](operations/lease-plane-operator-runbook.md) — Elixir lease-plane operations

### `dev/` — developer-internal

For people working on UNITARES itself, not using it.

- [`CANONICAL_SOURCES.md`](dev/CANONICAL_SOURCES.md) — arch-dispute resolution
- [`TOOL_REGISTRATION.md`](dev/TOOL_REGISTRATION.md) — how tools are wired into the MCP server

### `assets/`

Hero SVG and other rendered diagrams referenced from the README.

## Sibling repos

- The paper — [`unitares-paper-v6`](https://github.com/cirwel/unitares-paper-v6) (DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159))
- The plugin (governance-start, governance-lifecycle skills) — [`unitares-governance-plugin`](https://github.com/cirwel/unitares-governance-plugin)
- The Discord bridge — [`unitares-discord-bridge`](https://github.com/cirwel/unitares-discord-bridge)
- The Pi-side embodiment — [`anima-mcp`](https://github.com/cirwel/anima-mcp)

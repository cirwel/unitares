# UNITARES Documentation

This is the documentation tree for the [UNITARES governance MCP server](../README.md). If you're new to the project, **start with the [repo README](../README.md)** — it has the architecture summary, the `make demo` walkthrough, and the production snapshot. This page is the map for everything under `docs/`.

## Reader's path

Depending on why you're here:

| You are… | Read in this order |
|---|---|
| **A reviewer / first-time visitor** | [repo README](../README.md) → [`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md) → [`ontology/identity.md`](ontology/identity.md) → [`ontology/paper-positioning.md`](ontology/paper-positioning.md) |
| **Integrating an MCP client** | [`integration/MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) → [`guides/START_HERE.md`](guides/START_HERE.md) → [`guides/TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) |
| **Installing / deploying** | [`install/PLAYBOOK.md`](install/PLAYBOOK.md) → [`install/cross-machine-surface.md`](install/cross-machine-surface.md) → [`operations/OPERATOR_RUNBOOK.md`](operations/OPERATOR_RUNBOOK.md) |
| **Trying to understand the identity model** | [`ontology/README.md`](ontology/README.md) → [`ontology/identity.md`](ontology/identity.md) → [`ontology/plan.md`](ontology/plan.md) |

## Layout

### Canonical reference

- **[`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md)** — the canonical architecture doc. End-to-end picture of the server, state model, transports, and storage.
- **[`CHANGELOG.md`](CHANGELOG.md)** — release history.
- **[`surface-taxonomy.md`](surface-taxonomy.md)** — editorial taxonomy of the system's surfaces (what agents read, what humans read, what operators control). Working draft, not contract.

### `guides/` — getting started

User- and integrator-facing how-tos. Thin by design — most architecture lives in `UNIFIED_ARCHITECTURE.md` and the repo README.

- [`START_HERE.md`](guides/START_HERE.md) — workflow + canonical-sources pointer
- [`TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) — common failures
- [`CIRS_PROTOCOL.md`](guides/CIRS_PROTOCOL.md) — Cooperative Inter-agent Resonance Signaling, a specialized coordination protocol

### `install/` — installation

- [`PLAYBOOK.md`](install/PLAYBOOK.md) — bare-metal install playbook (Homebrew Postgres, native Python). Docker path is in the repo README.
- [`cross-machine-surface.md`](install/cross-machine-surface.md) — multi-machine surface setup

### `integration/` — MCP and client wiring

- [`MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) — Streamable HTTP MCP endpoints and how to point Claude Code / other clients at them

### `ontology/` — identity ontology + research RFCs

The system's versioned identity ontology, the resolution ledger, and the working RFCs that evolve them. Has its own reader's guide because the folder is dense.

→ Start at **[`ontology/README.md`](ontology/README.md)**.

### `operations/` — operator-internal runbooks

How to run this in production. Most readers can skip these.

- [`OPERATOR_RUNBOOK.md`](operations/OPERATOR_RUNBOOK.md) — primary runbook
- [`DEFINITIVE_PORTS.md`](operations/DEFINITIVE_PORTS.md) — port assignments across services
- [`database_architecture.md`](operations/database_architecture.md) — single-Postgres / schema-isolation model
- [`lease-plane-operator-runbook.md`](operations/lease-plane-operator-runbook.md) — Elixir lease-plane operations
- `contract-drift-playbook.md`, `machine-rain-protocol.md`, `DATA_NOTES.md`, `DEPLOYMENT_DATA_CAVEAT.md` — narrower playbooks

### `dev/` — developer-internal

For people working on UNITARES itself, not using it.

- [`CANONICAL_SOURCES.md`](dev/CANONICAL_SOURCES.md) — arch-dispute resolution
- [`TOOL_REGISTRATION.md`](dev/TOOL_REGISTRATION.md) — how tools are wired into the MCP server
- [`CIRCUIT_BREAKER_DIALECTIC.md`](dev/CIRCUIT_BREAKER_DIALECTIC.md), [`validation-roadmap.md`](dev/validation-roadmap.md) — internal design notes

### `proposals/` — RFCs in flight

Active and resolved RFCs that don't (yet) belong in `ontology/`. The Plexus / lease-plane / BEAM-coordination thread lives here:

- `plexus-scope.md`, `surface-lease-plane-v0.md`, `surface-lease-plane-phase-a-plan.md`, `lease-plane-phase-a-latency-*.md`
- `beam-footprint-roadmap-v0.md`, `beam-wave-1-sentinel.md`, `beam-wave-3-handler-dispatch.md`, `wave-1-window-evaluation-*.md`

Plus a long tail of narrower RFCs (sync-fingerprint, attestation, audit/contract-drift, etc.). These are dated and resolution status is in the doc body.

### `handoffs/` — implementation handoffs

Time-stamped handoff documents from one implementation session to the next (R1, R2, anyio refactor, BEAM waves). Useful for archeology, not entry points.

### `assets/`

Hero SVG and other rendered diagrams referenced from the README.

## What's not here

- The paper — see [`unitares-paper-v6`](https://github.com/cirwel/unitares-paper-v6) (DOI [10.5281/zenodo.19647159](https://doi.org/10.5281/zenodo.19647159))
- The plugin (governance-start, governance-lifecycle skills, etc.) — see [`unitares-governance-plugin`](https://github.com/cirwel/unitares-governance-plugin)
- The Discord bridge — see [`unitares-discord-bridge`](https://github.com/cirwel/unitares-discord-bridge)
- The Pi-side embodiment — see [`anima-mcp`](https://github.com/cirwel/anima-mcp)

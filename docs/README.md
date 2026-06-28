# UNITARES Documentation

This is the documentation tree for the [UNITARES governance MCP server](../README.md). If you're new to the project, **start with the [repo README](../README.md)** — it has the core idea, the `make demo` walkthrough, and the integration loop. The deeper [scope & threat model](SCOPE_AND_THREAT_MODEL.md) and [production snapshot](PRODUCTION_SNAPSHOT.md) now live here under `docs/`. This page is the map for everything under `docs/`.

For a single, task-ordered walkthrough that stitches the docs below into one guide — install → run → integrate → read the signals → operate → troubleshoot — see the **[User Manual](manual/README.md)**.

## Reader's path

| You are… | Read in this order |
|---|---|
| **A reviewer / first-time visitor** | [repo README](../README.md) → [`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md) → [`ontology/identity.md`](ontology/identity.md) → [`ontology/paper-positioning.md`](ontology/paper-positioning.md) |
| **Integrating an MCP client** | [`manual/04-integrating-agents.md`](manual/04-integrating-agents.md) → [`integration/MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) → [`guides/TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) |
| **Installing / deploying** | [`manual/02-install.md`](manual/02-install.md) → [`install/PLAYBOOK.md`](install/PLAYBOOK.md) → [`operations/OPERATOR_RUNBOOK.md`](operations/OPERATOR_RUNBOOK.md) |
| **Working on the identity layer** | [`../AGENTS.md`](../AGENTS.md) → [`ontology/README.md`](ontology/README.md) → [`ontology/identity.md`](ontology/identity.md) → [`ontology/plan.md`](ontology/plan.md) |

## Layout

### `manual/` — the user manual

A cohesive, multi-chapter front door for operators and integrators. Thin chapters that stitch the canonical docs below into one walkthrough; the deep references stay canonical.

→ Start at **[`manual/README.md`](manual/README.md)** (overview · install · running · integrating · reading the signals · operating · troubleshooting).

### Canonical reference

- **[`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md)** — the canonical architecture doc. End-to-end picture of the server, state model, transports, and storage.
- **[`CANONICAL_COMPONENTS.md`](CANONICAL_COMPONENTS.md)** — component/layer map, orthogonal to the check-in pipeline view in `UNIFIED_ARCHITECTURE.md`.
- **[`REVIEWER_GUIDE.md`](REVIEWER_GUIDE.md)** — guided tour for reviewers evaluating the project.
- **[`SCOPE_AND_THREAT_MODEL.md`](SCOPE_AND_THREAT_MODEL.md)** — who this is for, why an agent can't game the signal, and what robustness is still unproven.
- **[`PRODUCTION_SNAPSHOT.md`](PRODUCTION_SNAPSHOT.md)** — frozen live metrics and dashboard views.
- **[`trust-contract.md`](trust-contract.md)** — what the system guarantees, what it does not, and what honest failure looks like.
- **[`tonality-metaphor.md`](tonality-metaphor.md)** — a teaching lens: how key signatures and chromaticism map onto EISV, coherence, and drift. Intuition for [`EISV_COMPUTATION.md`](EISV_COMPUTATION.md), not a spec.
- **[`CHANGELOG.md`](CHANGELOG.md)** — release history.

### Subsystem guides

Operating guidance for individual subsystems lives next to the code as Skills, not in `docs/` (this keeps the guide in sync with its `source_files:` and carries its own freshness budget). The map to those lives here:

- **Knowledge graph (KG)** — agent-facing operating manual: search-before-write discipline, the `knowledge()` actions, discovery types/statuses, tagging, and closing the loop → [`skills/knowledge-graph/SKILL.md`](../skills/knowledge-graph/SKILL.md)

### `guides/` — getting started

User- and integrator-facing how-tos. Thin by design — most architecture lives in `UNIFIED_ARCHITECTURE.md` and the repo README.

- [`START_HERE.md`](guides/START_HERE.md) — workflow + canonical-sources pointer
- [`TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) — common failures
- [`CIRS_PROTOCOL.md`](guides/CIRS_PROTOCOL.md) — multi-agent coordination protocol (specialized; not a general architecture overview)

### `install/` — installation

- [`PLAYBOOK.md`](install/PLAYBOOK.md) — bare-metal install playbook (Homebrew Postgres, native Python). Docker path is in the repo README. **Live reference** — keep this current.
- [`cross-machine-surface.md`](install/cross-machine-surface.md) — *point-in-time install-surface audit (2026-04-24), preserved as a record.* Inventory of machine-varying values; useful background for a cross-machine setup, but the install path itself is `PLAYBOOK.md`, not this.

### `integration/` — MCP and client wiring

- [`MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) — Streamable HTTP MCP endpoints, stdio bridges, and hosted/client-neutral setup

### `ontology/` — identity ontology

The system's versioned identity ontology, the resolution ledger, and the working RFCs that evolve them. Has its own reader's guide because the folder is dense.

→ Start at **[`ontology/README.md`](ontology/README.md)**.

### `operations/` — operator-internal runbooks

How to run this in production. Most readers can skip these.

- [`OPERATOR_RUNBOOK.md`](operations/OPERATOR_RUNBOOK.md) — primary runbook
- [`github-workflow-conventions.md`](operations/github-workflow-conventions.md) — canonical delivery contract (branch naming, draft PRs) shared by Codex and Claude; `AGENTS.md`/`CLAUDE.md` carry the short form
- [`merge-automation-plan.md`](operations/merge-automation-plan.md) — branch-protection + operator-armed auto-merge plan (not yet applied)
- [`ci-issue-surfacing.md`](operations/ci-issue-surfacing.md) — experiment wiring the surfacing instinct into GitHub CI (deduped issues from new findings)
- [`automation-overrides.md`](operations/automation-overrides.md) — operator-authored metadata layered onto the automation census for accountability/gate classification
- [`automation-census-setup.md`](operations/automation-census-setup.md) — agnostic setup for the automation census behind the dashboard Automations registry
- [`resident-roster.md`](operations/resident-roster.md) — `UNITARES_RESIDENTS` configuration; the named resident set is config, not a hardcoded fleet
- [`ablation-negative-controls.md`](operations/ablation-negative-controls.md) — synthetic bad-outcome fixtures for red-team ablation plumbing
- [`DEFINITIVE_PORTS.md`](operations/DEFINITIVE_PORTS.md) — port assignments across services
- [`database_architecture.md`](operations/database_architecture.md) — single-Postgres / schema-isolation model
- [`glossary-site.md`](operations/glossary-site.md) — GitHub Pages publishing path for the ontology glossary site
- [`lease-plane-operator-runbook.md`](operations/lease-plane-operator-runbook.md) — Elixir lease-plane operations
- [`branch-hygiene-runbook.md`](operations/branch-hygiene-runbook.md) — resident branch-hygiene sweep (`agents/vigil_hygiene`)
- [`resident-validation-cohort.md`](operations/resident-validation-cohort.md) — experimental long-running resident validation tick contract
- [`resident-validation-supervised-invocation.md`](operations/resident-validation-supervised-invocation.md) — local-only supervised canary invocation wrapper
- [`dormant-capability-registry.md`](operations/dormant-capability-registry.md) — distinguishes built-but-unwired capability from genuine cruft, so cleanup is deliberate
- [`research-registry.md`](operations/research-registry.md) — file-backed registry for agent-network research runs, rigor checklist, and REST/MCP query surfaces
- [`kg-lineage-dashboard-handoff.md`](operations/kg-lineage-dashboard-handoff.md) — deferred implementation handoff for KG supersession/related-lineage dashboard exposure
- [`test-suite-triage.md`](operations/test-suite-triage.md) — current state of the test gate and known-triaged suites
- [`DATA_NOTES.md`](operations/DATA_NOTES.md) — operational data dictionary for the production governance database
- [`DEPLOYMENT_DATA_CAVEAT.md`](operations/DEPLOYMENT_DATA_CAVEAT.md) — what the cited deployment numbers do and don't mean
- Dated finding record (point-in-time, preserved by design): [`ablation-initiates-finding-2026-06-16.md`](operations/ablation-initiates-finding-2026-06-16.md)

### `dev/` — developer-internal

For people working on UNITARES itself, not using it.

- [`CANONICAL_SOURCES.md`](dev/CANONICAL_SOURCES.md) — arch-dispute resolution
- [`DRIFT_LEDGER.md`](dev/DRIFT_LEDGER.md) — guard/seam index for drift prevention and known unguarded seams
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

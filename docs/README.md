# UNITARES Documentation

This is the documentation tree for the single-operator
[UNITARES federation kernel](../README.md). Start with the repo README for
the core idea and quickstart, then choose the path below. Canonical references,
operator runbooks, research provenance, and optional essays are kept separate so
an analogy or proposal is not mistaken for a deployed contract.

## Reader's path

| You are… | Read in this order |
|---|---|
| **A reviewer / first-time visitor** | [repo README](../README.md) → [product](PRODUCT_DEFINITION.md) → [architecture: the record's layers](UNIFIED_ARCHITECTURE.md) → [interface](INTERFACE_CONTRACT.md) → [evidence and limits](EVIDENCE_AND_LIMITS.md) |
| **Integrating an agent client** | [`INTERFACE_CONTRACT.md`](INTERFACE_CONTRACT.md) → [`integration/CLIENT_CAPABILITY_MATRIX.md`](integration/CLIENT_CAPABILITY_MATRIX.md) → [`manual/04-integrating-agents.md`](manual/04-integrating-agents.md) → [`integration/MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) → [`guides/TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) |
| **Installing / deploying** | [`manual/02-install.md`](manual/02-install.md) → [`install/PLAYBOOK.md`](install/PLAYBOOK.md) → [`operations/OPERATOR_RUNBOOK.md`](operations/OPERATOR_RUNBOOK.md) |
| **Contributing to the identity layer** | [`../AGENTS.md`](../AGENTS.md) → [`ontology/README.md`](ontology/README.md) → [`ontology/identity.md`](ontology/identity.md) → [`ontology/plan.md`](ontology/plan.md) |
| **Following current research** | [current designs and contracts](proposals/active/README.md); [frozen protocols](proposals/registered/README.md) have their own registration and execution rules |
| **Reading research history** | [`EVALUATION_INDEX.md`](EVALUATION_INDEX.md) → [`ontology/README.md`](ontology/README.md) → [`proposals/README.md`](proposals/README.md) |

Project-level status lives in `docs/`; participation and community policies live in `.github/`:
[roadmap](ROADMAP.md), [compatibility](COMPATIBILITY.md),
[governance](GOVERNANCE.md), [support](../.github/SUPPORT.md), and
[contributing](../.github/CONTRIBUTING.md).

## Reader-facing documentation

### `manual/` — the user manual

A cohesive, multi-chapter front door for operators and integrators. Thin chapters that stitch the canonical docs below into one walkthrough; the deep references stay canonical.

→ Start at **[`manual/README.md`](manual/README.md)** (overview · install · running · integrating · reading the signals · operating · troubleshooting).

### Canonical reference

- **[`CAPABILITIES_AND_DEPLOYMENT.md`](CAPABILITIES_AND_DEPLOYMENT.md)** — core workflow versus advanced capabilities, deployment prerequisites, and discovery versus readiness.

- **[`PRODUCT_DEFINITION.md`](PRODUCT_DEFINITION.md)** — what UNITARES is, in plain language: the one-sentence product, the Core/userland boundary, MCP's place in the stack, identity, claims/evidence, review, outcomes, and reconstruction, and the honest limits. Start here when the architecture nouns aren't landing.
- **[`UNIFIED_ARCHITECTURE.md`](UNIFIED_ARCHITECTURE.md)** — the canonical architecture doc, organized around the record: userlands and interfaces, then the core layers (identity and lineage, findings and the knowledge graph, review, outcomes, policy and the EISV instrument), coordination, and storage, with each layer's maturity. Start here for architecture.
- **[`EISV_COMPUTATION.md`](EISV_COMPUTATION.md)** — the owner of the EISV instrument: how each check-in's state estimate, risk, and margin are computed, the warmup stages, and what the evidence does and does not show.
- **[`FLAGS.md`](FLAGS.md)** — generated catalog of `UNITARES_*` / `GOVERNANCE_*` environment reads and their reader fallbacks.
- **[`REPO_SCOPE.md`](REPO_SCOPE.md)** — what belongs in this repository and what belongs in an operator's own deployment repository.
- **[`INTERFACE_CONTRACT.md`](INTERFACE_CONTRACT.md)** — versioned, transport-neutral capability names and the boundary between tool reachability and host lifecycle integration.
- **[`REVIEWER_GUIDE.md`](REVIEWER_GUIDE.md)** — guided tour for reviewers evaluating the project.
- **[`SCOPE_AND_THREAT_MODEL.md`](SCOPE_AND_THREAT_MODEL.md)** — who this is for, what anchors the signal, and what gaming or robustness remains unproven.
- **[`DEPLOYMENT_SECURITY.md`](DEPLOYMENT_SECURITY.md)** — the infra/deployment counterpart: the Compose quickstart's container hardening, what's published to the host by default, and the hardening gaps that pass didn't close.
- **[`PRODUCTION_SNAPSHOT.md`](PRODUCTION_SNAPSHOT.md)** — frozen live metrics and dashboard views.
- **[`EVIDENCE_AND_LIMITS.md`](EVIDENCE_AND_LIMITS.md)** — the full public claim ledger: every claim with its evidence class, the deployment record behind the numbers, the protocol qualification on the outcome read, and the named cross-operator blocker. The root README and the public landing page link here rather than summarizing it.
- **[`evaluations/review-correction-traces-2026-09-21.md`](evaluations/review-correction-traces-2026-09-21.md)** — a fixed 20-PR retrospective linking review findings to corrective patches, with attribution and causal limits stated beside the result.
- **[`trust-contract.md`](trust-contract.md)** — what the system guarantees, what it does not, and what honest failure looks like.
- **[`CHANGELOG.md`](CHANGELOG.md)** — release history.

### Subsystem guides

Operating guidance for individual subsystems lives next to the code as Skills, not in `docs/` (this keeps the guide in sync with its `source_files:` and carries its own freshness budget). The map to those lives here:

- **Agent lifecycle** — six-rule bootstrap card, identity, check-ins, response
  control flow, and recovery →
  [`skills/governance-lifecycle/SKILL.md`](../skills/governance-lifecycle/SKILL.md)
- **Knowledge graph (KG)** — agent-facing operating manual: search-before-write discipline, the `knowledge()` actions, discovery types/statuses, tagging, and closing the loop → [`skills/knowledge-graph/SKILL.md`](../skills/knowledge-graph/SKILL.md)

### `guides/` — getting started

User- and integrator-facing how-tos. Thin by design — most architecture lives in `UNIFIED_ARCHITECTURE.md` and the repo README.

- [`AGENT_SESSION_WALKTHROUGH.md`](guides/AGENT_SESSION_WALKTHROUGH.md) — one task across two processes, every call with the server's captured response: search, start, check in, grade, exit cleanly, hand off
- [`CODEX_START.md`](guides/CODEX_START.md) — human-facing Codex quickstart for direct repository work
- [`START_HERE.md`](guides/START_HERE.md) — compatibility redirect to the current audience paths
- [`TROUBLESHOOTING.md`](guides/TROUBLESHOOTING.md) — canonical symptom-and-recovery guide
- [`CIRS_PROTOCOL.md`](guides/CIRS_PROTOCOL.md) — multi-agent coordination protocol (specialized; not a general architecture overview)

### `install/` — installation

- [`CHOOSING_INSTALLATION.md`](install/CHOOSING_INSTALLATION.md) — choose one primary installation: Homebrew preview, Docker Compose, or a source checkout.
- [`HOMEBREW_SETUP.md`](install/HOMEBREW_SETUP.md) — published Apple Silicon Homebrew preview, private configuration, database readiness and native service control. Package install and first-time provisioning are in the [tap README](https://github.com/cirwel/homebrew-unitares).
- [`PLAYBOOK.md`](install/PLAYBOOK.md) — advanced source-checkout install (Homebrew Postgres, manually built extensions, native Python). Docker setup is in the [installation manual](manual/02-install.md#21-docker-quickstart). **Live reference** — keep this current.
- [`cross-machine-surface.md`](install/cross-machine-surface.md) — *point-in-time install-surface audit (2026-04-24), preserved as a record.* Inventory of machine-varying values; useful background for a cross-machine setup, but the install path itself is `PLAYBOOK.md`, not this.

### `deployment/` — hosted installation

- [`glama.md`](deployment/glama.md) — private Glama image, persistent storage, validation, and deployment prerequisites.

### `integration/` — MCP and client wiring

- [`MCP_CLIENTS.md`](integration/MCP_CLIENTS.md) — Streamable HTTP MCP endpoints, stdio bridges, and hosted/client-neutral setup
- [`CLIENT_CAPABILITY_MATRIX.md`](integration/CLIENT_CAPABILITY_MATRIX.md) — what direct MCP, host plugins, adapters, and UNITARES Resident do and do not automate
- [`INTENT_MAP.md`](integration/INTENT_MAP.md) — *draft:* the tool surface organized by agent need, with recipes and the guarantees it does not make
- [`INTENT_MAP_AUDIT.md`](integration/INTENT_MAP_AUDIT.md) — *draft:* overlaps, gaps and open questions behind the intent map, and a proposed check for new tools

### `ontology/` — identity ontology

The system's versioned identity ontology, the resolution ledger, and the working RFCs that evolve them. Has its own reader's guide because the folder is dense. The question-keyed vocabulary glossary also lives here: [`ontology/glossary.md`](ontology/glossary.md), published with an interactive viewer at [cirwel.github.io/unitares](https://cirwel.github.io/unitares/glossary.html).

→ Start at **[`ontology/README.md`](ontology/README.md)**.

## Operator and contributor documentation

### `operations/` — runbooks and dated evidence

Production runbooks share this directory with dated deployment and evaluation
records. Operators use the runbooks; evaluators follow the evidence links in
[`EVIDENCE_AND_LIMITS.md`](EVIDENCE_AND_LIMITS.md) and
[`EVALUATION_INDEX.md`](EVALUATION_INDEX.md). The operations index separates
live procedures from preserved evidence and its inference limits.

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
- [`TOOL_REFERENCE.md`](dev/TOOL_REFERENCE.md) — generated: what every tool does, with its identity requirement, timeouts, and other names
- [`CIRCUIT_BREAKER_DIALECTIC.md`](dev/CIRCUIT_BREAKER_DIALECTIC.md) — recovery semantics (specialized)

### `releases/` — release notes and errata

Per-release notes and post-release errata, for example [`releases/3.4.0.md`](releases/3.4.0.md). [`CHANGELOG.md`](CHANGELOG.md) is the cumulative history.

## Research, provenance, and optional interpretation

### `ontology/eisv-telemetry-envelope-v1.md` — EISV telemetry envelope

[`ontology/eisv-telemetry-envelope-v1.md`](ontology/eisv-telemetry-envelope-v1.md)
is the versioned measurement → derivation → policy → enforcement provenance
stored with each new state row. It belongs to the EISV instrument; read it after
[`EISV_COMPUTATION.md`](EISV_COMPUTATION.md).

### `evaluations/` and `evaluation/` — dated evaluation records

Study protocols, enrollments, schemas and results, one subfolder per study.
[`EVALUATION_INDEX.md`](EVALUATION_INDEX.md) is the reader's index; the
subfolders are:

- [`accountability-journey/`](evaluations/accountability-journey/) — accountability-journey rehearsal (mechanism validation only)
- [`accountable-coordination-ablation/`](evaluations/accountable-coordination-ablation/) — draft plumbing pilot plan
- [`assurance-cases/`](evaluations/assurance-cases/) — draft outcome-binding assurance case
- [`ax-dogfood/`](evaluations/ax-dogfood/) — exploratory agent-experience dogfood read
- [`eisv-ei-referent/`](evaluations/eisv-ei-referent/), [`eisv-i-rest-live-read/`](evaluations/eisv-i-rest-live-read/), [`eisv-s-faithfulness/`](evaluations/eisv-s-faithfulness/) — EISV coordinate studies
- [`eisv-incremental-value/`](evaluations/eisv-incremental-value/) — episode and federation-pilot schemas
- [`kg-agent-adoption/`](evaluations/kg-agent-adoption/) — enrollment schema and canary receipts
- [`orientation-constraint-set/`](evaluations/orientation-constraint-set/) — diagnostic orientation constraint set: enrollments and registered results
- [`production-snapshots/`](evaluations/production-snapshots/) — machine-readable snapshots behind [`PRODUCTION_SNAPSHOT.md`](PRODUCTION_SNAPSHOT.md)
- [`relational-calibration-instrument-supply/`](evaluations/relational-calibration-instrument-supply/) — registered instrument-supply read
- [`review-correction-traces-2026-09-21.md`](evaluations/review-correction-traces-2026-09-21.md) — the 20-PR review retrospective listed above

[`evaluation/dialectic-reviewer-labels.md`](evaluations/dialectic-reviewer/dialectic-reviewer-labels.md)
labels what dialectic reviewer antithesis messages actually do.

### `analysis/` — dated audits and one-off reads

Point-in-time audits and measurement reads (for example the
[paper evidence audit](analysis/paper-evidence-audit-2026-10-11.md)). Each file
states its own scope and date; none is a deployed contract.

### `plans/` — dated plans and review records

Execution plans and point-in-time review records, for example the
[EISV instrument-quality sequence](plans/eisv-instrument-quality-2026-10-13.md).
Each carries its own status line; a plan authorizes nothing beyond what it states.

### `proposals/` — internal RFCs and decision history

These are research and engineering provenance, not a list of shipped features.
Active and resolved RFCs that do not belong in `ontology/` live here. Each doc
carries its own resolution status in the body.

→ [Proposal guide](proposals/README.md):
[current designs and contracts](proposals/active/README.md),
[registered protocols](proposals/registered/README.md), and
[historical and parked work](proposals/archive/README.md).

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

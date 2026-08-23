# Operations documentation

Operator-internal runbooks, deployment records, and maintenance notes live
here. Most evaluators and integrators can stay in the [Reviewer Guide](../REVIEWER_GUIDE.md)
or [User Manual](../manual/README.md).

## Start here

- [`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md) — primary production runbook.
- [`DEFINITIVE_PORTS.md`](DEFINITIVE_PORTS.md) — service and port registry.
- [`database_architecture.md`](database_architecture.md) — PostgreSQL/schema
  ownership and Redis posture.
- [`DATA_NOTES.md`](DATA_NOTES.md) — operational data dictionary.
- [`DEPLOYMENT_DATA_CAVEAT.md`](DEPLOYMENT_DATA_CAVEAT.md) — limits on what
  maintainer-deployment counts establish.

## Delivery and automation

- [`RELEASE_PROCESS.md`](RELEASE_PROCESS.md) — server, SDK, container, and
  deployment release checklist.
- [`github-workflow-conventions.md`](github-workflow-conventions.md) — branch,
  draft-PR, and delivery contract.
- [`merge-automation-plan.md`](merge-automation-plan.md) — operator-armed
  auto-merge plan; not yet applied.
- [`ci-issue-surfacing.md`](ci-issue-surfacing.md) — deduplicated CI finding
  experiment.
- [`automation-overrides.md`](automation-overrides.md) — operator metadata for
  the automation census.
- [`automation-census-setup.md`](automation-census-setup.md) — automation-census
  setup behind the dashboard registry.
- [`branch-hygiene-runbook.md`](branch-hygiene-runbook.md) — safe resident
  branch-hygiene sweep.
- [`test-suite-triage.md`](test-suite-triage.md) — test-gate state and known
  triaged suites.

## Runtime services and residents

- [`resident-roster.md`](resident-roster.md) — configured resident set.
- [`redis-retirement-soak-runbook.md`](redis-retirement-soak-runbook.md) —
  staged Redis mirror-retirement checks and rollback gates.
- [`lease-plane-operator-runbook.md`](lease-plane-operator-runbook.md) — Elixir
  lease-plane operations.
- [`public-site.md`](public-site.md) — public landing page and ontology glossary publishing path.
- [`dormant-capability-registry.md`](dormant-capability-registry.md) — built but
  unwired capabilities and their disposition.
- [`research-registry.md`](research-registry.md) — agent-network research-run
  registry and query surfaces.
- [`kg-lineage-dashboard-handoff.md`](kg-lineage-dashboard-handoff.md) — deferred
  KG lineage dashboard handoff.

## Validation and evidence records

- [`model-harness-risk-cohorts.md`](model-harness-risk-cohorts.md) — prospective,
  descriptive model/harness provenance and like-for-like cohort reporting.
- [`ablation-negative-controls.md`](ablation-negative-controls.md) — synthetic
  negative controls for the ablation plumbing.
- [`positive-control-validity-2026-08-23.md`](positive-control-validity-2026-08-23.md)
  — when a positive control may make an instrument's silence informative, and the
  coherence-gate control that could not fail.
- [`eisv-ablation-frozen-2026-08-09.md`](eisv-ablation-frozen-2026-08-09.md) —
  current frozen trusted-anchor matrix; selection-adjusted result is negative.
- [`ablation-initiates-finding-2026-06-16.md`](ablation-initiates-finding-2026-06-16.md)
  — historical measurement record, superseded for current lift claims.
- [`self-report-verdict-dependence-2026-06-28.md`](self-report-verdict-dependence-2026-06-28.md)
  — dated verdict-provenance worked example with an inline correction.
- [`stateless-mcp-consumer-ux-2026-07-04.md`](stateless-mcp-consumer-ux-2026-07-04.md)
  — dated first-contact consumer UX report; preserve as history, not current
  identity guidance.
- [`resident-validation-cohort.md`](resident-validation-cohort.md) — experimental
  resident validation tick contract.
- [`resident-validation-supervised-invocation.md`](resident-validation-supervised-invocation.md)
  — local supervised canary invocation.

The [top-level documentation index](../README.md) owns audience routing. This
file owns the inventory of operator-internal documents.

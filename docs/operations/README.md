# Operations documentation

Operator-internal runbooks, deployment records, and maintenance notes live
here. Most evaluators and integrators can stay in the [Reviewer Guide](../REVIEWER_GUIDE.md)
or [User Manual](../manual/README.md).

## Start here

- [`OPERATOR_RUNBOOK.md`](OPERATOR_RUNBOOK.md) — primary production runbook.
- [`DEFINITIVE_PORTS.md`](DEFINITIVE_PORTS.md) — service and port registry.
- [`database_architecture.md`](database_architecture.md) — PostgreSQL/schema
  ownership and Redis posture.
- [`DEPLOYMENT_DATA_CAVEAT.md`](DEPLOYMENT_DATA_CAVEAT.md) — limits on what
  maintainer-deployment counts establish.

## Delivery and automation

- [`RELEASE_PROCESS.md`](RELEASE_PROCESS.md) — server, SDK, container, and
  deployment release checklist.
- [`deployment-env-overlay.md`](deployment-env-overlay.md) — external reviewed
  environment configuration required by the managed MCP deploy.
- [`deployment-inventory.md`](deployment-inventory.md) — the explicit,
  operator-owned inventory that deployment status and dispatch read.
- [`github-workflow-conventions.md`](github-workflow-conventions.md) — branch,
  draft-PR, and delivery contract.
- [`ci-issue-surfacing.md`](ci-issue-surfacing.md) — deduplicated CI finding
  experiment.
- [`automation-overrides.md`](automation-overrides.md) — operator metadata for
  the automation census.
- [`automation-census-setup.md`](automation-census-setup.md) — automation-census
  setup behind the dashboard registry.
- [`branch-hygiene-runbook.md`](branch-hygiene-runbook.md) — safe resident
  branch-hygiene sweep.
- [`ci-coverage-validation.md`](ci-coverage-validation.md) — CI shard layout,
  coverage measurements, and the opt-in drills that skip in the default gate.

## Runtime services and residents

- [`resident-roster.md`](resident-roster.md) — configured resident set.
- [`reference-monitoring.md`](reference-monitoring.md) — optional reference
  monitoring agents and the repository, owner and services they are pointed at.
- [`cloud-deploy-runbook.md`](cloud-deploy-runbook.md) — hosting the governance
  stack off the primary machine: exposure, off-site backups, cutover.
- [`cloud-session-plugin.md`](cloud-session-plugin.md) — why the governance
  plugin is absent in Claude Code cloud sessions, a hook-by-hook audit of what
  survives a residentless container, and the setup-script wiring.
- [`redis-retirement-soak-runbook.md`](redis-retirement-soak-runbook.md) —
  staged Redis mirror-retirement checks and rollback gates.
- [`lease-plane-operator-runbook.md`](lease-plane-operator-runbook.md) — Elixir
  lease-plane operations.
- [`dialectic-resolution-effects.md`](dialectic-resolution-effects.md) —
  read-only inventory and state-specific recovery for interrupted dialectic
  runtime effects.
- [`../public-site/README.md`](../public-site/README.md) — public landing page
  and ontology glossary publishing path (`public-site.md` here is a locator).
- [`dormant-capability-registry.md`](dormant-capability-registry.md) — built but
  unwired capabilities and their disposition.

## Validation and evidence records

- [`eisv-instrument-preservation-harness.md`](eisv-instrument-preservation-harness.md) —
  partial, tests-only pin of the EISV estimator and of the read's join mechanics
  (not the anchor-scoped registered cohort), with the estimator input inventory.
- [`agent-analysis-provenance.md`](agent-analysis-provenance.md) — mark every
  load-bearing claim tree / system / inferred; the tree is not the deployment.
- [`model-harness-risk-cohorts.md`](model-harness-risk-cohorts.md) — prospective,
  descriptive model/harness provenance and like-for-like cohort reporting.
- [`ablation-negative-controls.md`](ablation-negative-controls.md) — synthetic
  negative controls for the ablation plumbing.
- [`positive-control-validity-2026-08-23.md`](positive-control-validity-2026-08-23.md)
  — when a positive control may make an instrument's silence informative, and the
  coherence-gate control that could not fail.
- [`eisv-ablation-frozen-2026-08-09.md`](eisv-ablation-frozen-2026-08-09.md) —
  frozen trusted-anchor matrix; descriptive non-detection after selection
  adjustment, with read-specific power unresolved in the preserved evidence.
- [`ablation-initiates-finding-2026-06-16.md`](ablation-initiates-finding-2026-06-16.md)
  — historical measurement record, superseded for current lift claims.
- [`self-report-verdict-dependence-2026-06-28.md`](self-report-verdict-dependence-2026-06-28.md)
  — dated verdict-provenance worked example with an inline correction.
- [`resident-validation-cohort.md`](resident-validation-cohort.md) — experimental
  resident validation tick contract and its local supervised canary invocation
  (`resident-validation-supervised-invocation.md` is now a locator).
- Dialectic reviewer evidence (the 2026-08-09 verdict cohort and the 2026-08-19
  antithesis labels, split at 2026-07-02 and never pooled) lives in
  [`../evaluations/dialectic-reviewer/`](../evaluations/dialectic-reviewer/README.md).

## Archive

- [`archive/README.md`](archive/README.md) — superseded plans, deferred
  handoffs and dated snapshots retired from current guidance: the superseded
  merge-automation plan, the deferred KG lineage dashboard handoff, the
  2026-07-04 consumer UX report, the 2026-04-10 data dictionary (`DATA_NOTES.md`;
  for the current schema read `db/postgres/schema.sql` and
  [`database_architecture.md`](database_architecture.md)), and the 2026-06-14
  test-suite triage.

The [top-level documentation index](../README.md) owns audience routing. This
file owns the inventory of operator-internal documents.

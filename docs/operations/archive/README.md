# Operations archive

Superseded plans, deferred handoffs, dated reports and dated snapshots that
used to sit beside the live runbooks. **Archive placement retires a document as
current guidance; it does not delete the record, reject the idea, or change any
runtime behaviour.** Each file keeps its original text, apart from relative
links repaired after the move and, where noted, a short archival banner.

[Operations index](../README.md) · [Proposal archive](../../proposals/archive/README.md)

| Document | Archived because | Current source |
|---|---|---|
| [`merge-automation-plan.md`](merge-automation-plan.md) | **Superseded** 2026-09-27 by the self-declared status line. Its branch-protection payload is out of date; do not apply it | The merge queue in [`../github-workflow-conventions.md`](../github-workflow-conventions.md) §2 and §4 |
| [`kg-lineage-dashboard-handoff.md`](kg-lineage-dashboard-handoff.md) | **Deferred** handoff (2026-06-21) for showing knowledge-graph supersession lineage on the dashboard; it waits on supersession edges accumulating | Resume from the handoff itself; it records the data prerequisites |
| [`stateless-mcp-consumer-ux-2026-07-04.md`](stateless-mcp-consumer-ux-2026-07-04.md) | **Dated** first-contact consumer UX report. History, not current identity guidance | [`../self-report-verdict-dependence-2026-06-28.md`](../self-report-verdict-dependence-2026-06-28.md) and the active verification-weighted verdict proposal |
| [`DATA_NOTES.md`](DATA_NOTES.md) | **Dated** (2026-04-10) data dictionary of one deployment's database: row counts and date ranges from that day | [`../../../db/postgres/schema.sql`](../../../db/postgres/schema.sql) for the schema, [`../database_architecture.md`](../database_architecture.md) for ownership |
| [`test-suite-triage.md`](test-suite-triage.md) | **Dated** (2026-06-14) test-gate triage; its skip counts are stale | Opt-in drills now live in [`../ci-coverage-validation.md`](../ci-coverage-validation.md) |

Dialectic-reviewer evidence that used to sit in `docs/operations/` moved to
[`../../evaluations/dialectic-reviewer/`](../../evaluations/dialectic-reviewer/README.md),
not here: it is an evaluation record, not a retired runbook.

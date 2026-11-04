# Registered protocols

Frozen protocols and protocols that register at their introducing merge. Preserve their registration, amendment and stop rules. **Placement here does not enroll a cohort, authorize execution, or re-register anything.** Rows re-read on 2026-09-23 carry their registration commit; some bodies still show the DRAFT header they had before merge.

Some bodies preserve their original “DRAFT” or “proposed” headers. Read the
registration clause and the operative sections linked below before acting;
the dated index tag supplies no execution authority.

[Proposal guide](../README.md) · [Migration inventory](../../dev/proposals-layout-2347.json)

## Before a registered result is reported

These rules apply to every protocol in this folder. They change no protocol's
design, statistic or stop rule; they govern what is posted about a read
(operator approval of this guard, 2026-11-03).

1. **Attrition first.** Before any verdict, the write-up states how many rows
   the read admitted and how many each filter removed. A read can't claim a
   result while that removal is unexplained. The ablation matrix prints an
   "Evidence retained" table ahead of its other sections. When a slice keeps
   less than half of what it admitted, the report opens with **EVIDENCE
   STARVED** and the script exits 3.
2. **Author sign-off.** An agent may run a registered read and post its raw
   report, labeled as unreviewed. It does not post a verdict until the operator
   has signed off on it. That covers a pass, a fail, a closure, a refutation, or
   a status change in the table below. It applies on an issue, in a doc and in
   the knowledge graph. The verdict post cites the sign-off with a link or the
   operator's dated words.
3. **A review is not a sign-off.** A reviewer approving the write-up, from any
   family, checks the write-up. It doesn't stand in for step 2.

Why: on 2026-11-03 the stop-rule read's fixture rule removed 4,649 of 4,876
trusted outcomes. The read kept 227 rows, and an agent posted "FAIL on all four
conditions" across the issue, the docs and the knowledge graph. A cross-family
reviewer passed that post. The operator asked where the data had gone, and only
then was the cause found ([#1425](https://github.com/cirwel/unitares/issues/1425),
[#3183](https://github.com/cirwel/unitares/pull/3183)).

## Accountability and calibration protocols

| Document | Status |
|---|---|
| [`accountable-coordination-ablation-v0.md`](accountable-coordination-ablation-v0.md) | **Closed** · **Feasibility stop, 2026-10-01**, before any build, task selection, model run or enrollment: infeasible for a single operator (the primary test has at most 57% power at 40 tasks for a 15-point effect; see its amendment log). Not a result. Registered at the merge of #2249 (`74bd59cd`, 2026-09-15); the body's DRAFT wording predates that merge. A four-arm, task-family-clustered comparison of independent work, ephemeral messages, durable coordination, and full accountable coordination. Reopening needs a new premise and a new protocol version |
| [`relational-calibration-pilot-v0.md`](relational-calibration-pilot-v0.md) | **Registered** · v0.2 specification and adversarial threat model only — adds temporal/instrument validity, experimental-principal accounting, dyadic inference, and a frozen exposure/horizon contract; runtime collection remains explicitly blocked |
| [`relational-calibration-maturity-capacity-v1.md`](relational-calibration-maturity-capacity-v1.md) | **Closed** · **Result recorded: `instrument_supply_not_ready`** — the one authoritative read ran 2026-09-23T23:43:57Z against the frozen `2026-09-17T00:00:00Z` cutoff and found `strict_supply = 0` (953 recent → 38 mature → 0 temporally established); the stop rule closes this attempt; a disclosed pre-read row count was ruled harmless by the operator (`docs/evaluations/relational-calibration-instrument-supply/result-v1.md`). Process UUID counts never established participant or federation capacity. The protocol text is frozen |
| [`accountable-testbed-metrics-preregistration-v0.md`](accountable-testbed-metrics-preregistration-v0.md) | **Registered** · Frozen evaluation pre-registration for the accountable multi-principal testbed. Superseded for future evaluation by v1, which preserves v0 as an auditable registration. V0's protocol is unchanged; one cited path was updated by the 2026-09-23 relocation (#2350) and is recorded in its deviation log |
| [`accountable-testbed-metrics-preregistration-v1.md`](accountable-testbed-metrics-preregistration-v1.md) | **Registered** · Frozen v1.1 evaluation contract for future headline, ablation, and scale-sweep runs; the document merge did not execute those runs |
| [`orientation-constraint-set-preregistration-v0.md`](../orientation-constraint-set-preregistration-v0.md) | **Closed** · **Result recorded: `SAFETY_STOP`** — cohort v1 completed all 240 scored calls on 2026-08-25 and crossed the registered treatment safety-stop conditions (`docs/evaluations/orientation-constraint-set/result-v1.md`; cohort v0 aborted before scored calls, `docs/evaluations/orientation-constraint-set/result-v0.md`). Blocks shipping the tested renderer as-is; says nothing about an untested visual self-schema. The protocol text is frozen and keeps its original header |

## EISV maths, coherence, and outcome grounding

| Document | Status |
|---|---|
| [`eisv-outcome-grounding-stop-rule-v0.md`](eisv-outcome-grounding-stop-rule-v0.md) | **Registered** · Fixed 2026-12-01 read. The historical “proposed” header is preserved; the [Summary](eisv-outcome-grounding-stop-rule-v0.md#summary) states the operative decision and the [pre-registered gate](eisv-outcome-grounding-stop-rule-v0.md#pre-registered-gate) binds execution. The [fixture-rule decision packet](../archive/outcome-fixture-conflation-decision-packet-v0.md) governs post-cutoff rows. Never re-run or refresh the registered read |
| [`legacy-coherence-dependency-ablation-v0.md`](legacy-coherence-dependency-ablation-v0.md) | **Registered** · Prospective shadow contract, 2026-08-12; a distinct v0.1 shadow with the corrected fixture rule was registered beside it 2026-09-02 |
| [`independent-operator-cohort-preregistration-v0.md`](independent-operator-cohort-preregistration-v0.md) | **Registered** · Original DRAFT header preserved. Its registration-at-merge clause took effect with [#1785](https://github.com/cirwel/unitares/pull/1785), merge commit `4907e926` (2026-08-21); amended 2026-09-02 before any enrollment. The [separate ledger](../active/independent-operator-cohort-enrollments.md) records enrollment and window starts. Two further post-registration edits, `eea55ccc` (#1840, 2026-08-23) and `1bdc33f4` (#1929, 2026-08-27), were not logged at the time; they are disclosed in its amendment log (2026-09-23) and both predate any enrollment. Its #1791 setup note is stale (fixed by #1794) |
| [`self-improvement-loop-evaluation-v0.md`](self-improvement-loop-evaluation-v0.md) | **Registered** · Registered at the merge of #1806 (`c68d6997`, 2026-08-22); the body's DRAFT header predates that merge. No enrollment recorded |

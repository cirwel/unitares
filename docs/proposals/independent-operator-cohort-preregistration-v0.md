# Independent-operator validation cohort — protocol pre-registration v0

**Status: DRAFT.** The protocol registers at the merge commit of the PR
introducing this file, after an adversarial design review. Per-operator
parameters (fleet shape, outcome producers, window start) freeze at that
operator's dated enrollment record, appended below by PR. The observation
clock for an operator starts at their first governed check-in, never earlier.
Tracks [#1607](https://github.com/cirwel/unitares/issues/1607).

## Why this exists

The public evidence base is one maintainer-operated deployment. The frozen
2026-08-09 trusted-anchor matrix (PR #1603) labels all 12 overall slices
`NOISE-LEVEL` after selection adjustment — useful negative evidence, but it
establishes nothing about external adoption, nothing about behavior under an
operator who does not share the maintainer's habits or database, and nothing
causal. This protocol defines what one external deployment can and cannot
establish, before any outcome is read.

Relationship to the outcome-grounding stop rule
(`eisv-outcome-grounding-stop-rule-v0.md`, #1425): that rule owns the
outcome question **for the maintainer deployment** and its registered read is
2026-12-01. This protocol does not reopen, anticipate, or substitute for that
read. An external operator's outcome labels are a different label channel on
a different deployment; results here are reported per-deployment and are
never pooled with maintainer data.

## Three claim lanes, separated by design

| Lane | Question | Status in this protocol |
|---|---|---|
| **U — operational usability** | Can an operator who is not the maintainer deploy, run, and keep the instrument healthy without maintainer code changes? | **Primary.** |
| **P — predictive validity** | On the operator's own outcome labels, does any EISV stream beat the shipped dumb baseline, by the shipped harness's own selective-inference discipline? | Secondary, gated on an evaluability floor. |
| **C — causal efficacy** | Did governance *prevent* anything? | **Not claimable from this design.** Observational deployment cannot separate verdict effects from selection; a causal claim requires an interventional design that does not exist yet. Recorded as out of scope, not deferred silently. |

## Cohort definition and separation

- The operator is not the maintainer, does not share the maintainer's
  production database, and runs their own stack from the Tier-1 install
  contract (release-tagged `docker compose up`, per README Quickstart).
- Separation is by construction: two deployments, two databases. Shared
  artifacts are harness outputs, telemetry-health snapshots, and the friction
  log — not raw databases. An operator may opt in to sharing more; the
  protocol does not require it.
- Enrollment record (appended to this file by PR, dated): operator name or
  pseudonym, fleet shape (agent count, client types), intended outcome
  producers (their CI, test runners, review tooling), and window start.

## Observation window and stopping rule

- **28 days** from the operator's first governed check-in. One extension is
  allowed only if declared before day 21 and only for operational reasons
  (e.g. an infrastructure outage), recorded in the enrollment record.
- **Lane U** is read at day 7 and at window end. Friction is captured
  continuously — usability observation is not outcome peeking.
- **Lane P** is read **once**, at window end plus 30 days of outcome
  maturation, with the harness `--as-of` frozen at that instant. No interim
  lane-P reads. A second read is a new pre-registration.

## Lane U — metrics and verdict rule

Collected: wall-clock time from clone to first governed check-in
(self-reported); a friction log where every blocker is recorded with severity
(blocked / workaround-found / cosmetic); percentage of window days with at
least one governed check-in; instrumentation health from the shipped
`/v1/eisv/telemetry-health` surface (envelope coverage, behavioral-vs-fallback
source rate, missingness) at day 7 and window end; count of operator overrides
and manual interventions. Abandonment mid-window is a usability **result**,
reported with its friction log — not a null.

The claim "independently deployable" is earned only if **all** of:

1. the operator reaches a first governed check-in with no maintainer code
   changes (questions are allowed and are logged as friction entries);
2. at least 50% of window days have a governed check-in;
3. envelope coverage is at least 80% at window end.

Anything less: the numbers are reported and no claim is made.

## Lane P — evaluability floor and read rule

The instrument is the shipped harness, run by the operator against their own
database (`eisv_ablation_matrix.py` + `eisv_skeptic_report.py`, per the
falsifiability section of `docs/REVIEWER_GUIDE.md`). The primary slice is
declared now, not chosen after looking: **scope=task, full-window, lead 0,
trusted anchor scope.**

- **Evaluability floor:** the harness's own minimums must be met on the
  primary slice, and the operator's window must contain outcome rows spanning
  at least two agents. Below the floor, lane P reports **"not evaluable at
  this fleet's volume"** — recorded plainly on #1607, not massaged.
- **Read rule:** the harness's self-assigned labels are reported verbatim. A
  "signal" claim requires the harness's selective p ≤ 0.05 on the primary
  slice. A point-estimate win without a cleared selective null is reported as
  the noise it is.
- **Class-composition honesty:** the operator's bad-class `outcome_type`
  composition is reported verbatim. If it is entirely task-negative (failed
  tasks and tests), the claim ceiling is rework prediction on that
  deployment. No composition of results under this protocol supports a
  "detects bad or misaligned agents" claim.

## Publication rule

Results — negative and inconclusive included, verbatim — are posted to #1607
within 14 days of each read, with the dated harness outputs attached as the
reproducible artifact. If recruitment has produced no dated enrollment record
by **2026-11-30** [operator decision: date], that dependency is posted to
#1607 as the standing result. Synthetic traffic is not a substitute and will
not be presented as one.

## What a full success may claim (ceiling)

"UNITARES was deployed and operated for four weeks by an independent operator
on their own infrastructure without maintainer code changes; instrumentation
coverage held above the registered floor; on that operator's own outcome
labels the shipped harness reported [its verbatim labels]." Nothing stronger.
The standing "single-operator deployment" disclosure changes to "two
deployments, one external" only after a lane-U verdict is earned.

## Known limitations, documented now

- n = 1 operator; whoever volunteers is by construction friendlier than the
  median adopter. This bounds the usability claim.
- The operator's fleet may be too small for lane P to be evaluable; the
  protocol prefers an honest "not evaluable" over a widened floor.
- Lane U is partially self-reported (time-to-first-check-in, friction log).
- One deployment cannot support causal claims regardless of outcome.

## A dependency this cohort may unlock, claimed by nothing here

The individuality v2 pre-registration failed in part because the eligible
behavioral-instrument population was three agents, where the leg-B exact
enumeration is arithmetically unpassable. An external fleet with five or more
genuinely distinct agents on one instrument would satisfy that composition
floor for the first time. Any future individuality test would still require a
changed measurement process per the v2 record; this protocol makes no
individuality claim and collects nothing toward one.

# Independent-operator validation cohort — protocol pre-registration v0

**Status: DRAFT.** The protocol registers at the merge commit of the PR
introducing this file, after an adversarial design review (three independent
passes: conceptual refutation, ground-truth verification against the live
system, and stranger-executability; material findings incorporated below).
Enrollment records live in the separate append-only file
`independent-operator-cohort-enrollments.md`, so the registered protocol text
never moves after merge. Amendments to this file after registration are
listed in the Amendment log at the bottom and change nothing retroactively.
Tracks [#1607](https://github.com/cirwel/unitares/issues/1607).

## Why this exists

The public evidence base is one maintainer-operated deployment. The frozen
2026-08-09 trusted-anchor matrix (PR #1603) classifies all 12 overall slices
`NON_DETECTION` after selection adjustment — a descriptive non-detection, not a
negative result or refutation. It establishes nothing about external adoption,
nothing about behavior under an operator who does not share the maintainer's
habits or database, and nothing causal. This protocol defines what one external
deployment can and cannot establish, before any outcome is read.

**Relationship to the outcome-grounding stop rule** (#1425,
`eisv-outcome-grounding-stop-rule-v0.md`): that rule owns the outcome
question for the maintainer deployment; its registered confirmatory read is
2026-12-01. This protocol does not reopen, anticipate, or substitute for it.
An external operator's labels are a different channel on a different
deployment; results are reported per-deployment and never pooled. Two
interlocks, frozen now: results here and there are never combined into one
claim, and **no lane-P result is published between 2026-11-15 and the
posting of the 2026-12-01 stop-rule read** — a scheduling embargo so neither
read can frame the other.

## Three claim lanes, separated by design

| Lane | Question | Status in this protocol |
|---|---|---|
| **U — operational usability** | Can an operator who is not the maintainer deploy, run, and keep the instrument healthy without maintainer intervention in the code? | **Primary.** |
| **P — deployment-scoped association** | On the operator's own outcome labels, do the harness's registered candidate features beat the shipped previous-outcome baseline, by the harness's own selective-inference discipline? | Secondary, gated on an evaluability floor. Called "association", not "validity": the lead-0 slice is contemporaneous, and only the lead-30 slice is predictive in the temporal sense. |
| **C — causal efficacy** | Did governance *prevent* anything? | **Not claimable from this design.** Observational deployment cannot separate verdict effects from selection; a causal claim requires an interventional design that does not exist. Recorded as out of scope, not deferred silently. Override counts are reported as totals only and are never cross-tabulated with outcomes; operator statements about usefulness, if quoted, carry an explicit "anecdote, not evidence" label. |

## Cohort definition and separation

- The operator is not the maintainer, does not share the maintainer's
  production database, and runs their own stack from the Tier-1 install
  contract (release-tagged `docker compose up`, per README Quickstart).
- Separation is separation of **data-generating processes**, not of
  analysts: the maintainer wrote the instrument, the harness, and every
  threshold here, and will draft the published posts. That limitation is
  disclosed in Known limitations, and the operator must review and approve
  each #1607 post about their deployment before it is published. (The
  testbed pre-registration's arms-length evaluator standard is stricter;
  this protocol does not meet it and says so rather than implying it.)
- Shared artifacts are harness outputs, the raw telemetry-health JSON
  bodies, and the friction log — not raw databases. An operator may opt in
  to sharing a de-identified analytic slice; without it the lane-P artifact
  is **attested, not independently reproducible**, and is described that
  way.

### Enrollment (two phases, recorded in the enrollments file by PR)

**Phase 1 — enrollment PR, merged before the window may start.** Records:
operator name or pseudonym; disclosed relationship to the maintainer and any
consideration received; fleet shape (agent count, client types, which agents
are cron-driven vs interactive); intended outcome producers; the release tag
and commit sha under test; the sha256 of
`scripts/analysis/eisv_ablation_matrix.py` and
`scripts/analysis/eisv_skeptic_report.py` at that tag (the lane-P read is
conditional on these hashes — a changed harness voids the read); the
`identity_id` of the Quickstart demo agent (see Demo traffic below); and a
publication consent that explicitly covers negative results and abandonment.

**Phase 1 also includes a plumbing check**, because the trusted-anchor
predicate is server-derived and no operator-facing switch declares a
producer trusted: the operator emits at least 3 outcomes through each
declared producer and confirms they land with
`verification_source='external_signal'`, an eligible `outcome_type`, and a
joinable prior-state snapshot. The counts are attached to the enrollment
record. **"Producers not wired" and "not enough volume" are distinct
published results** — a zero-eligible-row window after a passed plumbing
check is a volume result; without the check it would be an undiagnosable
wiring failure published under the wrong name.

**Enrollment validity minimums** (an enrollment that fails these is recorded
but does not start a cohort window): a real workload — agents doing declared
work that would run anyway, not a stack stood up solely to produce traffic —
and at least one declared producer passing the plumbing check.

**Phase 2 — window-start amendment.** After the operator's first qualifying
check-in (defined below), a follow-up PR records the actual window-start
timestamp. Enrollment intent is declared before the clock; the clock is
stamped from what actually happened.

### Demo traffic

`make demo` — step 3 of the Tier-1 install — emits real check-ins under a
demo identity. Two facts, verified in source at registration: the harness's
fixture filtering keys on identity metadata, and `--as-of` runs load no
identity metadata (`include_identity_metadata=as_of is None`), so **label-based
fixture filtering cannot fire on a frozen read**. The demo rows would enter
lane P silently. Therefore: the demo agent's `identity_id` is recorded at
enrollment, and the operator deletes that identity's rows from their
database after the install check and before the window-start check-in. The
lane-U "time to first check-in" metric is likewise defined against the first
**non-demo** check-in, so the Quickstart's own smoke test cannot satisfy it.

## Clocks, windows, and definitions

- **Qualifying check-in:** a `process_agent_update` call (or its
  `sync_state` alias) from a declared non-demo agent that receives a
  success response containing a decision. Calls refused because an agent is
  paused are counted and reported separately; they do not qualify for the
  operation-day criterion.
- **Day boundary:** UTC calendar days. Day 1 is the UTC date of the first
  qualifying check-in, which must postdate the phase-1 enrollment merge.
- **Lane-U window:** 28 UTC days from day 1.
- **Lane-P analytic population:** all rows from day 1 through the read
  instant at day 58 (window end + 30 days of outcome maturation). Declared
  as 58 days because the harness's `--windows` flag counts integer days back
  from `--as-of` and cannot express "states from days 1–28 only"; the
  fresh database contains nothing before day 1, so `--windows 58` covers
  exactly this population, including post-window operation days 29–58. The
  30-day maturation figure is arbitrary, frozen at registration, not derived
  from measured label latency; the plumbing check records observed producer
  latency so the next protocol version can derive it.
- **Extension:** at most one, at most 14 additional days, declared before
  day 21, only for a reason on this list: infrastructure outage, operator
  absence, upstream service outage. The extension affects only how long
  observation continues. **Denominators do not move**: the operation-day
  criterion stays out of 28, and the lane-P analytic population stays days
  1–58 as originally computed. This kills the two known gaming paths
  (stretching the denominator after a weak start; growing lane-P volume
  after seeing the day-7 numbers).

## Lane U — metrics and verdict rule

Collected continuously (usability observation is not outcome peeking):

- Wall-clock time from clone to first qualifying check-in (self-reported).
- **Friction log**: `friction-log.md` in the operator's repository fork,
  timestamped rows — UTC date, blocker description, severity
  (`blocked` / `workaround-found` / `cosmetic`), resolution. Attached
  verbatim to every #1607 read post.
- **Maintainer-support log**: every maintainer interaction about this
  deployment, with timestamp and approximate minutes. Support given is an
  uncontrolled treatment a real adopter would not receive; it is measured
  rather than pretended away.
- Operation days, refusal counts, operator overrides and manual
  interventions (totals only).
- Instrumentation health: the **raw JSON body** of
  `GET /v1/eisv/telemetry-health?days=7` captured on day 7 and
  `?days=28` captured within one hour after the end of day 28 (UTC),
  attached to the #1607 posts — a live endpoint's number rolls forward
  daily, so the artifact is the body, not a transcribed number. The
  verdict fields are `summary.behavioral_primary_rate` and
  `summary.contract_violation_rate`. (`summary.coverage_rate` is also
  reported but is **not** verdict-bearing: a fresh install emits
  envelope-schema rows natively, so coverage sits near 100% with zero
  operator effort and cannot discriminate.)

The claim "independently deployable" is earned only if **all** of:

1. No changes to this repository were made **in response to a blocker this
   operator raised** during setup or the window. Unrelated commits landing
   in the actively developed repo do not count against this. Questions are
   allowed and are logged as friction and support minutes.
2. At least 14 of the 28 window days (50%) have at least one qualifying
   check-in. The enrollment record's cron declaration is published with
   this number, along with the interactive fraction — a cron satisfies the
   criterion and the reader gets to see that it did.
3. `summary.behavioral_primary_rate ≥ 0.5` and
   `summary.contract_violation_rate = 0` in the day-28 capture.
4. No friction-log entry with severity `blocked` is unresolved at window
   end, and total logged maintainer support is at most 120 minutes.

Anything less: the numbers are reported and no claim is made. Abandonment
mid-window is a usability **result**, reported with whatever artifacts
exist; the publication consent signed at enrollment covers this case, and
21 days of operator silence is recorded and published as abandonment.

Lane U is read at day 7 and at window end (day 28). The day-7 read is
operational monitoring; nothing in the protocol may be changed in response
to it except an extension declaration per the rules above.

## Lane P — instrument, floor, and read rule

**Instrument.** The confirmatory number comes from
`scripts/analysis/eisv_ablation_matrix.py` only — it is the only script
that computes a selective null. `eisv_skeptic_report.py` output may be
attached as descriptive context and gates nothing. Both scripts are frozen
by the sha256 recorded at enrollment.

**The registered command**, run once by the operator against their own
database with `--as-of` set to the day-58 instant, at three seeds:

```bash
python3 scripts/analysis/eisv_ablation_matrix.py \
  --read-protocol registered \
  --read-id operator-<participant>-day58-seed-<s> \
  --not-before <day-58 UTC instant> \
  --scopes task --windows 58 --leads 0,30 \
  --anchor-scope trusted --as-of <day-58 UTC instant> \
  --selective-null-resamples 400 --uncertainty-resamples 2000 \
  --uncertainty-seed <s>        # s in {0, 1, 2}
```

Each seed has its own predeclared read ID and atomic receipt. The not-before and
as-of instants are identical. Reusing an ID or moving either boundary after an
access is a protocol deviation that must be disclosed; the three registered
seeds are planned reads, not permission for additional exploratory slices.

The primary slice is **scope=task, window=58, lead=30** (the temporally
separated one); the lead-0 slice is reported as contemporaneous context.
The candidate family is the harness's registered candidate set on that one
slice; the number of candidates actually fitted (some drop below the
harness's per-feature row minimum) is reported with the result. No other
slice, stratum, or candidate may be published as a confirmatory claim.

**Evaluability floor.** Two layers, and they are different kinds of thing:

- *Harness-enforced* (hardcoded in the scripts at the frozen sha): at least
  100 trusted outcomes and 10 bad outcomes in the window, at least 30 rows
  per fitted feature, and at least 3 permutable clusters to form a
  selective null at all.
- *Protocol-added, checked by hand because the harness does not compute
  them*: at least **30 permutable bad clusters** after collapsing replicate
  identities (identities sharing one host, role, and process family count
  once — the reviewer-guide rule; the individuality v2 post-mortem is what
  happens when replicates are counted as agents), spanning at least two
  effective independent clusters. The 30-cluster floor is **deliberately
  weaker than the #1425 stop rule's 150** and the protocol says so here:
  #1425's floor gates closing the fleet outcome-grounding question; this
  read is a descriptive, deployment-scoped association and claims nothing
  fleet-level. A reader comparing bars is comparing different questions.

Below either layer, lane P publishes **"below the registered evaluability
floor"** with the observed counts. That sentence is the protocol's, not the
harness's; the harness's own low-volume strings are `INCONCLUSIVE:` variants
and each maps to that same published sentence plus the harness string
verbatim.

**Read rule.**

- The harness's self-assigned labels are reported verbatim, always
  accompanied by the provenance tuple: anchor scope, cutoff, outcome and
  bad-row counts, permutable clusters, agents, per-`outcome_type` counts,
  selected delta, null p95, and selective p.
- A "signal" sentence requires **all** of: selective p ≤ 0.05 on the
  primary slice at all three seeds, and the winning candidate family
  identical at lead 0 and lead 30 (the stop rule's family-consistency
  condition, adopted verbatim — it exists to stop argmax noise-mining).
- **If the selective null was not formed** (fewer than 3 permutable
  clusters; the harness prints the conclusion *unqualified* in that case —
  verified in source at registration), the read publishes as "below the
  registered evaluability floor" regardless of the printed label. This is
  the single most extractable over-claim in the design and this clause is
  its lock.
- Class-composition honesty: per-`outcome_type` **counts** (not
  proportions) are published. If the bad class is entirely task-negative,
  the ceiling is rework-association on this deployment. **A mixed
  composition does not raise the ceiling**: additional label types broaden
  what "rework/deviation" covers, and no composition under this protocol
  supports a "detects bad or misaligned agents" claim. A
  non-task-negative `outcome_type` may be named in a claim sentence only
  if it has at least 10 rows.
- Lane P may be read when lane U failed, but its publication must lead
  with the lane-U result, and the claim ceiling is unchanged.

## Publication rule

Results — negative and inconclusive included, verbatim — are posted to
#1607 within 14 days of each read (subject to the #1425 embargo above),
with the dated harness outputs and raw telemetry-health bodies attached.
The operator reviews and approves each post about their deployment before
it goes up. If recruitment has produced no valid phase-1 enrollment by
**2026-11-30** (maintainer-set date, firm), that dependency is posted to
#1607 as the standing result. Synthetic traffic is not a substitute and
will not be presented as one.

## What may be claimed (ceilings, written now)

Lane U earned: *"UNITARES <tag>@<sha> was deployed and operated for four
weeks by an independent operator on their own infrastructure; no repository
changes were made in response to their blockers; behavioral instrumentation
held above the registered floor; total maintainer support was <N> minutes
(usability only; no outcome evidence)."*

Lane P, three branches, verbatim with the provenance tuple filled in:

- Below floor: *"The lane-P read fell below the registered evaluability
  floor (<counts>); no association claim is made."*
- No signal: *"On this operator's own labels the registered slice reported
  <verbatim label>; the selective null was not cleared (<tuple>)."*
- Signal: *"On this operator's own labels the registered slice cleared its
  selective null at all three seeds with a consistent candidate family
  (<tuple>). This is a deployment-scoped association on task-negative
  labels, not fleet-level validation and not misalignment detection."*

**Disclosure swap, scoped:** a lane-U pass updates only the
deployment-count statements (README "single-operator deployment" and the
reviewer-guide equivalent) to *"two deployments, one external (usability
only; no outcome evidence)"*. It does **not** touch the self-hosted
by-design statements or the threat-model statements in SECURITY.md and the
architecture docs — a usability result carries no evidence about either.

## Threshold provenance

| Threshold | Basis |
|---|---|
| 28-day window; 30-day maturation; 14-day publication; day-21 extension gate; 14-day extension cap | Arbitrary, frozen at registration, not tuned against observed data. |
| 50% operation days; behavioral rate ≥ 0.5; 120 support minutes; 30 bad clusters; 10-row minimum per named outcome type; 21-day silence rule | Arbitrary, frozen at registration. For reference, so a reader can see where the floors sit against the only deployment with known values: the maintainer deployment measured `behavioral_primary_rate` ≈ 0.97 and `coverage_rate` ≈ 0.78 on 2026-08-21. The maintainer knows his own deployment's values for every metric here; that is a forking-path risk this table discloses rather than denies. |
| contract_violation_rate = 0 | The fresh-install expectation; any violation on a release-tag install is a defect, not noise. |
| Selective p ≤ 0.05; 400 selective-null resamples; 2000 uncertainty resamples; 3 seeds | Adopted from the #1425 stop rule's registered invocation and its recorded seed-sensitivity observations. |
| Harness-internal minimums (100/10/30/3) | Quoted from the frozen scripts; not this protocol's choices. |

## Known limitations, documented now

- n = 1 operator; whoever volunteers is by construction friendlier than the
  median adopter, and the enrollment disclosure (relationship,
  consideration) bounds but does not remove this.
- Analyst independence is **not** achieved: the maintainer built the
  instrument and drafts the publications; the operator's sign-off is a
  check, not independence. The testbed pre-registration's arms-length
  standard remains the stricter bar this cohort does not meet.
- Maintainer support during the window is an uncontrolled treatment; it is
  logged and budgeted, not eliminated.
- The operator's fleet may sit below the lane-P floor; the protocol prefers
  an honest "below floor" over a widened one.
- Lane U is partially self-reported (time-to-first-check-in, friction log).
- One deployment cannot support causal claims regardless of outcome.
- Raw behavioral EISV states are collected incidentally by any running
  deployment; nothing from this cohort may be analysed for individuality
  claims under this registration (any such test requires its own
  pre-registration with a changed measurement process, per the v2 record).

## Amendment log

- (none — v0 as registered)

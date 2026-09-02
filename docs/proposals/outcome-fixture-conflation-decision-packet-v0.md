# Decision packet: what the registered 2026-12-01 read does with outcome rows the fixture predicate excludes for a scraped confidence

**Status:** decision packet, raised 2026-09-02; **selected R1 on 2026-09-02**
under the operator's delegation (see *Selection*). No code in the packet PR;
the implementation is a follow-up PR. Shape follows the contract in
[`operator-decision-packet-v0.md`](operator-decision-packet-v0.md).
**Class:** authority (what a pre-registered instrument counts as evidence).
**Reversibility:** `one_way_door` for branches R2, R3 and R4 (a disclosed
protocol deviation cannot be un-disclosed); `reversible` for R1 and for the
engineering items.
**Blast radius:** `fleet`. Two pre-registered contracts are touched (the
2026-12-01 read and the legacy-coherence dependency shadow), and the README
claim table cites the frozen slice the December read is read against.
**Raised by:** `da6974df-ca2a-4119-b26d-2311a3151d32` (a Claude Code session,
2026-09-02).
**Review provenance:** the same day, a three-seat council (code review, an
adversarial pass, a live re-verification of every number against the
deployment database) and a Codex review. The recommendation below is sourced
from the adversarial seat's finding that the stop rule already supplies the
remedy, and from the Codex finding that disclosure does not preserve
registered standing on a condition whose inputs the author had inspected. The
author's first lean (retroactive correction) was withdrawn on that review. The
full record is on the PR that carries this file.
**Default if silent:** the December read runs as registered with the current
predicate. The inventory keeps reporting the excluded rows as attrition. No
forecast about condition 3 is made here; the stop rule forbids refreshing that
diagnostic with live data.

## Selection, 2026-09-02

The operator delegated the selection to the working agent with one criterion,
"proceed on your own accord, best for federation" (2026-09-02). Under that
criterion the selection is **R1**. E1 and E2 are implemented in the follow-up
PR; the pre-declared sensitivity cohort is recorded in the stop-rule document.

One refinement was proposed with it and withdrawn on review: flipping the
shared default to the corrected rule for every instrument that is not a
protocol-bound read, on the argument that an independent operator whose
producers send no confidence would otherwise lose their outcome channel. The
review found that this reversed E1 as this packet specifies it and the
governed reviewer's condition, changed a value the coherence-shadow contract
declares fixed after its zero had been inspected, and would have moved every
instrument's output on a deploy with no version marker. The shipped shape
therefore keeps `registered` as the shared default and makes `corrected`
opt-in (`--fixture-rule corrected`, named in every report and receipt). The
federation gain that survives is the mechanism itself: the switch, the
writer's reasons, the attrition counters, and a registered read that cannot
be moved by any default.

Left for the operator, as a follow-up and not a blocker: whether
non-protocol instruments should default to `corrected`, and whether the
independent-operator cohort protocol's plumbing check (which requires
`calibration_excluded` to be false on posted rows) should be restated in terms
of validation visibility. Also still the operator's judgment: whether a
corrected instrument counts as the reopening premise if condition 3 fails.

## The question, as a fork

An outcome row for which the server had to scrape a confidence (the caller
sent none, no registered prediction resolved, and a value was found in the
agent's previous check-in or in the audit trail) is stamped
`calibration_excluded`, so calibration does not train on it. That flag is
also, deliberately and test-locked, a standalone fixture marker, so the
discrimination instruments drop the row as fixture traffic. The producers that
post trusted `external_signal` outcomes send no confidence, so every
instrument-visible trusted row written after the frozen 2026-08-09 cutoff has
been stamped and dropped (951 of 951), and 182 of the 449 written between
2026-05-01 and the cutoff were, all of them after the stamping began on
2026-08-01. Rows posted with a confidence, rows recorded through
`record_result` with a resolvable registered prediction (the endpoint has no
prediction path: it ignores `prediction_id` by design), and rows for which
nothing could be scraped are not stamped.

Two engineering items follow that need no operator decision and are stated
below. The decision that belongs to the operator is one question:

**What does the registered 2026-12-01 read do with the rows the current
predicate excludes?**

| # | Branch | Consequence | Cost |
|---|---|---|---|
| R1 | **Run as registered, and pre-declare the corrected cohort as a sensitivity analysis reported alongside.** The registered predicate decides. The corrected cohort is declared now and computed at the read by running the registered command a second time with E1's classification switch on, reported next to the registered result without authority. If condition 3 fails, and if the operator accepts a corrected instrument and producer contract as the "materially different measurement process" the stop rule requires, that is the premise for reopening; whether it qualifies is the operator's judgment (see R4 and the first disconfirmer). | No deviation. Registered standing intact. The operator sees both numbers at the read, before any authority question. The reopening path, if accepted, is the one the registration itself provides. | The rows written between 2026-08-01 and the read count only in the sensitivity column. |
| R2 | **Correct prospectively from a declared date.** From the disclosure date forward the registered read applies the corrected predicate; earlier rows stay as registered. Disclosed in the stop-rule doc as a third deviation. | Support accrues under the corrected semantics from the disclosure date. Needs E1's switch to carry the declared date so earlier rows keep the registered classification. | A deliberate change to the sampling frame after the analyst saw the attrition, even if only prospective; the December report carries it as a deviation, and comparability with the frozen 2026-08-09 slice is lost for rows after the date. |
| R3 | **Correct retroactively.** As R2, and the rows written since 2026-08-01 count too. | The largest support gain. | Support is a PASS condition and this packet inspected the support counts before proposing the change, so the read cannot keep its registered standing on condition 3. The claim that no interim read ever counted these rows cannot be established: the interim watchdog and dogfood-guard runs after the cutoff came from a separate checkout whose code version per run is not recorded. Comparability with the frozen slice cited by README and the paper is lost. |
| R4 | **Withdraw and re-register.** Close the December read as registered and register a new one under the corrected instrument. | Clean standing for the new read. | Whether an instrument correction is the "new premise" the stop rule requires is itself an operator call, and the read date moves. |

Rejected, and recorded so nobody rediscovers it: the env escape hatch
`UNITARES_CALIBRATION_ALLOW_SCRAPED_CONFIDENCE=1`. It makes scraped rows
visible by making them calibration-eligible again, the defect PR #1445
removed. It was never one of the branches above.

## Recommendation (non-binding; provenance stated in the header)

**R1.** The stop rule already forecloses the harm this packet was first written
to prevent: a condition-3 failure must be reported as closure for insufficient
eligible evidence, never as a measured null, and reopening needs a materially
different measurement process. Whether a corrected instrument and producer
contract count as that process is the operator's judgment, so R1's reopening
path is conditional on it; R1 still costs nothing if the answer is no, because
the registered read runs unchanged either way. R1 gives the operator the corrected number at the
read without spending the registration, and the general measurement-authority
rules in `CLAUDE.md` do not apply here: that section exempts pre-registered
stop rules by name. R2 to R4 remain available with their costs stated.

## Engineering items, proceeding under any branch once the operator says go

- **E1: distinct reasons at write, one gate for calibration.**
  `calibration_excluded` is a three-way OR at the writer
  (`src/mcp_handlers/observability/outcome_events.py:426-430`): explicit
  fixture, `shadow_write`, or scraped confidence. The writer should stamp the
  cause under distinct keys and keep `calibration_excluded` as the single
  calibration gate. Because the instruments run from `master` and the
  registered command has no frozen-predicate mode, the corrected
  classification ships behind a switch in the shared wrapper the instruments
  call (`--fixture-rule registered|corrected`, default `registered`). A
  registered read rejects any other value, the frozen prospective-cohort
  contract pins the registered reading at its call site, and `corrected` is
  opt-in everywhere else (see *Selection*). R1's sensitivity cohort is the registered
  command run again with the switch set to `corrected`, and R2's prospective
  date would be a parameter of the same switch. Without the pin, E1 would
  change the registered cohort silently, which is R3 without the
  decision. With the switch on, the fixture predicate keys on fixture causes only, so
  a scraped-only row is validation-visible while `shadow_write` rows (76 in the
  21-day window; the Phase-5 evidence quarantine) stay excluded. Rows already
  written are classified by `prediction_source`, which the writer records:
  `prev_confidence_fallback` and `audit_trail_fallback` are scraped. The change
  touches three flag sets, not one (`src/grounding/outcome_anchors.py:57-63`,
  `outcome_events.py:59-67`, `scripts/analysis/outcome_inventory.py:129-137`),
  the delegating wrapper `outcome_inventory.is_controlled_validation_fixture`
  that every instrument actually calls, and the tests that lock the current
  meaning (`tests/test_outcome_inventory.py:303`,
  `tests/test_outcome_events_synthetic_exclusion.py:43`,
  `tests/test_outcome_events_scraped_confidence_exclusion.py`). It does not
  change item 2 of the legacy-coherence dependency shadow's fixed defaults
  (`legacy-coherence-dependency-ablation-v0.md`, a prospective contract
  registered on 2026-08-12 that has accrued no rows): that read keeps the
  registered rule by default, and a corrected run is a deviation that document
  says must be disclosed if cited. A narrower variant,
  scoping the admission to `detail.recorded_via = "harness_outcome_endpoint"`
  rows, is possible but admits only one producer path.
- **E2: the producer contract, stated honestly.** Today's contract (#1790 fix
  3, `src/http_routes/substrate.py:298-304`) tells producers to supply
  `confidence` or a registry-bound `prediction_id`. For a producer with no
  prediction of its own, a pytest hook or a finding-resolution poster, binding
  the row to whatever prediction the agent last registered is the laundering
  PR #1445 removed, relabelled `registry`. The repo's own Phase-5 evidence
  path shows the shape of the hazard: it mints a fresh tactical prediction per
  unbound evidence row from the current check-in confidence, after the
  evidence exists, and passes that confidence explicitly
  (`src/mcp_handlers/updates/phases.py:239-263`, `:2324-2328`, locked by
  `tests/test_phases_phase5_evidence.py`), which is why those rows are held
  behind `shadow_write` (`outcome_events.py:403-408`).
  So the producer-side change is not "bind": producers without a prediction
  keep sending none, and E1 makes their rows visible without making them
  train calibration. The endpoint advertised `prediction_id` in its warning
  text but never forwarded it (`substrate.py:365-377` forwards `confidence`
  only); on review that was kept as the deliberate contract, because the
  endpoint takes an operator-asserted `agent_uuid` with no work correlation
  and a forwarded id could bind any open prediction of that agent to an
  unrelated outcome. The text now says so. The warning is only seen by a
  caller that reads the response. The producer surface is every caller
  that posts through `/v1/harness/outcome` without a confidence (the endpoint
  ignores `prediction_id`, so a supplied id does not change this) and every
  `record_result` caller with neither a confidence nor a resolvable registered
  prediction: in the 21-day window, 845 stamped
  `external_signal` rows came through `/v1/harness/outcome` (the machine-local
  `~/scripts/hooks/outcome-tracker.sh` and `agents/watcher/agent.py`'s
  resolution poster) and 5 through `record_result`. Issue #1790 was filed from
  an independent operator's fresh compose stack, so the trap is general.

## Finding

### Mechanism, verified in the tree at `origin/master` `24cfaa32`

| Step | Where | Since |
|---|---|---|
| The server scrapes a confidence (previous check-in, then audit trail) when the caller sent none and no registered prediction resolved ⇒ `prediction_source` is `prev_confidence_fallback` or `audit_trail_fallback` ⇒ `calibration_excluded = true`. A row for which nothing can be scraped is not stamped. | `outcome_events.py:338-372`, `:420-441` | PR #1445, 2026-08-01 |
| `calibration_excluded` is a standalone member of `_CONTROLLED_FIXTURE_FLAGS` in the reader used by the analysis scripts | `scripts/analysis/outcome_inventory.py:129-137` | commit `6a290e09`, 2026-06-16 |
| … and in the structural predicate the dashboard and the wrapper use | `src/grounding/outcome_anchors.py:57-63`, `:95` | PR #1562, 2026-08-10 |
| The discrimination instruments call `outcome_inventory.is_controlled_validation_fixture(detail, include_declared_purpose=False)`, which delegates to the structural predicate, and drop the row | `eisv_ablation_matrix.py:562`, `eisv_skeptic_report.py:1710`, `legacy_coherence_dependency_shadow.py:538` | |
| The inventory and the telemetry-health dashboard apply the same predicate but report the excluded rows as their own attrition bucket | `outcome_inventory.py:661-719`, `src/eisv_telemetry_health.py:643` | PR #1793, 2026-08-21 |
| `prospective_prediction_cohort.py` also requires a registry-bound prediction, so E1 alone would not admit these rows there | `prospective_prediction_cohort.py:232-237` | |

Issue #1790 (2026-08-21, from an independent operator's stack) described the
conflation and proposed three observability fixes: a write-time warning, an
attrition bucket, and producer documentation. PR #1793 shipped all three and
deliberately left row selection unchanged. The current state is therefore a
documented, warned and counted contract, not an unnoticed bug; what this
packet adds is the consequence for the two pre-registered contracts and the
question of what the registered read does about it.

The frozen 2026-08-09 read was generated from commit `5f050f04`, which
contains the structural rule, so that read already applied it.

### Measurement, deployment database, 2026-09-02

All counts below are on the **instrument-visible population**: rows with
`verification_source = 'external_signal'` that satisfy the shared trusted-anchor
predicate (a joinable EISV snapshot; `anchored_outcomes_predicate` in
`src/grounding/outcome_anchors.py`). The registered read layers its outcome
scopes, windows and harness-lane exclusion on top, so these counts bound the
affected population and are not the registered slice. Every window is bounded
by explicit UTC instants so the queries reproduce.

| Population | Rows | Stamped `calibration_excluded` | `is_bad` rows | Agents |
|---|---|---|---|---|
| Written before the frozen cutoff, 2026-05-01 to 2026-08-09T20:00Z | 449 | 182 (all after 2026-08-01) | 91 | |
| Written after the cutoff, 2026-08-09T20:00Z to 2026-09-02T18:00Z | 951 | 951 | 151 | 99 |
| 21-day window, 2026-08-12T18:00Z to 2026-09-02T18:00Z | 816 | 816 | 131 | 88 |

| 21-day window by outcome type | Rows | `is_bad` |
|---|---|---|
| `test_passed` | 677 | 0 |
| `test_failed` | 118 | 118 |
| `watcher_finding_dismissed` | 16 | 13 |
| `sentinel_finding_confirmed` | 5 | 0 |

| Instrument, 21-day window | Result |
|---|---|
| Structural fixture predicate applied to the 816 rows from the tree's own code | 816 excluded, 0 kept |
| Key that flips the verdict when removed, checked on every one of the 816 rows | `calibration_excluded`, and only that key |
| `prediction_source` on the 816 rows | 696 `prev_confidence_fallback`, 120 `audit_trail_fallback`; no `argument`, no `registry` |
| Explicit fixture flags or controlled test names on those rows | 0 |
| `outcome_inventory.py --window-days 21` | `fixture_rows_excluded: 2664`, `calibration_excluded_only: 1553`, strict outcomes visible: 5 |

**Bad clusters are not estimated here.** Condition 3 counts `(agent,
prior-state snapshot)` permutation blocks, and the stop rule forbids
refreshing the condition-3 feasibility diagnostic with live data before the
read. The `is_bad` row counts above are inventory, which the falsification
audit permits; a cluster estimate for either cohort is deliberately withheld.

**Which of the four states this is.** The producers ran and the rows exist
(not state 1 or 2). The instruments' predicate discards them before counting:
state 3, *not recorded*. It is not state 4. A zero from the discrimination
instruments over this period carries no information about label supply and
must not be cited as such, which the stop rule's own reporting rule already
guarantees.

### Interim access made while preparing this packet, disclosed

On 2026-09-02 the raising session ran
`scripts/analysis/legacy_coherence_dependency_shadow.py` three times against
the live database: the default 365-day `task` scope, `--window-days 21` in
`task` scope, and `--window-days 21 --scope strict`. That script reads the
matching live outcome rows from the database and then applies the fixture
predicate; it computes discrimination statistics and carries no read-protocol
guard. All three runs returned 0 eligible outcomes after that filter, so no
discrimination result was computed or exposed.
`outcome_inventory.py` was also run, which the audit permits. The same note is
appended to the stop-rule document's deviation record in this PR.

### What it reaches

- **Legacy-coherence dependency shadow** (prospective contract of 2026-08-12).
  The envelope is emitting on 6,762 of 8,215 state rows in the last 7 days and
  the read joins 0 outcomes under the current predicate. Its fixed default
  "controlled validation fixtures excluded" was registered two days after the
  flag entered the structural set, so E1 changes that contract too. Its
  non-inferiority margin inherits the withdrawn −0.05 bound; out of scope here,
  noted so nobody reads a future PASS against it as validated.
- **The registered 2026-12-01 read.** Cohort: `--anchor-scope trusted
  --exclude-harness-lanes beam` with the fixture predicate layered on top. Every
  instrument-visible trusted row after the cutoff is stamped, so under the
  current predicate condition 3 is evaluated on rows written before 2026-08-01
  plus whatever unstamped rows arrive later (rows posted with a confidence or
  a resolvable prediction, or for which nothing could be scraped). What that means for the verdict
  is not forecast here.
- **Inventory and telemetry health** report the attrition honestly and count 5
  strict outcomes in 21 days.
- **The revenue-engine pilot.** If its label ingestion posts `external_signal`
  rows without a confidence, those rows are dropped by the same instruments.
  Not verified here; the pilot owners should check before the first episode.
- **The README claim table** cites the frozen 2026-08-09 read for predictive
  lift; that row does not change under any branch. R2 and R3 change what the
  December read is comparable to.

## Disconfirmers, aimed at the recommendation

- If the stop rule's reopening clause does not accept a corrected instrument
  and producer contract as a "materially different measurement process", R1's
  exit closes and the fork is really between R2 and R4.
- If an interim automation run after the cutoff counted these rows, R3 is
  post-hoc cohort steering. This cannot be cleared from the retained artifacts,
  which is why R3's cost column says so rather than asserting the negative.
- If the 816 rows carried caller confidences after all, `prediction_source`
  would show `argument` or `registry`. It shows neither.
- If the rows were genuine fixtures, they would carry the explicit fixture
  flags or the controlled test names. They carry neither; their `detail`
  holds the pytest command lines of agent worktrees working on this repository.

## Evidence

Issue #1790. PRs #1445, #1562, #1793, #1831, #1855; commit `6a290e09`. Docs:
`eisv-outcome-grounding-stop-rule-v0.md` (the gate, its two 2026-08-23
disclosures, the reopening clause, the feasibility-diagnostic prohibition),
`docs/operations/eisv-ablation-frozen-2026-08-09.md`,
`docs/ontology/falsification-design-system-audit-2026-08-23.md`,
`legacy-coherence-dependency-ablation-v0.md`, README "Current claim status".

Queries (read-only; each bounded by explicit instants; `is_bad` and stamping
are inventory columns):

```sql
-- instrument-visible trusted-anchor rows after the frozen cutoff
SELECT count(*) AS rows_,
       count(*) FILTER (WHERE (detail->>'calibration_excluded')::boolean IS TRUE) AS stamped,
       count(*) FILTER (WHERE is_bad) AS bad,
       count(DISTINCT agent_id) AS agents
FROM audit.outcome_events o
WHERE o.ts >= '2026-08-09T20:00:00Z' AND o.ts < '2026-09-02T18:00:00Z'
  AND o.verification_source = 'external_signal'
  AND o.eisv_e IS NOT NULL
  AND coalesce((o.detail->>'snapshot_missing')::boolean, false) = false;
-- before the cutoff: replace the bounds with '2026-05-01' and '2026-08-09T20:00:00Z'
-- 21-day window: '2026-08-12T18:00:00Z' and '2026-09-02T18:00:00Z'

-- prediction_source on the 21-day window
SELECT detail->>'prediction_source', count(*)
FROM audit.outcome_events o
WHERE o.ts >= '2026-08-12T18:00:00Z' AND o.ts < '2026-09-02T18:00:00Z'
  AND o.verification_source = 'external_signal'
  AND o.eisv_e IS NOT NULL
  AND coalesce((o.detail->>'snapshot_missing')::boolean, false) = false
GROUP BY 1;
```

The predicate test fetched the 21-day rows with `asyncpg` and applied
`src.grounding.outcome_anchors.is_structurally_controlled_fixture` from the
tree to each row's `detail`, then removed keys one at a time from every row to
find the key that flips the verdict. The council's live re-verification
repeated it independently and reproduced the 816 and the single key.

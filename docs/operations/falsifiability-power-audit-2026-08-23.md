# What the falsifiability harness could have detected — 2026-08-23

**Status:** Corrected instrument characterisation. Measures the *harness*, not
the deployment. No production data was read and no database was queried. The
power table originally published here is **withdrawn**: the synthetic producer
reused cluster IDs as outcome row IDs, corrupting candidate/baseline pairing.

**Governing framework:** inference status for EISV falsification claims is set
by [`../ontology/falsification-inference-containment-2026-08-22.md`](../ontology/falsification-inference-containment-2026-08-22.md),
which requires four questions answered before any test may earn `REFUTED`. This
audit concerns its question 3 — *did the observed sample have independent units
and adequate power?* — on the frozen 2026-08-09 read. The frozen transcription
omitted the total permutation-cluster count and this audit's first power probe
was defective, so that question cannot be answered from the preserved evidence.
That is sufficient to keep the read a non-detection rather than a refutation.

**Scope guard:** this changes nothing about the pre-registered 2026-12-01
confirmatory read in
[`../proposals/eisv-outcome-grounding-stop-rule-v0.md`](../proposals/eisv-outcome-grounding-stop-rule-v0.md)
— not its date, its cutoff, its four PASS conditions, or its kill criterion. It
supplies a corrected procedure that read will need in order to be reported
honestly. It does not supply a valid historical power number for the frozen
slice.

## Why this exists

`eisv_ablation_matrix.py` answers one question: did the selected candidate
separate from its best-of-candidates null? When the answer is `NON_DETECTION`, two
very different worlds produce that same word:

1. prior state carries no association with outcome, or
2. it carries one, and this cohort is too small for this instrument to see it.

Nothing in the harness, and nothing in any document citing it, distinguished
them. A minimum-detectable-lift estimate did exist — the `≈ 0.05 AUC` figure —
and it was **withdrawn** on 2026-08-17 for using the contaminated
`--anchor-scope all` cohort. It was never replaced. From that date until this
audit, every published statement about EISV's predictive lift rested on a
non-detection whose power was unmeasured.

That gap has a direction. It is the mirror image of the failure `AGENTS.md`
already forbids under *Measurement authority* — there, a zero must not retire a
capability; here, a non-rejection must not become a ceiling. Both mistake "we
did not see it" for "it is not there".

## Method

`scripts/analysis/ablation_power_probe.py` plants an association of known
strength into synthetic cohorts with requested row, class, cluster, and agent
counts, then runs **the same** `build_matrix_row` /
`estimate_selective_null` machinery the frozen run used. Power is the fraction
of all requested trials reaching `selective p <= 0.05`; an unscorable trial is a
failed detection and remains in the denominator.

The latent is drawn per `(agent, prior-state snapshot)` cluster, preserving the
important fact that prior state is constant inside a cluster. The simulation
does **not** recover the real cluster-size distribution. Most prior-state
candidates receive the same clean signal, with no measurement noise,
missingness, provenance drift, enforcement effects, or alternative outcome
causes. Treat it as an optimistic scenario, not a theorem-level upper bound.

The original producer set `row_key=f"cluster-{cluster_id}"`. Production rows set
`row_key` from unique `outcome_id`. The paired scorer indexes the baseline by
`row_key`, so duplicate synthetic keys overwrote rows and mismatched or dropped
candidate/baseline pairs. The corrected producer keeps cluster identity in
`prior_measurement_id` and gives every synthetic outcome a unique row key.

```bash
python3 scripts/analysis/ablation_power_probe.py \
  --trials 30 --resamples 200 --rows 224 --bad 53 --clusters 70 --agents 16
```

The `70` cluster count above is the assumption used by the original audit, not a
recovered property of the frozen read. The frozen transcription preserved 28–29
*bad* clusters but omitted total `Null clusters`; those are different counts.
The CLI now requires all four shape arguments so an unknown count cannot be
silently inherited as a default.

## Results

This is the corrected re-run of the same **assumed 70-cluster scenario**: 30
trials per effect size, 200 permutations per trial, alpha = 0.05. All 30 trials
were scorable at every effect. Wilson intervals are reported because 30 trials
cannot support precise power claims.

| Planted beta | Scorable / requested | Detections / requested | True AUC | Baseline AUC | Median AUC delta | Null max median | Null max p95 | Median selective p | **Power (95% Wilson CI)** |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.00 | 30 / 30 | 2 / 30 | 0.510 | 0.515 | 0.023 | 0.006 | 0.140 | 0.483 | **0.07 [0.02, 0.21]** |
| 0.25 | 30 / 30 | 5 / 30 | 0.568 | 0.514 | 0.065 | 0.018 | 0.141 | 0.259 | **0.17 [0.07, 0.34]** |
| 0.50 | 30 / 30 | 12 / 30 | 0.636 | 0.507 | 0.116 | 0.017 | 0.148 | 0.090 | **0.40 [0.25, 0.58]** |
| 0.75 | 30 / 30 | 21 / 30 | 0.707 | 0.512 | 0.166 | 0.017 | 0.149 | 0.017 | **0.70 [0.52, 0.83]** |
| 1.00 | 30 / 30 | 21 / 30 | 0.737 | 0.535 | 0.156 | 0.017 | 0.135 | 0.017 | **0.70 [0.52, 0.83]** |
| 1.50 | 30 / 30 | 29 / 30 | 0.821 | 0.635 | 0.167 | -0.007 | 0.083 | 0.005 | **0.97 [0.83, 0.99]** |

The `beta = 0` interval includes the nominal 0.05 rate. That is compatible with
the type-I target in this small simulation; it is not enough to certify the
selective null across other cohort geometries or data-generating processes.

## What the numbers say

- In this assumed geometry, the weak planted signal (median AUC ≈ 0.57) was
  detected in 5 of 30 trials: 0.17 power, with a wide 0.07–0.34 interval. That
  remains inadequate for a strong negative inference, but the prior 0.03/"blind"
  claim was an artifact of corrupted pairing.
- The two middle-high scenarios (AUC ≈ 0.71–0.74) each produced 0.70 power, with
  intervals spanning 0.52–0.83. The AUC ≈ 0.82 scenario produced 0.97 power,
  interval 0.83–0.99. This grid and trial count do not identify a precise 80%
  minimum detectable effect.
- These figures are not bounds on the frozen cohort. A wider null median is
  diagnostically concerning, but a single quantile does not establish
  stochastic dominance or a strict ordering of power.

**Therefore: the frozen 2026-08-09 evaluation still does not establish that EISV
lacks predictive lift, but this audit also cannot quantify what it could have
detected.** Its exact cluster geometry is missing, the relevant effect was never
predeclared, and the first power figures were invalid.

## Withdrawn post-hoc comparison

The original audit compared frozen selective p-values with medians from the
defective simulation and suggested which planted-AUC rows they resembled. That
comparison is withdrawn, not updated. The simulations do not share the frozen
cluster geometry or null distribution, and the frozen p-values were selected
post hoc. No positive or negative evidence follows from putting the two tables
side by side.

## Defects found while measuring this

The first is a validity defect in this power audit. The latter two do not alter
the selective p-values already computed for the frozen production rows.

1. **Synthetic row identity corrupted model pairing.** Multiple outcomes in a
   cluster shared one `row_key`, while production uses unique `outcome_id`.
   Baseline indexing silently overwrote duplicates. The published power figures
   and every inference drawn specifically from them are withdrawn.

2. **The frozen record dropped the columns needed to read it.** The harness
   emits `Null max p95` and `Null clusters`; the transcription in
   [`eisv-ablation-frozen-2026-08-09.md`](eisv-ablation-frozen-2026-08-09.md)
   kept neither, while keeping `Selective p`. The harness's own output text says
   "few clusters bound how small `Selective p` can get, so read them together",
   and the stop rule makes `AUC delta > Null max p95` a distinct PASS condition.
   Every downstream citation therefore quotes a p-value stripped of the two
   figures that scope it. Recovering them would need a live re-run at the same
   `--as-of` cutoff; do not perform one solely to repair a historical
   transcription. An errata note records the gap.

3. **The baseline reads below chance and nothing flags it.** The frozen slices
   report baseline AUC 0.427–0.435 — the `previous_outcome_bad` reference is
   anti-predictive. `summarize_conclusion` guards only the extreme case where
   the training split holds *no* bad outcomes; a baseline that trained and still
   lands under 0.5 passes silently. This is worth fixing beyond tidiness: a
   degenerate baseline is a **power sink**. Every candidate beats it by some
   random margin, so the max-over-seven-candidates null inflates — which is
   consistent with the frozen null max median (0.144–0.177) sitting far above
   this corrected simulation's (−0.007–0.018) at a healthy baseline. Narrowing that null by
   fixing or replacing the reference model would raise the instrument's power
   before December, at no cost to its validity.

## Consequences for what the project says

The internal canon was already correct. The stop rule records the honest state
as "**unresolved** pending the registered trusted-anchor read"; the Reviewer
Guide and the evaluation index both say the snapshot is "not a standing AUC
bound". The storefront had gone past all three, and a lint enforced it:

| Surface | Was | Now |
|---|---|---|
| `README.md` | "found no predictive lift … that **bounds** the EISV score's forecasting power"; "**is a negative result**" | non-detection; unresolved; links this audit |
| `docs/PRODUCT_DEFINITION.md` | "**is a negative result** — no demonstrated predictive lift" | detected no lift; unresolved rather than ruled out |
| `check_doc_health.py` contested claims | flagged only the optimistic phrasing ("weak early signal"), treating the former matrix label as having **superseded** it | symmetric: flags unsupported claims in both directions, and the stated reason is now "unresolved", not "superseded" |

## This is directional, not drift

An earlier version of this document called the above "drift". That was the wrong
word and it let the finding off too lightly. Drift is undirected. Every
deviation found here points the same way, and they compound into a structure:

**1. The tested thing is overclaimed negative.** A non-detection with unknown
read-specific power became "bounds the EISV score's forecasting power" — while
three documents in the same repository said it bounds nothing. The first attempt
to repair that gap then understated simulated power because its pairing keys
were defective, reinforcing the same direction of conclusion.

**2. The evidence that would reveal the weakness was dropped.** The frozen
transcription kept `Selective p` and dropped exactly `Null max p95` and `Null
clusters` — the two columns the harness's own output tells readers to read
alongside it, and the two that would show the test could not resolve a weak
effect. Whatever the intent, the columns that survived are the quotable ones and
the columns that vanished are the scoping ones.

**3. The untested thing is overclaimed positive — and it is the product.**
Directly after the negative, both storefront surfaces asserted "an accountability
instrument with **one working circuit breaker**". The project's own contract
ledger says otherwise, in the same words:

- Row 28 — "The 2026-08-02 high-risk verdict demonstrates the enforcement path
  **working** live." → **EVENT RECONCILED: VERDICT RECORDED, DELIVERY
  SUPPRESSED.** The event distinguishes computation from delivery.
- Row 27 — "Enforcement is currently protecting the fleet." → **UNTESTED as a
  protection claim.** "The event proves the circuit breaker *can actuate*; it
  does not show prevention, benefit, or correctness."
- Row 24 — pause delivery → **IMPLEMENTATION MISMATCH plus DEPLOYMENT
  SNAPSHOT.** A `gap_suppress`
  cadence window downgraded **195 of 218 recorded pauses (89.4%)** in the
  2026-08-06 audit window before they reached the agent. Delivery is live —
  corrected 2026-08-10 after a governed pause landed on 2026-08-09 — but the
  suppression rate has not been re-measured since.

The 89.4% figure was public, but only inside a
[threat-model](../SCOPE_AND_THREAT_MODEL.md) paragraph arguing that a
*hypothetical* one-line diff could disarm enforcement. It appeared as
attack-surface leverage, never as a plain statement of how often the breaker
currently fails to deliver. A reader looking for the product's limits would not
find it there.

**The structure.** An unsupported negative about the component that was measured
sits immediately beside an unsupported positive about the component that was
not, and the negative is explicitly fenced off from the positive in the same
sentence ("that bounds the EISV score's forecasting power, **not** the
accountability mechanism it sits inside"). The negative therefore costs nothing
while buying the credibility of a project that reports its own failures — and
that credibility is spent on the claim no one tested. The word "working" carries
the whole product claim, and it is the exact word the ledger's event
reconciliation does not support.

**On intent.** None of this requires anyone to have decided to mislead. The
operator raised this concern unprompted, which is evidence against intent. But
intent is not the question: the structure produces the effect whether or not
anyone chose it, it survived many review passes, and one part of it was
mechanically enforced by CI. A project whose central claim is that self-report
must be checked against recorded evidence is the last place this should hold.

Both overclaims are now corrected on both storefront surfaces, the pause
suppression rate is disclosed in the README's own limits list, and
`check_doc_health.py` flags "working circuit breaker" alongside the two
negative-direction patterns.

## Not fixed here

- **The frozen cohort's total cluster geometry.** `Null clusters` and the
  cluster-size distribution were not preserved. The corrected 70-cluster run is
  a hypothetical sensitivity scenario, not a reconstruction.
- **The smallest relevant effect.** The stop rule requires power at a
  predeclared relevant effect, but no beta, AUC delta, or equivalent effect size
  currently fills that slot. The simulation must not choose it after the read.
- **The dropped columns.** They remain missing. Recovery would require a live
  re-run at the same `--as-of` cutoff, which is not justified solely to repair
  a historical transcription before the registered decision read.
- **The current pause-suppression rate.** The 89.4% figure is from 2026-08-06.
  Whether it still holds is unknown and is the single most decision-relevant
  number about the product's core mechanism.
- **The anti-predictive baseline** (below), which is also a power sink.

## What did not change

- Every number in the frozen table. This audit re-ran nothing against
  production.
- The 2026-12-01 read: date, cutoff discipline, all four PASS conditions, and
  the operational kill criterion stand exactly as written. The later protocol
  audit requires disclosure of repeated interim accesses and read-specific
  power; see
  [`../ontology/falsification-design-system-audit-2026-08-23.md`](../ontology/falsification-design-system-audit-2026-08-23.md).
- "No demonstrated predictive lift" and "no demonstrated prevention" — both
  remain true and remain on every surface that carried them.

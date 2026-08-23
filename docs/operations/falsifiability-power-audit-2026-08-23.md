# What the falsifiability harness could have detected — 2026-08-23

**Status:** Corrected instrument characterisation. Measures the *harness*, not
the deployment. No production data was read and no database was queried. The
power table originally published here is **withdrawn**: the synthetic producer
reused cluster IDs as outcome row IDs, corrupting candidate/baseline pairing,
and did not hold expected class balance fixed as planted strength changed.

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
strength into synthetic cohorts with requested row, expected class balance,
cluster, and agent counts, then runs **the same** `build_matrix_row` /
`estimate_selective_null` machinery the frozen run used. Power is the fraction
of all requested trials reaching `selective p <= 0.05`; an unscorable trial is a
failed detection and remains in the denominator.

The latent is drawn per `(agent, prior-state snapshot)` cluster, preserving the
important fact that prior state is constant inside a cluster. The simulation
calibrates its intercept separately for each generated cohort, so increasing the
planted effect does not also change the expected bad-outcome rate. Realised
counts still vary under Bernoulli sampling. The simulation
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
| 0.00 | 30 / 30 | 1 / 30 | 0.494 | 0.499 | 0.054 | 0.025 | 0.145 | 0.520 | **0.03 [0.01, 0.17]** |
| 0.25 | 30 / 30 | 4 / 30 | 0.566 | 0.518 | 0.048 | 0.014 | 0.134 | 0.284 | **0.13 [0.05, 0.30]** |
| 0.50 | 30 / 30 | 7 / 30 | 0.630 | 0.509 | 0.078 | 0.017 | 0.140 | 0.209 | **0.23 [0.12, 0.41]** |
| 0.75 | 30 / 30 | 11 / 30 | 0.673 | 0.503 | 0.142 | 0.030 | 0.163 | 0.104 | **0.37 [0.22, 0.54]** |
| 1.00 | 30 / 30 | 20 / 30 | 0.736 | 0.553 | 0.164 | 0.004 | 0.135 | 0.015 | **0.67 [0.49, 0.81]** |
| 1.50 | 30 / 30 | 28 / 30 | 0.824 | 0.591 | 0.210 | 0.001 | 0.105 | 0.005 | **0.93 [0.79, 0.98]** |

The `beta = 0` interval includes the nominal 0.05 rate. That is compatible with
the type-I target in this small simulation; it is not enough to certify the
selective null across other cohort geometries or data-generating processes.

## What the numbers say

- In this assumed geometry, the weak planted signal (median AUC ≈ 0.57) was
  detected in 4 of 30 trials: 0.13 power, with a wide 0.05–0.30 interval. That
  remains inadequate for a strong negative inference, but the prior 0.03/"blind"
  claim was an artifact of corrupted pairing.
- The AUC ≈ 0.67 and AUC ≈ 0.74 scenarios produced 0.37 and 0.67 power,
  respectively. The AUC ≈ 0.82 scenario produced 0.93 power, interval
  0.79–0.98. This grid and trial count do not identify a precise 80% minimum
  detectable effect.
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

The first two are validity defects in this power audit. The latter two do not
alter the selective p-values already computed for the frozen production rows.

1. **Synthetic row identity corrupted model pairing.** Multiple outcomes in a
   cluster shared one `row_key`, while production uses unique `outcome_id`.
   Baseline indexing silently overwrote duplicates. The published power figures
   and every inference drawn specifically from them are withdrawn.

2. **Planted strength changed the simulated class balance.** The producer used
   `logit(target_bad_rate)` as a fixed intercept. That only preserves the target
   marginal rate when `beta = 0`; stronger effects also shifted prevalence and
   confounded sensitivity with class balance. The corrected producer calibrates
   the intercept against every generated cohort's weighted cluster latents.

3. **The frozen record dropped the columns needed to read it.** The harness
   emits `Null max p95` and `Null clusters`; the transcription in
   [`eisv-ablation-frozen-2026-08-09.md`](eisv-ablation-frozen-2026-08-09.md)
   kept neither, while keeping `Selective p`. The harness's own output text says
   "few clusters bound how small `Selective p` can get, so read them together",
   and the stop rule makes `AUC delta > Null max p95` a distinct PASS condition.
   Every downstream citation therefore quotes a p-value stripped of the two
   figures that scope it. Recovering them would need a live re-run at the same
   `--as-of` cutoff; do not perform one solely to repair a historical
   transcription. An errata note records the gap.

4. **The baseline reads below chance and nothing flags it.** The frozen slices
   report baseline AUC 0.427–0.435 — the `previous_outcome_bad` reference is
   anti-predictive. `summarize_conclusion` guards only the extreme case where
   the training split holds *no* bad outcomes; a baseline that trained and still
   lands under 0.5 passes silently. This is worth fixing beyond tidiness: a
   below-chance reference can change both observed deltas and the
   max-over-candidates null. The frozen null max median (0.144–0.177) sits far
   above this corrected simulation's (0.001–0.030), but that contrast does not
   identify the cause. Whether repairing or replacing the reference narrows the
   null and improves power must itself be tested prospectively.

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

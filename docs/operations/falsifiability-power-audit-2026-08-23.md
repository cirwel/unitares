# What the falsifiability harness could have detected — 2026-08-23

**Status:** Instrument characterisation. Measures the *harness*, not the
deployment. No production data was read and no database was queried.

**Scope guard:** this changes nothing about the pre-registered 2026-12-01
confirmatory read in
[`../proposals/eisv-outcome-grounding-stop-rule-v0.md`](../proposals/eisv-outcome-grounding-stop-rule-v0.md)
— not its date, its cutoff, its four PASS conditions, or its kill criterion. It
supplies the one number that read will need in order to be reported honestly: if
it FAILs, at what power did it fail.

## Why this exists

`eisv_ablation_matrix.py` answers one question: did the selected candidate
separate from its best-of-candidates null? When the answer is `NOISE-LEVEL`, two
very different worlds produce that same word:

1. prior state carries no association with outcome, or
2. it carries one, and this cohort is too small for this instrument to see it.

Nothing in the harness, and nothing in any document citing it, distinguished
them. A minimum-detectable-lift estimate did exist — the `≈ 0.05 AUC` figure —
and it was **withdrawn** on 2026-08-17 for using the contaminated
`--anchor-scope all` cohort. It was never replaced. From that date until this
audit, every published statement about EISV's predictive lift rested on a
non-detection whose power was unmeasured.

That gap has a direction. It is the mirror image of the failure `CLAUDE.md`
already forbids under *Measurement authority* — there, a zero must not retire a
capability; here, a non-rejection must not become a ceiling. Both mistake "we
did not see it" for "it is not there".

## Method

`scripts/analysis/ablation_power_probe.py` plants an association of known
strength into synthetic cohorts shaped like the frozen 2026-08-09 slice (224
rows, 53 bad, 16 agents), then runs **the same** `build_matrix_row` /
`estimate_selective_null` machinery the frozen run used, with the same 200
permutations. Power is the fraction of trials reaching `selective p <= 0.05` on
a cohort that genuinely contained the effect.

The latent is drawn per `(agent, prior-state snapshot)` cluster, matching the
real dependence structure — prior state is constant within a cluster, so the
association can only live at cluster granularity.

```bash
python3 scripts/analysis/ablation_power_probe.py \
  --trials 30 --resamples 200 --rows 224 --clusters 70
```

## Results

30 trials per effect size, 200 permutations per trial, alpha = 0.05. At 30
trials the standard error on each power figure is roughly 9 percentage points;
read the shape, not the third digit.

| Planted beta | True AUC | Baseline AUC | Median AUC delta | Null max median | Null max p95 | Median selective p | **Power** |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.00 | 0.510 | 0.515 | 0.065 | 0.062 | 0.334 | 0.654 | **0.07** |
| 0.25 | 0.568 | 0.514 | 0.107 | 0.019 | 0.242 | 0.291 | **0.03** |
| 0.50 | 0.636 | 0.507 | 0.095 | 0.045 | 0.273 | 0.100 | **0.27** |
| 0.75 | 0.707 | 0.512 | 0.149 | -0.021 | 0.199 | 0.062 | **0.47** |
| 1.00 | 0.737 | 0.535 | 0.146 | -0.086 | 0.109 | 0.037 | **0.63** |
| 1.50 | 0.821 | 0.635 | 0.120 | -0.121 | 0.030 | 0.010 | **0.80** |

The `beta = 0` row is the type-I check. Power there is 0.07 against a nominal
0.05, within sampling error of the target: **the null is calibrated and the
harness is not statistically broken.** Whatever is wrong here is not a bug in
the selective null.

## What the numbers say

- **Against a weak signal (AUC ≈ 0.57) the harness has ~3% power.** It is not
  merely underpowered; it is blind. A real weak effect of that size would have
  produced `NOISE-LEVEL` essentially every time.
- **80% power needs roughly AUC ≈ 0.82.** The instrument can reliably detect
  only a near-strong predictor.
- **These are upper bounds.** The synthetic cohort is deliberately generous:
  every prior-state feature carries the planted signal cleanly, the outcome
  depends on prior state alone, and there is no measurement noise, missingness,
  or provenance drift. The checkable proof of the bound is the null width — the
  synthetic null max median runs 0.02–0.06 here, against **0.144–0.177** in the
  frozen run. A wider null is strictly less power, so the real slice's power is
  at most what this table shows.

**Therefore: the frozen 2026-08-09 evaluation was never capable of establishing
that EISV lacks predictive lift.** It could only ever have detected a strong
predictor. It did not find one. That is the whole of what it shows.

## An observation the December read should test, not a finding

The frozen run's selective p-values (0.070–0.567, with the four 30-minute-lead
slices at 0.070–0.085) sit well below this simulation's no-effect median of
0.654, and near the medians for planted effects around AUC 0.64–0.71 (0.100 and
0.062).

**This is not a test and must not be cited as evidence of a positive effect.**
The comparison is confounded exactly where it matters: the frozen run's null is
two to seven times wider than the simulation's, which shifts the whole p-value
distribution, and the frozen p-values were themselves selected post hoc. It is
recorded here only because it points the same way as the power result — toward
*unresolved* rather than *negative* — and suppressing it would repeat the error
this audit is about. The registered read is the next fixed decision point; its
read-specific power will determine what scientific conclusion it can support.

## Two harness defects found while measuring this

Both are reporting defects, not validity defects. The selective p-values in the
frozen table remain correct as computed.

1. **The frozen record dropped the columns needed to read it.** The harness
   emits `Null max p95` and `Null clusters`; the transcription in
   [`eisv-ablation-frozen-2026-08-09.md`](eisv-ablation-frozen-2026-08-09.md)
   kept neither, while keeping `Selective p`. The harness's own output text says
   "few clusters bound how small `Selective p` can get, so read them together",
   and the stop rule makes `AUC delta > Null max p95` a distinct PASS condition.
   Every downstream citation therefore quotes a p-value stripped of the two
   figures that scope it. Recovering them would need a live re-run at the same
   `--as-of` cutoff; do not perform one solely to repair a historical
   transcription. An errata note records the gap.

2. **The baseline reads below chance and nothing flags it.** The frozen slices
   report baseline AUC 0.427–0.435 — the `previous_outcome_bad` reference is
   anti-predictive. `summarize_conclusion` guards only the extreme case where
   the training split holds *no* bad outcomes; a baseline that trained and still
   lands under 0.5 passes silently. This is worth fixing beyond tidiness: a
   degenerate baseline is a **power sink**. Every candidate beats it by some
   random margin, so the max-over-seven-candidates null inflates — which is
   consistent with the frozen null max median (0.144–0.177) sitting far above
   this simulation's (0.02–0.06) at a healthy baseline. Narrowing that null by
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
| `check_doc_health.py` contested claims | flagged only the optimistic phrasing ("weak early signal"), citing `NOISE-LEVEL` as having **superseded** it | symmetric: flags unsupported claims in both directions, and the stated reason is now "unresolved", not "superseded" |

## This is directional, not drift

An earlier version of this document called the above "drift". That was the wrong
word and it let the finding off too lightly. Drift is undirected. Every
deviation found here points the same way, and they compound into a structure:

**1. The tested thing is overclaimed negative.** A non-detection at ~3% power
became "bounds the EISV score's forecasting power" — while three documents in
the same repository said it bounds nothing.

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

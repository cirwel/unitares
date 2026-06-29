# EISV grounding/decision — next-move proposal (v0)

**Status:** design-intent / roadmap (not a change). Output of a 15-agent design
tournament (5 seeded proposals → 3-judge panel → adversarial stress on the top 2
→ synthesis), grounded in the label-free validation work of PRs #1285/#1289/
#1293/#1294. Companion to `docs/proposals/eisv-maths-roadmap-v0.md`.

## TL;DR

Run an **"honest-floor" quarter, not a Stage-B quarter.** The maths are not the
blocker; exogenous **bad-label supply** is — and a power analysis shows the
outcome-validity gate is unwinnable at the current supply, by arithmetic. Do two
near-free killer experiments first, build exactly one thin label leg, make the
AR(1) decontamination null the scientific deliverable, and ship nothing to the
live verdict path.

## Where the design space landed (tournament ranking, 3-judge mean /30)

| # | proposal | mean | fate |
|---|---|---|---|
| 1 | Label-acquisition-first | 26.3 | survived (0/3 fatal) but **not crowned** — headline target arithmetically unreachable |
| 2 | Validation-as-product | 25.3 | survived (0/3 fatal) but **not crowned** — largely a do-nothing rebrand of in-flight work |
| 3 | Proprioception-first | 23.3 | — |
| 4 | Pooled-falsifiability | 22.0 | — |
| 5 | Radical-simplification | 19.0 | — |

Neither top proposal was crowned as-spec:
- **Labels** correctly diagnoses the binding constraint but its "≥150 clean /
  ≥5 balanced agents per quarter" target needs a ~17× sustained rate increase
  the system cannot *cause* (labels are a property of the world — a solo
  operator's correction bandwidth + a mostly-non-committing fleet — not of
  plumbing). Forward-only, so it cannot reclaim the dirty ~96; only 1–3 agents
  author attributable events, so "5 balanced" may be unreachable in principle.
- **Validation-as-product** ("the harness IS the undamp") makes no live-path
  change, its core tests already ran, and its one new test (AR(1)) is already in
  flight via #1294 — net delta is anticipatory bureaucracy, anti-demand-triggered.

## The power result (this PR: `scripts/analysis/eisv_label_power.py`)

Gate-3 ("an EISV/residual model beats the baseline at predicting bad outcomes")
is a minority-class problem; power is set by the scarce **bad**-label count.
Current pooled exogenous budget: **114 bad / 2287 good** (skeptic scored slice
~21 bad).

- At the realistic scored slice (~21 bad), an EISV model cannot even be shown to
  beat a **coin** unless its AUC lift over 0.5 exceeds **+0.165**.
- The comparison target — the previous-outcome (autocorrelation) baseline — is
  both very high (~0.94 on clean data) and **unpinnable**: it swings 0.61→0.94
  between contaminated and clean slices, and its own CI is ±0.07 at n=21. There
  is no stable thing to beat; the headroom (0.06) is narrower than the baseline's
  own uncertainty.
- **Caveat against false comfort:** a naive one-sample MDE makes "beat 0.94"
  look cheap (~13 labels for +0.05). That is an artifact of AUC variance
  collapsing near the 1.0 ceiling plus ignoring baseline uncertainty; the honest
  paired (DeLong) requirement is materially larger.

**Conclusion:** Stage-B / `GROUNDING_APPLY` is not validatable on outcomes at
this label supply, independent of how good the maths are.

## Recommended sequence (falsifiable, reversible, demand-triggered)

1. **Latent-supply count** (read-only, ~days). Instrument operator-correction +
   resident-resolve + CI paths; count clean, exogenous, attributable bad labels
   the fleet *would* emit at current activity.
   - *Gate:* extrapolated ceiling **≥ ~30 clean bad/quarter**. If below, the
     constraint is structural fleet activity, not engineering — stop building
     pipeline, go label-free only. (A real decision, not a failure.)
2. **MDE/power** — *done in this PR.* Result above: underpowered at current
   supply; records exactly how many bad labels a meaningful lift would need.
3. **Build ONE label leg only:** operator-correction capture (revert / reject /
   rejected-PR → `outcome_event` with commit→agent attribution) **plus one
   exogenous good-label source** (e.g. survived-N-days merged commit).
   Forward-only, eval-only, never wired to the live verdict. Judge on a
   **rate-based** criterion with a zero-synthetic contamination audit — not the
   unreachable 150/5 absolute. Record *why* each item was adjudicated, so
   EISV/Φ-conditioned selection can be excluded (sampling-circularity guard).
   - *De-scope:* the resident-adjudication throughput dial (contamination
     pressure → 497-synthetic redux) and CI-on-critical-path (dead on
     connectivity).
4. **AR(1) decontamination null** — the quarter's make-or-break. Once #1294 raw
   pre-EMA obs accrue, re-run self-predictability with a per-agent
   persistence/AR(1) null on the raw series.
   - *Gate:* per-agent hierarchical reference beats **both** fleet-mean **and**
     per-agent AR(1), out-of-sample, for a majority of agents with ≥50 states.
     Beating fleet-mean alone is **not** success. If it fails, the "self-model"
     is dressed-up autocorrelation — the individuality claim dies and must not be
     shipped or publicly framed.
5. **Lightweight hygiene only:** anti-laundering guard (synthetic labels — e.g.
   the 497 BEAM smoke tests — can never count as outcome evidence) +
   pre-registration of thresholds/nulls, as a checklist that fires *when* an
   undamp ships, not a standing merge-blocker.

## Do NOT

- Ship Stage B as-spec (no EISV feature beats the baseline; underpowered by
  arithmetic).
- Wire **any** new statistic — EISV residual *or* a recent-failure-rate scalar —
  to the live verdict path. The failure-rate gate is un-deployable (decision-time
  per-agent label coverage ≈ 0) and hard-codes punish-toward-zero-failures
  (Axiom-2 / anti-RLHF violation) into the load-bearing path.
- Pursue the ≥150/≥5-balanced absolute target, build the resident throughput
  dial, or put CI attribution on the critical path.
- Stand up a standing merge-blocking validation regime for hypothetical undamps.
- Frame an "EISV self-model" publicly before the AR(1) null clears.
- Double-count per-agent autocorrelation as evidence both *for* individuality and
  *against* EISV.

## Preserved dissent (kept honest)

This plan can become **a permanent retreat dressed as rigor.** Even a clean AR(1)
win proves only a non-trivial per-agent self-model (detection / individuality) —
it says nothing about **outcome validity**, which stays blocked behind a label
supply capped by the world, not the quarter. If the latent-supply count returns
< ~30 clean bad/quarter, the honest conclusion is not "iterate the pipeline" but
"**EISV is plausibly permanently unfalsifiable on outcomes**," at which point the
entire grounding program — not just Stage B — deserves reconsideration. And the
anti-laundering guard is the only thing preventing a coherence/decontamination
battery from greenlighting an internally-consistent, perturbation-responsive EISV
that still bears no relation to real outcomes; if it is ever relaxed under
ship-pressure, this "honest-floor" posture becomes the most credible-looking way
to ship an ungrounded undamp.

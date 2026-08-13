# Proprioceptive coherence thresholds — derivation (v0)

> **Status:** proposal. Changes no deployed behaviour. The measurements are live-DB reads
> (2026-08-10); the threshold *form* is argued, the constant `k` is a stated tolerance and
> is flagged as such rather than dressed up as derived.
>
> **Prerequisite:** the coherence signal is currently frozen (see #1572 — it reads the ODE V
> that `69ee5a79` demoted). Nothing here is actionable until that is repaired. This document
> exists so the repair does not land against thresholds nobody re-derived.

> **Correction (2026-08-11):** Sections 2–4 preserve the original one-sided derivation as
> archaeology, but its health interpretation is invalid. Strict monotonicity of `C(V)` preserves
> order; it does not make positive `V` healthier than negative `V`. Positive means running hot,
> negative means running careful, and both are directional imbalance. The shadow implementation
> is therefore now v2: a **two-sided**, leave-current-out behavioral-V residual over a bounded
> recent window. It remains measurement-only. Its payload names whether empirical dispersion or
> the calibrated scale floor supplied the denominator, so floor-normalized units are never
> presented as measured sigma.

## 1. Why the existing thresholds cannot simply be scaled

Four gates read `coherence`. All were calibrated while it was frozen. Crossing counts over
the 2026-08-10 non-synthetic `core.agent_state` snapshot (observed range [0.288, 0.561]):

| gate | threshold | crossings | consumer |
|---|---|---|---|
| `tau_floor` | 0.25 | **0, ever** | `AdaptiveGovernor.make_verdict` hard block |
| `tau_low` | 0.30 | 1 | CIRS hard block |
| `COHERENCE_CRITICAL_THRESHOLD` | 0.40 | 9 (0.013%) | `coherence_pause`, `is_critical`; former coherence-derived health status removed 2026-08-11 |
| `TARGET_COHERENCE` | 0.50 | reachable | `coherence_deficit`, fed a near-constant |

The whole stack produced **2 `coherence_pause` actions in that snapshot**. The obvious
repair — unfreeze the signal, keep the numbers — is the thing this document argues against.

## 2. The measurement that decides the form

If coherence is computed from the **primary** (behavioral) V rather than the demoted ODE V,
the per-agent structure over 30d (11 agents with >=50 check-ins) is:

- per-agent means span **0.2848 to 0.5201**
- **between-agent sd = 0.0798**
- **within-agent sd = 0.0265** on average (range 0.0109 to 0.0411)

**Between-agent dispersion is ~3x within-agent dispersion.** Agents differ from each other far
more than they vary within themselves. That single ratio determines the correct gate form: on a
between-agent-dominated signal, a fleet-constant threshold does not measure deviation. It
measures identity.

That is not an abstraction. Applying the current `COHERENCE_CRITICAL_THRESHOLD = 0.40` to the
primary-V signal:

| agent | n | own mean | own sd | below fleet 0.40 | below own mu-3sigma |
|---|---|---|---|---|---|
| 69a1a4f7 | 7527 | 0.2848 | 0.0242 | **100.00%** | 0.00% |
| 9a6681ec | 2694 | 0.3190 | 0.0215 | **100.00%** | 0.00% |
| e55caaf1 | 778 | 0.4664 | 0.0229 | 0.00% | 0.00% |
| 94c0dd1c | 64 | 0.4822 | 0.0328 | 3.13% | 3.13% |
| 09f2bfa5 | 55 | 0.4830 | 0.0280 | 1.82% | 1.82% |
| df9bacc6 | 63 | 0.4883 | 0.0411 | 3.17% | 3.17% |
| 3745aa33 | 62 | 0.4957 | 0.0291 | 1.61% | 1.61% |
| 9fc1b1e0 | 79 | 0.5037 | 0.0297 | 2.53% | 2.53% |
| 907e3195 | 1569 | 0.5063 | 0.0109 | 0.00% | 0.25% |
| aa0497e8 | 72 | 0.5073 | 0.0292 | 1.39% | 2.78% |
| f92dcea8 | 4728 | 0.5201 | 0.0224 | 0.00% | 0.13% |

Two agents would sit in **permanent violation** — 10,221 consecutive pauses between them — and
three could never trip the gate at all. Judged against their own baselines those two
"violators" fire **0.00%** of the time: they are not deviating, they occupy a lower operating
point. A fleet threshold would pause them for their identity.

This is the north star stated arithmetically. Individuality: an agent is judged against its own
normal. Growth-not-punish: a low-V operating point is a characteristic, not an offence.
Groundedness: the threshold is derived from that agent's own measured dispersion rather than a
constant chosen elsewhere.

## 3. Proposed form

For agent `a` with a matured baseline:

```
tau_a = mu_a - k * sigma_a
```

where `mu_a`, `sigma_a` are the agent's own running mean and dispersion of coherence (Welford /
EMA over its own history — the same machinery `BehavioralEISV` already maintains for E/I/S).

Each existing gate maps to a `k`, preserving the current ordering of severity:

| gate | today | proposed |
|---|---|---|
| `COHERENCE_CRITICAL_THRESHOLD` (pause) | 0.40 fleet | `mu_a - k_pause * sigma_a` |
| `tau_low` (CIRS hard block) | 0.30 fleet | `mu_a - k_block * sigma_a`, `k_block > k_pause` |
| `tau_floor` (governor hard block) | 0.25 fleet | `mu_a - k_floor * sigma_a`, `k_floor >= k_block` |
| `TARGET_COHERENCE` (deficit) | 0.50 fleet | `mu_a` — the agent's own normal IS the target |

The last row is the cleanest win: a deficit measured against a fleet constant of 0.50 is
partly just "distance from the fleet mean." Measured against `mu_a` it is what it claims to be.

## 4. Honest treatment of `k`

**`k` is a stated tolerance, not a derived constant.** It encodes how far outside its own normal
an agent goes before the system speaks. Under approximate normality `k=3` implies ~0.13%.

The data does not support quoting that number as an expectation. Observed rates at `mu-3sigma`
are 0.13-0.25% for high-n agents but **1.4-3.2% for agents with 55-79 samples** — heavy tails
plus dispersion estimated from too few points. Two consequences, both required:

1. **A maturity gate.** Do not apply a proprioceptive threshold until the agent's baseline has
   enough observations to estimate `sigma_a`. **Correction after reading the code:** an earlier
   draft of this section said to reuse `BehavioralEISV`'s `confidence >= 0.3`. That is the wrong
   constant — `confidence` is *bootstrap* confidence over 10 updates, not dispersion stability.
   The repo already has the right notion: `is_baselined` (`baseline_confidence >= 0.8`, i.e.
   `BASELINE_WARMUP_UPDATES = 30`). Use that. Below it, fire nothing and report ineligibility
   explicitly rather than falling back to a fleet prior silently.
2. **A robust dispersion estimate.** Plain sd over-reacts to tails. **Also already solved in
   the repo:** `deviation()` floors its denominator with `eisv_min_std_for_dimension`, an
   empirical constant calibrated against the 2026-06-13 Sentinel false-pause trace — precisely
   the "tiny sigma makes everything look anomalous" failure. Reuse it; a fresh MAD/trimmed
   estimator here would discard a calibration already paid for in production. #1518 flags the
   same issue for its residual form ("heavy tails over-react; a Student-t/trimmed surprise is a
   refinement, not derived"), which remains the longer-term refinement.

**What is genuinely derived here is the *form*, not the constant** — the 3:1 between-to-within
ratio is a measurement, and it rules out a fleet constant regardless of which `k` is chosen.
Choosing `k` is a policy call and should be recorded as one.

## 5. What this does not claim

- **Not outcome-validated.** No claim that low proprioceptive coherence predicts bad outcomes.
  The label volume for that test is not available (83 bad-with-EISV rows across 26 independent
  clusters against a stop rule of >=150) and is not buyable with plumbing.
- **The oracle path stays open, deliberately.** Proprioception is the primary justification, not
  a replacement that forecloses outcome validation. Per-agent thresholds *improve* the eventual
  oracle test: they remove between-agent identity variance, which is confound rather than
  signal for a within-agent deviation hypothesis. Nothing here should be read as retiring
  outcome grounding — it retires it as a *prerequisite*, not as a goal.
- **`C1` is assumed at its default 1.0** for the recomputation in section 2. It is nominally
  adaptive within [0.5, 1.5]; no per-agent adaptation was observed in the live path, but the
  numbers above should be regenerated if that changes.
- **Nothing about the transfer function.** This proposes changing the *gates*, not
  `C(V) = 0.5(1+tanh(C1*V))`. #1518 proposes replacing the form outright with a per-agent
  residual; that is the larger question and this is compatible with it — a per-agent threshold
  is the same instinct applied one layer out.

## 6. Sequence

1. Preserve the legacy scalar as explicitly tagged `ode_control_feedback`; do not
   “repair” a directional controller by feeding it behavioral V and then calling
   the result health. Define the replacement instrument separately (for example a
   two-sided behavioral residual or V-free manifold measurement).
2. Land the replacement instrument and its prospective, per-agent policy together,
   never by swapping a new distribution under the fleet constants. Also shadow the
   E/I sensor without its current 25–40% legacy-controller contribution before
   changing those behavioral baselines. The measurement-only first step and its
   separate trusted-outcome safety read are specified in
   [Legacy-coherence dependency ablation v0](legacy-coherence-dependency-ablation-v0.md).
3. Ship behind the existing shadow pattern first: compute both, record the divergence, apply
   nothing — the same discipline `grounding_shadow` already uses. **Built and corrected:**
   `src/coherence_gate_shadow.py` + `coherence_gate_shadow` audit event, behind
   `UNITARES_COHERENCE_GATE_SHADOW` (default off, no APPLY counterpart by design). Shadow v1 used
   a one-sided expanding-Welford V score. Shadow v2 supersedes it with
   `abs(V_current - mean(V_recent_prior)) / effective_scale`: both directions count, the current
   sample is excluded, history is bounded, and `effective_scale=max(sample_sd, calibrated_floor)`
   is accompanied by `scale_source`. `statistic_version` keeps v1/v2 events from being pooled.
   The comparison contract is separately versioned as
   `coherence_cause_attribution_v2`: agreement uses `sub_action` and
   `nearest_edge`, so risk/void/basin pauses are no longer mislabeled as
   legacy coherence-gate firings. Rows without enough causal detail carry
   `agrees=null` and stay out of agreement rates.
4. Re-read the gate crossing counts after a soak. A proprioceptive gate that still never fires
   is an unfair zero (the lever is untested, not disproven) and needs a positive control before
   anyone concludes anything from its silence. **Built (2026-08-13):**
   `scripts/analysis/coherence_gate_shadow_read.py` — the soak reader (volume, eligibility,
   scale provenance, per-agent |z| structure, k sweep, tri-state agreement; v1/v2 never pooled;
   prints the unfair-zero guard when nothing reached `k_pause`) and `--positive-control`, which
   drives the real `evaluate()` path with injected excursions at every tier plus the
   floor-scale and immature-baseline edges. The control also runs in CI
   (`tests/test_coherence_gate_shadow_read.py`), so "the instrument can fire" is a
   regression-tested property, not a one-off claim.

## 7. Reproduce

```sql
-- per-agent structure of primary-V coherence
WITH r AS (
  SELECT identity_id, 0.5*(1+tanh(volatility)) AS c
  FROM core.agent_state
  WHERE synthetic IS NOT TRUE AND recorded_at > now() - interval '30 days'
)
SELECT identity_id, count(*), avg(c), stddev(c) FROM r GROUP BY 1 HAVING count(*) >= 50;
```

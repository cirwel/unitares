# EISV Distributional Signal Probe — v0

**Created:** 2026-06-20
**Status:** **Probe A run — KILL (2026-06-22).** Dispersion carries no predictive lift over the boring `previous_outcome_bad` baseline; the larger "make EISV distributional" work is **not** greenlit. Probe B not run (Probe A did not greenlight, and is not inconclusive-suggestive). Cheap, falsifiable probe that must greenlight (or kill) distributional work **before** any dynamics change — it killed it.

> **Run result (2026-06-22, against the live governance DB).** Ran Probe A at the
> module-default 90-min dispersion window, `--window-days 365`, both lead bands.
> - `strict` scope: **0 bad outcomes** in 2737 trusted outcomes → INCONCLUSIVE by
>   construction (no failure signal to predict). The objective scope cannot exercise
>   the probe; this is itself a finding about where verified-bad signal lives.
> - `task` scope (581 bad / 9222, weaker objectivity): the dispersion model
>   `previous_bad_plus_dispersion` scores **AUC delta −0.117 (lead 0) / −0.265 (lead 5)**
>   vs the previous-outcome baseline — negative, the *wrong sign* for the greenlight
>   rule (needs ≥ +0.03). Every EISV/prior-state feature (phi, S, verdict, dispersion)
>   loses to "what happened last time for this agent." Conclusion line: `SKEPTICAL`.
> - Robust: same wrong-signed result at a 30-min window too. Honest limits: `task`
>   scope includes self-reported outcomes; dispersion paired-N is smaller (555–654)
>   from the ≥5-snapshot requirement. Reports archived under `data/analysis/eisv_skeptic_report_2026-06-22_*` (gitignored; reproducible via the command in this doc).
>
> **Decision:** do not build distributional EISV (`governance_core/dynamics.py` /
> observation blend) on this evidence. Revisit only if a future data slice shows
> verified-bad signal in `strict` scope where the dispersion feature could actually
> be tested against objective outcomes.
**Companion to:** `docs/REVIEWER_GUIDE.md` (§ Falsifiability), `scripts/analysis/eisv_skeptic_report.py`, `docs/EISV_COMPUTATION.md`, `docs/ontology/glossary.md` (FEP roadmap).

> **Implementation note (2026-06-20).** Probe A landed in the harness: the
> dispersion-window `LATERAL` join, the `prior_*_disp` fields on `OutcomeRow`, the
> `prior_eisv_dispersion_binned` + `previous_bad_plus_dispersion` models, the
> `--dispersion-window-minutes` flag, and a "By recent-state dispersion quartile"
> bad-rate table. Tunables are module constants: `DISPERSION_WINDOW_MINUTES=90`,
> `MIN_DISPERSION_SNAPSHOTS=5`, `DISPERSION_FEATURE="prior_s_disp"` (one-line swap
> to another axis). Unit-tested; the A1–A5 sections below are the design of record,
> now realized. **Next action is a single run against real outcomes** — there is no
> deployment DB in this environment, so the greenlight/kill decision is pending data.

---

## The question, sharpened

The glossary's FEP roadmap names "make EISV distributional (mean + precision)" as the
highest-leverage move. But "would it improve signal?" is an **empirical** question, and
distributional dressing can fit noise as easily as it can earn signal (same trap as the
dropped 128-parameter idea). So before touching `governance_core/dynamics.py` or the
observation blend, we test the cheap proxy:

> **Does the *uncertainty* in recent EISV carry predictive lift over the boring
> `previous_outcome_bad` baseline — and over the current best feature `prior_risk`?**

If yes → distributional EISV is worth building, with evidence in hand. If no → the
bottleneck is upstream in the *observations*, not the *representation*, and effort should
redirect there. Either answer is a win; only "build it and hope" is a loss.

## Why this is testable today (no new instrumentation)

The skeptic harness scores features that already live in the DB:
`audit.outcome_events` joined (`fetch_rows`, LATERAL) to the agent's prior
`core.agent_state` snapshot at a lead time before the outcome. Each "model" in
`build_model_scores` is just: quartile-bin a feature on train, fit a smoothed bad-rate
per bin, predict on test, score AUC (ranking) + Brier (calibration) **paired against the
baseline** (`score_deltas_vs_baseline`), and self-label
INCONCLUSIVE / SKEPTICAL / WEAK SIGNAL / KEEP TESTING (`summarize_conclusion`).

A point estimate hides its own uncertainty — but the **dispersion of recent state
snapshots** is a stored proxy for that uncertainty. The harness currently joins exactly
one prior snapshot (`LIMIT 1`). Widen that to a window and aggregate, and you recover a
historical proxy variance for free. That is the whole probe.

## Probe A — dispersion-as-feature (cheap; ~½ day)

Tests the **uncertainty-as-feature** leg: is the spread of recent EISV predictive on its
own and additively over `prior_risk`?

### A1. Widen the prior-state join to a dispersion window

In `fetch_rows`, add a second LATERAL that aggregates over the last *N* non-synthetic
snapshots strictly **before the lead cutoff** (reusing the existing leak-safe
`recorded_at <= o.ts - lead` guard):

```sql
LEFT JOIN LATERAL (
    SELECT
        count(*)                                               AS n_prior_snapshots,
        stddev_samp((s.state_json->>'S')::float)               AS prior_s_disp,
        stddev_samp((s.state_json->>'E')::float)               AS prior_e_disp,
        stddev_samp((s.state_json->>'I')::float)               AS prior_i_disp,
        stddev_samp((s.state_json->>'V')::float)               AS prior_v_disp,
        stddev_samp(s.risk_score)                              AS prior_risk_disp
    FROM core.identities i
    JOIN core.agent_state s ON s.identity_id = i.identity_id
    WHERE i.agent_id = o.agent_id
      AND s.synthetic IS NOT TRUE
      AND s.recorded_at <= o.ts - ($2::double precision * INTERVAL '1 minute')
      AND s.recorded_at >  o.ts - ($2::double precision * INTERVAL '1 minute')
                              - (INTERVAL '90 minutes')         -- dispersion window
) disp ON TRUE
```

Tunable: the window length and a minimum `n_prior_snapshots` (suggest ≥ 5) below which
dispersion is null — a stddev over 1–2 points is noise, not a signal.

### A2. Carry the fields on `OutcomeRow`

Add `n_prior_snapshots`, `prior_s_disp`, `prior_e_disp`, `prior_i_disp`, `prior_v_disp`,
`prior_risk_disp` to the `OutcomeRow` dataclass and `_row_from_record`, gated on
`n_prior_snapshots >= 5` (else null, so sparse agents don't pollute the bins).

### A3. Add the models (mirror `prior_s_binned` / `previous_bad_plus_prior_risk`)

In `build_model_scores`, add — using the existing `quantile_cuts` + `_fit_group_rates`
machinery, no new scoring code:

- **`prior_eisv_dispersion_binned`** — bin a single composite dispersion (suggest
  `prior_s_disp`, since S already *is* the entropy/uncertainty axis; or the L2 of the
  four axis dispersions). Raw AUC score = the dispersion value (higher disp ⇒ higher
  predicted bad).
- **`previous_bad_plus_dispersion`** — the additive test that actually matters: group by
  `(previous_bad, dispersion_quartile)`, exactly as `previous_bad_plus_prior_risk` groups
  by `(previous_bad, risk_quartile)`.

Then add both names to `EISV_PRIOR_STATE_MODELS` so they enter
`score_deltas_vs_baseline` and the conclusion.

### A4. Run it

```bash
export GOVERNANCE_DATABASE_URL=postgresql://...   # real outcomes
python3 scripts/analysis/eisv_skeptic_report.py --scope task --window-days 90 \
  --output data/analysis/eisv_dispersion_probe.md
# repeat --lead-minutes 0 and 5 (the lead band where current signal exists)
```

Also eyeball the new "By dispersion quartile" bad-rate table for **monotonicity** — a real
signal shows rising bad-rate across dispersion quartiles, not a flat or U-shaped smear.

### A5. Decision rule (uses the harness's own thresholds)

| Result | Read | Action |
|---|---|---|
| `previous_bad_plus_dispersion` **beats baseline** (AUC Δ ≥ 0.03 **and** Brier improvement ≥ 0.001 → "KEEP TESTING") **and** beats `previous_bad_plus_prior_risk` on the paired delta | Uncertainty carries lift the point estimate was hiding | **Greenlight** the distributional build (Probe B, then dynamics) |
| Dispersion model present but `SKEPTICAL` / non-monotonic quartiles | Spread doesn't rank outcomes | **Kill** distributional-for-signal; redirect to the observation layer |
| `INCONCLUSIVE` (the 90d task scope has only ~80 bad outcomes; adding a ≥5-snapshot filter shrinks coverage further) | Underpowered, not negative | **Hold**; the modeling/honesty case for distributional EISV still stands, but it can't be justified on *signal* yet |

## Probe B — precision-reweighted state (follow-up; heavier)

Probe A tests uncertainty as a *feature*. The other leg of "distributional" is
**precision-weighted updates** — the EMA today uses a schedule-based α
(`behavioral_state.py`, ramps ~0.5 → configured), so a noisy observation moves state as
much as a clean one. Probe B tests the *update rule*, offline, without shipping it:

1. From stored observation history, recompute an alternative EISV where the per-step α is
   scaled by inverse dispersion (confident observations move state more).
2. Expose the reweighted `risk`/`S` as new harness features
   (`prior_risk_precisionweighted_binned`).
3. If the reweighted feature out-predicts the as-stored one on the same paired delta,
   precision-weighting earns its place — that's the strongest single signal argument for
   the distributional build, and it's the cheapest *real* slice of it.

Probe B is only worth running if Probe A greenlights or is inconclusive-but-suggestive.

## Honesty caveats (state these in any result write-up)

- **Power.** ~80 bad task-outcomes over 90d; the harness gates `<10 bad` and `<100
  trusted` as INCONCLUSIVE. The ≥5-snapshot dispersion filter reduces coverage. A null
  here is weak evidence, not proof of absence.
- **Proxy ≠ the thing.** Static dispersion of stored snapshots is a *proxy* for the
  uncertainty an explicit precision term would carry. A positive result greenlights;
  a null leaves Probe B (the update-rule leg) still open, since precision-weighting can
  improve *calibration* even when a static dispersion feature doesn't improve *ranking*.
- **Leakage.** All dispersion must come from snapshots strictly before `o.ts - lead`. The
  existing query already enforces this; the window addition must keep both bounds.
- **What it does not prove.** This checks for measurable predictive lift over dumb
  baselines — not that EISV is the right ontology, and not that distributional EISV would
  let the ODE drive verdicts safely. It only decides whether the *signal* case for the
  build is earned.

## Why bother (the connecting thread)

This is the disciplined version of the whole glossary thread applied to code: a physics-
shaped upgrade (distributional state) gets a falsifiable test against the system's own
skeptic harness **before** it ships, so "more rigorous-looking" has to become "measurably
better" first. Greenlight buys the distributional build with evidence; a kill saves the
build and redirects to the real bottleneck. The probe is pure SQL + deterministic stats —
no model API, no new instrumentation — so it costs a day, not a quarter.

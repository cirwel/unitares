# When a control may make silence informative — 2026-08-23

**Status:** instrument correction, plus the general rule it generalises to.
Measures the *control*, not the deployment. No production data was read and no
database was queried.

**Scope guard:** this changes no threshold, flag, verdict, or weight. The
coherence gate shadow remains shadow-only with no APPLY counterpart, and the
operator's `k_pause=3.0 / k_block=4.0 / k_floor=5.0` selection stands exactly as
recorded. What changes is what its supporting control is entitled to claim.

## The gap this fills

[`falsification-design-system-audit-2026-08-23.md`](../ontology/falsification-design-system-audit-2026-08-23.md)
fixes six checks a result must pass before a **negative** may be called
`REFUTED`. That covers the direction where a project overclaims what it
disproved.

There is a mirror direction with no equivalent gate. When an instrument is
quiet, the repo already knows the zero is unfair unless a positive control shows
the instrument can fire — the `UNFAIR ZERO GUARD` in
`scripts/analysis/coherence_gate_shadow_read.py` says so, and
`coherence-proprioceptive-thresholds-v0.md` section 6 step 4 requires it.
Nothing checked whether the control itself could ever have failed.

A control that cannot fail does not license anything. It converts an untested
lever into a "validated" one for free, and the conversion is invisible because
the control genuinely runs, genuinely exercises the real code path, and
genuinely passes.

## The defect, in the instrument that had it

The control table in `scripts/analysis/coherence_gate_shadow_read.py` injected
each tier's excursion **in units of the threshold it was validating** —
`_history_with_excursion(0.0, spread, K_FLOOR + 0.5)`.
Raise k and the injected excursion rises with it. The control therefore
demonstrated that `>=` works, and nothing else.

Three consequences, all verified against the pre-repair code:

1. **It certified a tier from a state the system cannot produce.** At the
   shipped k, `floor_tier` reached `hard_block_floor` by setting **V = 1.10**.
   `BehavioralEISV.update` clamps V to `[-1, 1]` (`src/behavioral_state.py`).
   No deployed agent has ever been in that state or can be.

2. **It kept passing as k left the reachable range.** With `k_floor = 12` the
   scenario injected V = 2.50; at `k_floor = 25`, V = 5.10. The control
   reported PASS in both cases.

3. **It tested the wrong scale regime.** Spread was chosen at 0.2 precisely so
   "the empirical sd dominates the calibrated V floor". But 6,343 of 6,510
   eligible rows in the six-day read carried `scale_source=floor`. Tier
   reachability was demonstrated only in the regime holding ~2.6% of observed
   traffic. The one floor-scale scenario asserted `would_action != "proceed"`
   at |z| = 16 — far above every tier, so it never separated them.

The operator's k-policy call rests on this control in one clause: *"The
real-path positive control reached every intended tier."* It did. The top tier
was reached from V = 1.10.

## What the repair adds

- **A domain guard.** Every scenario's `V_history` and `V` must lie inside
  `[V_MIN, V_MAX]`. A tier that can only fire from outside is reported FAIL,
  however the comparison came out. `V_MIN`/`V_MAX` are now named in
  `src/behavioral_state.py` and the clamps use them, so the control and the
  runtime read one source.

- **Floor-scale tier coverage.** `pause_tier_floor_scale`,
  `block_tier_floor_scale`, and `floor_tier_floor_scale` exercise each tier in
  the regime nearly all real rows occupy, and assert `scale_source == "floor"`
  so the scenario cannot silently drift into the other regime.

- **An arithmetic reachability bound**, independent of any injected excursion —
  the one check a bad k cannot satisfy by construction. The statistic is
  `|V_current - mean(V_recent_prior)| / effective_scale` with
  `effective_scale >= floor` and V clamped, so

  ```
  |z| <= (V_MAX - V_MIN) / floor = 2.0 / 0.05 = 40.0
  ```

  bounds every magnitude the instrument can emit. `reachable::*` rows check each
  k against it and report what the k means in V units — `k_pause = 3.0` is
  `|ΔV| >= 0.15` at floor scale, which is the number the operator's call states
  by hand and the code now derives.

The repaired control fails where the old one passed:

| `k_floor` | before | after | why |
|---:|---|---|---|
| 5.0 | PASS | PASS | shipped value; reachable in both regimes |
| 12.0 | PASS | **FAIL** | empirical-sd scenario needs V = 2.50 |
| 25.0 | PASS | **FAIL** | both regimes leave V's domain |
| 45.0 | PASS | **FAIL** | above the 40.0 ceiling; no state reaches it |

## What this still does not establish

Reachability is **necessary, never sufficient.** A k selected because observed
traffic was quiet at it passes this control, and should. The control answers
"can the instrument fire at this tier?" — not "does anything fire?", not "should
it?", and not "does firing help?".

So the operator's own reading of the six-day zero is unchanged and correct:
another zero establishes quietness over the observed window, not efficacy and
not outcome validity. The soak read remains the only thing that speaks to
observed traffic, and neither speaks to outcomes at all.

One structural point the k-policy call leaves open, recorded here because this
audit is about controls and not about the policy: the promotion gate names
reopen conditions (control failure, routine or agent-concentrated crossings,
changed scale provenance, changed statistic) and states that another zero
establishes only quietness — but names no condition under which APPLY *would* be
authorized. A read whose every branch leads to "reopen" or "learned nothing" is
not yet a decision procedure. That is an operator call to make or decline, not a
defect for this change to fix.

## The general rule

A positive control earns the right to make silence informative only if all four
hold. Any instrument whose zero is cited as evidence should be able to answer
these.

| Check | Required question |
|---|---|
| Falsifiability | Can this control fail? Name the input that makes it FAIL, and verify it does. |
| Domain | Does every scenario stay inside the range the deployed variable is clamped or bounded to? |
| Regime | Is the tier exercised in the regime the observed traffic actually occupies, not only a convenient one? |
| Independence | Is the injected effect parameterised by something other than the threshold being tested? |

The fourth is the one that fails silently. `scripts/analysis/ablation_power_probe.py`
is the counter-example worth copying: it plants an effect of known **absolute**
strength (`beta`) and measures the fraction of trials the instrument catches, so
its answer moves when the instrument's power moves. A control parameterised by
its own threshold has no such degree of freedom.

## Not fixed here

- **Whether observed traffic reaches any tier.** Needs the soak read against the
  deployment database; unchanged by this work.
- **The other instruments' controls.** `scripts/ops/dialectic_canary.py` states a
  usable gate contract (green canary + organic zero retires the lever, red canary
  restarts the clock) and `ablation_power_probe.py` is parameterised
  independently, so neither shows this defect. The rest of the falsification
  surface is inventoried in the design-system audit (PR #1836), not here.
- **The k-policy promotion gate's missing PASS condition**, noted above as an
  operator call.

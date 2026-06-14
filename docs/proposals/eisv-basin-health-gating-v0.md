# EISV self-relative risk: absolute-basin-health gating (issue #689)

Status: shipped — PR #696 (issue #689), 2026-06-14; refined by #699 (z-floor scaled by EMA alpha)
Supersedes the blunt portion of: PR #686 (`MIN_MEANINGFUL_EISV_STD = 0.05`)
Council: architect + code-reviewer + live-verifier (see "Council" below)

## Problem

After warmup, `assess_behavioral_state` scores risk from **z-score deviations of
the agent's current EISV from its own behavioral baseline**
(`_score_self_relative`). For an ultra-stable agent the baseline σ collapses to
~0.007–0.024, so a small, **absolutely healthy** fluctuation becomes a many-σ
"severe deviation". On 2026-06-13 the Sentinel resident (E 0.77→0.66, I 0.68→0.66
— both healthy) scored:

| component | z (σ≈true) | risk |
|-----------|-----------|------|
| low_E     | −4.69     | 0.300 |
| low_I     | −3.51     | 0.300 |
| high_S    | +3.17     | 0.200 |
| high_V    | −2.57     | 0.143 |
| **risk**  |           | **0.943 → high-risk → cirs_block → ~18h pause** |

PR #686 added a flat floor on the z denominator
(`MIN_MEANINGFUL_EISV_STD = 0.05`, `WelfordStats.z_score(min_std=…)` via
`BehavioralEISV.deviation()`). It drops the Sentinel trace to 0.333 (safe) and is
live. It works but is blunt:

1. It is an empirical constant with no EISV-semantic derivation.
2. The baseline σ is computed over **EMA-smoothed** E/I/S/V
   (`behavioral_state.update` feeds the smoothed scalars into Welford), so σ
   collapse is partly a *double-smoothing artifact*. A single flat 0.05
   over-floors slow-α dims (I, α=0.08) and under-floors fast-α ones (S, α=0.15).
3. It still lets a tight-σ agent accrue risk from purely self-relative motion
   while sitting in an absolutely-healthy state.

Blast radius: ~21–22 of 96 recently-baselined agents have tight (σ<0.05) E/I;
**all baselined residents are in this hypersensitive class.**

## Options considered

### (a) Basin-gate self-relative risk — **chosen**
Multiply each self-relative EISV risk component by a per-dimension gate
`g ∈ [0,1]` that measures how far the **absolute** value has travelled from the
healthy-basin edge toward its absolute danger floor:

```
low_E  : g = clamp((0.60 − E) / (0.60 − 0.30), 0, 1)     # BASIN_HIGH.E_min → ABSOLUTE_E_FLOOR
low_I  : g = clamp((0.70 − I) / (0.70 − 0.30), 0, 1)     # BASIN_HIGH.I_min → ABSOLUTE_I_FLOOR
high_S : g = clamp((S − 0.25) / (0.70 − 0.25), 0, 1)     # BASIN_HIGH.S_max → ABSOLUTE_S_CEILING
high_V : g = clamp((|V| − 0.15) / (0.50 − 0.15), 0, 1)   # BASIN_HIGH.V_abs_max → ABSOLUTE_V_CEILING
```

Inside the healthy basin `g = 0`: "you moved from your own norm" is information,
not danger, and contributes no risk. As a dimension leaves the basin toward its
absolute floor, `g` ramps 0→1, restoring full self-relative sensitivity exactly
where σ resolution is meaningful. The absolute floors (`_score_absolute_floors`)
remain a hard backstop via the existing per-component `max()`.

Why this is principled:
- It gates on **absolute health**, never on σ — so it never blunts the
  meaningful variance of a genuinely unstable agent (rejects the global-
  desensitization failure mode the issue calls out).
- It **sidesteps the double-smoothing artifact entirely**: inside the basin a
  collapsed σ is irrelevant because the gate is 0, regardless of how many σ the
  move spans. No per-α correction needed.
- The healthy edges are single-sourced from `config.governance_config.BASIN_HIGH`
  (duplicated as constants in `behavioral_assessment` only to keep numpy/config
  off the scoring hot path; a drift-guard test pins parity).

### (b) Per-dimension σ floor tied to the EMA step — rejected
Derive `min_std_dim` from each dimension's EMA α (e.g. the steady-state variance
attenuation `α/(2−α)`). This corrects the over/under-floor asymmetry of (a-flat)
but is still a σ-floor: it desensitizes by dimension globally, still raises risk
for self-relative motion inside the basin, and adds a second hand-tuned mapping
(α→floor). The basin-gate removes the *need* for any σ correction in the
in-basin regime, so (b) solves a problem (a) no longer has.

### (c) Keep the flat floor, justify the value — rejected as the primary fix
Retained only as **secondary** defense-in-depth (see below). As the primary
mechanism it leaves all three weaknesses above.

## Decision

- **Primary:** basin-health gate (`_basin_health_gate`) applied to the four
  EISV-derived components in `_score_self_relative`. rho and continuity-energy
  components are absolute signals and are intentionally not gated.
- **Secondary:** `MIN_MEANINGFUL_EISV_STD = 0.05` is **retained** as
  defense-in-depth — it bounds the raw z-magnitude in the boundary region (gate
  partially open) so a collapsed σ cannot produce an absurd z there. It only
  binds when `std < 0.05`, so it never touches unstable agents. With the gate in
  front, its exact value is far less load-bearing than under #686.

This is "augment" rather than "replace": the gate is the new principled
mechanism; the flat floor degrades to a numerical guard. Minimal blast radius —
`deviation()` semantics are unchanged, and the #686 σ-floor tests stay green.

## Empirical validation

Harness: `scripts/analysis/validate_basin_gate.py` — rebuilds a `BehavioralEISV`
from a persisted baseline + current EISV and assesses it under the current
(flat-floor-only, gate forced open) vs proposed (gate active) regimes. Runs
trace-anchored cases always; `--db` adds a live-fleet sweep over the most recent
`state_json->'behavioral_eisv'` per recently-baselined agent in
`core.agent_state` (the path the live-verifier runs post-merge).

Field provenance (issue gotcha): EISV is read from `state_json->'behavioral_eisv'`
(phi from `state_json->>'phi'`). The `core.agent_state.entropy` column stores
behavioral **S**, not phi.

Results (this environment — trace + parametric sweep; the live DB is not
reachable from the web container, so the 21-agent live sweep is deferred to the
live-verifier's environment via `--db`):

```
Sentinel pause (real trace)        before=0.333/safe       after=0.053/safe
Sentinel S=0.70 (danger edge)      after high_S=0.200 (genuine excursion still scores)

in-basin +0.06 E wobble            before=0.000/safe       after=0.000/safe
in-basin +0.05 S wobble            before=0.000/safe       after=0.000/safe
in-basin multi-dim small wobble    before=0.093/safe       after=0.000/safe
boundary I→0.40 (de-escalation)    before=0.800/high-risk  after=0.278/safe
boundary S→0.62 (de-escalation)    before=0.800/high-risk  after=0.289/safe
deep exit E→0.35,I→0.45,S→0.65     before=1.000/high-risk  after=0.644/high-risk
abs-floor breach E→0.20,I→0.25     before=0.867/high-risk  after=0.800/high-risk
```

Key properties confirmed:
- **False-pause fixed:** the real Sentinel trace lands at 0.053 (deep safe),
  not merely below the caution line.
- **No masking of basin-exit / absolute danger:** the deep multi-dim basin exit
  (0.644) and the absolute-floor breach (0.800) still reach high-risk → pause.
  (See "Accepted cost" below for the one regime this path deliberately does not
  cover — slow creep that stays *fully inside* the basin.)
- **Gate never raises risk:** `after ≤ before` for every case (multiplier ∈ [0,1]).
- **Sensitivity preserved, not desensitized:** the boundary states the gate
  de-escalates (0.278, 0.289) still score *above* what the system's own absolute
  fixed-threshold scoring gives a fresh agent at the identical state (0.016,
  0.048). The gate sits between "absolute" (floor) and "ungated self-relative"
  (ceiling) — closer to absolute the deeper inside the basin.

## Accepted cost / non-goal

By design, self-relative degradation that stays **fully inside** the healthy
basin accrues **zero** risk on this path (gate = 0). The deliberate consequence:
a slow drift that asymptotes just inside a basin edge — never crossing
E=0.60 / I=0.70 / S=0.25 / |V|=0.15 — is not caught by self-relative scoring.
This is the intended semantics ("inside the basin, moving from your own norm is
information, not danger"), not an oversight, but it is a real trade against the
pre-#686 behavior, which would have flagged such drift via the σ-explosion.

In-basin slow creep is left to the other, orthogonal signals that already exist:
the boundary ramp (the moment any dimension crosses its basin edge the gate
begins to open), the absolute floors, the `trend` bonus/penalty path, and
coherence-based health. If longitudinal in-basin creep detection is later judged
necessary, it belongs in a dedicated trend monitor, not in re-sensitizing the
self-relative z-path inside the basin (which would reintroduce the false-pause).

### Gate-exposed weakness of the absolute floors (live-verifier finding)

The live-verifier's adversarial grid surfaced a class the gate **exposes but does
not create**: a state breaching exactly ONE absolute floor while the other
dimensions are healthy (e.g. E=0.20 with I=0.65, S=0.18, V=0.0) goes
`before=0.60/high-risk → after=0.34/safe`. Root cause: a single absolute-floor
breach contributes at most ~0.30 (E→0) or ~0.20 (S/|V|), both below the 0.35
safe threshold. Pre-#689 such a state was held high-risk by the **false**
self-relative σ-explosion on the *healthy* dimensions (counting I=0.65 as −3σ
"dangerous"); the gate correctly removes that false contribution, leaving only
the genuinely-weak floor backstop.

Two things are true at once: (a) the gate never scores a baselined agent *below*
what the system's own absolute fixed-threshold scoring gives a fresh agent at the
identical state (E=0.20/healthy-rest scores 0.15 fresh vs 0.34 gated — the gate
is the more conservative of the two), so it is not masking relative to the
absolute standard; (b) the absolute floors were nonetheless never strong enough
for a single hard danger-edge breach to stand on its own, and the gate makes that
latent weakness reachable on more states.

**Operator decision (2026-06-14): leave as-is, no follow-up.** Because the gated
score is already the more conservative of the two relative to the absolute
baseline, the single-floor-breach behavior is accepted as the intended floor
semantics; the absolute floors are not being strengthened now. Documented here
so the behavior is discoverable and not later mistaken for a regression
introduced by the gate.

## Acceptance mapping

| Acceptance criterion | Status |
|---|---|
| Stable-agent false-pause stays fixed (Sentinel trace safe) | ✅ 0.053, verdict safe |
| Genuine basin-exit still pauses | ✅ deep exit 0.644 / floor breach 0.800 → high-risk |
| Flat 0.05 replaced/justified by basin-gated rule | ✅ gate primary; flat floor demoted to documented secondary guard |
| Tests cover false-positive (stable wobble safe) + true-positive (basin exit flags) | ✅ `TestBasinHealthGate`, `TestBasinGateSentinelTrace` |
| Full test-cache.sh green | see PR CI |

## Council

- **Architect — APPROVE-WITH-CHANGES (addressed).** Confirmed basin-gating is
  the right fix over a per-dim σ floor; confirmed the linear ramp (not assumed
  in-basin membership) is what keeps the boundary-region Sentinel safe; confirmed
  no circularity (gate reads only absolute EISV, never risk/coherence). Required:
  document the in-basin slow-creep accepted cost (done, "Accepted cost" above).
  Suggested: clarify the floor `max()` backstop in the docstring (done).
- **Code-reviewer — APPROVE.** Verified gate math (no div-by-zero: the
  `value>=healthy`/`value<=danger` early-returns precede the division), correct
  component application (rho/CE ungated), the `max()` floor backstop, parity
  drift-guard sufficiency, `deviation()` callers unaffected, and strong (non-
  tautological) tests. Nit (addressed): reworded the import-avoidance comment.
- **Live-verifier — APPROVE-WITH-CHANGES.** Audited the harness method (correct;
  reads `state_json->'behavioral_eisv'`, not the entropy column). Reproduced the
  default run; confirmed 0 violations of "gate never raises risk" across a 9,680-
  point grid. Live DB not reachable from this container — the 21-agent `--db`
  sweep is the operator-side pre-merge step. Found the gate-exposed single-floor-
  breach masking class (documented above) — recommends running `--db` before
  merge and a separate follow-up to strengthen the absolute floors.

Operator is the merge gate; PR left as draft. Do not auto-merge. Pre-merge
operator step: `python3 scripts/analysis/validate_basin_gate.py --db` in a
DB-reachable env; a `genuine-risk-masked: 0` line clears the live sweep.

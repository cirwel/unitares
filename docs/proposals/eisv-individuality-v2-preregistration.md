# EISV individuality test v2 — pre-registration

**Status:** PRE-REGISTERED 2026-07-02. Design frozen at the merge commit of the
PR introducing this file, after an adversarial design review
(independent statistical, implementation-correctness, and live-data
feasibility passes) whose material findings are incorporated and disclosed
below. **The verdict counts only on
raw observations recorded after `2026-07-02T18:00:00Z`** — an instant later
than every piece of data consulted during design, including the review's.
Companion to `docs/proposals/eisv-grounding-next-move-v0.md` (step 4);
successor to the v1 operationalization in
`scripts/analysis/eisv_self_predictability.py`.

## Why a v2 exists (and why it is suspect by construction)

The v1 gate — per-agent EMA must beat fleet-mean AND persistence AND fitted
AR(1) on 1-step MAE over the raw series — **failed 0/4 at native cadence**
(2026-07-01 early read; confirmed 2026-07-02 with cadence stratification,
PR #1355). Two structural problems were then discovered in the
operationalization itself:

1. **Sticky measurement.** Sentinel's raw series is 53% exact consecutive
   repeats. Against a piecewise-constant series, last-value is 1-step-MAE
   optimal at any cadence *whether or not a stable per-agent normal exists*.
2. **The AR(1) leg tests estimator choice, not individuality.**
   AR(1)-with-intercept nests the stationary per-agent normal (φ→0 reduces it
   to the per-agent mean), so on the axiom's own best case the expanding
   AR(1) fit converges to the optimal predictor and the fixed-α EMA loses
   asymptotically (pinned by regression test in
   `tests/test_eisv_self_predictability.py`).

Because v2 was designed AFTER seeing v1 fail, it gets stricter treatment:
fresh data only, thresholds frozen here, machinery validated on synthetic
organisms, an adversarial design review run BEFORE freeze, and a kill criterion.

The v1 result stands as reported: FAILED. v2 does not reopen v1.

## The claim under test (scoped by review findings R2/R4)

**Each eligible agent's raw behavioral EISV series has an agent-specific,
temporally stable operating level** — a level the series is anchored to
(excursions revert; the level does not wander) and that is distinct and
rank-stable across agents.

Two scope concessions, made explicit rather than discovered later:

- **Stable environmental coupling is admitted** (review finding R2). A fleet-shared
  driver with agent-specific stable gains (x_i = μ + β_i·Z_t + ε) is
  indistinguishable from an intrinsic home in this data and — for the
  residual paradigm's purpose, a per-agent reference against which deviation
  is meaningful — does not need to be distinguished. A PASS earns "stable
  agent-specific operating level," not "intrinsic essence."
- **The eligible population is ~4 role-distinct resident daemons**
  (review finding R4 + live-data feasibility check). A PASS must be read as "these residents show
  anchored, distinct, rank-stable operating levels," NOT as the universal
  individuality axiom over arbitrary agents. Two instances of the same role
  sharing a home is untested here.

This says NOTHING about outcome validity (label-blocked); it is the
precondition for a residual to mean anything.

## Structure: two gating legs + one estimator leg

| Leg | Question | Null it must beat |
|---|---|---|
| A(i) — reversion | Does reversion structure exist (vs pure random walk)? | Increment permutation: same increments, shuffled order |
| A(ii) — drift veto | Does the level wander more than a stationary process would? | Small-block permutation: short-range correlation kept, long-range wandering scrambled |
| B — individuality | Is the anchor level agent-specific and rank-stable over time? | Agent-label permutation on split-half means |
| C — reference quality at jumps (estimator; NOT part of the axiom verdict) | When the state moves, does the runtime-form EMA beat last-value? | Persistence at moved observations |

**Why leg A has two parts (review finding R1, the design's hardest finding):** the
raw observations are ~10-event rolling-window features
(`src/behavioral_sensor.py`, `history[-10:]`). Window-filtered measurement
noise reverts at short horizons *by construction*, so an affirmative
variance-ratio rejection alone cannot distinguish "stable level" from
"slowly drifting level under window noise" — the adversarial organism (a
random-walking behavior rate seen through the window) defeats any single VR
statistic at feasible horizons; this was confirmed empirically during test
construction, not just argued. Part (i) therefore proves reversion exists
(kills pure walks), and part (ii) — sensitive exactly where (i) is blind —
vetoes level-wandering. A dim passes leg A only if (i) rejects AND (ii) does
not fire. Consequence, disclosed: an agent with a *constant* underlying
behavior rate passes leg A — correctly, because a constant rate IS a home;
the `windowed-stable-rate` organism pins this as intended behavior. Leg A is
the anti-drift precondition; **leg B carries the individuality burden.**

### Leg A — anchoredness (per agent, per dim, E/I/S)

- **(i)** `VR(h) = Var(x_{t+h} − x_t) / (h · Var(x_{t+1} − x_t))`,
  overlapping differences, primary **h = 24** (chosen > 2× the 10-event
  feature window; h ∈ {8, 16, 48} reported descriptively, no inference).
  Null: permute the 1-step increment sequence (1000 permutations, per-dim
  seeds), rebuild cumulatively, recompute. One-sided
  `p = (1 + #{perm VR ≤ obs}) / (1 + N)`. Rejects at p < 0.05.
- **(ii)** Dispersion (variance) of non-overlapping big-block means
  (block = 32 obs) vs a null that permutes small blocks (16 obs ≥ window)
  and recomputes; veto fires when observed dispersion exceeds the null at
  one-sided p < 0.05. Weak on short series (few blocks) — which is why (i)
  must also pass affirmatively; veto power grows toward the final read.
- **Dim passes** = (i) rejects AND (ii) does not fire. **Agent passes leg A**
  on ≥ 2 of 3 dims.

### Leg B — split-half stability of the per-agent home

Across eligible agents, per dim: split each series at its midpoint (row
count); Spearman rho between first-half and second-half mean vectors across
agents; null = agent-label permutation of the second-half vector (exact for
≤ 7 agents, else 1000 sampled). One-sided; dim passes at p < 0.05; **leg B
passes on ≥ 2 of 3 dims.** At the n=4 verdict floor the exact null has 24
orderings, so only a perfect rank agreement (rho = 1) clears α = 0.05 —
brittle by design and disclosed: with 4 agents, leg B passes only if the
home ordering is exactly preserved out-of-sample in time.

### Leg C — jump-conditional reference quality (estimator leg)

At moved observations (consecutive values not byte-identical, |Δ| > 1e-12),
after a 15-observation burn-in: compare `|ema_ref − x_t|` vs `|x_{t−1} − x_t|`
where the reference is the live EMA's **form** (runtime alphas E 0.12 /
I 0.08 / S 0.15, folding every observation) **cold-started at the first
post-registration observation** — an approximation of, not a replay of, the
true runtime EMA, whose earlier history the fresh-data rule excludes.
Win rate > 0.5 with one-sided binomial p < 0.05, trials pooled across dims.
Disclosed caveats: the pooled binomial treats cross-dim trials at one
timestep as independent (they are not; the p-value is anti-conservative),
and the per-row persisted `alphas` are cross-checked — a mismatch against
the hardcoded runtime mirror invalidates leg C for that agent (legs A/B are
alpha-free). Leg C **does not gate the axiom**: axiom-pass + C-fail = retune
the reference; it can neither rescue nor kill the axiom.

## Eligibility, verdict rule, schedule

- **Agent eligibility:** ≥ 100 post-registration raw states AND ≥ 30 moved
  observations.
- **Verdict floor:** ≥ **4** eligible agents. Live-verified arithmetic
  (2026-07-02): exactly four agents have unambiguous accrual support —
  Sentinel (~287 rows/day), Watcher (~133), Vigil (~54), lumen-broker-ex-
  shadow (~480/day measured cadence; eligible within hours if the P2 soak
  survives past the cutoff). Claude-session identities fragment by design
  (fresh agent_id per session under strict identity) and have never
  sustained 100 states; Lumen and Steward emit zero raw_obs rows. A floor of
  5 would make the verdict hostage to an atypical long-lived session — the
  arithmetically-unwinnable-gate failure mode the label-power analysis
  already killed once. At n=4: leg A majority = ≥ 3 of 4; leg B as above.
- **AXIOM EARNED** iff leg A passes for a majority of eligible agents AND
  leg B passes.
- **Reads:** interim (trend-only) **2026-07-16**; final **2026-07-30**.
  If < 4 eligible agents at the final read: **NOT EVALUABLE — treated as
  not-earned for all shipping and public-framing purposes** (identical
  consequences to FAIL; distinct wording because it indicts fleet activity).

## Provenance disclosure (review finding R5)

Chosen before any post-registration data existed, with the following
design-time knowledge: the v1 FAIL and its diagnostics (stickiness fractions,
φ decay, decimation curves) on pre-registration data; the feature window
length (10 events) read from source during the review; measured accrual
rates. h was initially drafted as 8 *before* the window length was inspected;
the review flagged h < window as mechanically confounded and h moved to 24
(> 2w) plus the drift veto was added — both changes made in direct response
to an adversarial organism, before freeze, with the organism now in the test
suite. α = 0.05, dim-majority 2-of-3, and the eligibility floors were set by
convention and accrual arithmetic respectively, not tuned against any VR
curve. The residents' processes are stationary across the freeze line, so
garden-of-forking-paths risk cannot be fully eliminated by the timestamp —
it is mitigated by the organism-validated nulls, the adversarial review,
and the kill criterion below.

## Kill criterion

If v2 **fails** at the final read (2026-07-30) on the frozen thresholds: the
individuality axiom is **retired for raw behavioral EISV as currently
measured**. No v3 against this measurement process — a further attempt
requires *changing the measurement* (different raw features, event-triggered
sampling), pre-registered before any of its data exists. The
grounding-program reconsideration escalates per the preserved dissent in
`eisv-grounding-next-move-v0.md`.

Standing prohibitions (unchanged, restated for self-containment): no public
"EISV self-model" framing, no Stage B, nothing new wired to the live verdict
path — regardless of v2's outcome. A v2 PASS earns the scoped claim above,
for this population, only.

## Known limitations (documented, not fixed)

- **Cadence heterogeneity** (review finding R3): h and blocks are step-counts, so
  h=24 spans ~2 hours for Sentinel and ~11 hours for Vigil. The per-agent
  verdicts therefore probe anchoredness at different wall-clock scales; the
  report includes each agent's median gap so reads are interpreted per-agent.
- **Leg B common-driver admissibility** (F2) and **population scope** (F4):
  see the claim section — conceded and scoped, not controlled away.
- **Per-dim tests share the agent's underlying events**, so the 2-of-3
  majorities are over correlated tests (conservative direction for type-I;
  permutation seeds are decorrelated per dim, review finding R7).

## Implementation

`scripts/analysis/eisv_individuality_v2.py` — frozen in the same PR. The
fetch applies the registration cutoff in SQL (with a deterministic
`state_id` tie-break); there is deliberately no flag to include earlier
rows. Machinery validated by deterministic model-organism tests
(`tests/test_eisv_individuality_v2.py`):

| Organism | A(i) | A(ii) veto | A | B | Why it matters |
|---|---|---|---|---|---|
| Random walk | no reject | — | fail | — | the dressed-up-autocorrelation null |
| iid noise, distinct means | reject | no | pass | pass | the textbook case |
| Sticky-anchored (pins + reverting jumps) | reject | no | pass | pass | the v1 blind spot — v2 sees through stickiness |
| Sticky-drift (pins + same-sign jumps) | — | fires | fail | — | anti-laundering: drift must not pass |
| Windowed stable rate (review finding R1) | reject | no | pass | — | window mechanics + constant rate = a real home, passes by design (disclosed) |
| Windowed drifting rate (review finding R1) | reject (falsely) | **fires** | fail | — | the organism that defeats any single-statistic leg A — caught by the veto |
| Shared home (identical means) | reject | no | pass | fail | anchored ≠ individual |

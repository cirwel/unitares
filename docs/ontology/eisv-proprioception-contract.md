# EISV Proprioception Contract

**Created:** June 26, 2026
**Last Updated:** August 6, 2026
**Status:** Active

---

## Contract

**EISV is proprioception**: runtime self-state telemetry about agent strain,
coherence, entropy, integrity, and imbalance. It is the system saying "my balance
is changing" or "this process is running hot," not a court deciding whether a
worker was morally bad.

EISV is **not an outcome oracle**, **not a grand jury**, and **not a
bad-verdict dispenser**. It does not decide whether harm occurred, whether the
user was wronged, or whether an agent is "guilty." Those judgments require
external outcome evidence, policy, and review surfaces that are separate from
the measurement vector.

## Core mathematical posture

The public math should lead with proprioceptive residuals, not bad-outcome
classification. The live behavioral path is deliberately modest:

- **Warmup:** the behavioral track scores against fixed universal thresholds
  while the agent has too little history for individualized drift; the live
  verdict falls back to the mostly server-derived cold-start prior.
- **After warmup:** self-relative z-score deviation from the agent's own Welford
  baseline.
- **Always:** absolute safety floors and basin-health gates remain in force.

The roadmap target for richer cold-start grounding is a hierarchical reference:

```text
measurement_t = EISV_t
reference_t   = blend(agent_baseline_t, class_anchor; w(grounding))
residual_t    = measurement_t - reference_t
```

Until that hierarchical blend is the live path, public docs should describe class
or population anchors as roadmap semantics, not deployed authority. The stable
semantics are still residual-first: `residual_t` is information about state change
— running hot, brittle, scattered, or unusually careful — before it is a policy
concern. Deviation inside a healthy basin is room to learn, not proof of failure.

## Layer separation

The canonical stack is:

```text
measurement → diagnosis → policy → enforcement → external outcome evidence
```

| Layer | Owns | Must not pretend to own |
|---|---|---|
| Measurement | EISV, confidence, coherence, risk, phi, provenance | Outcome truth, blame, or punishment |
| Diagnosis | Interpretations such as strained, scattered, brittle, running hot | The authority to pause by itself |
| Policy | Rules mapping measured/diagnosed state to advice, review, or circuit-breaker candidates | Raw measurement truth |
| Enforcement | Actual pause/block/review/allow effects, with actor and mode | The reason an outcome was good or bad |
| External outcome evidence | Test/CI/tool/user/harm/verifier results, with provenance | Retrospective EISV self-justification |

A thermometer can inform a safety protocol; it does not itself pause the body.
Likewise, EISV can inform governance policy, but pause authority belongs to a
policy/enforcement layer.

## Evidence / label taxonomy

The word `bad` in storage and reports is a compact label class, not a moral
verdict. Human-facing docs should name the class whenever possible:

| Class | Examples | Validation use |
|---|---|---|
| `task-negative` | CI failed, test failed, command exit nonzero, answer was corrected | Useful for calibration and rework prediction; not automatically governance-bad |
| `contract/process violation` | Claimed tests passed without running them, hid uncertainty, ignored explicit constraints, fabricated output | Stronger governance signal because it breaks the work contract |
| `authority/harm` | Deleted data, leaked secrets, bypassed ask-first boundaries, charged money, spammed or impersonated a user | Strict governance-bad / escalation class when externally verified |
| `synthetic red-team fixture` | Known-safe adversarial cases, negative controls, containment probes | Validates plumbing and containment only; never production trust by itself |
| `unknown/unmeasured` | No verifier, no rubric, no external signal | Exclude from strict validation |

A normal mistake is usually task-negative feedback. It becomes governance-bad
only when it is tied to a contract violation, authority overreach, concealment,
or user harm.

## Validation rule

Never validate EISV by letting EISV create its own labels. A prospective claim
needs all of the following to be meaningful:

1. a prediction or prior-state snapshot recorded before the outcome;
2. an external label source or pre-registered rubric;
3. enough negative-class coverage for the lane being scored;
4. comparison against boring baselines such as previous outcome, task type,
   confidence, recency, and harness lane;
5. language that distinguishes internal signal, predictive lift, policy effect,
   and enforcement effect.

Red-team data is useful, but only if labeled as red-team data. It answers "can
we detect this frozen failure mode?" It does not answer "does EISV generally
judge agents correctly in production?"

## Tested claims — ledger

Added 2026-07-29. This section exists because the doctrine above was already
correct in June and nine surfaces drifted anyway. Doctrine states what we may
say; this ledger states what we have actually tested, with the mechanism cited
to line. Statuses are deliberately distinct:

| Status | Meaning |
|---|---|
| `EARNED` | pre-registered test passed |
| `REFUTED` | tested with adequate power, failed |
| `REFUTED BY CONSTRUCTION` | the code makes the claim arithmetically impossible; no data required |
| `UNTESTED AS DEPLOYED` | a test ran and returned a negative, but the instrument had no usable power against the claim |
| `LABEL-BLOCKED` | not testable at current external-label supply |

**1. Individuality axiom** — *"each agent's raw behavioral EISV series has an
agent-specific, temporally stable operating level."* → **UNTESTED AS DEPLOYED.**

Pre-registered v2 (`docs/proposals/eisv-individuality-v2-preregistration.md`,
script sha `e512c01c…`, thresholds frozen 2026-07-02) returned **FAIL** at the
2026-07-16 interim read; an unofficial dry run of the frozen script on
2026-07-29 reproduced it (leg A 1/7, leg B 1 of 3 dims).

**The pre-registered FINAL read ran 2026-07-30 09:00 local and returned FAIL**
(`~/.unitares/analysis/eisv-individuality-v2-final-2026-07-30_0900.md`, script
sha `e512c01c…` confirmed in the report header — the repo copy, matching the
frozen snapshot). **Leg A 0/7, leg B fail (E rho=0.86 p=0.012; I p=0.249;
S p=0.118), leg C 0/7.** It is *harder* than the dry run: Vigil, the lone leg-A
pass on 07-29, flipped to fail on one further day of data, which tells you that
pass was fragile rather than that the instrument changed. The 2026-06-13
broken-join checks pass — `Eligible agents: 7`, a real verdict line, populated
per-agent and VR-curve tables — so this is a finding, not an empty join.
**The kill criterion is triggered and honoured: the axiom is retired for raw
behavioral EISV as currently measured; no v3 without changing the measurement
process.**

Honour that as a pre-registration commitment kept, and do **not** record the
FAIL as evidence against the axiom. Two independently sufficient reasons, both
still visible in the final report — note its eligible list contains
`lumen-broker-ex-shadow`, `..._a00e9d21` and `..._f4eba889`, i.e. three of the
seven "agents" are the replicate Pi described below:

- **Leg A is a whiteness detector.** Against synthetic stationary,
  mean-reverting series that *all satisfy the axiom*, with only autocorrelation
  length varying, pass rate falls from 1.00 (φ=0, white) to 0.07 (φ=0.90) to
  0.00 (φ=0.95). Mechanism: `drift_veto` permutes 16-observation blocks, which
  destroys between-block correlation a stationary process legitimately holds at
  lags 16–32, so the null's dispersion is too tight and the veto false-fires —
  ~0.70 at N=1500 against a nominal 0.05, rising with N. The more strongly an
  agent satisfies the axiom, the more certainly it fails leg A. This is the v1
  unwinnable-gate failure mode reproduced inside the v2 instrument.
- **Leg B ran at effective n=4**, where the exact permutation null admits only
  perfect out-of-sample rank preservation (p_min = 1/24 = 0.042) — brittleness
  the spec disclosed. The nominal n=7 counted one Raspberry Pi four times: the
  three `lumen-broker-ex-shadow` identities replicate Lumen at matched
  timestamps (E r=0.952, I r=0.932, **S r=0.998**, byte-identical rows) and died
  at the Elixir broker cutover. Collapse them and **no dimension passes**
  (all p=0.1667); a within-cluster rank shuffle gives P(rho ≥ 0.857) = 0.264,
  i.e. the one apparently-passing dimension was the single one-Pi-vs-cron-daemons
  block contrast.
- **The window contains a fleet outage the statistics cannot see.** The host was
  shut ~10 days inside the observation window (2026-07-12→19 and 07-22→23); fleet
  volume fell from ~137k tool calls/day to 200–250. Over 07-14→18 Lumen and Vigil
  emitted **zero** observations and Sentinel fell to 28 from 862. Legs A and B
  count *steps, not wall-clock*, so the observations either side of the gap are
  treated as adjacent — a ~6-day discontinuity spanning a service restart, read
  as one time step, in a test about whether a level reverts or wanders. The spec
  contemplates cadence heterogeneity but not an outage. Any future
  resident-trajectory test should be wall-clock-aware or exclude such gaps
  explicitly.

Corollary: the **n=4 verdict floor is absence-limited, not structural.** The
spec's feasibility argument asserted Claude-session identities "have never
sustained 100 states"; the longest single session by month is Apr 18, **May 107**,
**Jun 93**, Jul 53, with 212 sessions in June against 37 in July. Arithmetic
concluding that the eligible population cannot grow — and therefore that further
streams require new hardware — is unsound if derived from the July window.

**2. Individuality of the I dimension** → **ATTENUATED BY CONSTRUCTION** (corrected
2026-07-31; this row previously read "REFUTED BY CONSTRUCTION" and overstated).
The mechanism is real and re-verified: `cal_I` carries 50–60% of I
(`src/behavioral_sensor.py:147,149`) and comes from `get_mean_calibration_error`
(`src/mcp_handlers/updates/context.py:85`), which takes no `agent_id` — it averages
bins from a module-level singleton keyed by confidence range only, and `agent_id`
appears **zero** times in `src/calibration.py`. Every agent receives the identical
scalar on the same tick.

But the inference previously drawn from that mechanism does not follow, on three
independent grounds:

- **A fleet-common additive term cannot move a rank statistic.** Leg B is a Spearman
  correlation on split-half *means*. A component shared identically across agents at
  each tick shifts every agent's mean by the same amount and leaves the ordering
  untouched. Whatever defeated leg B's I dimension, it was not this.
- **40–50% of I is agent-local**, so I is diluted, not constant.
- **The remaining signal is not negligible**: the Sentinel–Vigil I gap of 0.0896 is
  3.3× the mean within-agent SD.

So the earlier claims "a per-agent I *home* is not a measurable quantity" and
"leg B's I failure is arithmetic, not sampling noise" are **struck**. The honest
statement is that I is a low-contrast dimension whose agent-specific component is
diluted by a fleet-common majority term — a measurement weakness worth fixing, not
a proof that the quantity does not exist.

*Why the distinction is load-bearing rather than pedantic:* the v2 kill criterion
permits a v3 only on a **changed measurement**. Measurement defects are therefore
the licence key, and an overstated measurement defect is an inflated licence. An
agent-scoped calibration feature remains a legitimate qualifying change; it should
be justified on its actual merits, not on a refutation that the data do not support.

**3. The per-agent reference does useful work** → **REFUTED as deployed.** Leg C
compares the runtime-form EMA reference against last-value persistence at moved
observations: 0/7 streams beat persistence on 2026-07-29 (win rates 0.32–0.48);
1/7 did at the interim (Sentinel 0.52, p=0.031). Caveats that must travel with
the number, both spec-disclosed: leg C scores a *cold-started reconstruction* of
the reference rather than the deployed EMA, and the pooled cross-dim binomial is
anti-conservative. The honest reading is that the reference is not currently
earning its description — not that a correctly-warmed reference could not.

**4. Outcome validity** → **LABEL-BLOCKED, and negative where measurable.** The
tracked weekly ablation (`~/.unitares/analysis/eisv-skeptic-trend.tsv`) has
lead-30 `auc_delta` = −0.181 against a previous-outcome baseline: adding
EISV/prior-state makes prospective prediction *worse*, not merely no better.
Never quote an EISV outcome-AUC as validation.

**5. "Bounded and mean-reverting, not a random walk"** → **partly TRUE BY
CONSTRUCTION.** E/I/S are hard-clipped in `src/behavioral_state.py` and S is
floored in `src/behavioral_sensor.py`, so boundedness is not a finding. Do not
cite it as one.

**Measurement note for any future work.** `src/behavioral_state.py` persists
`raw_obs` as `round(v, 4)`. Post-cutoff, for Sentinel, Vigil and Watcher the
median one-step change is **exactly zero** at that resolution, with 70–89% of
consecutive steps inside a single quantization step (Lumen 2.4%). Separately,
`VR_HORIZON` and the drift-veto blocks are counted in **rows, not events**, so
for a sticky agent a 24-row horizon can span fewer event-advances than the
10-event feature window that horizon was raised to clear. Any future measurement
work starts with resolution and event-locking, not with a new statistic.

### Trajectory identity — same discipline, different instrument

Added 2026-07-30. `TrajectorySignature.similarity()` and its `lineage_similarity`
consumer are a *different* instrument from behavioural EISV, but they are audited
under this contract because they make the same shape of claim and drifted the same
way. Population counts below are from `core.identities`: **427** identities carry a
`trajectory_genesis`, **364** carry both genesis and current, **275** carry
`attractor.covariance` on both, and **253** of those also carry a non-null stored
`trust_tier.lineage_similarity` (substrate-earned tiers store `None`, which is why
the two figures differ — they are not inconsistent).

Audited bytes are live bytes: `src/trajectory_identity.py` is md5
`279fa9c46623343df78b7efa4e703811` in both this repo and the deploy checkout
(verified 2026-08-06; the earlier `ab5bf17…` anchor matched the 07-24 bytes — the
delta is **#1411 + #1421**, not #1421 alone). A third checkout,
`unitares-orchestrator`, holds a stale 960-line copy (`817d665a…`,
pre-`_DRIFT_EMIT_DELTA`) that serves no traffic — do not cite line numbers from it.

**6. The six-component model measures six things** → **REFUTED BY CONSTRUCTION.**
`src/behavioral_trajectory.py:40-44` assigns `homeostatic.set_point =
attractor.center`, `basin_shape = attractor.covariance` and `recovery_tau =
recovery.tau_estimate` — η is α and ρ restated. `_compute_beliefs` and
`_compute_attractor` both reduce to the EISV window mean, so β aliases α too.
Live: `attractor.center == homeostatic.set_point` in **338/427**,
`attractor.covariance == homeostatic.basin_shape` **338/427**,
`beliefs.values == attractor.center` **421/427**. η's set-point Bhattacharyya is
the numerically identical call to component α. The six weighted terms run on
about two distinct quantities.

**7. Π (preferences) contributes** → **REFUTED BY CONSTRUCTION.** The producer
emits `preferences` as `{decision_bias, task_type_distribution}`; the consumer
requires `preferences["vector"]`. Live: **0 of 427** genesis and **0 of 364**
current signatures carry a `vector` key, so the branch never fires and its 0.15
renormalizes away. Total live weight is 0.85, not 1.0 — every threshold
comparison is against a renormalized sum.

**8. ρ (recovery) contributes** → **REFUTED BY CONSTRUCTION.** ρ is
`exp(-|ln(tau1/tau2)|)`, which is exactly 1.0 whenever tau is shared. Live: tau
identical genesis↔current in **359/364** pairs, and `3.0` in **421/427** genesis
signatures. ρ is 0.20 of unconditional free credit. δ (relational) is weaker but
similar — identical in 237/364 — so call δ degenerate, not constant.

**9. α (attractor) discriminates** → **REFUTED BY CONSTRUCTION** for the
covariance-bearing population. The covariance is eps-regularized **twice**: the
producer adds `1e-6` to the diagonal (`src/behavioral_trajectory.py:176`) and
`bhattacharyya_similarity` adds `eps=1e-6` again
(`src/trajectory_identity.py:67-73`). That puts the σ floor at 1e-3 while real
genesis↔current centre drift is 0.0065–0.023. Sweep: similarity 0.785 at euclid
2e-3, 3e-11 at 2e-2, exactly 0 at ≥0.1. So α is 0.25 of always-zero, and with ρ+δ
free credit the composite collapses to a constant:

| agent | observations | `similarity()` |
|---|---|---|
| Lumen | 135,416 | 0.632819 |
| Sentinel | 24,297 | 0.633565 |
| Watcher | 7,411 | 0.634505 |
| Vigil | 4,538 | 0.635000 |
| Chronicler | 89 | 0.642313 |

A **0.0095 spread across a 1,520× range in observation count**, all within 0.007
of the theoretical α-saturated ceiling 0.6353. Two consequences. First, the
`is_anomaly` cut of 0.6 sits **below the metric's own floor**, so the detector
cannot fire for that population. Second, the tier gates (lineage > 0.70 for
tier 2, > 0.80 for tier 3) are unreachable on this path; `resolve_trust_tier`'s
substrate-earned route and `stabilize_demoted_tier` are what actually hold
resident tiers up.

Scope this correctly: the metric is **not** inverted with respect to drift.
Spearman over the 253 scorable identities gives lineage↔real-centre-drift
**−0.856** (correct sign), lineage↔covariance-magnitude **+0.32**, and
covariance↔drift **−0.04**. The honest claim is that stored covariance *width*
shifts the score at fixed drift — an artifact of serialization, not an inversion.
Banded by `covariance[0][0]`, mean lineage rises 0.7794 → 0.8557 → 0.9403 →
0.9930 while mean real drift stays flat.

**10. `lineage_similarity` is telemetry-only** → **REFUTED.** It is on the hot
check-in path and it moves two live quantities. The entry point is
`@enrichment(order=170)` in `src/mcp_handlers/updates/enrichments.py:750-789`,
which *synthesizes* a signature via `compute_behavioral_trajectory` for any agent
with `update_count >= 10` that did not supply one, then calls
`update_current_signature` → `similarity()` at `:818`. Its imports are
function-local, so a top-of-file grep for callers finds nothing — that absence is
not evidence of dormancy. Then:

- `enrichments.py:878-905` rewrites the agent-visible `metrics.risk_score`:
  **+0.15** on anomaly, **+0.05** at tier ≤ 1, **−0.05** at tier 3. With 324/364
  identities at tier 1, the +0.05 is close to universal.
- `middleware/trajectory_step.py:19-27` sets `trajectory_confidence =
  min(coherence_sim, lineage_sim)`, which reaches
  `updates/phases.py:174-181`, where it adds `0.2 × confidence` to the
  identity-assurance score and upgrades `weak → medium` at ≥ 0.7. That upgrade
  **exempts the caller from the confidence clamp** at `phases.py:782-786`, whose
  stated purpose is that low-assurance identity should not drive high-confidence
  updates. `require_strong_identity` (`phases.py:434`) is *not* reachable this
  way — the upgrade stops at medium.

**11. Drift alerting has discriminated anything** → **REFUTED as deployed.**
`audit.events` holds 1,891 `trajectory_drift` events from **4 agent_ids ever**,
max firing similarity 0.5995. **1,746 of them are Lumen alone**, pinned at
0.1224–0.1260:

| week | events | lineage range | agents |
|---|---|---|---|
| 2026-04-13 | 4 | 0.3941–0.4570 | 3 |
| 2026-04-27 | 32 | 0.5728–0.5995 | 1 |
| 2026-05-18 | 21 | 0.5745–0.5982 | 1 |
| 2026-06-08 | 88 | 0.5728–0.5992 | 1 |
| 2026-07-06 | 1,402 | 0.1231–0.1260 | 1 |
| 2026-07-20 | 344 | 0.1224–0.1237 | 1 |

The step change coincides with the Elixir broker cutover, which changed Lumen's
check-in producer. The last event is stamped `2026-07-24 12:36:11.494893-06` and
the stored genesis `computed_at` is `2026-07-24T18:36:11.493914Z` — the same
instant to the millisecond. **The alarm was silenced by rewriting the baseline,
not by the drift resolving**, and the documented rebaseline-on-cutover step ran
15 days after it was due while a 1,402-event week went unread. No
`trajectory_drift_resolved` event has ever been emitted; the resolve branch
requires `metadata['trajectory_drift_emit']`, which only the later throttle code
writes, and the throttle landed after this drift had already cleared.

*Amendment (2026-08-06): the anchors moved; the REFUTED verdict stands.*
`audit.events` now holds **204** `trajectory_drift_resolved` events — a
2026-07-30/31 tombstone-bug flood stopped by #1421, not discrimination — and the
drift did not end on 07-24: **16** further `trajectory_drift` events span
2026-07-30→08-05, all Lumen (trust_tier 3), at lineage 0.089–0.414, each flipping
Lumen's risk adjustment −0.05 → +0.15 via the elif chain at
`enrichments.py:879-887` (telemetry/advice only). The resolve-branch sentence
above describes pre-#1421 code.

**12. Lumen's Pi-side lineage is a six-component signature** → **REFUTED BY
CONSTRUCTION.** `anima-mcp` runs the same maths (same six weights, same
eps-regularized Bhattacharyya, same `exp(-dist*2)` fallback) but its genesis,
frozen `2026-02-22T09:03:34`, was written on a path without numpy:
`preferences: {}`, `relational: {}`, `homeostatic: null`,
`recovery.tau_estimate: null`, `beliefs.values` 11-dim against a current 13-dim
(so cosine returns `None` on length mismatch), and `attractor` carrying
`variance` plus `_note: "Full covariance requires numpy"` instead of
`covariance`. Five of six components drop; α survives at weight 0.25 renormalized
to 1.0, and the missing covariance forces the fallback. Read live 2026-07-30:

```
euclid(current centre, genesis centre) = 0.242892
exp(-2 * 0.242892)                     = 0.615214   <- this IS lineage_similarity
real per-dim sigma                     = 0.0262  ->  drift = 9.26 sigma
```

Lumen's trajectory identity is one scalar: `exp(-2 × euclidean distance between
two 4-dim ANIMA means)`. The same payload simultaneously reports
`identity_status: "stable"`. The value has no behavioural effect on the Pi
(display and report only), and the governance-side path for it is dormant — but
no statement that Lumen's identity is verified by a six-component trajectory
signature is true. Note the failure modes are **inverse**, not shared: on the Pi
tau is volatile rather than constant, so ρ injects spurious *dissimilarity* where
governance-side ρ is free credit. Do not port findings 6–9 to the Pi verbatim.

**Latent exposure, narrow but real.** `resolve_trust_tier` swallows every
exception (`src/identity/trust_tier_routing.py:194-200`) and falls through to
`compute_trust_tier`. At the saturated floor, `lineage_low = similarity < 0.7` is
permanently true, which disarms the second guard in `store_genesis_signature`, so
any identity that lands at tier ≤ 1 has its genesis Σ₀ overwritten on the next
check-in. Running `compute_trust_tier` against live metadata for all 26 tier-2/3
identities: **1 of 26** is exposed — Chronicler, at 89 observations and
`identity_confidence` 0.4249, just under the 0.45 `stabilize_demoted_tier`
threshold that protects the others. The residents all compute to tier 2 and are
safe. This is an automatic Σ₀ overwrite, which is what the standing "do not edit
Σ₀ or tier by hand" rule exists to prevent; it wants an operator decision, not a
quiet patch.

*Amendment (2026-08-06): the operator decision was answered, one day after this
entry.* #1423 (2026-07-31) rewrote exactly the cited `except` block: it now
preserves the stored tier ("Fail SAFE, not down…") and falls through to
`compute_trust_tier` only when no stored tier dict exists — verified in-tree at
`src/identity/trust_tier_routing.py:199-231`. #1411 applied the same fail-up
pattern at the call site in `update_current_signature`. Together they close the
exception-path Σ₀ overwrite for every identity carrying a stored tier dict,
including Chronicler (which carries `{tier 3, substrate_earned}`). Residual scope:
identities with no stored tier dict at all.

**Cross-repo reconciliation.** The trajectory-identity paper's Appendix A states
that the cited reference implementation "uses the five informationally-independent
weights (0.18, 0.18, 0.30, 0.22, 0.12) … and η is exposed as a derived view rather
than included in the weighted sum." `anima-mcp/src/anima_mcp/trajectory.py:395-399`
appends η with `weights.append(0.15)` inside the same weighted sum, and the other
weights are 0.15/0.15/0.25/0.20/0.10. The paper is archived with a DOI and the
repo is public, so this is checkable by any reader in one file open. Finding 6
makes it more than version skew: the double-count the paper says was removed is
the one live data confirms is present.

**Do not, each verified unsafe or laundering.**

- **Do not lower the 0.6 anomaly cut to make the detector fire.** The ~0.633 floor
  is the eps artifact; moving the threshold to meet it calibrates against a bug.
- **Do not widen eps or the covariance floor to un-saturate α.** Score already
  rises with covariance width at flat real drift, so widening buys a higher score
  for less continuity.
- **Do not promote the Pi's genesis `variance` list to a diagonal covariance
  matrix.** It reads as a three-line repair and flips Lumen from passing to hard
  failing, because with the Pi's real covariance the Bhattacharyya branch has a
  usable window about 1σ wide against a live 9.26σ drift.
- **Do not re-freeze or delete a genesis to raise a low score,** and do not run
  the rebaseline script as a general remedy. It is correct only for an
  operator-attested client cutover; anywhere else it converts an unexplained
  divergence into a clean baseline and returns a green number, which is what
  happened on 2026-07-24.
- **Do not add a `vector` key to stored preferences to revive Π** without first
  checking it is not another copy of `attractor.center`. Every component added so
  far turned out to be a re-serialization of the same 4-vector.
- **Do not build a reject or pause on `lineage_similarity`.** At a floor above the
  cut it would be inert for most of the fleet and arbitrary for the rest.
- **Do not cite trajectory identity as validated or discriminating** on any
  surface, and do not describe governance-side and Pi-side as sharing a failure
  mode.

### Core governance math — 2026-08-06 wave

Added 2026-08-06 from a 13-agent adversarial audit (6 surface auditors + 6
adversarial verifiers + synthesis), pinned to `origin/master` e1743d49; 44
findings entered verification, 39 CONFIRMED / 5 WEAKENED-to-corrected-scope / 0
refuted. This wave covers the surfaces rows 1–12 did not: the behavioural
sensor→verdict circle, the phi/ODE dynamics, the calibration stack, AdaptiveGovernor/
CIRS, and ΔEta/ethical-drift. Full working notes with every query and output live at
`~/.unitares/analysis/eisv-core-math-audit-2026-08-06.md`. Same discipline as above:
each row cites mechanism to line, and live counts were reproduced by an independent
verifier agent. WEAKENED findings appear at the verifier's corrected scope, never
the original overclaim.

**13. "Behavioral EISV is observation-first — no ODE"** → **REFUTED BY
CONSTRUCTION.** The banner at `src/governance_monitor.py:1157` says so; the
observations passed under it are substantially the ODE's own state: 25–30% of E is
mean C(V_ODE) (`src/behavioral_sensor.py:117,120`), 30–40% of I is the C(V) trend
(`:147,149`), 35% of S is regime instability (`:187`, one indirection via
`src/monitor_regime.py:33-44`), and V is 100% ODE E/I history slopes (`:205-223`);
the source histories are properties of `unitaires_state`
(`governance_state.py:77-91`, `governance_monitor.py:703-713`). These observations
drive the per-agent Welford z-scores whose verdict/risk is returned outright at
confidence ≥ 0.3 (`governance_monitor.py:1194,1306-1317`), and the same ODE-derived
dict is injected back into the ODE as a default-ON spring (`phases.py:961-982`;
`parameters.py:182-185`; `dynamics.py:173-180`; live plist:
`UNITARES_SENSOR_COUPLING=behavioral_only`). The contaminant is the agent's own
time-varying trajectory — it does **not** wash out of per-agent z-scores the way
row 2's fleet-common additive term washes out of a rank statistic. While the spring
is ON, `eisv_divergence` telemetry measures a sprung pair, so "the divergence is
small" is not evidence the split is honest. Direction B alone is disclosed in
`parameters.py`'s docstring; the composite circle is what this row records.

**14. "Post-warmup enforcement can escalate on check-in content"** → **REFUTED BY
CONSTRUCTION.** For a baselined agent under default flags, the only risk components
content can move sum to 0.45 (high_S 0.20 + adversarial_rho 0.15 + high_CE 0.10,
`src/behavioral_assessment.py:313-364`), below the 0.60 high-risk cut (`:46,203-208`);
low_E/low_I (0.60 of weight) are basin-gated to 0 and pinned by inputs the agent
cannot degrade in-session, and `resolve_verdict_risk` (`governance_monitor.py:63-64`)
discards phi risk from the 3rd update on. CIRS beta_high=0.7 is equally unreachable.
Live 30d **verdicts**: safe 17,788 / caution 10 / high-risk 1 — the lone high-risk
was the **phi-owned cold-start path (updates 1–2)**, not the behavioral
fixed-threshold path (corrected by the wave-2 audit: `bootstrapping` spans updates
1–9 but verdict authority flips to behavioral at update 3; the 08-02 firing was that
agent's check-in 2, and the simultaneous behavioral read scored safe/0.1733 and was
discarded — see rows 25, 28, 43). This is the code reason an earlier severe
synthetic injection topped out at caution. The trade is partly deliberate (#1133
removed phi over-flagging); the unrecorded consequence is that the replacement
composite cannot see self-attested severity at all. Existing disabled mitigation:
`VERIFICATION_FLOOR_ENABLED` (`governance_config.py:410`). **Delivery caveat (row
24): that high-risk verdict was recorded as `pause` and delivered as `proceed` —
the verdict count above is not a delivered-enforcement count.**

**15. "Calibration error is consumed agent-scoped, or at worst as row 2's
rank-immune additive term"** → **REFUTED BY CONSTRUCTION.** The row-2 agent-blind
singleton has two further live consumers, both on threshold (not rank) surfaces,
so row 2's attenuation does not transfer. (a) `noise_S`: max-over-bins
`0.2·overconfidence·sample_weight` (`governance_monitor.py:643-657`) enters every
agent's dS/dt (`:676`; `dynamics.py:156-162`) — max rest-shift 0.2/μ = 0.4,
exceeding every per-class setpoint sigma (0.0988–0.2626), invalidating the 0.091
offset measured at noise=0, using the strategic channel that `calibration.py:570-583`
demotes to advisory and `monitor_drift.py:36-39` refuses. (b) ΔEta: the worst
overconfident tactical bin becomes every agent's `calibration_deviation`
(`monitor_drift.py:40-45,70`; `ethical_drift.py:~351`), entering drift norm,
behavioral S (0.40 weight ×1.5), d_eta_sq, and the ‖ΔEta‖ > 0.7 ×3 auto-dialectic
trigger. Live at audit time: bin 0.9–1.0 count=72, conf 0.9914, accuracy 0.75 →
noise_S penalty 0.0483 and calibration_deviation 0.2414, fleet-wide, per tick.
Scope honestly: the live cal_I source remains `get_mean_calibration_error` as row 2
records (the drift-deviation cal_I branch is fallback-only), and the ΔEta path
carries warmup dampening and exploration ×0.3 attenuation.

**16. "The coherence gates can fire on the deployed signal"** → **REFUTED BY
CONSTRUCTION** for the deployed V range (roadmap §4d/§9 asserted deadness; this row
supplies the arithmetic and the live-population proof). Every enforcement gate
(`monitor_decision.py:97,139`; `cirs.py:311` tau_low 0.3; basin bounds
`governance_config.py:70,78`; `health_thresholds.py:59`;
`governance_state.py:419-421`) reads legacy C(V)=0.5(1+tanh(V)), C1 pinned 1.0 at
all five Theta sites; firing requires V ≤ −0.203 / ≤ −0.424 / ≥ +0.203, while live
ODE V spans [−0.0894, −0.0030] (n=227) — C(V) affine, image ≈ [0.455, 0.499]. Live:
0 of 68,374 recorded states in [0.6, 1.0); zero coherence-triggered pauses or CIRS
coherence-floor blocks in `audit.events` ever. Consequence: risk_score and
void_active carry the entire decision; the manifold swap runs after gating and is
APPLY-off.

**17. "`metrics['stability']` is per-agent Lyapunov verification of the deployed
dynamics"** → **REFUTED BY CONSTRUCTION**, on both counts.
`approximate_stability_check` (`research.py:22,39-40`) evaluates DEFAULT_PARAMS
(γ_I 0.25) at a drift-free equilibrium where its only per-agent input (eta1)
multiplies ‖Δη‖²=0; with live C1=1.0 the output is the constant `{stable: true,
alpha_estimate: 0.101128}` for every agent, while the deployed system
(`get_active_params()`, γ_I 0.169; s_setpoint ON but absent from
`compute_equilibrium`) has contraction 0.042941 — reported rate 2.355× the deployed
one. Each cache miss re-runs a 4000-step relaxation per agent per 5 min to cache a
constant. Telemetry-only; `runtime_queries.py:274` masks only the
pre-first-check-in case.

**18. "AdaptiveGovernor owns threshold management"** → **REFUTED BY CONSTRUCTION**
as enforcement. hard_block reads only the static tau_floor=0.25/beta_ceiling=0.80
(`adaptive_governor.py:411-414`), never crossed live (0 of 17,800 30d rows with
risk > 0.80; min coherence 0.4801); `soft_dampen` is not in the v2 vocabulary so
`monitor_decision.py:152` is unreachable with the flag hardcoded
(`governance_config.py:396`); the governor's verdict labels are dropped for the
phi/behavioral verdict. The "per-agent" adaptation saturates fleet-identically —
445/445 live snapshots at tau=0.2519/beta=0.799 — and the OI resonance channel is
arithmetically dead (|oi| ≤ 1.4 < 2.5, `:452-483`; all 241 live resonance events
fired on 'flips', max |oi| exactly 0.7000). CIRS v2 also silently removed
oscillation's ability to block (legacy resonant→hard_block dead behind the flag).

**19. "The I residual is scored against the agent's own sigma"** → **REFUTED as
deployed.** `z_score` takes max(std, 0.05) (`agent_behavioral_baseline.py:52-66`;
floor passed at `behavioral_state.py:316-326`); with alpha_I=0.08 uniform across
all 72 baselined agents, the I-series variance collapses below the floor for **69
of 72** (S 33/72, E 8/72, V 9/72). The module's own comment calls the floor
"SECONDARY … defense-in-depth"; empirically it is the primary I denominator — a
fixed absolute threshold shared by 96% of the fleet, masked today only by the basin
gate. This completes row 2 on the residual side: I's level is majority fleet-common
*and* its residual scale is fleet-common.

**20. Row-2 open question (a): "are E and S also fleet-common?"** → answered; **E
REFUTED as deployed** (majority-degenerate by measurement, a different mechanism
than row 2), **S clean.** No agent-blind API feeds E or S — every input is
agent-keyed — but E's live terms are pinned: decision_e saturated ~1.0 (96.1%
approve of the governor's own verdicts), coh_e 0.578±0.006 (56 agents' coherence
means span 0.4829–0.4983), outcome_e near-constant and 85% self-referential;
cross-agent contrast survives only in the minority cal_e/tool/continuity terms
(weights `behavioral_sensor.py:116-120`). S's inputs genuinely vary (regime
distribution 9,831/5,752/1,823; weights `:187`). Consequence worth recording with
row 14: individuality and escalation share one bottleneck — an agent can move
neither its own E nor its own risk much.

**21. "ΔEta's Consistency pillar is measured"** → **REFUTED BY CONSTRUCTION.**
`stability_deviation = 1.0 − decision_consistency` (`ethical_drift.py:369`), but the
only runtime caller passes `decision=None` (`monitor_drift.py:90`) so the 0.8
dataclass default is permanent: live, 1,037/1,037 `core.agent_baselines` rows at
exactly 0.8 with empty `recent_decisions` (407 post-warmup). Every agent's
ethical_drift block reports 0.2 as if measured — a uniform +0.04 in ‖ΔEta‖², a
constant −0.02 Phi offset (telemetry-only post-warmup, verdict-driving pre-warmup),
and a 0.2 head start toward the 0.7 auto-dialectic threshold. Exact for the
governance-computed component; agents self-reporting drift blend 30% over it
(`phases.py:792`; `monitor_drift.py:111-113`).

**22. "auto_ground_truth grades against exogenous signals"** → **REFUTED BY
CONSTRUCTION.** The gate (`auto_ground_truth.py:207-240`) checks only that
exogenous signals exist; `evaluate_decision_outcome` (`:243-325`) never reads them —
outcome_quality derives from governance lifecycle status (paused/loop_detected →
0.2, archived → 0.95, active → 0.7+bonus) and substitutes governance-computed
coherence for missing confidence (`:268`), while the docstring claims the gate
"prevents self-referential feedback loops." Runs every 6h in the live server
(`background_tasks.py:1773→:38-52`); dedup is broken by construction (`:400` dead
assignment; newest-first `entries[:50]` recount). Footprint today ~5 of 282
strategic-bin rows — live-wired, near-silent, unbounded, and the labels are
semantically inverted (confidence-appropriateness recorded as correctness). The
unfiltered sensor-side twin: `get_recent_outcomes` (`tool_usage.py:195-217`) applies
no `verification_source` filter, so 85% self-referential labels (15,170/17,766 30d
rows) already carry 20% of E and 20% of I on the authoritative verdict path — the
shipped tier map (`outcome_anchors.py`) is imported only by analysis scripts. This
is the groundedness axiom violated on the measurement itself, not just prospectively
on the anchor registry.

**23. "Named residents run on measured per-class S setpoints"** → **REFUTED as
deployed.** Residents classify as their own label (`class_indicator.py:102-104`);
labels absent from the 5-generic-class dict fall back to the corner tuple
(0.6, 0.7, **0.0**) — not the measured `default` tuple — so `get_s_setpoint`
returns max(0, 0−0.091) = **0.0** (`governance_config.py:976-985,1187-1188`); the
deployment-overlay loader is a silent no-op on any failure (`:1036-1044`). The live
plist declares six residents and `UNITARES_S_SETPOINT=1` but no
`UNITARES_CLASS_CALIBRATION`, and the deploy tree carries identical fallback code —
as-declared, all six residents run at σ=0 now; their observed S rest (7-day means
0.207–0.286, not the σ=0 equilibrium ~0.091) is the behavioral spring + noise_S
(row 15), not the setpoint. Phi detrend degrades atomically, so verdicts are
invariant; the recentring the feature exists for is silently absent for exactly the
agents the individuality design most values.

**Amendment to rows 9–10** (from the same wave): the `coherence_sim` in
`min(coherence_sim, lineage_sim)` is the same `similarity()` instrument against the
hot-path-refreshed `trajectory_current` (`trajectory_identity.py:1010-1016,577,600`),
quasi-binary across the row-9 eps cliff — so for any genesis-drifted identity the
min() is pinned by lineage_sim ≈ 0.633 < 0.7 permanently, and the weak→medium
assurance upgrade (and its confidence-clamp exemption) is reachable essentially only
while genesis≈current, i.e. granted preferentially to the youngest identities and
permanently denied to established ones. This "coherence" is unrelated to the metrics
coherence in row 16 (a naming collision worth flagging). Do not lower the 0.7 bar —
it would calibrate against the row-9 eps artifact.

**Do not (core-math wave), each verified unsafe or laundering.**

- **Do not cite `governance_monitor.py:1167-1183` as the live sensor wiring** — it
  is a divergent fallback twin (passes agent-local `calibration_deviation` where the
  live path passes the fleet-common row-2 scalar, and reads current-cycle values
  where the live path reads the prior check-in's drift/continuity vectors).
- **Do not silently "fix" the pause-scores-0.5 bug** (`behavioral_sensor.py:65-70`;
  the five pause sub_actions miss `_DECISION_SCORES` and score 0.5, same as
  'revise'). Scoring pauses at 0.0 would *strengthen* a pause→low_E→pause
  self-referential spiral — this wants a deliberate decision, not a patch.
- **Do not loosen the Phase-5 auto-emit calibration gate** (`phases.py:~1962-1966`,
  evidence_weight ≥ 0.65). Its unreachability is accidentally protective: it is the
  only path that would train calibration from keyword self-reports, and it is dead.
- **Do not treat `metrics['stability']`, `drift_forecast.eisv_projection`, or
  `approximate_stability_check` as describing the deployed dynamics** — all three
  integrate the pre-#1133 setpoint-free equilibrium (row 17) and forecast S decaying
  toward ~0.091 against a live fleet S mean 0.277.
- **Do not read the roadmap §6 status table as current** — it is stale on 4 of 5
  rows against master defaults (the setpoint/Φ moves are the live default since
  #1133, not dormant); the same stale posture is echoed in comments at
  `governance_config.py:1094-1108` and `dynamics.py:123-124`.

### Bootstrapping / phi path — 2026-08-06 wave 2

Added 2026-08-06 from a 5-surface adversarial audit of the phi/bootstrapping
fixed-threshold verdict path — the surface the wave-1 findings identified as the
de-facto live enforcement layer (post-warmup, coherence gates cannot fire, CIRS
bounds never cross, behavioral risk ceils at 0.45). Same workflow shape and
pinned tree (e1743d49); 24 findings CONFIRMED/WEAKENED, 0 refuted whole. Full
working notes with every query and output:
`~/.unitares/analysis/eisv-bootstrap-phi-audit-2026-08-06.md`. (One auditor, the
void/V-pause surface, died on a mid-response API error; its territory — whether
`void_pause` reaches delivery — was covered by the action-reconciliation surface,
rows 40 and 24, and by wave-1 row 16.)

**Headline: UNITARES has no live enforcement layer as deployed — it has a live
*advisory* layer.** Three verified facts compose it. (1) The phi cold-start path
is the *modal* regime, not a fallback: 86% of identities never reach update 3, and
only ~12% of the recently-active fleet is governed by the self-relative behavioral
machinery the public math leads with (row 26). (2) In that window the verdict
measures caller self-report, not server-derived behavior — every drift input is
zeroed at update 1 and ×0.2 at update 2, velocity is 0.0, behavioral assessment is
discarded below confidence 0.3, and the keyword-risk channel is weighted 0.0; in
the one live firing, `|derived_cx − self_cx|` carried 99.2% of the norm (row 25).
(3) Even when a pause fires it is usually not delivered: a cadence gate built for
laptop sleep-wake (`gap_suppress`) downgraded 195 of 218 recorded pauses (89.4%)
to `proceed`, firing at any inter-check-in gap > 150s — median suppressed gap
300.9s, i.e. ordinary 5-minute resident cadence (rows 24, 41). **The last pause
delivered fleet-wide, ever, was 2026-06-28T23:25:49Z**; every post-#1133-flip
"delivered" enforcement is two integration-test fixtures (row 27). This is a gap
between *produced verdicts* and *delivered actions*, and between the population the
math targets and the one it reaches — not a claim the design is unsound or the math
wrong. The audit record itself is complete and honest (`gap_suppressed=true` and
paired suppression events are all present; this audit reconstructed everything from
it).

**24.** "A pause verdict is delivered to the agent it is issued against." — **REFUTED BY CONSTRUCTION.** `_maybe_gap_suppress` (`src/governance_monitor.py:1425`, impl `:1515-1548`) downgrades ANY pause, `risk_pause` included, whenever `GAP_RECOVERY_CYCLES=2` is armed; arming happens at `:1080-1081` in the *same* `process_update` whenever `elapsed·(0.1/15.0) > DT_MAX=1.0`, i.e. elapsed >150s (`config/governance_config.py:690-702`, no env override). The sibling `_maybe_warmup_structural_suppress` frozenset (`:1550-1556`) deliberately excludes `risk_pause` as "real signal" — internal inconsistency. Delivery requires the 2nd+ consecutive sub-150s check-in. Live: 195/218 recorded pauses in 90d suppressed (89.4%); suppression elapsed median 300.9s; 83/96 flag-era high-risk auto_attests suppressed. Escape: a first check-in <150s after monitor creation is unarmed (`last_update` init `:196`) — the path all 9 delivered bootstrapping `cirs_block`s took; returning agents always arm (`last_update_iso` DB-restored `:397-408`). Scope: 12 June high-risk pauses landed unsuppressed under pre-current code; July-onward, 1/1 suppressed.

**25.** "In updates 1-2, phi measures the agent's behavior." — **REFUTED BY CONSTRUCTION** for non-self-reporting callers. Warmup ramp `update_count/5` zeroes calibration/coherence/stability at update 1 and scales them ×0.2 at update 2, with `complexity` explicitly exempt (`governance_core/ethical_drift.py:375-381`, comment `:379`); `_last_state_velocity` is provably 0.0 through update 2 by two mechanisms — first-update branch (`src/governance_monitor.py:748,758-759`) and a one-update pipeline lag (drift at `:1149` precedes `update_dynamics` at `:1208`); velocity-risk needs ≥3 history rows (`src/monitor_risk.py:37,95`); behavioral assessment discarded at confidence <0.3 (`:1306`). Surviving verdict inputs are all caller MCP args (`src/mcp_handlers/updates/phases.py:733,753,792,795`): `complexity` (via `|derived−self|`), `ethical_drift` (blend fixed 0.3 post-dampening, `src/monitor_drift.py:102-113`, phi ceiling ≈0.053 = caution only), `task_type` (−0.08 risk, floor 0.45, `src/monitor_phi.py:63-73`), `sensor_eisv`. Live: 1 firing ever (2026-08-02, agent 3568f987) with divergence 0.6708 = **99.2% of norm²**, `self_cx=0.9` vs `derived_cx=0.2292`; simultaneous behavioral read `safe`/0.1733 discarded. Evasion: echo the derived value → divergence 0; omitting `complexity` yields |derived−0.5| ≤ 0.5 (phi penalty ≤0.125, cannot reach high-risk) because `phases.py:733-737` coerces None→0.5, making the `continuity.py:163` None-branch dead on the live MCP path.

**26.** "The phi cold-start path is a fallback; the self-relative behavioral machinery governs the fleet." — **REFUTED** empirically. `core.agent_state` (synthetic=false), lifetime max `behavioral_eisv.updates` per identity over 2,793 identities-with-rows (of 6,230 identities): 387 (13.9%) ever reached update 3, 186 (6.7%) reached confidence 1.0, 73 (2.6%) `is_baselined`, 58 (2.1%) phase `baselined`; 2,327/2,793 (83.3%) have ≤2 rows ever, median 1. Recently-active (≥2026-07-01, N=277): 155-156 in-window, 89 fixed-threshold, 32 (11.6%) self-relative. Verdict concentration: updates ≤2 → 1,293 rows, 11 high-risk, mean risk 0.2649; ≥3 → ~44,946 rows, 66 high-risk (all June 2026), mean 0.0515; since 07-01, in-window share of high-risk = 1/1. Crosser latency: 466/median 8m13s/p90 1h45m (persisted-row proxy) or 387/9m10s/1h50m (behavioral-updates definition) — state the definition. Caveat: pre-#545 rows lack the blob, undercounting old-agent maturity; row-count proxy bounds it (83% vs 86%).

**27.** "Enforcement is currently protecting the fleet." — **REFUTED** empirically for the last 39 days. 218 recorded pauses in 90d − 195 gap-suppressed − 2 warmup-structural = 21 delivered, exactly matching 21 `lifecycle_paused` rows (2026-05-08 → 2026-06-28). Max `lifecycle 'paused'` **ever** = 2026-06-28T23:25:49Z (`audit.events` and `core.identities` metadata agree). Post-#1133-flip (9e32e1d1, 2026-06-27 23:26 -0600) recorded pauses = 3: `itest-warm-find` (0.96) and `itest-warm-find2` (0.93), both purpose `testing`/`{ephemeral}`/`baselined=false`, plus the suppressed 08-02 codex event. Only delivered `baselined=true` pause in 90d: Sentinel `f92dcea8`, 2026-06-13, risk 0.80 (pre-flip legacy). Zero delivered pauses are both post-warmup and post-flip.

**28.** "The 2026-08-02 high-risk verdict demonstrates the enforcement path working live." — **REFUTED.** `log_auto_attest` (`src/governance_monitor.py:1368`) runs before the suppression mutation (`:1425`), with `gap_suppression_pending` pre-flagged at `:1354-1357`, so the row truthfully records `pause` while the agent received `proceed`. Live: auto_attest 2026-08-02 00:46:44.964359-06, agent 3568f987 (`codex_3568f987`, purpose `implementation`, `{ephemeral}`, still `active`), `decision='pause'`, `gap_suppressed='true'`, risk 0.7939818286979232; paired `attest_gap_suppressed` at .964589, `original_reason='UNITARES high-risk verdict (risk_score=0.79)…'`, `elapsed_seconds=2894.5`, `cycles_remaining=1`; zero `lifecycle 'paused'` records for this agent. Amends row 14: the verdict fired, the enforcement did not.

**29.** "Content severity influences the pre-warmup verdict." — **REFUTED BY CONSTRUCTION as deployed**, at semantic scope. `RISK_TRADITIONAL_WEIGHT = 0.0` (`config/governance_config.py:375-376`, "keyword blocklist preserved but zeroed"; applied `src/monitor_risk.py:90-91`); the escalate-only verification floor whose comment names "the sub-warmup hole where a confessed-sabotage check-in scores like a clean refactor" (`src/governance_monitor.py:1319-1335`) is gated at `:1329` on `VERIFICATION_FLOOR_ENABLED`, default `'false'` (`config:410` — a disabled-by-default review gate, already noted in row 14 as a disabled mitigation). New live evidence: `GOVERNANCE_VERIFICATION_FLOOR` is absent from the live `com.unitares.governance-mcp.plist` (48 EnvironmentVariables keys enumerated; `UNITARES_PHI_TELEMETRY_ONLY=1`, `UNITARES_S_SETPOINT=1` present) and from `launchctl print gui/501/...`; all 12 audited files md5-identical to `~/projects/unitares-deploy`. Correct scope: `response_text` *does* reach the verdict, but only through `derived_complexity`'s structural features (`src/dual_log/continuity.py:65-134`) — a harm confession scores identically to an equally-shaped clean refactor.

**30.** "CIRS `hard_block` is an enforcement tier." — **REFUTED BY CONSTRUCTION as deployed** (decision-shadowed, not merely unreachable). `make_decision` consumes only `response_tier=='hard_block'` (`src/monitor_decision.py:82-121`), whose arms are fixed `beta_ceiling=0.80` / `tau_floor=0.25` (`governance_core/adaptive_governor.py:411-414`); `BASIN_LOW_RISK_FLOOR=0.70` (`config:80`) makes any risk>0.80 state low-basin → priority-5 pause, and `COHERENCE_CRITICAL_THRESHOLD=0.40` (> 0.25) → priority-3 pause, so both arms are unconditionally shadowed. In-window, risk>0.80 ⇒ phi<−0.05 ⇒ already `high-risk` → priority-4 `risk_pause` (`:156-187`, `governance_core/scoring.py:69-76`); from update 3, behavioral risk >0.60 is already `high-risk` (`src/behavioral_assessment.py:44-46,203-208`). Legacy `beta_high=0.7` branch dead (`config:396` hardcodes True; `src/monitor_cirs.py:18` needs an import failure). Live: 74 auto_attest rows risk>0.8, all 2026-03…06 (last 2026-06-28), 73/74 already `high-risk`; the 08-02 event missed 0.80 by 0.006 and paused via `risk_pause` instead.

**31.** "`adversarial_rho` grades the severity of adversarial state movement on [−1,1]." — **REFUTED BY CONSTRUCTION.** ρ = dE·dI/((|dE|+1e-8)(|dI|+1e-8)) (`src/hck_reflexive.py:32-38`) is a magnitude-blind sign product, so any anti-directional wiggle scores ρ≈−1 and awards full weight; the ramp `0.15·min(1,(−0.2−ρ)/0.8)` (`src/behavioral_assessment.py:276-277,353-354`) and the −0.2 trigger are inert. Live (2026-08-06, 75,941 rows carrying the component): 52,907 (69.7%) exactly 0; 23,034 nonzero, 22,587 (98.1%) ≥0.14 (ρ ≤ −0.947); ramp interior (0,0.14) = 447 rows (0.59%) — so "any threshold in (−0.94,0) is equivalent" is *nearly*, not exactly, true. Full weight lands at 0.15 (14,601 rows) or 0.1499 (2,054, an eps artifact). Nonzero rate 42.3% pre-baseline, 29.0% baselined. It is the largest *ungated absolute-mode* component, not the largest content-reachable one (`high_S` = 0.20).

**32.** "Behavioral cautions reflect graded evidence." — **REFUTED** empirically; `adversarial_rho` is the sole crossing margin. Baselined not-safe verdicts = 323 caution + 79 high-risk; 201 cautions have risk ≥0.35 but risk−ar <0.35 with ar>0. Pre-baseline not-safe = **2 of 2**, both ar-margin (agent 9a6681ec, 2026-05-23 23:58 risk 0.3527 and 05-24 00:08 risk 0.3582, ar=0.15, final risk == behavioral risk confirming authority). All 203 margin rows finalized `proceed` — guidance tier only, never a pause. These cautions enter `verdict_history` and are exactly the safe↔caution flips the resonance flip-counter consumes (`src/monitor_cirs.py:23`, `governance_core/adaptive_governor.py:473-478`, threshold 4 / window 10); 241/241 live resonance events fired on `flips`. August nonzero-ar volume: 1,455 rows.

**33.** "Outcome labels feeding the behavioral sensor are externally verified." — **REFUTED BY CONSTRUCTION** on the producer side (amends row 22, which covers the reader/sensor twin). Every resonance tick is persisted as `outcome_type='cirs_resonance'`, `is_bad=True`, `outcome_score=0.0`, `verification_source='server_observation'` (`src/mcp_handlers/updates/phases.py:1703-1727`); `get_recent_outcomes` (`src/db/mixins/tool_usage.py:195-217`) filters neither field and does not even select `verification_source`, feeding `compute_behavioral_sensor_eisv` (`phases.py:948-952`, limit 20 / 24h) where success_rate swings E by up to 0.12 (`src/behavioral_sensor.py:112-117`). Closed loop: verdict flip → server-authored bad label → lower E → higher risk → more flips. Conditional inversion: in a score-homogeneous window (a pure resonance burst) variance is 0 and `consistency_I` evaluates to its **maximum** (`:138-147`); in mixed windows it is depressed toward the 0.3 floor instead. Live: 241 rows, 241/241 trigger `flips`, 12 agents, all `is_bad`, max score 0, last 2026-06-23 — **dormant ~6 weeks, armed** on any ≥4-in-10 flip burst, which row 31's sign flag structurally generates.

**34.** "The in-window enforcement edge is `risk_score >= 0.60`." — **REFUTED**; the live edge is `sign(phi)`. `make_decision` pauses on `unitares_verdict=='high-risk'` (`src/monitor_decision.py:156-187`), i.e. phi<0 (`governance_core/scoring.py:69`); a caution verdict returns `guide` at priority 7 (`:224-235`) before `config.make_decision` (`:238`), even though `src/monitor_risk.py:72-77` maps that band to risk 0.3-0.7. The documented tier at `config/governance_config.py:569-571` ("If risk_score < 0.60: PROCEED … Else: PAUSE") is unreachable in-window, as is the `risk_score >= 0.60 → critical` status label (`src/governance_monitor.py:1444-1446`) while the action is `guide`. Live: 2026-08-01 23:58:30 phi=+0.000809159 / risk 0.6959542 → `caution`/`guide`/proceeded, vs 2026-06-16 21:28:59 phi=−0.005429 / risk 0.7108582 → `high-risk`/`risk_pause`. All 11 lifetime in-window high-risk rows have phi<0. `soft_dampen` (`:152`) and the F2 fast-trip (`:255-288`) are risk-valued but yield only guide/caution.

**35.** "‖ΔEta‖² enters phi once." — **REFUTED BY CONSTRUCTION**, at reduced amplitude. The same drift list is added directly as `−0.5·d²` (`governance_core/scoring.py:63`) and, in the same update *before* phi is read, injected into the ODE as `dS/dt += λ₁·d²` and `dE/dt += γ_E·d²` (`governance_core/dynamics.py:~139-142,~155-158,579`; drift at `src/governance_monitor.py:1149` → `update_dynamics` `:1208` → phi `:1297`). λ₁ is adaptive and hard-clamped to [0.05,0.20] (`governance_core/coherence.py:82-129`; the `lambda1_base=0.3` fallback is unreachable), so net extra drag is ≤0.075·d² and ≈0.038·d² mid-range — **not** the 0.125·d² first claimed — and the `−μ(S−σ)` relaxation partially offsets it in-step. Because `effective_dt = elapsed/150` capped at `DT_MAX`, the integration is maximal on exactly the check-ins whose pauses row 24 suppresses: the amplifier and the disarmer share a trigger. Live: both 08-02 check-ins ran at dt=1.0 (gaps 2116s / 2894.5s); base phi 0.19725 → 0.17988 while grounded S rose 0.2055 → 0.2488.

**36.** "The S-setpoint recenter leaves verdict/risk invariant." — **EARNED at rest, REFUTED BY CONSTRUCTION in the cold-start window.** `src/monitor_setpoint.py:15-23,53-61` asserts invariance under the attractor move; the detrend is ungated by warmup on the hot path (`src/monitor_phi.py:26-28`) while the phi window evaluates at bootstrap S=0.2 (`governance_core/dynamics.py:70`), never the legacy rest ≈0.091. Default-class σ = 0.2364 − 0.091 = 0.1454 (`config:971,1101`, `:1178-1188`), weights all 0.5 (`governance_core/parameters.py:101-105`): phi(prior) 0.15 → 0.2227, credit +0.0727; pause corridor d² 0.30 → 0.4454 (+48.5%), ‖ΔEta‖ 0.548 → 0.667. Direction lenient for all five classes (every healthy_S > 0.091); the +0.073 magnitude is default-class only (σ ranges 0.0988 ephemeral → 0.2626 engaged_ephemeral). Keyed to a thin-N constant, self-flagged at `config:953` ("too thin to re-measure") and `:971` (N=16 in the 06-27 window). The 08-02 event does **not** discriminate the two bases (I and S unobserved; both fit the 0.180 credit).

**37.** "The permanent −0.02 Consistency offset is verdict-driving pre-warmup." — **REFUTED BY CONSTRUCTION** for fresh agents; EARNED as telemetry bias (amends wave-1 row 21). `decision_consistency` stays 0.8 because `src/monitor_drift.py:90` always passes `decision=None` (`governance_core/ethical_drift.py:185,245`), so `stability_dev=0.2` ramped by `update_count/5` (`:375-381`) contributes 0 at update 1 and −0.0008 at update 2, reaching the full −0.02 only at count ≥5 — while behavioral confidence `update_count/10` (`src/behavioral_state.py:38,280-285`) takes verdict authority at update 3 (`src/governance_monitor.py:1306`). The windows are disjoint. Live payloads: update-1 component 0.0, update-2 exactly 0.04 = 0.2·(1/5). Residual corner: a persisted drift baseline at count ≥5 (`src/mcp_handlers/updates/phases.py:1322-1330`) against a reset behavioral state.

**38.** "`adversarial_rho` can escalate a bootstrapping agent." — **REFUTED BY CONSTRUCTION** (answers wave question (a)). ρ is hard-coded 0.0 while `_prev_E/I/S/V` are None (`src/governance_monitor.py:748-758`); those attributes are process-transient (`:234-237`), absent from `GovernanceState` persistence and the restore path (`:370-390`), so update 1 *and* the first check-in after any monitor re-instantiation score ar=0 — silently blanking the component for warmed agents once per process lifetime. At update 2 ar can reach 0.15 but the whole assessment is discarded at confidence 0.2 (`:1306`). Live on 3568f987: check-in 1 ar 0.0 (behavioral 0.0/safe vs final 0.696/caution); check-in 2 ar 0.15 (behavioral 0.1733/safe discarded, final 0.794/high-risk); check-in 3 final == behavioral == 0.2337. Qualifier: the "pure phi risk in updates 1-2" statement assumes the behavioral clock and ODE history advance in lockstep — the backward-compat restore (`:374-376`) can break that, making velocity_risk live during behavioral updates 1-2 (unobserved live).

**39.** "`governance_core/scoring.py` documents its own deployed role." — **REFUTED** (doc-inconsistency). `scoring.py:51-54` says phi is "used primarily in research/optimization … Production UNITARES uses coherence-based decision making … Could be integrated into production," and `:91` documents "Steady-state … gives phi≈0.11." Deployed: `src/monitor_phi.py:27-32` runs `phi_objective`/`verdict_from_phi` every check-in and the only override is gated at `src/governance_monitor.py:1306` plus a non-None behavioral verdict at `:63`; with `UNITARES_S_SETPOINT=1` live, `src/monitor_setpoint.py`'s docstring says "production Φ rests ≈0.26" and the empirical fresh-agent base is +0.19725 — so the documented margin-to-cut understates the deployed one ≈2×.

**40.** "`void_pause`/`coherence_pause` counts measure governance activity." — **REFUTED** empirically. 90d: `void_pause` 112 recorded (51.4% of 218 pauses), delivered **0** — Steward's 110 = 108 gap-suppressed + 2 warmup-structural (2026-06-10 06:54:51, 06-14 07:20:52, the only two such events ever); Lumen's 2 gap-suppressed. `coherence_pause` 2 recorded (Lumen, 2026-06-03), delivered 0 — which refines row 16's "zero coherence-triggered pauses ever": cold-restart transients exist in the record, none delivered. `basin_pause`: 0 rows. All 21 delivered pauses are `cirs_block` (11) or `risk_pause` (10). The recorded-vs-delivered divergence is intentional and documented at `src/governance_monitor.py:1574-1583`.

**41.** "Gap-recovery suppression guards against sleep-wake artifacts." — **REFUTED** empirically (deployed-vs-target; doc companion to row 24). Rationale at `config/governance_config.py:694-701` and `src/governance_monitor.py:1076-1079,1520-1521` cites MacBook clamshell sleep-wake and the 2026-05-08→12 false high-risk incident. Live elapsed of 195 `attest_gap_suppressed` events: 4 <150s (carryover), 185 in [150,334s], 0 in (334,900], 2 in (900s,1h], 3 in (1h,12h], 1 at 43,905.9s; median 300.9s. Sleep-shaped (>1h) = 4/195 = **2.1%**. The deployed mechanism is a cadence gate on enforcement.

**42.** "Live `cirs_block` firings evidence oscillation detection." — **REFUTED** empirically, 16:1. Of 34 recorded `cirs_block`s in 90d, 32 are "CIRS risk ceiling breached" (risk 0.806-1.00, branch `src/monitor_decision.py:93-96`, `beta_high=0.7` at `src/cirs.py:352-353`) and 2 are "CIRS resonance detected" (Lumen `69a1a4f7`, 2026-05-15 OI=0.45 and 05-18 OI=0.30, both flips=2). All 9 delivered risk-ceiling firings carry `behavioral.baselined=false`; all are ephemeral / short-lived agents — two lease-plane probes, two PR-stack review probes, the two `itest-warm-find` fixtures, two unlabeled ephemerals, and Lumen's cold restart. At-fire update counts are not recorded (one of those agents is now at 30 lifetime updates), so `baselined=false` is the verified pre-warmup marker. Post-warmup unreachability of 0.7 follows from row 14's 0.45 ceiling.

**43.** "Verdict authority can be inferred from the `bootstrapping` phase label." — **REFUTED** (doc-inconsistency; answers wave question (c) negative). `bootstrapping` spans updates 1-9 (`src/behavioral_state.py:38,350-357`) but authority flips at update 3, so row 14's "the lone high-risk was phase bootstrapping, the fixed-threshold path" misattributes the author: the 08-02 event was check-in **2**, phi-owned, and the fixed-threshold path scored 0.1733/safe and was discarded. Record author windows as "phi-owned (updates 1-2)" vs "behavioral fixed-threshold (3-9)". Resonance provenance: of 241 `cirs_resonance` events, 173 have ≥30 prior `core.agent_state` rows (46 `state_json`-baselined + 127 row-proxy), 68 have zero prior rows (unclassifiable), zero events since 2026-06-23 — no pre-warmup resonance channel exists in the record.

**44.** "A warmed agent can silently fall back into the phi window." — **UNTESTED AS DEPLOYED**, exposure currently zero. Confidence `update_count/10` is monotone with no wall-clock decay (`src/behavioral_state.py:280-285`), so the only re-entry is counter loss: `hydrate_from_db_if_fresh` (`src/agent_monitor_state.py:297,302,345-352`) restores the behavioral blob only from `chrono[-1]`, and both a swallowed exception (`:351-352`) and a blob-less latest row (silent isinstance/truthiness miss) leave `update_count=0`. A blob-less writer exists (`epistemic_class='substrate_observation'`: 1,051 of 5,760 August rows; 1,051 of 1,062 substrate rows). Live: `lag()` scan over all persisted counters = **0 regressions ever**; exposure join = **0 identities** with a blob-less latest row over an earlier mature (≥3) blob. Right-censored: a reset inside a Redis-only session that never re-persists is invisible. No action; re-check only if `substrate_observation` rows ever become the latest row for a governed check-in identity.

**Do not (wave-2 path), each verified.**

- **Do not "fix" gap-suppression by adding `risk_pause` to the suppressed set or
  lowering the 150s arm** without recognizing it is the delivery gate for the whole
  fleet — the sibling warmup suppressor already excludes `risk_pause` as "real
  signal," so the two suppressors disagree by construction (row 24). Any change here
  is an enforcement-delivery decision, not a tuning tweak.
- **Do not read a pause *verdict* count as delivered enforcement.** Recorded ≠
  delivered by design (`governance_monitor.py:1574-1583`); reconcile against
  `lifecycle 'paused'` rows (rows 27, 40).
- **Do not enable `VERIFICATION_FLOOR_ENABLED` as a quiet patch** to close the
  content-blind hole (row 29) — it is review-gated and absent from the live plist
  by intent; turning it on is an operator/enforcement decision.
- **Do not attribute a verdict's author from the `bootstrapping` phase label** — the
  label spans updates 1–9 but authority flips at update 3 (row 43).
- **Do not treat `cirs_resonance`/`adversarial_rho` guidance as graded evidence.**
  `adversarial_rho` is a magnitude-blind sign product, bimodal 0-or-0.15 (row 31),
  and every resonance tick is persisted as a server-authored `is_bad` label that
  re-enters the sensor (row 33, armed-but-dormant).


## Preferred wording

Use:

- "EISV/prior-state telemetry"
- "proprioceptive signal"
- "agent strain / incoherence / overload"
- "policy input"
- "task-negative / contract / authority-harm outcome label"
- "external outcome evidence"

Avoid:

- "EISV decided this was bad"
- "EISV prevented harm" unless an enforcement path actually did
- "bad outcome" without naming the label source/class
- "validated EISV" from synthetic fixtures, single-class strict scope, or
  retrospective self-labels
- "we judge each agent against its own normal" as a *validated* property — the
  mechanism exists, the validation does not (ledger rows 1–3)
- "the individuality axiom was refuted / disproved / tested and failed" — the
  pre-registered test failed, which is not the same claim (ledger row 1). Say
  "untested as deployed" and cite the instrument's power
- "rolling" for the Welford per-agent baselines — they are expanding, and the
  difference changes how a fixed z-threshold behaves over an agent's lifetime

## What ablations should say

Ablations should ask whether EISV/prior-state telemetry adds predictive signal or
useful policy steering over baselines. They should not imply EISV is the judge of
badness or the component handing down bad verdicts. The skeptical report,
inventory, and prospective cohort reports should make weak data obvious: sparse
bad labels, synthetic fixtures, unbound prediction IDs, missing prior-state
coverage, and harness-lane contamination are data-quality limits, not
philosophical failures of proprioception.

## Prior art / positioning

EISV-as-proprioception is an **engineering instance of interoceptive inference,
not a new theory** (prior-art audit:
`docs/ontology/trajectory-identity-prior-art-2026-06.md`). The
"sense your own internal state, keep it within viable bounds, before any verdict"
posture this contract describes is the established interoceptive-inference branch
of the Free Energy Principle: Seth (2013), *Trends in Cognitive Sciences*
17(11):565-573; the Friston-co-authored "Life-inspired Interoceptive AI" (arXiv
2309.05999), with its self/world Markov-blanket factorization; Tschantz, Seth &
Pezzulo (2022), *Biological Psychology* (interoceptive control as prediction-error
minimization against homeostatic/allostatic set-points); and the Interoceptive
Machine Framework (2026), *Physics of Life Reviews*.

Two cautions follow, both consistent with the rest of this contract:

- **Neighbor, not grounding.** Cite these as the framework EISV instantiates;
  do **not** claim EISV's coordinates are variational free-energy quantities —
  that grounding claim retired with the v7 F-hat spike (see
  `paper-positioning.md`, 2026-04-23). The "thermometer, not a court" framing
  here is *the same* pre-judgmental stance the interoceptive literature gives
  interoception: it informs regulation, it does not adjudicate.
- **Novelty window.** The interoceptive-AI literature is converging quickly
  (2024–2026); positioning EISV as a rediscovered/instantiated framework rather
  than a novel one is the honest and durable framing.

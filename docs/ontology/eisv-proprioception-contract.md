# EISV Proprioception Contract

**Created:** June 26, 2026
**Last Updated:** July 29, 2026
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
2026-07-29 reproduced it (leg A 1/7, leg B 1 of 3 dims). The final read is
scheduled 2026-07-30. Honour the kill criterion — no v3 against this
measurement — but do **not** record the FAIL as evidence against the axiom. Two
independently sufficient reasons:

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

**2. Individuality of the I dimension** → **REFUTED BY CONSTRUCTION**, before any
data existed. `cal_I` carries 50–60% of I (`src/behavioral_sensor.py:147,149`)
and comes from `get_mean_calibration_error` (`src/mcp_handlers/updates/context.py:85`),
which takes no `agent_id` — it averages bins from a module-level singleton keyed
by confidence range only, and `agent_id` appears **zero** times in
`src/calibration.py`. Every agent receives the identical scalar on the same tick,
so a per-agent I *home* is not a measurable quantity. Leg B's I failure is
arithmetic, not sampling noise.

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
`ab5bf17101955153dfdde1644b0435c6` in both this repo and the deploy checkout.
A third checkout, `unitares-orchestrator`, holds a stale 960-line copy
(`817d665a…`, pre-`_DRIFT_EMIT_DELTA`) that serves no traffic — do not cite line
numbers from it.

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
producer adds `1e-6` to the diagonal (`src/behavioral_trajectory.py:175`) and
`bhattacharyya_similarity` adds `eps=1e-6` again
(`src/trajectory_identity.py:63-69`). That puts the σ floor at 1e-3 while real
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

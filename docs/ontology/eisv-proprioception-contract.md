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

# S axis validation protocol (v1, draft)

**Status:** DRAFT, 2026-11-03. Nothing here has run, and no data for v1 have been
collected. Collection waits for the operator to approve or edit the deciding
standard in [§7](#7-deciding-standard-operator-slot). When it is approved, this
file moves to `docs/proposals/registered/` with the approval cited, and from then
on it changes only through a dated amendment.

**Scope:** whether S, as computed, responds to known changes in its inputs with
the predicted sign, size, timing and recovery. It also covers whether S stays
still under changes that should not move it, and whether it repeats itself. S is
a measurement. This protocol tests it the way a measurement is tested. It makes
no outcome claim, no claim about real work, and no claim that acting on S helps.

**Lineage:** this is the next step named in the
[instrument-quality plan](../../plans/eisv-instrument-quality-2026-10-13.md)
("Specify S's next validation"). It follows the exploratory
[S faithfulness v0](../../evaluations/eisv-s-faithfulness/result-v0.md)
(2026-10-12, reproduced 2026-10-13). It applies the
[positive-control validity rule](../../operations/positive-control-validity-2026-08-23.md)
and the regime the contract assigns to S
([sensory class split](../../ontology/eisv-proprioception-contract.md#sensory-class-split--which-axis-needs-which-validation-regime-2026-08-17)):
test-retest reliability and faithfulness under intervention.

**Design critique:** one consult on 2026-11-04 (council `d23885fa`, Codex and
Antigravity hosts, openai and google families). Their findings are folded in
below, and [§11](#11-design-critique-and-what-changed) lists what changed.

## 1. What v0 left open

v0 showed that S moves with overclaims, a confidence collapse and thrash, and
that it barely moves under an honestly reported change of work. Its own limits
section, and the plan, name what it could not settle:

- The confidence-collapse condition also raised claimed complexity, so it
  confounded two inputs.
- The vocabulary-only sham moved S by -0.038 on live code. Holding the regime
  term constant removed that shift.
- Tool-usage statistics were pinned to zero. v0 used one agent class and one
  cadence band.
- No deciding standard was declared, and the test was not pre-registered. Its
  only test-retest was bit-identical replay, which shows determinism, not
  reliability.
- There was no comparison against S's own raw inputs, so it could not say what
  the combined reading adds.

## 2. How S is computed (the paths an intervention can take)

Read from `src/behavioral_sensor.py` (`_compute_S_components`,
`compute_behavioral_sensor_components`) and `src/dual_log/continuity.py` at
`d2b3fac3d`. [Step 0](#5-step-0-derived-predictions-before-any-collection) re-reads
them at the pinned commit.

| Path into S | Weight | What drives it |
|---|---|---|
| Drift | 40% of raw S, as `min(1, 1.5 × drift_norm)` | The drift vector (`src/monitor_drift.py`): reported confidence, complexity divergence against its baseline, the calibration error, and control feedback |
| Regime instability | 35% of raw S | Label flips over the last 10 regime labels. `detect_regime` labels each check-in from S's own last change, so this term is a **self-loop** |
| Complexity divergence | 25% of raw S, as `min(1, cd)` | `cd = |derived − claimed|`. It is an absolute value, so underclaiming counts too |
| Continuity blend | 20% of the result | `S_input = 0.1 + 0.5 × cd` (+0.1 when a session does not continue; +0.1 with no self-report) |
| Tool velocity | +0.10 × `clip((velocity − 5) / 10, 0, 1)` | Calls per minute, averaged over the past hour (`tool_vel = calls in the last hour / 60`, in `src/mcp_handlers/updates/phases.py`). Only the MCP update path supplies it. The `process_update` path the harness drives never passes it, so in the harness this term is absent, not zero |

Two consequences shape the design:

- **Divergence reaches S by three routes.** Its direct gain is about 0.8 × 0.25 +
  0.2 × 0.5 = **0.30 per unit of divergence**, before drift. v0's 0.45 overclaim
  gave +0.195, or 0.43 per unit, so drift appears to carry the rest. Step 0
  confirms or corrects this arithmetic.
- **Confidence reaches S only through drift**, and drift is measured against
  baselines that adapt. A step change in confidence alone may therefore give a
  transient rather than a level shift. The protocol has to predict which one,
  before any collection.

## 3. Arms

Every condition runs in both arms, on the same inputs.

- **(a) Deployed:** `metrics["S"]` from `UNITARESMonitor.process_update` at the
  pinned commit, with the live plist's estimator flags (as in v0).
- **(b) Regime-held:** the same path, with only the regime term held at
  `REGIME_NEUTRAL_INSTABILITY = 0.12`. The harness patches
  `_regime_instability` to return that constant; nothing else changes. It is the
  same substitution the live shadow makes through `regime_instability_override`
  (`eisv_telemetry.shadow_ablations.regime_self_loop_neutralized`). The broader
  `neutralize_self_loops` flag is **not** used, because it also neutralises the
  decision and coherence contributions to E and I. Arm (b) would then differ in
  more than the regime term. v0's `--ablate-regime` held the term at 0.1; v1 uses
  0.12 to match the shadow.

Each arm gets its own verdict. One arm's verdict says nothing about the other.

## 4. Conditions

Each stream has 100 check-ins: baseline 0–29, intervention 30–44, recovery
45–99. v0's recovery of 25 steps left the 0.45 overclaim still 0.030 above
baseline, so recovery is longer here. Inputs are generated as in v0: templated
reports, `task_type="mixed"`, intervals of 30–90 s, and jitter of 0.03 on the
self-reported values. Exceptions are noted below.

**Positive controls** (predicted to move S):

| ID | Intervention, steps 30–44 | Separates |
|---|---|---|
| P1 | Overclaim complexity by 0.10 / 0.20 / 0.30 / 0.45, with confidence held at 0.75 | Divergence alone, as a dose series |
| P1u | Underclaim by 0.30 | Whether S treats underclaiming as divergence, as `abs()` predicts |
| P2 | Confidence drops to 0.55 / 0.45 / 0.30, with complexity reported accurately | Confidence alone (drift path only) |
| P3 | v0's joint collapse: confidence 0.30 and claimed complexity 0.90 | Continuity with v0 |
| P4 | Thrash: alternating terse/confident and heavy/unsure reports, each reported accurately | Switching |
| P5 | Tool-velocity burst: tool calls arrive at 60 per minute from step 30 to step 44, every other input unchanged. Velocity is the past hour's calls ÷ 60, with a floor of 5 per minute. At 30–90 s intervals the window lasts about 15 minutes, so a lower rate might never cross the floor. 60 per minute brings the hourly average to about 15 per minute, which is full pressure, by the window's end. The predicted response builds through the window and decays over the following hour, so Step 0 checks that recovery fits the stream | The tool term. **Conditional:** it runs only if Step 0 shows the harness can drive a path that supplies `tool_call_velocity` (§8). Otherwise it is dropped, and the result says the tool term is untested |

**Negative controls** (predicted not to move S):

| ID | Change, steps 30–44 | Why S should stay still |
|---|---|---|
| N1 | None | The reference stream |
| N2a, N2b | Reports use two vocabulary sets disjoint from baseline, at the same structure and length | Wording is not instability |
| N3 | Heavier work, reported accurately: derived and claimed complexity rise together | Antagonist to P1. The work changes, but divergence does not |
| N4 | Cadence of 5–15 s, and separately 300–600 s | Tempo is not instability. If P5's path is available, N4 also runs on it with a fixed number of tool calls per minute, so that a cadence change doesn't change velocity |
| N5 | Terser work, reported accurately | The mirror of N3 |

**Classes:** the default agent class, plus one other class overlay if the harness
can instantiate one. If it can't, the result says the coverage is one class.
This is decided in Step 0, not after collection.

## 5. Step 0: derived predictions, before any collection

Before collection, a commit adds `prediction-v1.md` beside this file. Using only
the code at the pinned commit (no stream is run), it states the following for
every condition and both arms:

1. **Direction**, and **steady-state magnitude** with a band. For divergence this
   is the direct-path gain (§2) times the dose, plus a drift contribution
   derived from `monitor_drift.py`.
2. **Shape:** a level shift, a transient (peak, and steps to half-decay), or no
   change. P2 is the case to settle here.
3. **Latency:** the number of check-ins to half the steady response.
4. **Recovery:** the number of check-ins until S is back within ±δ of control.
5. **What neutralising each path leaves.** For each positive control, list every
   causal path and the response predicted when one path is removed. Do not
   assume it goes to zero. Regime-loop and drift side effects belong here.

The operator approves the predictions together with §7. A prediction that turns
out wrong is reported as wrong; it is never re-derived after collection.

## 6. Pairing, seeds and windows

- **Common random numbers, with branching from a cloned state.** For seed *k*,
  run the baseline once and clone the monitor state at step 30. Every condition
  continues from that clone. Draws come from keyed generators, not one shared
  stream, so a branch that executes differently can't shift another branch's
  draws. **Shared nuisance draws** (report text, jitter on fields the condition
  doesn't change, intervals) are keyed by `(seed, step, field)` alone. Every
  branch of a seed therefore gets identical nuisance inputs at every step,
  through baseline, intervention and recovery. Only a field the intervention
  itself changes takes a condition-specific draw, keyed by `(seed, condition,
  step, field)`. For example, N2's vocabulary set draws the words it substitutes
  that way, while sentence structure and length stay shared. The regime trajectories are expected to diverge after step 30, because
  that divergence is part of the effect.
- **Microstate sensitivity (arm a).** Add a second control stream per seed that
  differs from N1 only by ±1e-4 on reported confidence from step 30. Report the
  largest |ΔS| it produces. This measures how far the self-loop amplifies a
  negligible difference, which bounds what the paired design can resolve in
  arm (a).
- **N1 against N1 is identical by construction,** so it is asserted as identity
  and not tested inferentially.
- **Unit:** the seed. All inference uses one value per seed per window, never
  pooled steps.
- **Windows:** onset 30–34 (transients), response 37–44 (after burn-in),
  recovery 85–99, retest A 15–22 and retest B 22–29 (both in baseline).
- **Seeds:** the count is set so that the expected standard error of a paired
  window difference is at most δ/3. As a conservative bound it uses v0's
  unpaired spread (SD about 0.045), which gives n ≥ 182, so **n = 200 per condition
  per arm**. Cloned branching should make the realised error smaller. If the
  operator changes δ, n follows this rule. The cost is estimated in Step 0. Run
  offline, as v0 was, this is several hundred thousand `process_update` calls.

## 7. Deciding standard (operator slot)

These are proposed values. The operator approves or edits them before collection,
and the approved values are fixed from then on.

**δ, the equivalence margin, absolute in S units.** Proposed **0.01**: a
quarter of the gentlest positive response v0 recorded (+0.040 at the 0.10
overclaim). On this margin, a change that should not register moves S by less
than a quarter of the smallest real miscalibration tested. The same δ applies
to both arms. If the operator can name a consequence-based value (the smallest
S change that would alter what an agent or operator does), that value should
replace this one.

An arm is **FAITHFUL** only if every criterion below holds. Otherwise it is
**NOT FAITHFUL**, and the result lists each criterion that failed. There are no
exemptions, including for failures this protocol predicts.

1. **Direction and magnitude.** For every positive control, the paired window
   mean (response window, or onset window where Step 0 predicts a transient) has
   a one-sided 95% lower bound above zero. Its point estimate also falls inside
   the Step 0 band.
2. **Dose order.** For P1 and P2, every adjacent pair of doses is
   non-decreasing in paired ΔS: one-sided paired tests by seed, Holm-corrected.
   Page's L test (blocked by seed) reports the overall trend.
3. **Specificity.** For every negative control except N1, TOST equivalence
   within ±δ: the 90% CI of the paired window mean lies inside ±δ.
4. **Recovery.** For every positive control, the recovery window's paired ΔS is
   within ±δ (TOST). If Step 0 predicts a longer recovery than step 85 allows,
   the result reports that, and this criterion is scored at Step 0's recovery
   step.
5. **Reliability.** Within each seed's baseline, retest windows A and B agree:
   the 95% Bland–Altman limits of agreement lie within ±2δ, and ICC(A,1)
   across seeds is reported. Bit-identical replay of one stream is asserted
   again (determinism).
6. **Signal against noise.** For every positive control, the mean response is at
   least 3× the within-seed resting step SD, the SD of step-to-step change in
   retest windows A and B.

**Declared predictions for the verdicts:**
- Arm (a) is NOT FAITHFUL. It fails criterion 3 for N2a and N2b, because of the
  regime self-loop (v0: -0.038 live). It also fails criterion 6 for the lowest P1
  dose: v0's resting step SD was 0.027, so 3× that is 0.081, against a response
  of about +0.040.
- Arm (b) is FAITHFUL.

Each prediction is reported as confirmed or not.

## 8. Can the procedure fail? Mutants and validity checks

Before the real arms are scored, the whole procedure (generation, pairing,
windows, statistics and criteria) runs against deliberately broken estimators.
These are made by patching `_compute_S_components` inside the hermetic harness
only:

| Mutant | Breakage | Must fail |
|---|---|---|
| M1 | Divergence weight 0 | Criterion 1 on P1 |
| M2 | Raw S held constant | Criterion 1 on every positive control |
| M3 | Raw S plus a deterministic term from a hash of the report's vocabulary (±0.03) | Criterion 3 on N2 |
| M4 | All gains halved | The criterion 1 band on P1 |
| M5 | Recovery removed: the S increase is latched after the intervention | Criterion 4 |

If any mutant passes a criterion it should fail, the procedure is invalid for
that criterion. The run stops and reports that, and no arm is scored on that
criterion. Mutant outputs are scored by script, and their results are published
with the run.

Mapped onto the four [positive-control checks](../../operations/positive-control-validity-2026-08-23.md#the-general-rule):

- **Falsifiability:** the mutants above, plus N3 as the antagonist for P1.
- **Domain:** every injected value stays inside [0, 1] after jitter (clipped, and
  the clip count is reported). The harness also confirms that no report trips
  the degenerate-text exclusion (`exclude_template_div`, #2418), which would
  silently remove divergence.
- **Regime:** baseline confidence and the claimed-minus-derived offset are set
  to the live fleet's medians, taken from a stated, dated, aggregate read. That
  read happens in Step 0, before collection, and is the only database contact.
- **Independence:** every dose is in absolute input units. None is expressed in
  units of an S threshold.

**Harness extension for P5 and N4:** stubbing the tool-usage tracker is not
enough. The `process_update` path the harness drives never passes
`tool_call_velocity` to the sensor (`src/governance_monitor.py`, the behavioral
sensor call). Step 0 decides between two routes and records its choice:

- drive the MCP update phase that does compute velocity (calls in the last hour
  ÷ 60) with a stubbed tracker;
- or pass velocity in those same units through a harness-only hook.

The second route measures a path the deployed `process_update` doesn't take,
and the result says so. Either route lands with its own tests, with tool-call
timestamps on the injected clock. If neither route is feasible, P5 is dropped
(§4).

## 9. Characterisation: what the combined reading adds (no pass or fail)

For each condition and arm, the response-window score of the intervention branch
is compared with the control branch, one value per seed. The AUC across seeds is
reported, with a seed-bootstrap interval, for:

- S;
- each raw input: drift norm, `cd`, reported confidence, the regime-instability
  term, and tool velocity;
- the single raw input that does best across all conditions, chosen once.

Score directions are fixed in advance (a higher value means more instability).
This says whether S beats, ties or loses to its own parts. Losing to one input on
one condition is expected (a sensor that sums several inputs dilutes each one),
and it is reported plainly. AUC measures discrimination only, not calibration or
reliability.

## 10. Execution, reporting and limits

- **Order:** Step 0 predictions and the operator's approval, then the harness
  extension, then the mutant run, then one run of both arms. Plumbing checks use
  seeds 10,000 and up, print no ΔS, and assert only that streams complete. The
  confirmatory run uses seeds 0–199. It runs once, and the result is
  `docs/evaluations/eisv-s-faithfulness/result-v1.md`.
- **Reporting:** before any verdict, report the attrition: streams that failed to
  complete, values clipped, and reports excluded as degenerate. Report every
  criterion for every condition and arm, including the ones that pass. Under the
  [registered-protocol reporting rules](../registered/README.md#before-a-registered-result-is-reported),
  the arm verdicts are posted only after the operator signs off.
- **Nothing live changes.** This run is offline and hermetic, and it writes no
  rows. Any change to S that follows it, such as repairing the self-loop, is a
  separate instrument change. It records `instrument_change_at` under the
  [2026-10-15 amendment](../registered/eisv-outcome-grounding-stop-rule-v0.md#amendment-2026-10-15-data-boundary-at-the-first-instrument-change),
  and the repaired S does not inherit this result.
- **Limits:**
  - The inputs are synthetic and templated, so real reports vary in ways the
    templates don't.
  - Faithful here means that S tracks the quantities it is built from, in the
    way its construction says it should. Whether those quantities are the right
    ones to track is a separate question. So is the answer for E, I and
    coherence.
  - A live-agent leg, with known state changes in real agents, is out of scope
    and needs its own decision. It would write live rows.

## 11. Design critique and what changed

The council (one consult, advisory, `d23885fa`) raised the issues below. Each
was folded into the protocol as shown.

| Raised | Change |
|---|---|
| No overall decision rule; a predicted failure could become an exemption | §7: one verdict per arm, with no exemptions |
| A lower bound above zero accepts tiny responses, and in a weighted sum the sign is almost guaranteed | §5 Step 0 magnitude bands, and §7 criterion 1 |
| Zeroing a weight only tests that multiplying by zero gives zero | §8 mutants that the procedure must reject, plus the N3 antagonist |
| Test-retest was missing (replay shows determinism only) | §7 criterion 5 and the retest windows |
| CRN breaks under the self-loop unless states are cloned and draws are event-indexed. N1 makes zero-variance pairs | §6 cloned branching, indexed draws, the microstate control, and N1 asserted as identity |
| Jonckheere–Terpstra assumes independent groups, and a trend doesn't show each adjacent step | §7 criterion 2: adjacent paired tests plus Page's L |
| δ = 0.03 came from noise dominated by the self-loop and was too wide | §7 δ = 0.01, fixed across arms, an operator slot, and seeds set by δ |
| Signal-to-noise against between-seed SD can be gamed | §7 criterion 6 uses within-seed step SD |
| Per-step AUC pools autocorrelated steps and compares across time | §9 one score per seed, matched windows, the best input chosen once, fixed directions |
| No recovery criterion | §7 criterion 4, and streams lengthened to 100 |
| P2's path into S, and cadence against tool counts, were unmapped | §2 path table, Step 0 shape prediction, and §8 fixed calls per check-in |

The implementation review of this draft (Codex, PR #3189) found three defects in
the first version. All three are fixed above:

| Found | Fix |
|---|---|
| Stubbing tool counts can't reach S: `process_update` never passes `tool_call_velocity`, and velocity is in calls per minute over an hour, not per check-in | §2 units corrected. P5 is conditional on a path that supplies velocity (§8), and its rate now crosses the 5-per-minute floor |
| `neutralize_self_loops` also neutralises the decision and coherence terms of E and I, so arm (b) would change more than the regime term | Arm (b) patches only `_regime_instability`, the same substitution the live shadow makes |
| Keying every draw by condition gave control and intervention different jitter, which breaks common random numbers | Shared nuisance draws are keyed by `(seed, step, field)`. Only fields the intervention changes take condition-specific draws |

# EISV maths — policy & roadmap (v0)

**Status:** design-intent / roadmap (not a change). Captures the direction; each
*move* lands as its own flagged, reversible PR with its own gate.
**Method:** starts from design **values**, translates them into maths, and
sequences reversible moves. The values are the operator's; the translation and
the layer-tagging are the engineering. Where a decision is a value, it is held
open and marked **[L3]** — this document does not resolve it.
**Provenance:** synthesises the `eisv-fixed-point-calibration-gap` finding (incl.
the phi/damping archaeology) and an independent-reviewer pass. The break/remedy
evidence lives in `scripts/analysis/eisv_stage_a_redteam.py` and PR #1058.

---

## 1. North star — the design axioms (operator's; fixed here)

1. **Individuality.** An agent is judged against *its own* normal, not a
   fleet-wide ideal. Heterogeneity is the default, not a deviation to correct to
   a mean.
2. **Growth, not punishment.** Deviation is *information*, not error. The
   reference an agent is measured against is allowed to **move** as the agent
   learns. Track a *trajectory*; do not penalise distance from a fixed point.
   (Explicitly NOT the RLHF shape: reward/penalty toward a prescribed optimum.)
3. **Groundedness.** Signal from **measurement** — what the agent did — anchored
   to something *exogenous* to the loop. Not the model's idea of health.

**Priority ordering (refinement):** **groundedness gates individuality.** An
agent earns the right to be judged against its own normal *to the extent it can
be externally checked*. Individuality is earned against grounding, not asserted.
This ordering is what makes the three axioms a system rather than three wishes.

---

## 2. Glossary — keep these four distinct, never blur

The bug class is letting these collapse into one object.

| term | definition | box |
|---|---|---|
| **baseline** | descriptive estimate of *this agent's* normal (per-agent, learned, non-stationary) | estimator |
| **anchor** | measured healthy population/class operating point (exogenous, slow) | estimator (prior on the baseline) |
| **prior** | the ODE's *prediction* of next state | predictor |
| **policy threshold** | the decision rule over residuals/outcomes | policy |

"Reference" = whichever of {baseline, anchor} an estimate is compared against;
never a synonym for prior or threshold.

---

## 3. Diagnosis — why the maths drifted from the north star

| imported tool | encodes | violates |
|---|---|---|
| Objective Φ (loss vs fixed ideal E→1, I→1, S→0, V→0) | RLHF/punish-toward-optimum | 2 |
| Contraction / V-damping (ODE → one fixed point) | homogeniser (V→0, coherence→0.49) | 1 |
| Thermodynamics **as controller** | a groundedness-attempt that inverted into a prescribed ideal | 3 |

Not translation failures by the operator: the default vocabulary of applied
maths — objectives, controllers, stability proofs — is built *for* the
converge-to-ideal paradigm. Reaching for a standard tool imports a value the
design opposes. That mismatch is the friction.

---

## 4. The architecture — three boxes the ODE currently fuses

- **Estimator (detection → individuality).** Per-agent baseline; the live signal
  is the **residual = measurement − reference**. ~80% exists already
  (`behavioral_eisv` Welford baselines). Net-new is §4a/§4b.
- **Predictor (the ODE).** A *prior*, not a verdict. Thermodynamics demoted from
  controller to predictor: keeps its physics without dictating health. Damping
  becomes harmless — a biased prediction shows up as residual, not a bad
  decision; stability stops being load-bearing.
- **Policy (in service of growth).** A **decision statistic** over residuals and
  outcomes — NOT an "objective". (Wording is load-bearing: "objective" smuggles
  the attractor logic back. A statistic is a *test*; an objective is a *target*.)
  Hysteresis lives here, never in the sensor. Temperament: §4c.

### 4a. The reference is hierarchical, not purely per-agent

```
reference = blend( agent_baseline, class_anchor ; w(confidence) )
residual  = measurement − reference
```

Shrinkage toward the class anchor when the agent is sparse/cold-start; the blend
shifts to per-agent as evidence accrues. Individuality is thus **earned through
evidence**, not asserted from too few samples — which is *more* faithful to the
growth axiom than instant per-agent overfitting.

**Confidence must be measured, not assumed.** Today `baseline_confidence`
saturates to 1.0 by *sample count* (`updates_completed`) — that is the
homogenised form (everyone reaches 1.0 the same way). The non-homogenising form
is **measured self-predictability**: does this agent's baseline actually predict
its own next state? A stable agent earns confidence fast; a genuinely chaotic one
never reaches 1.0 regardless of sample count — correctly. Confidence becomes
per-agent and *falsifiable* (it ties to groundedness).

**Humility is structural, not a tunable scalar.** It decomposes the same
hierarchical way as the reference:
- an operator-set **floor** — the minimum shrinkage ever allowed **[L3]**;
- a fleet **prior** — learnable from outcomes (population-optimal shrinkage);
- a per-agent **modulation** — by *external-anchor coverage* (rich recent labels
  for this agent → less shrinkage; sparse/stale → more).

Every edge in that is allowed **except one**: *agent self-confidence must never
lower humility.* A quantity that guards against overconfidence cannot be governed
by confidence, or it stops guarding. This is the single forbidden coupling.

### 4b. Baseline update discipline — where self-confirmation lives

The risk is not *that* the baseline learns; it is *when*.

1. **Outcome-gated learning.** A blunt freeze-on-high-residual rule would violate
   axiom 2 — a high residual is also what legitimate *growth* looks like, so a
   blunt freeze punishes growth. Gate on the **outcome**, not the deviation:
   - deviation **+ bad/pending outcome** → do **not** fold into baseline;
   - deviation **+ verified-good outcome** → **do** fold it in (this is growth).
   This is "deviation is information" made precise: the information is *resolved
   by the outcome*, then learned. Requires labels → §0.
2. **Cap the baseline drift rate** (no fast self-tracking).
3. **Downweight/freeze updates during pause/reject windows.**
4. **Audit every recenter with the exogenous anchor that licensed it.**

### 4c. The policy's temperament — generous tit-for-tat

The governor↔agent relationship is an iterated game. The temperament that is
non-punishing yet non-exploitable is **generous tit-for-tat**:

- **Nice** — never pause-first without provocation. (The historical over-pausing
  of a healthy coding agent was the *governor defecting on a cooperator*; this
  rule forbids it.)
- **Provocable** — withdraw trust on a *verified-bad* outcome (§4b gate). Not a
  doormat.
- **Forgiving** — restore trust quickly once outcomes recover. One response, not
  a permanent grudge (anti-Grim-Trigger). This is the growth axiom in the policy.
- **Legible** — predictable, not an opaque reward model. (Anti-RLHF: the agent
  can see why.)
- **Generous** — forgive *noisy/ambiguous* defections; require a *clear* signal
  before withdrawing trust. Pure tit-for-tat echoes into recrimination spirals
  on a noisy channel, and ours is noisy. (Same as the outcome-gate: never pull
  trust on one murky signal.)

Retaliation here is a *proportional, reversible signal*, not corrective force
toward a target — that is the line between reciprocity and punishment, and it is
the needle the design threads. (Limit: tit-for-tat is dyadic; the fleet level
needs reputation / indirect reciprocity layered on — a later concern, not solved
here.)

### 4d. Two V channels — say which is which

- **ODE V** (what the damping acts on): feeds only the dead legacy coherence cast
  + `void_active` (live footprint = 3 events / 47k). → **demoted to predictor**;
  nearly free to demote.
- **Behavioral V**: one axis of the per-agent residual; already used in
  behavioral assessment. → **drives residuals.**
- **Coherence** (manifold): already **V-free** (E/I/S only). Demoting V does not
  touch the live coherence readout.

---

## 5. Invariants — the rule that stops the homogenisation recursion

Adding any control metric can re-homogenise one level up. These hold at *every*
level:

1. **A uniform *procedure* is fine; a uniform *target* homogenises.** Everyone
   measured the same way is physics applied evenly; everyone pulled toward the
   same point is the bug. Test every new metric: procedure or target?
2. **Estimates are per-agent and measured** (confidence, baseline, residual).
   **Constants are small, explicit, operator-set values** (the humility floor,
   anchor trust tiers). Never an emergent metric standing in for a value.
3. **No metric that guards overconfidence may be driven by confidence** (§4a).
4. **No self-referential anchor**: a signal derived from the loop cannot anchor
   the loop (§7).

---

## 6. Roadmap — reversible, flagged, layer-tagged

Layers: **[L1]** measured (*verify*) · **[L2]** resolvable-by-stress (*red-team +
harness*) · **[L3]** ontology (*operator's call*).

| step | what | status | layer | gate |
|---|---|---|---|---|
| **0** | exogenous anchor registry (§7) — prerequisite | not started | L1 | registry + trust tiers defined; labels flowing |
| A.1 | per-class S setpoint | shipped, flagged-off (#1048) | L1 | setpoint lands on measured-healthy |
| A.2 | Φ recentred on setpoint (population-relative) | shipped, flagged-off (#1058) | L1 | verdict invariant at new attractor |
| B | decision statistic on per-agent residual (hierarchical, §4a–c) | next, flagged | L2 | statistical gate, §6-gate below |
| C | estimator/predictor split — ODE → prior, residual is the signal | research | L2/L3 | cold-start intact; §0 anchor live |

**Stage 0 is a prerequisite, not a safeguard.** §4b (outcome-gated updates) and
B's gate ("residual predicts bad outcomes") both depend on labeled outcomes
existing first.

The recenter progression is one operation at finer grain:
```
absolute ideal (Φ today) → population-relative (A.2) → per-agent hierarchical (B) → moving prior (C)
```

**B's gate is statistical, not just "no band shift":**
1. baselined healthy agents keep the **same verdict distribution within tolerance**;
2. synthetic excursions still escalate at **equal-or-better recall**;
3. the **residual statistic predicts bad outcomes better than absolute Φ** (the
   falsifiability test — needs §0 labels);
4. **heterogeneity false-positives fall without raising false-negatives.**

(1)+(2) are the safety floor (computable without labels — can start before §0
completes); (3)+(4) are the justification (need §0).

---

## 7. The exogenous anchor registry (Stage 0)

Accepted anchors, with trust tiers:

| tier | anchor | note |
|---|---|---|
| gold | operator corrections, verified production-incident flags | human/ground-truth |
| strong | CI/test pass-fail, verified tool failures | objective but gameable |
| medium | task_completed / task_failed, PR-review outcomes | outcome-attested, noisy |

Rules: every baseline recenter (§4b) and every B-gate label (§6) cites an anchor
of declared tier; weight by tier; no self-referential anchor (Invariant 4).
First task: inventory which of these the runtime already emits (`outcome_event`,
`record_result`, task outcomes) vs. which need wiring.

### 7a. Framing — informativeness, not suspicion (operator poke, 2026-06-26)

Build the tiers as an **informativeness gradient** (how much an outcome tells you
about the work), NOT a **trust/suspicion ledger** (how likely the agent is to be
bluffing). The two yield similar weights — an independent+broad outcome is both more
*trustworthy* and more *informative* than a self-selected+narrow one — but the
ontology is the cooperative one (axiom 2, growth-not-punishment; the governor is a
generous cooperator per §4c, not a dealer defending a pot). Consequences:

- The "strong" tier's "objective but **gameable**" is better read as "objective but
  **selection-limited**": a self-selected passing test has narrow coverage, so it
  carries less information — discount it for *coverage*, not for imputed intent. No
  accusation; just less signal. (A *failing* test is high-information regardless of
  who triggered it — keep it trusted.)
- Invariant 4 ("no self-referential anchor") survives — but on the **circularity**
  basis, not the suspicion basis: validating the loop with the loop's own output is
  mathematically degenerate (self-confirmation), independent of whether the agent is
  cooperating. Keep it as an information rule, not a trust gate.
- The genuinely adversarial case — *deliberate* concealment / sandbagging (the
  cross-framing finding: EISV inverts on calibrated concealment) — is a
  **detector-validation** question (red-team / model-organism lab), NOT a live trust
  gate on cooperating agents. Adversarial-aware in the lab; cooperative at the table.
  Do not import the lab's suspicion into the operating relationship.

Build note: when Stage 0 wires tiers, weight by informativeness; do not encode
suspicion of intent into the live path. (This is a framing constraint, not a recut
of the live corroboration model — see operator call 2026-06-26: reframe, don't
re-cut.)

---

## 8. L3 decisions — RESOLVED by operator (2026-06-25)

The ontology knobs are now decided. Recorded as direction; each lands as its own
flagged change.

0. **Φ → telemetry, not a verdict floor.** Φ stops gating decisions; the
   per-agent residual / behavioral path becomes authoritative and Φ is kept only
   as a telemetry field. Cleanest expression of axiom 2 (Φ is the RLHF/
   punish-toward-ideal shape), and now *evidenced safe*: the Stage-B safety-floor
   probe (`scripts/analysis/eisv_stage_b_safety_floor.py`, 2026-06-25) shows
   currently-safe agents at residual p99≈6.8 vs caution ≈10 — clean separation,
   ~1% regression floor — so demoting Φ doesn't strand healthy agents. (NB:
   supersedes the *purpose* of the A.2 coupling — once Φ is telemetry, keeping
   its verdict invariant is moot; A.2 stays correct as the interim while Φ still
   gates, i.e. until this lands.)

1. **Resident ground truth = "whatever we can."** No canonical per-resident
   source required; take every exogenous signal and tier it (§7): Watcher
   resolve/dismiss (shipped, #1061), CI/test outcomes, dialectic outcomes,
   operator corrections. Breadth over purity-of-source; wire opportunistically,
   weighted by trust tier.

2. **Hysteresis — measured small, and mostly already pre-paid** (§4c). The
   operator's `0.5–0.6` was flagged as an arbitrary guess; measuring the residual
   (`scripts/analysis/eisv_residual_autocorr.py`, 2026-06-25) replaces it:
   - lag-1 residual autocorrelation **ρ = 0.994** (16 agents, 6357 step-pairs);
     **90%** of >p90 excursions persist beyond one check-in — the residual barely
     flickers, so it needs *little* added hysteresis (≪ 0.5–0.6).
   - **The smoothing already exists in the wrong place.** That ρ is high *because*
     the behavioral EISV the residual is built on is itself EMA-smoothed (alphas
     ~0.08–0.15). The hysteresis is **pre-paid by the estimator.** Stacking a
     0.5–0.6 policy dwell on top would be **double-damping — re-importing the
     `delta=0.4` over-damping disease one layer up** (an arbitrary constant *and*
     a compounded smoothing). DO NOT re-introduce 0.5–0.6 as a fresh magic number.
   - So the real question is *placement*, not value: to get the roadmap's
     "responsive sensor, calm policy," **reduce the behavioral alpha and move a
     small deliberate dwell into the policy** — keep the *total* near today's, do
     not add to it.
   - Seams: can't cleanly separate true persistence from the existing EMA without
     unsmoothing the behavioral path; can't validate that small hysteresis catches
     real drift (vs misses it) until the anchor bridge has outcome labels. This is
     a data-grounded *estimate* ("small, mostly already spent"), not yet a
     *validated* value.

3. **Humility = structural, not a tuned scalar** (§4a). Per the operator: "an
   unquantifiable thing." Stays a *stance* (nice/provocable/forgiving reciprocity,
   §4c) expressed via anchor-coverage modulation — no humility *number* to set,
   no floor to pick.

---

## 9. Process note — keep the layers separate (the roadmap's main job)

The accretion (phi-only-risk, damping firefight, dead coherence gates) happened
because determinate fixes, control-theory machinery, and an unmade ontology
decision were all resolved by the *same* ad-hoc commit.

- **L1 (measured)** → verification. A diverse-opinion council *convolutes*.
- **L2 (resolvable)** → red-team / single independent skeptic + harness. (This
  document's own maturation was an instance: an independent review disputed and
  sharpened it rather than rubber-stamping.)
- **L3 (ontology)** → operator. A council here launders taste as a verdict.

Spend the red-team budget on one thing: **does the freeze/anchor rule actually
keep "baseline learns normal" externally falsifiable?** (§4b/§7). That is the
whole design in one line: *recenter the reference, but keep the reference
externally falsifiable.*

---

## Appendix A — Stage 0 reconnaissance (2026-06-25)

Stage 0's substrate already exists: **`audit.outcome_events`** (~79k rows,
partitioned monthly since Feb 2026) carries per-agent attribution, an `is_bad`
label, the EISV state *joined at the outcome moment* (`eisv_e/i/s/v/phi/verdict/
coherence/regime`), and a **`verification_source`** column that *is* the §7
trust-tier field. So Stage 0 is "tier and filter what already flows", not "build
a table".

**But Invariant 4 bites on the live data — 88% of rows are self-referential.**
Tiering by `verification_source`:

| source | count | bad | tier |
|---|---|---|---|
| `server_observation` (mostly `trajectory_validated`) | 29.6k | 934 | **EXCLUDED — self-referential** (the loop validating its own trajectories) |
| `(null)` provenance | 45.8k | 947 | EXCLUDED — untiered |
| `agent_reported_tool_result` | 2.1k | 36 | SOFT — self-attested, gameable |
| **`external_signal`** | **1.6k** | **498** | **TRUSTED_EXTERNAL** (task/test outcomes verified outside the loop) |

The dominant `trajectory_validated` signal is the governance loop validating its
*own* trajectories. Using `outcome_events` unfiltered as the anchor would build
the exact echo chamber this roadmap prevents. **Invariant 4 is load-bearing on
this table today.**

**Real exogenous label budget: ~1,632 events, ~498 bad.** That is the honest
ceiling for B's falsifiability gate (§6.3). Enough for a per-class analysis of
high-population classes (Lumen/Sentinel); **statistically thin for rare classes**
until more exogenous labels accrue — B's justification will be class-uneven, and
the roadmap should not claim otherwise.

**Gap:** `test_failed` = **1** event. The most objective *bad* anchor — a failing
test — is essentially not being captured. CI/test-failure wiring into
`outcome_events` is a concrete Stage 0 task.

**Stage 0 reduces to three tasks** (not a build-from-scratch):
1. tier-map `verification_source` and expose only externally-anchored outcomes —
   `external_signal` → trusted, `agent_reported_tool_result` → soft,
   `server_observation`/`null` → **excluded** (Invariant 4). *(First code:
   `src/grounding/outcome_anchors.py` + `scripts/analysis/outcome_anchor_inventory.py`.)*
2. wire the missing CI/test-failed anchor.
3. accept the ~498-bad-label ceiling as the current limit and let it grow;
   gold/strong separation within `external_signal` (operator-correction vs CI)
   is a later refinement (may live in `detail` jsonb).

---

## Appendix B — Stage B viability: anchor and EISV populations are disjoint (2026-06-25)

Going to compute B's falsifiability gate (§6.3 — does the per-agent residual
predict bad outcomes better than Φ), it turns out to be **not computable today
for a reason deeper than thin labels: the labelled population and the EISV
population do not intersect.**

| measure | value |
|---|---|
| anchored (`external_signal`) outcomes | 1,632 |
| …carrying EISV (`eisv_phi` not null) | **2** |
| distinct agents with anchored outcomes | 137 |
| …with *any* EISV state row | **1** |
| …that are *baselined* (→ residual computable) | **0** |
| anchored outcomes joinable to a prior baselined state | **0** |

By contrast, EISV-at-outcome *is* recorded for the self-referential
`server_observation` stream (100%) — exactly the rows Invariant 4 forbids as
anchors. So the agents we can **ground** (external outcomes) have no residual,
and the agents with rich **residuals** (residents) receive only self-referential
outcomes. B's §6.3 gate is **structurally uncomputable** — the validation-gap
pathology, by construction, not by label scarcity.

**Revised prerequisite (supersedes "just wire test_failed"):** the EISV pipeline
and the exogenous-anchor pipeline must be made to *overlap*. Concretely:
(a) **snapshot the agent's EISV onto `external_signal` outcome rows** (today
`eisv_*` is null for them), and (b) ensure resident/EISV agents actually
**receive** external task/test/outcome labels attributed to them. Until (a)+(b),
B's *justification* (§6.3) cannot be claimed; B's *safety-floor* (§6.1–6.2,
non-regression) remains computable now without it.

Probe / regression guard: `scripts/analysis/stage_b_viability.py` — reports the
overlap and starts emitting the residual-vs-Φ AUC the moment it becomes non-zero.

**Sequencing consequence:** Stage 0 is now two prerequisites, not one — the
*tier filter* (shipped, `outcome_anchors.py`) **and** the *population bridge*
(a+b above). B follows the bridge, not the filter alone.

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

---

## 8. Open knobs — [L3], the operator's, held open

1. **Humility floor (§4a):** does high-confidence individuality go all the way
   (`w→0`) or keep a permanent humility term toward the class? Its *meaning* is
   now fixed (how much external check before granting individuality, per the
   priority ordering); only its *amount* is a value call.
2. **Hysteresis amount (§4c):** dwell-time before a residual becomes a verdict —
   the damping's old job, relocated to the policy. How generous is "generous".

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

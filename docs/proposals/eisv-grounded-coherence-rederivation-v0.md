# EISV grounded coherence — re-derivation & the grounding position (v0)

**Status:** design proposal / **whiteboard candidate**. NOT deployed, NOT
outcome-validated. The stability properties below are numerically verified; the
groundedness property is a claim to be earned with data, not proven here.
**Method:** starts from the operator's design values, translates them into maths,
and holds value-decisions open (marked **[L3]**). The values are the operator's;
the translation and the numerical checking are the engineering.
**Provenance:** closes the paper's own open item — `unitares-v6.tex` remark
"Contraction under a grounded coherence" — using the two-wave core-math audit, the
patent-origin audit (`docs/ontology/eisv-proprioception-contract.md` rows 13–44),
and a design exploration of three independent formulations, each with its stability
certificate numerically re-derived and checked. Full working notes are kept out of
the repo (operator's machine, `~/.unitares/analysis/`).

---

## 1. Why this exists

The paper (v6) already fixed the worst of the original (patent-era) maths: it
unified the two conflicting entropy definitions into a single SDE, damped the void
integral `V` so it is no longer an unbounded accumulator, and — unlike the
deployment — carries an actual contraction certificate that *includes* `V`. Those
gains are real and this proposal keeps all of them.

What the paper left open is coherence. It still computes
`C(V) = C_max·½(1 + tanh(C₁·V))`, and it notes itself (`unitares-v6.tex` ~L553)
that on the live `V ∈ [−0.1, 0.1]` this pins `C(V) ≈ 0.49`. The consequence,
confirmed against the live store: the `proceed`/`pause` coherence gate cannot fire
— **0 of 68,374 recorded states** reach the healthy band. `V`'s only consumer is
that pinned `C(V)`, so `V` is also near-inert; and the equilibrium `I* ≈ 0.80` is a
fleet constant, which contradicts the individuality value. The paper flags the fix
— "contraction under a grounded coherence" — as open work. This document does it.

## 2. Design values (the north star; not relitigated here)

1. **Individuality** — judge an agent against *its own* normal, not a fleet ideal.
   ⇒ a fleet-constant equilibrium is a defect to remove.
2. **Growth, not punishment** — deviation is information; the reference is allowed
   to move. ⇒ no fixed-ideal squared-loss objective (no reward toward
   `E→1, I→1, S→0, V→0`). The patent-origin audit confirmed the original design
   never had one; this proposal does not reintroduce it.
3. **Groundedness** — signal from measurement anchored to something exogenous to
   the self-model loop. ⇒ coherence must be a measured residual, and its
   anti-masking property must be *earned with data*, not asserted.

Endorsed target shape: predictive-processing / active-inference with non-stationary
per-agent priors; the ODE is a **predictor/prior**, not a controller; policy is a
decision statistic on the residual.

## 3. Recommended formulation

### 3.1 Coherence — a per-agent measured residual [L1]

$$C_a(z) = \exp\!\left(-\tfrac12\,D_a^2\right), \qquad
D_a^2 = \sum_j w_{a,j}\,(z_j - r_{a,j})^2,\quad z=(E,I,S)$$

- `r_a = (Ē_a, Ī_a, S̄_a)` is the agent's **own** slow, non-stationary,
  outcome-gated baseline; `w_{a,j} = 1/σ²_{a,j}` its own precision (expanding
  Welford / EMA with an operator floor). This is the Gaussian form of the paper's
  own `eq:C-KL`/`eq:C-manifold`, individualised — developing the paper's coherence,
  not inventing a new one.
- **Evaluated at two tagged points** (one definition, not the patent's dual-S sin):
  `C_a(yₜ)` on the raw per-turn measurement residual is the **policy gate
  statistic**, read as a windowed/hysteretic statistic, **never a per-turn
  threshold** (a naive per-turn gate false-pauses a *healthy* agent ~24% of the
  time — hysteresis is mandatory). `C_a(x)` on the smoothed state is optional
  telemetry and is **not** in the certified ODE.
- A well-calibrated residual has `E[D_a²] ≈ d`, so the healthy operating point sits
  mid-range (~0.61 at the median under NIS normalisation) — usable and crossable in
  both directions, which is the whole point.

### 3.2 Dynamics — reduced 3-state, coherence out of the ODE [L1/L2]

$$\begin{aligned}
\dot E &= \alpha(I-E) - \beta_E E S + \gamma_E\|\Delta\eta\|^2 &&\text{[unchanged — the sound relational core]}\\
\dot I &= -kS - \gamma_I\,(I - \bar I_a) &&\text{[per-agent linear relaxation toward the agent's own baseline]}\\
\dot S &= -\mu S + \lambda_1\|\Delta\eta\|^2 + \beta_c\,C_{\text{task}} &&\text{[coherence entropy-sink }-\lambda_2 C(V)\text{ removed}\Rightarrow S^*\ge 0]}
\end{aligned}$$

**`V` is dropped from the certified core** (numerical check: `V` is ~85% redundant
with `E` at production damping, so dropping it forfeits ~15% slow-accumulator
variance). **[L3] operator call:** if the four-coordinate ontology is worth
keeping, reintroduce `V` only as a *policy-layer* leaky EMA of the outcome-channel
residual — a debt/hysteresis accumulator — not as a certified state coordinate.

### 3.3 Per-agent equilibrium (closed form)

$$S^* = \frac{\lambda_1 u + \beta_c C_{\text{task}}}{\mu}\ (\ge 0),\quad
I^* = \bar I_a - \frac{k}{\gamma_I}S^*,\quad
E^* = \frac{\alpha I^* + \gamma_E u}{\alpha + \beta_E S^*},\quad u:=\|\Delta\eta\|^2$$

Per-agent via `Ī_a, u` — **no fleet constant** (`Ī_a ∈ {0.5, 0.8, 0.9} ⇒
I* ∈ {0.44, 0.74, 0.84}`, checked). Relational (`E→I`), earned (the reference
moves), no fixed ideal.

### 3.4 Contraction certificate (re-derived under the new coherence) [L1]

Because coherence is a readout (`∂C/∂x = 0`), the Jacobian is upper-triangular:

$$J = \begin{bmatrix} -(\alpha+\beta_E S) & \alpha & -\beta_E E\\ 0 & -\gamma_I & -k\\ 0 & 0 & -\mu \end{bmatrix},\qquad
M = \mathrm{diag}(0.1,\,1.0,\,0.2)$$

- Eigenvalues = diagonal = `{−0.435, −0.25, −0.80}`, spectral abscissa **−0.25**.
- Lohmiller–Slotine condition `MJ + Jᵀ M + 2α_c M ⪯ 0` holds on `[0,1]²` with
  certified **α_c = 0.15** (worst-corner eigenvalue −0.036, 0 violations on a 26×26
  grid; diagonal-metric optimum ≈ 0.228; empirical metric-distance decay 0.23–0.25).
  Compare the deployed **α_c = 0.019**.
- **G8 resolved:** `J₂₂ = −γ_I` is a *constant* bounded off 0 on the whole box.
  Individuality lives in the setpoint `Ī_a`, not in a boundary-vanishing logistic —
  the linear-vs-logistic tension was a false dichotomy (the logistic form turns
  *expanding* at the `I=1` boundary; the linear form does not).

### 3.5 The certificate's fine print (state it plainly)

1. **Quasi-static scope.** The certificate is for the fast `(E,I,S)` subsystem with
   the slow per-agent baseline frozen. The singular-perturbation (Tikhonov) proof
   that slow non-stationary baseline drift cannot destabilise the fast contraction
   is **not carried out**; it holds when baseline-EMA-rate ≪ α_c ≈ 0.15 — an
   **[L3] operator setting**.
2. **Readout, not coupled-case.** The strong global α_c comes from coherence being
   an exogenous readout (this *is* the endorsed predictor-not-controller shape).
   The paper's literal in-the-loop coherence is answered by relocation, and
   certifies only at **α_c ≈ 0.071** if kept in-loop (documented as a fallback, not
   recommended). Stress-testing confirms in-loop feedback wiring breaks the
   contraction — which validates the readout choice.
3. **Deterministic only.** The `σξ` mean-square ball is `O(σ²/2α_c)`; the constant
   for production `σ` is not evaluated.

## 4. What it preserves and what it costs

Preserves: the unified S SDE, the sound `(E−I),S` relational core, `μ > k²/4`.
Reintroduces neither a Φ punish-shape nor a fleet-constant equilibrium.
Costs: `V` dropped from the certified core (~85% redundant; mitigation above);
`γ_I` becomes a free relaxation-rate knob decoupled from the setpoint.

## 5. The grounding position — the part that is NOT a maths problem

This is the load-bearing finding, and it reframes the whole programme.

**The population bridge is already built.** A census of `audit.outcome_events`
(2026-08-06) shows that EISV now attaches to exogenous (`external_signal`) outcome
rows and has since ~2026-07-01:

| month | `external_signal` rows | carry EISV | bad-with-EISV |
|---|---|---|---|
| 2026-06 | 1,717 | 28 (1.6%) | 4 |
| 2026-07 | 274 | 221 (81%) | 49 |
| 2026-08 | 171 | 146 (85%) | 30 |

So the "anchored-outcome and EISV populations are disjoint" blocker (roadmap
App. B, "2 of 1,632") is **stale** — it described June. The prerequisite the
roadmap named is done; **building more label-attachment plumbing is not the lever.**

**The binding constraint moved to label volume and independence, and that is
structural.** The exogenous, EISV-carrying **bad-label** budget is **83 rows in 26
independent (agent, session) clusters, 3 agents past the per-agent threshold** —
against the pre-registered outcome-grounding stop rule's requirement of **≥150
clusters** (`eisv-outcome-grounding-stop-rule-v0.md`, read wired for 2026-12-01).
Twenty-six versus one-fifty, heavily autocorrelated. This is not a plumbing gap; it
is how few *independent* bad outcomes a governance fleet produces, and the
`project_eisv-validation-gap` analysis argues it is not buyable (CI cannot attribute
to agents; the clean-label rate has a low ceiling). *(This is a census of label
availability, not the stop-ruled discrimination read — that read is left for its
scheduled date.)*

**Consequence for the programme.** Outcome-validation of EISV is walking toward the
stop-rule floor and no engineering removes the wall. The earned claims are all
**label-free**: the verified contraction/proprioception properties above, the
policy-coherence invariants, and the honest advisory-instrument posture
(`docs/ontology/eisv-proprioception-contract.md`, "Deployed posture"). **[L3]** The
positioning decision — commit to UNITARES as an advisory proprioceptive instrument
with a sound mathematical spine that does **not** claim to be an outcome oracle, and
retire the outcome-validation ambition — is a value/positioning call for the
operator. This document makes clear only that the maths and the data both support
that framing and not the oracle one.

## 6. Open questions & next moves

- **[L3, data/operator]** Groundedness anti-masking is unvalidated and failing today
  (a baseline that chases real slow drift keeps the gate satisfied ~99% of the time
  under a total drift of 0.6). Mitigations (baseline drift-rate cap,
  self-predictability-weighted shrinkage toward a class anchor, outcome-gating on the
  trusted tier only, Invariant 4) are designed but cannot be validated at the
  per-agent independence the data currently supports. This is the real prerequisite,
  and it is a data/positioning question, not a derivation.
- **[L2]** Deferred proofs: the Tikhonov timescale-separation result and the
  stochastic mean-square constant.
- **[L1]** Gate calibration (lengthscale, dispersion floor, `τ`) and E-channel
  weighting must be *measured* per class, not asserted.
- Each *move* toward deployment (should it ever be taken) lands as its own flagged,
  reversible PR with its own gate, per the roadmap discipline — and only behind the
  advisory posture, never as a re-armed enforcement path.

**This document is a candidate for review. It does not change any deployed
behaviour, flag, or threshold, and it is not evidence that EISV predicts outcomes.**

## 7. Decision record — V-reintroduction (2026-08-07)

The §4 question "reintroduce V as a policy-layer debt EMA?" was resolved by
governed dialectic review (session `3e003d82fb2d251e`, converged): **DEFERRED
with a wired wake condition.** Binding conditions (full record and the deployed-V
consumer inventory live in `docs/ontology/eisv-proprioception-contract.md`,
"Decision record — V-reintroduction deferred"):

1. V stays out of the certified core and off governed/verdict surfaces; the
   2026-12-01 #1425 stop-rule read is the calendar trigger only (necessary, not
   sufficient), and the frozen #1425 pre-registration is not amended for V.
2. Any build first requires a V-specific pre-registration: evidence-quality
   minima (per-agent / per-independent-cluster effective samples, cadence,
   external corroboration, settlement examples, missingness audit), the full EMA
   maths including **settlement**, and an incremental-information test vs
   E, I, S and naive baselines. Exogenous input alone does not establish
   independence.
3. If built: soak clears both ≥30 days and the evidence minimum, advisory-tagged
   at zero verdict weight, with rollback criteria and demonstrated settlement;
   any gate/weight use is a separate review.
4. At implementation of the 3-state core: CI proof of V-absence from verdict,
   pause, risk, coherence, gating, and hysteresis paths.

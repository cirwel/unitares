# EISV reduced-core general solution (v0)

**Status:** derivation + numerical verification. **No deployed behaviour, flag,
or threshold changes.** Companion code: `scripts/analysis/eisv_general_solution.py`
(verification suite; exit 0 iff all checks pass) and
`tests/test_eisv_general_solution.py`.
**Provenance:** completes, at the general level, the two proofs
`eisv-grounded-coherence-rederivation-v0.md` §3.5 explicitly deferred (the
slow-drift/Tikhonov-style bound and the stochastic mean-square constant), and
derives the analytic statistics of the residual coherence readout. Triggered by
the doctor's `signal_degeneracy` finding on `coherence` (live doctor run,
2026-08-13: fleet sd 0.008030, range [0.4671, 0.5077], 7,940 distinct values
among n=8,401 rows; a different window from the 2026-08-10 figures embedded in
`governance_core/coherence.py`) — the legacy
`C(V) = ½(1 + tanh(C₁V))` is input-starved, per the diagnosis in
`governance_core/coherence.py`. This document does **not** repair the live
signal; it makes the *replacement's* statistics computable in advance, so gate
re-derivation can be model-informed rather than alarm-rate-fitted (the exact
failure mode the doctor finding warns against).

---

## 1. The claim

The rederivation proposal's reduced 3-state system (§3.2),

$$\begin{aligned}
\dot S &= -\mu S + \lambda_1 u + \beta_c\,c, \qquad u := \|\Delta\eta\|^2,\ c := C_{\text{task}}\\
\dot I &= -kS - \gamma_I\,(I - \bar I_a)\\
\dot E &= \alpha(I-E) - \beta_E E S + \gamma_E u
\end{aligned}$$

is **upper-triangular**: S sees nothing, I sees only S, E sees (I, S). For
piecewise-constant inputs $(u, c, \bar I_a)$ this makes the maths *general* in
the classical sense — the system is **solvable by quadratures**. S and I are
elementary; E reduces to one explicit 1-D integral with an incomplete-gamma
closed form. Everything downstream (equilibrium sensitivities, the tracking
bound, the stationary covariance, the coherence readout's moments) then comes
out as formulas rather than simulations.

## 2. The general solution

Let $x^\* = (E^\*, I^\*, S^\*)$ be the closed-form equilibrium (proposal §3.3),
$a := S_0 - S^\*$, $A := \alpha + \beta_E S^\*$.

**S** (exact): $\;S(t) = S^\* + a\,e^{-\mu t}$.

**I** (exact): for $\gamma_I \neq \mu$, with $b := -ka/(\gamma_I - \mu)$,

$$I(t) = I^\* + (I_0 - I^\* - b)\,e^{-\gamma_I t} + b\,e^{-\mu t},$$

and in the resonant case $\gamma_I = \mu$:
$I(t) = I^\* + (I_0 - I^\*)e^{-\mu t} - k\,a\,t\,e^{-\mu t}$.

**E** (by quadratures): E obeys a linear time-varying scalar ODE with
integrating factor $\Phi(t) = \exp\!\big(A t + \kappa(1 - e^{-\mu t})\big)$,
$\kappa := \beta_E a / \mu$:

$$E(t) = e^{-\Psi(t)} E_0 + \int_0^t e^{-(\Psi(t)-\Psi(\tau))}\,
\big(\alpha I(\tau) + \gamma_E u\big)\,d\tau,\qquad \Psi(t) := \ln\Phi(t).$$

Substituting $w = \kappa e^{-\mu\tau}$ turns each forcing exponential
$e^{-\rho\tau}$ ($\rho \in \{0, \gamma_I, \mu\}$) into an upper
incomplete-gamma difference,

$$\int_0^t \Phi(\tau)\,e^{-\rho\tau}\,d\tau
= \frac{e^{\kappa}\kappa^{s}}{\mu}\Big[\Gamma(-s,\;\kappa e^{-\mu t}) - \Gamma(-s,\;\kappa)\Big],
\qquad s := \frac{A-\rho}{\mu},$$

real-valued for either sign of $\kappa$ by analytic continuation (with
$\kappa^s$ and $\Gamma(-s,\cdot)$ taken on a common, e.g. principal, branch).
Scope notes: at $\kappa = 0$ ($S_0 = S^\*$) the formula is indeterminate and
the integral is elementary, $(e^{(A-\rho)t}-1)/(A-\rho)$; in the resonant case
$\gamma_I = \mu$ the forcing contains a $\tau e^{-\mu\tau}$ term covered by
$d/ds$ of the same identity, not by the exponential-only statement above. The
companion module evaluates the $\tau$-form directly with Gauss–Legendre
quadrature (the continuation-safe evaluation, uniform in all these cases);
against RK45 at rtol $10^{-11}$ the closed forms agree to **max abs error
2.1·10⁻¹²** across random parameter draws, both named parameter sets, and the
resonant case.

Because the only nonlinearity is the bilinear $E\cdot S$ term and S decouples,
this is the **exact** solution of the nonlinear system — not a linearization.

## 3. Slow-drift tracking — the deferred §3.5(1) proof, made explicit

The equilibrium map is closed-form, so its sensitivity is too:

$$\frac{\partial x^\*}{\partial \bar I_a}
= \Big(\tfrac{\alpha}{A},\; 1,\; 0\Big),\qquad
\frac{\partial S^\*}{\partial u} = \tfrac{\lambda_1}{\mu},\quad
\frac{\partial I^\*}{\partial u} = -\tfrac{k\lambda_1}{\gamma_I\mu},\quad
\frac{\partial E^\*}{\partial u} = \tfrac{\alpha\,\partial_u I^\* + \gamma_E - E^\*\beta_E \lambda_1/\mu}{A}.$$

With the frozen-input system contracting at certified rate $\alpha_c = 0.15$ in
constant metric $M = \mathrm{diag}(0.1, 1.0, 0.2)$ (proposal §3.4), the standard
perturbed-contraction (ISS) lemma gives, for a drifting baseline $r(t)$:

$$\|x(t) - x^\*(r(t))\|_M \;\le\; e^{-\alpha_c t}\,\|x_0 - x^\*(r_0)\|_M
\;+\; \frac{\nu}{\alpha_c},\qquad
\nu := \sup_t \|D x^\*\,\dot r\|_M .$$

For drift in $\bar I_a$ alone, $\nu = \|M^{1/2}\,\partial x^\*/\partial \bar I_a\|_2
\cdot |\dot{\bar I}_a| \approx 1.046\,|\dot{\bar I}_a|$ at the certificate
parameters (evaluation point $u{=}0.2$, $c{=}0.4$, $\bar I_a{=}0.8$). The
proposal's condition "baseline-EMA-rate ≪ α_c" becomes a number: **the bound
guarantees an asymptotic M-norm tracking error ≤ 0.05 whenever
$|\dot{\bar I}_a| \le 0.00717$ per unit time** (sufficient, not necessary —
faster drift merely voids the guarantee; observed error at the threshold rate
is ~0.03). The **[L3]** status of the drift-rate setting is unchanged — the
operator still picks the EMA rate — but the consequence of any choice is now a
formula, not a hope. Verified by simulation (sinusoidal drift saturating a
rate cap of 0.004/unit time, whose bound is ν/α_c = 0.0279): observed worst
error 0.0154 ≤ 0.0279.

*Scope:* this is the quasi-static tracking bound in the constant metric M,
valid while $x(t)$ and $x^\*(r(t))$ (and the segment between them) remain in
the certified $(E,S) \in [0,1]^2$ region — the LMI is certified only there,
and convexity of the box covers the mean-value segment. The stated sum form
drops the tighter $(1-e^{-\alpha_c t})$ factor on the ν/α_c term, so it is
slightly loose during the transient. It is not a full Tikhonov
singular-perturbation theorem for state-dependent metrics; for this system the
certificate metric is constant, so the ISS form is the theorem needed.

## 4. Stochastic mean-square constant — the deferred §3.5(3) proof

With noise $\sigma\,\xi$ entering the S channel, the stationary covariance P of
the fluctuation around $x^\*$ solves $JP + PJ^\top = -\,\mathrm{diag}(0,0,\sigma^2)$.
J is upper-triangular, so P has a closed form by back-substitution:

$$P_{SS} = \frac{\sigma^2}{2\mu},\quad
P_{IS} = \frac{-k P_{SS}}{\mu + \gamma_I},\quad
P_{II} = \frac{-k P_{IS}}{\gamma_I},\quad
P_{ES} = \frac{\alpha P_{IS} - \beta_E E^\* P_{SS}}{\mu + A},$$
$$P_{EI} = \frac{-k P_{ES} + \alpha P_{II} - \beta_E E^\* P_{IS}}{\gamma_I + A},\qquad
P_{EE} = \frac{\alpha P_{EI} - \beta_E E^\* P_{ES}}{A}.$$

The **(I, S) block is exact for the full nonlinear system** (that subsystem is
linear); the E row is a linearization of the bilinear term, so the mean-square
ball $\mathbb{E}\|x - x^\*\|_M^2 = \mathrm{tr}(MP)$ is exact under the E-row
linearization (the exactly-known (I,S) contribution is ~97.5% of the total at
these parameters). At the certificate parameters and the same evaluation
point:

$$\mathrm{tr}(MP) = 0.1526\,\sigma^2
\quad\text{vs the generic contraction bound}\quad
\frac{M_{SS}\,\sigma^2}{2\alpha_c} = 0.6667\,\sigma^2,$$

i.e. **the exact constant is 4.4× tighter than the ball at the certified
α_c = 0.15** (against the rederivation's metric-optimal rate ≈ 0.228 the
factor is ~2.9×). At $\sigma = 0.05$: stationary sds are E 0.0098, I 0.0077,
S 0.0395. Verified: Lyapunov residual ~10⁻²⁰; Euler–Maruyama Monte Carlo **on
the full nonlinear SDE** agrees to <1%, which also bounds the E-row
linearization error at this noise level. Both the ball and the linearization
implicitly assume the state stays where the certificate holds — automatic at
small noise (S sd 0.0395), unstated otherwise. The production constant is now
"plug the measured $\sigma$ into $\mathrm{tr}(MP)$" — the piece §3.5(3) left
unevaluated — with the caveat that a procedure for *estimating* the S-channel
$\sigma$ from discrete, gap-scaled check-in data does not exist yet and is
future work.

## 5. Analytic coherence statistics — the bridge to the doctor finding

The residual coherence readout (proposal §3.1) is
$C = \exp(-\tfrac12 D^2)$, $D^2 = (z - r_a)^\top W (z - r_a)$. Under the
stationary law $z - r_a \sim \mathcal N(m, P)$ ($m$ = baseline bias), the
Gaussian quadratic-form MGF gives **exact** moments:

$$\mathbb{E}[C] = \det(I + PW)^{-1/2}
\exp\!\Big(-\tfrac12 m^\top W (I + PW)^{-1} m\Big),$$

and $\mathbb{E}[C^2]$ is the same expression with $W \to 2W$, hence
$\mathrm{sd}(C)$ in closed form. Crossing probabilities
$P(C < \tau) = P(D^2 > -2\ln\tau)$ follow from the generalized-χ² law of $D^2$
(eigenvalues of $W^{1/2} P W^{1/2}$); the module verifies the moments against
Monte Carlo to 0.13%.

Consequences, at the certificate parameters with NIS weights
($W = \mathrm{diag}(1/P_{jj})$, so $\mathbb{E}[D^2] = 3$ exactly) and an
unbiased baseline:

$$\mathbb{E}[C] = 0.419,\qquad \mathrm{sd}(C) = 0.305 .$$

Two scope caveats before the readings. First, these are moments of the
coherence readout on the **smoothed state under process noise, with oracle
weights** (the true stationary variances). The rederivation's *policy gate
statistic* is $C_a(y_t)$ on the raw per-turn measurement residual with
per-agent Welford/EMA-estimated weights — that variable adds observation
noise and weight-estimation error on top of what is computed here, so its
dispersion is at least this large. Second, the MGF identity holds for
symmetric $W$ with $I + PW$ nonsingular (automatic for the positive-diagonal
NIS weights).

Three readings:

1. **The degeneracy is explained, not mysterious.** The doctor measures fleet
   sd 0.0080 on the legacy signal; a residual coherence has sd ≈ 0.3 *by
   construction* — the same order of magnitude as the shadow-measured
   manifold form (sd 0.285, `src/grounding/coherence.py`; note the
   functional forms differ — linear-in-distance vs $\exp(-D^2/2)$ — so this
   is corroboration of scale, not a like-for-like match). The legacy form's
   compression is input starvation (frozen ODE V), not an intrinsic property
   of coherence.
2. **Gate re-derivation becomes model-informed.** For any candidate threshold
   $\tau$ and lengthscale choice ($W \to W/\ell^2$ — the §6 **[L1]** knob), the
   healthy-stationary **per-sample marginal** crossing rate is an analytic
   function of $(\mu, \gamma_I, \alpha, \beta_E, k, \sigma, W)$, all
   measurable. That is the "compare the observed range with every configured
   consumer threshold" step of the doctor's contract review, done with
   formulas instead of fitted alarm rates. Note the rederivation (§3.1)
   mandates the deployed gate be **windowed/hysteretic, never per-turn**;
   sizing that statistic additionally needs a level-crossing/autocorrelation
   analysis (samples decorrelate at rate min(μ, γ_I) here), which is not done
   in this document.
3. **What this does NOT license.** These are the statistics of a *healthy
   stationary agent under the model*. Whether a given $\tau$ separates good
   from bad outcomes remains an outcome-evidence question governed by the
   stop rule (`eisv-outcome-grounding-stop-rule-v0.md`); nothing here changes
   `tau_floor` or any deployed gate, and the advisory posture is unchanged.

## 6. The certificate vs deployed parameters — one pass, one FAIL

The proposal's grid certificate (worst eigenvalue of
$MJ + J^\top M + 2\alpha_c M$ over $(E,S) \in [0,1]^2$, 26×26) is reproduced
independently: **−0.0356** at the certificate set ($\mu = 0.8$; proposal
reports −0.036).

**μ is not the sensitive knob.** The check at the deployed $\mu = 0.5$
(`DynamicsParams.mu`) passes with margin **−0.0244**. It does fail for
$\mu \lesssim 0.3$ (at $\mu = 0.3$ the worst eigenvalue is +0.0063), so the
certificate is *insensitive to the deployed S-decay value*, not
μ-independent.

**γ_I is — and the ACTIVE config fails.** The rederivation's certificate
assumes $\gamma_I = 0.25$. But `get_active_params()`
(`governance_core/parameters.py`) auto-applies $\gamma_I = 0.169$ (V42P
tuning) whenever linear I-dynamics mode is on with the default profile —
which is the running default. At the active $(\mu = 0.5, \gamma_I = 0.169)$
the α_c = 0.15 certificate **fails** (worst eigenvalue **+0.0411**); the
largest certifiable rate there is **α_c ≈ 0.117**. Consequence for anyone
carrying the reduced core toward deployment: either the certified rate drops
to ≤ 0.117 — which loosens the §3 tracking bound and drift budget by the
factor 0.15/0.117 ≈ 1.28 — or the reduced core adopts its own $\gamma_I$
rather than inheriting the active tuning. The companion script asserts this
finding (`ACTIVE_PARAMS` check).

## 7. Verification record (2026-08-13)

`python scripts/analysis/eisv_general_solution.py` — all PASS. The table
shows observed run values; enforced pass tolerances (in parentheses) are
looser. The incomplete-gamma expression itself is derived, not
machine-checked — evaluation uses the equivalent quadrature form.

| check | result (enforced tolerance) |
|---|---|
| closed forms vs RK45 (random draws + resonant case) | max abs err 2.1e−12 (<1e−6) |
| Lyapunov residual, certificate + deployed sets | ≤ 5.4e−20 (<1e−14) |
| stationary covariance vs Euler–Maruyama MC on the **nonlinear** SDE | max rel err 0.008 (<0.08) |
| coherence moments (MGF) vs MC, diagonal + non-diagonal W | max rel err 0.008 (<0.02) |
| contraction grid, μ=0.8 / μ=0.5 | −0.0356 / −0.0244 (≤0) |
| certificate FAIL at active config (γ_I=0.169), asserted | +0.0411; max α_c ≈ 0.117 |
| tracking bound under sinusoidal baseline drift (rate cap 0.004) | 0.0154 ≤ 0.0279 |

## 8. Limitations

- Inputs $(u, c, \bar I_a)$ piecewise-constant between check-ins; the closed
  forms compose across segments but are not derived for continuously-varying
  inputs (the §3 bound covers slow drift).
- §4–5 covariances linearize the E row (the (I,S) block is exact); §4's noise
  model is S-channel only, matching the unified S SDE.
- **This analyzes the proposed reduced system, not the deployed integrator.**
  The deployed dynamics add hard state bounds with the $S_{\min} = 0.001$
  epistemic-humility floor, soft barrier forces, default-on sensor spring
  coupling (`k_anchor`), adaptive λ₁/C₁, the V channel, and discrete
  gap-scaled integration — none of which appear in the reduced model. The
  analysis is continuous-time; check-ins are discrete. Headline constants are
  quoted at the stated evaluation point ($u{=}0.2$, $c{=}0.4$,
  $\bar I_a{=}0.8$, $\sigma{=}0.05$); all E-row quantities vary with it,
  while $P_{SS}, P_{IS}, P_{II}$ and the NIS-weight coherence moments do not.
- No estimation procedure exists yet for the physical parameters
  $(\mu, \gamma_I, \sigma, \ldots)$ from live check-in data; measuring them
  per class is the prerequisite for using these formulas on real agents.
- Everything here is label-free structure. It sharpens *how* to re-derive
  gates when a repair lands; it is not evidence that coherence predicts
  outcomes, and it moves no flags.

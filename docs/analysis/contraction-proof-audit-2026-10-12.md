# UNITARES v6 contraction proof audit — 2026-10-12

**Verdict:** the proof is incorrect as written. A corrected calculation certifies
rate **0.019 for the paper's fixed-parameter continuous vector field on its stated
box**, conditional on trajectories remaining there and sharing exogenous inputs.
It does not certify the deployed update. The actual default RK4 map has a
reproducible **4.60527× one-step expansion** in the stated metric at an interior
point. Several stochastic, network and equilibrium claims also need correction.

These conclusions concern mathematical claims, not whether EISV carries useful
information. They neither establish live runaway behavior nor measure predictive
value. No live outcomes, database records, protected ablation results or trial
statistics were read. No runtime, gates, stop rules or paper files were changed.
This audit is a prerequisite to a separate paper correction, not a release.

## Evidence and reproduction

- Paper: [v6 source at 8a888b30](https://github.com/cirwel/unitares-paper-v6/blob/8a888b30fcca783ba76a83334ca85ba287fc7c09/unitares-v6.tex).
  Line references below refer to that exact revision, including the earlier
  evidence-wording corrections in paper PR #5.
- Implementation: [UNITARES at 6d742273](https://github.com/cirwel/unitares/tree/6d742273434c6cc8a7cfa10ea4e29e969660e719).
  This is a source-tree audit; it does not assert that a particular live process
  has loaded this revision or visited the counterexample state.
- Three independent Codex subagent reviews covered continuous mathematics,
  implementation, and extensions; the primary agent reran the reproductions.
  This is agent review, not an external mathematician's certification.

Run from a checkout containing the pinned implementation commit, with NumPy
installed (SciPy's optional optimizer is not used):

```bash
python scripts/analysis/contraction_proof_audit/continuous.py
python scripts/analysis/contraction_proof_audit/discrete.py
python scripts/analysis/contraction_proof_audit/extensions.py
```

Each command prints JSON and checks its core conclusions with assertions; run
without Python's `-O` option. The continuous and extension scripts calculate
from the displayed equations. The discrete script exports exactly five modules
from the pinned Git object to a disposable directory, bypasses package startup,
pins the relevant environment switches and records source hashes, parameters,
forcing and outputs. It does not import the working checkout's runtime or start
services. `--repo PATH` selects another local clone containing that Git object.
The discrete result was reproduced with NumPy 2.4.2.

## 1. The continuous certificate can be repaired

Appendix B's Jacobian (lines 2723–2733) correctly differentiates the printed
linear-I ODE **when drift, complexity and Theta are exogenous and held common
between trajectories**. Write q = C'(V) and F = MJ + JᵀM. Its displayed symmetric
matrix (2837–2838) has two errors:

```text
F24 = mI betaI q - mV kappa       # omitted second term
F34 = -mS lambda2 q              # paper uses mV
```

The row-1 sufficient condition at 2862 also loses the factor two multiplying
alpha_c. Negative off-diagonal entries do not inherently help contraction
(2841, 2857): their quadratic-form contribution changes sign with the perturbation
direction. The Gershgorin bounds must use absolute values.

For the paper's actual numerical set,

```text
M = diag(0.1, 0.2, 1, 0.08)
alpha=.42, betaE=.1, betaI=.3, gammaI=.25, k=.1,
mu=.8, kappa=.3, delta=.4, lambda2=.05
E in [0,1], S in [.001,1], q in [0,.5]
```

the corrected worst-case Gershgorin row upper bounds for F + 2(.019)M are:

| Row | Exact bound | Decimal |
|---|---:|---:|
| E | −211/50000 | −0.00422 |
| I | −4/625 | −0.0064 |
| S | −1507/1000 | −1.507 |
| V | −149/25000 | −0.00596 |

All are strictly negative. The q interval conservatively enlarges the actual
sech-squared derivative range. The reproduction uses rational arithmetic for
these bounds, so the **0.019 certificate does not rely on a numerical grid**.
For example, the I row includes max_q |.06q − .024| = .024; the V row includes
max_q (|.06q − .024| + .05q) = .031. Both maxima occur at interval endpoints.

A secondary floating-point check of the normalized symmetric matrix
H = (1/2) M^(−1/2) F M^(−1/2) at all eight (E,S,q) vertices gives worst
lambda_max(H) = −0.129206391581. H is affine in those variables and lambda_max
is convex, so vertex maxima bound the box. This supports the roughly .13 figure
for this parameter set; the exact conservative .019 proof is the result to retain.
Replace the paper's ambiguous matrix division at 2775 with this normalization.
An equilibrium Jacobian's spectral abscissa is a different calculation.

### The general theorem is false as stated

The sufficient condition at 2755–2759 omits important couplings. Take M=I,
alpha=gammaI=mu=delta=1, kappa=.1, betaI=.1, qmax=.5, betaE=0, k=3,
lambda2=1. The stated inequality holds: 1 > max(.1,.05). Nevertheless, for
z=(0,1,−1,0), zᵀFz=2>0. Thus that condition does not imply contraction in the
claimed metric. This does not refute the corrected numerical certificate above.
Replace the general shortcut with explicit row inequalities or a verified
uniform matrix inequality that includes every coupling.

### Equilibrium and domain claims need their own repair

Line 2797 does not solve the I equation. The correct relation is
I* = (betaI C(V*) − k S*) / gammaI, as printed later at 2993.
Moreover, with zero drift, zero complexity and zero noise, the printed unprojected
ODE has S' = −mu S − lambda2 C(V) < 0 throughout S >= .001. There is no
equilibrium inside the stated box for this admissible forcing. A clamped or
projected model is a different mathematical object.

The certificate therefore establishes an incremental bound while solutions
remain in the region; it does not prove region invariance, existence of an
interior equilibrium or convergence to a fixed point for time-varying forcing.
Shared exogenous forcing can preserve contraction uniformly without producing a
fixed equilibrium. State-dependent inputs require their derivatives or an
augmented state. [Lohmiller and Slotine (1998)](https://web.mit.edu/nsl/www/preprints/contraction.pdf)
treat continuous and discrete contraction separately; those distinctions matter here.

## 2. The implemented finite-step map does not inherit that certificate

Use actual `compute_dynamics`, default active linear-I parameters and RK4,
zero drift and noise, complexity .5, no sensor, S setpoint zero:

```text
x = (.5, .999,     .3, 0)
y = (.5, .9990001, .3, 0)
M = diag(.1, .2, 1, .08)
dt = .1

||x-y||M                 = 4.47213595265e-8
||step(x)-step(y)||M     = 2.05953925326e-7
ratio                   = 4.605269775
proposed exp(-.019 * dt) = .998101804
```

Both initial states are interior. Instrumenting the pure clipping helper shows
**no intermediate or final clipping**, and the entropy floor does not activate.
The continuous local symmetric Jacobian is strictly negative at x (largest
metric-normalized eigenvalue about −.27879). Removing only the soft barrier in
an isolated counterfactual gives a distance ratio .983613. Near I=.999 the
barrier makes the Jacobian entry ∂I'/∂I about −115.4; strong continuous damping does not
automatically yield contraction under an explicit finite step.

At dt=1 the same pair expands 235.609×, with intermediate clipping. At dt=.1,
S setpoint .2 and delta values .15, .25, .4, .5 the example still expands about
4.60136×. Central differences over perturbations 1e−5 through 1e−8 also find a
step-map eigenvalue near −4.60407; the direct two-state distance is sufficient
for the counterexample and does not depend on that numerical derivative.

`config/governance_config.py:926–928` permits dt=.1 to 1;
`src/governance_monitor.py:1166,1343` selects and supplies the effective step.
The example is a witness against a universal one-step contraction claim over
the stated domain. It establishes neither reachability under normal operation,
frequency, asymptotic trajectory divergence nor instability of the full governor.

The boundary argument (paper 3047–3051) cannot bridge this gap: projection onto
a common coordinate box is nonexpansive in this diagonal metric, but the
numerical map preceding projection needs its own bound. Even x'=−x followed by
Euler dt=3 and clipping to [−1,1] doubles the distance between x=.1 and x=0.
Actual RK4 also clips intermediate stages, not merely the final output.

### Further implementation coverage gaps

| Surface at pinned implementation | Difference from the certificate |
|---|---|
| `parameters.py:get_active_params` | Default linear gammaI=.169, mu=.5; appendix uses .25 and .8. Default Theta yields lambda2=.06, not .05. |
| `dynamics.py:_derivatives` | Soft barriers and optional sensor springs are omitted by the paper's displayed Jacobian. Negative diagonal terms can help continuous contraction yet increase numerical stiffness. |
| `dynamics.py:compute_dynamics` | RK4/Euler, stage/final clipping, and complexity-dependent entropy floor define a finite-step map. |
| `src/monitor_drift.py:compute_drift_vector` | Drift uses current coherence and prior state velocity; treating it as exogenous misses feedback/history derivatives. |
| `src/governance_monitor.py:update_dynamics` | Delta is overridden by adaptive-governor state; S setpoint can change. |
| `adaptive_governor.py`, `src/monitor_lambda.py` | History, PID memory and changing parameters require common-metric switching bounds or an augmented-system analysis. |
| `stability.py:verify_lyapunov_stability` | Evaluates one state/equilibrium, not every state, controller history and permitted step. |

These are proof obligations, not assertions that every difference destabilizes
the system. A repaired frozen-system certificate is still useful on its stated scope.

## 3. The extensions need substantive correction

### Stochastic pair bound and decision claim (paper 1441–1495)

The theorem does not specify noise coupling and uses sigma²/(2 alpha_c) for a
pair of stochastic trajectories. For independent noises each bounded by sigma²,
the generator contributes 2 sigma² and the stationary mean-square bound is
sigma²/alpha_c. The half-sized bound applies to a noisy trajectory versus a
noise-free reference. Shared additive noise cancels in the difference.
[Pham, Tabareau and Slotine (2009), sections II-B/C and III-C](https://web.mit.edu/nsl/www/preprints/stochastic_contraction09.pdf)
explicitly distinguish these cases.

Exact scalar OU counterexample: alpha_c=.019, sigma=.02, independent noises,
identical zero starts. At t=100, E|x1−x2|²=.0205817, exceeding the paper's .0105263.
Even the latter number is a squared-distance bound: its RMS is .102598 (the
independent-pair asymptotic RMS is .145095). Comparing .011 directly with
amplitudes .1–.3 does not show an order-of-magnitude separation. Neither bound
establishes unaffected decisions without decision margins or crossing analysis.

The SDE also uses unreflected nonzero Brownian noise, which can leave the compact
certificate domain. It needs a specified boundary process or a suitable global
extension. Finally, the pinned monitor passes a deterministic calibration penalty
as `noise_S`, integrated through dt. That is not a Wiener increment. Sigma=.02
is an unestimated theoretical assumption, not production noise verification.

### Network theorem (paper 1522–1543)

Arbitrary H, an omitted coupling gain epsilon, and absent graph/metric
compatibility assumptions make the stated condition false. Two nodes with
f(x)=−x, H=−I, epsilon=1 and Laplacian eigenvalues {0,2} satisfy its inequality
1 > 2(−1), yet their difference obeys d'=d and grows exponentially.
A defensible replacement must include epsilon and the coupling's symmetric part
in the contraction metric. For an undirected nonnegative-weight graph and
nonnegative epsilon, MH+HᵀM positive semidefinite is a useful dissipative case.
Do not repair this by changing one eigenvalue symbol alone.

### Logistic and boundary claims (paper 2978–3058)

- The derivative −gamma(1−2I) is −gamma and +gamma at the boundaries, zero at
  I=.5. The paper reverses this behavior.
- A positive margin does not guarantee safe interior operation. With gamma=.25
  and constant input A=.03, margin .52 is positive but the upper root .860555 is
  unstable; from I=.95, I'=.018125 points toward the upper clamp.
- At zero margin, starts below .5 approach .5 rather than the upper boundary.
- A linear-I interior equilibrium requires 0 < A < gamma, not merely an upper
  bound on forcing.
- No preventive `m_sat < .1` governor-tightening path was located in the pinned
  sources. Retain such a historical claim only with implementation provenance.

## Resolution and decision boundaries

1. **Correct the paper's continuous proof.** Keep the .019 result under explicit
   printed parameters and domain/forcing assumptions; fix F, row arithmetic,
   equilibrium algebra and the false general shortcut. Verify invariance or state
   the conditional domain restriction. Do not label it a deployed certificate.
2. **Correct or withdraw the extension theorems as presently stated.** Specify
   stochastic coupling and boundary behavior; fix constants and units; remove
   unsupported verdict-safety claims; supply a valid network hypothesis set.
3. **Open a focused numerical fix separately.** This audit supplies a deterministic
   regression witness. Compare step subdivision or a suitable integrator under
   bounded forcing and controller assumptions. Validate the complete map before
   claiming deployment contraction; reducing dt at one point is insufficient.
4. **Keep empirical value decisions separate.** These mathematical findings do
   not reopen preregistered outcome gates or erase narrower measured information.
   Stability, predictive usefulness, and decision authority are separate questions.
5. **Keep publication drafts pending these corrections.** The earlier wording
   cleanup did not validate the proof. No new theorem, paper release or runtime
   change is implied by merging this audit record.

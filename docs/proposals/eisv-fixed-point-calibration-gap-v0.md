# EISV fixed-point calibration gap & the signal-readout split (v0)

**Status:** finding / proposal (not yet a change)
**Date:** 2026-06-24
**Scope:** `governance_core/dynamics.py`, `governance_core/coherence.py`,
`src/grounding/coherence.py`, `config/governance_config.py` (healthy operating
points + scale constants)

## TL;DR

Two coupled problems make EISV-derived signals encode *the model's idea of
health plus a class-dependent offset* rather than *this agent's drift from its
own baseline*:

1. **The legacy coherence signal is structurally pinned (~0.49) — a "cast."**
   It is computed off the void integral `V`, which the dynamics damp to ≈0, so
   `C = 0.5·(1+tanh(C₁·V)) ≈ 0.49`. Lowering the damping barely helps because
   `V` is pinned by the *architecture* (the E→I coupling drives `E−I → 0`), not
   by the damping value. The fix is not to remove the cast (destabilizing) but
   to **read a different signal**: the manifold readout, which is out of the
   feedback loop, has ~18× the dynamic range on the *same stable run*.

2. **The ODE fixed point does not sit where healthy agents actually live.** The
   equilibrium is `S* ≈ 0.09`, but the measured healthy operating point across
   all classes is `S ≈ 0.17–0.31`. The disagreement is almost entirely on the
   **S (entropy) axis**. Because the single fixed point lands at a different
   distance from each class's empirical center, manifold-coherence-at-rest
   varies from **0.018 (Vigil) to 0.556 (Watcher)** — a healthy agent's
   coherence is dominated by a class-dependent offset, not by its drift.

These are plausibly *the* blocker for EISV justifying itself: if the attractor
is in the wrong place, every downstream signal inherits the error.

## Evidence

All numbers from the real `governance_core` integrator (`get_active_params()`,
`DEFAULT_THETA`). Harness faithfulness verified: integrating from
`compute_equilibrium` with zero forcing leaves the state unmoved
(`drift = 0.00000`). Repro scripts: `scripts/analysis/eisv_cast_experiment.py`
and `scripts/analysis/eisv_equilibrium_gap.py`.

### Finding 1 — the cast, and that damping can't lift it

Stress perturbation (drift + complexity for a 20s window, then recovery):

| regime | signal | dynamic range |
|---|---|---|
| δ=0.4 (current) | legacy (V-driven, in-loop) | **0.021** |
| δ=0.25 (lighter) | legacy (V-driven, in-loop) | 0.028 |
| δ=0.4 (current!) | **manifold (readout, out-of-loop)** | **0.379** |

Halving the damping moves the legacy range from 0.021 → 0.028 (a signal that
needed ~10×). On the *identical δ=0.4 stable run*, the manifold readout swings
0.379 — ~18×, with zero change to stability. The conflict between provable
stability and discernment is an artifact of reading governance off the
contractive accumulator `V`; it dissolves when the sensor is taken out of the
control loop.

Mechanism: at equilibrium `V* = (κ/δ)(E−I)`, and the E-dynamics actively drive
`E → I` (α=0.42), so `E−I → 0 ⇒ V → 0 ⇒ C → 0.5·Cmax` regardless of δ.
Coherence also *feeds back* into the state (`∂İ/∂V = β_I·dC/dV`,
`∂Ṡ/∂V = −λ₂·dC/dV`), which is why naively lowering δ rings (the project's own
history comment: `delta reverted from 0.25 — caused coherence spiral`). A pure
readout has no such feedback rows and cannot ring.

### Finding 2 — the attractor is miscalibrated on the S axis

```
ODE equilibrium : E=0.805  I=0.822  S=0.091   V=-0.013
measured healthy: E≈0.73   I≈0.79   S≈0.24   (median over healthy slice, per class)
```

| class | healthy (E,I,S) | ‖Δ‖ to eq | manifold@eq |
|---|---|---|---|
| Lumen | (0.745, 0.800, 0.168) | 0.100 | 0.159 |
| default | (0.726, 0.793, 0.236) | 0.168 | 0.168 |
| Sentinel | (0.751, 0.798, 0.193) | 0.119 | 0.303 |
| Vigil | (0.737, 0.790, 0.240) | 0.167 | 0.018 |
| Watcher | (0.748, 0.769, 0.248) | 0.175 | 0.556 |
| engaged_ephemeral | (0.756, 0.685, 0.307) | 0.260 | 0.387 |

`E*` and `I*` are close to data (`I*` was explicitly tuned: `γ_I → 0.169 for
I*≈0.80`). `S*` was never calibrated to the measured value — it is whatever the
dynamics produce: at zero drift,
`S* = (β_c·complexity − λ₂·C)/μ = (0.15·0.5 − 0.06·0.5)/0.5 ≈ 0.09`. The
measured healthy `S ≈ 0.24` would require retuning `μ`, `β_complexity`, or
adding a baseline term.

## Why this blocks the EISV approach

The manifold readout (`src/grounding/coherence.py`) is correctly calibrated to
*measured* health — but it measures the distance of `(E,I,S)` from that point,
and `(E,I,S)` is contractive ODE state being dragged toward `S*≈0.09`. So even
the good readout's *inputs* are pulled away from real health, and the residual
offset differs by class. The "coherence stuck near 0.49", the class
homogenization, and the weak discernment are all downstream of the attractor
being in the wrong place on the S axis.

## Recommendations (ordered, cheapest first)

1. **Read the readout, not the loop.** Treat the manifold form as the canonical
   coherence and demote `coherence_legacy` to telemetry only. (~90% done since
   PR #26; finish it.) Guarded by
   `tests/test_grounding_coherence_dynamic_range.py`.
2. **Recalibrate `S*` to measured healthy entropy** (~0.24, per class). Smallest
   honest lever; turns the manifold's inputs back toward real health. Must be
   red-teamed (see below) — `S*` interacts with the `check_basin` threshold
   (0.5), verdict bands, and risk scoring.
3. **Decouple coherence from the state dynamics** if the legacy form is kept at
   all (remove the `C → I` and `C → S` feedback). Then δ can fall for dynamic
   range with no spiral.
4. **Longer term — invert the substrate.** Wire the grounding tiers (logprob
   entropy, FEP free energy) so `E/I/S` are *measured*, and demote the ODE to a
   predictive model compared against measurement. The signal becomes the
   residual (measurement − prediction), informative by construction. Separates
   the two stabilities: a high-dynamic-range *estimate* with a calm decision
   *policy* (hysteresis lives in the verdict layer, not the sensor).

## Process note: red-team, don't council

This is a ground-truth calibration question (math + measured data), which has a
determinate answer. A diverse-opinion design council would dilute it. The
high-value multi-agent use here is narrow and adversarial:

- **Refute the finding:** try to show `S*≈0.09` is correct and the measured 0.24
  is the artifact (e.g., a contaminated healthy slice). If it can't be refuted,
  it hardens.
- **Red-team the recalibration before it ships:** moving `S*` 0.09 → 0.24 —
  what crosses the basin threshold, shifts a verdict band, or moves risk? That
  is verification with a determinate answer and is worth independent eyes.

## Addendum (v0.1) — Stage sequencing correction

The Stage-1 red-team (`scripts/analysis/eisv_critical_branch_audit.py`) found
the recommendation ordering above is **wrong**: recommendation 2 (fix the
attractor) is a *prerequisite* for recommendation 1 (make the manifold the
control signal), not the reverse.

Two measured facts, driving the real ODE through healthy / degrading / severe
runs (synthetic states, not the production corpus):

1. **The legacy coherence-critical branch is dead.** `state.coherence` never
   drops below **0.493** in any scenario — including maximum drift + complexity
   — so `state.coherence < COHERENCE_CRITICAL_THRESHOLD` (=0.40) fires **0 of
   1200 steps every time**. Today "critical" status can only come from
   `void_active` or `risk ≥ 0.60`; the coherence path is vestigial. This branch
   feeds not just `status` (`governance_monitor.py:1338`) but `is_critical`
   (`monitor_decision.py:195`) and CIRS (`monitor_cirs.py`).

2. **Swapping that branch to the manifold form, *today*, flags every healthy
   agent critical.** In the healthy run the manifold reads ≈0.168 (< 0.40) for
   **1200/1200** steps, because the agent rests at the ODE attractor (S≈0.09)
   while the manifold measures distance from *measured* health (S≈0.24). The
   attractor offset makes the manifold **unthresholdable** as a control signal.

**Corrected sequencing:**

- **Stage A — fix the attractor first** (former rec 2): add a per-class `S`
  setpoint so the ODE rest state matches measured healthy and manifold-at-rest
  → ~1.0. Prerequisite for everything else. Red-team: verdict / basin / risk on
  healthy agents.
- **Stage B — then move the control signals** (former rec 1): once
  manifold-at-rest is ~1.0, repoint `status` / `is_critical` / CIRS from legacy
  `C(V)` to the manifold form and re-tune the threshold to the manifold scale.
  Safe only after Stage A.
- **Stage C — invert the substrate** (former rec 4): measure `E/I/S`, demote the
  ODE to a predictor, signal = residual.

The manifold form remains valid for *relative* display today; only its *absolute
threshold* is blocked on Stage A.

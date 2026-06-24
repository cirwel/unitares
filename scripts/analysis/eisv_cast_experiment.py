"""Discernment-vs-stability experiment ("removing the cast").

Drives the REAL governance_core ODE with a stress perturbation (an agent under
elevated drift + complexity, then recovery) and compares three regimes:

  A. delta=0.4 (current), read LEGACY coherence  -> the cast: stable, no range
  B. delta=0.25 (lighter), read LEGACY coherence  -> range appears, but it RINGS
  C. delta=0.4 (current!), read MANIFOLD coherence -> range WITHOUT ringing

The point: you don't have to trade stability for discernment IF you stop reading
the signal off the in-loop feedback variable (V) and read it off a pure readout
(manifold distance of E/I/S from the healthy point) instead.
"""
import dataclasses
import math

from governance_core.dynamics import State, _derivatives, compute_equilibrium
from governance_core.coherence import coherence as legacy_coherence
from governance_core.parameters import get_active_params, DEFAULT_THETA
from config.governance_config import get_healthy_operating_point, get_delta_norm_max

THETA = DEFAULT_THETA
HP = get_healthy_operating_point("default")
DMAX = get_delta_norm_max("default").value


def manifold_coherence(s: State) -> float:
    dx, dy, dz = s.E - HP[0], s.I - HP[1], s.S - HP[2]
    ratio = math.sqrt(dx * dx + dy * dy + dz * dz) / DMAX
    return 1.0 - max(0.0, min(1.0, ratio))


def run(delta, dt=0.05, steps=1200):
    params = dataclasses.replace(get_active_params(), delta=delta)
    s = compute_equilibrium(params, THETA, complexity=0.5)
    legacy, manifold, Vs = [], [], []
    for n in range(steps):
        t = n * dt
        # Stress window: sustained drift + complexity from t=10..30, then recover.
        if 10 <= t < 30:
            d_eta_sq, complexity = 0.5, 0.9
        else:
            d_eta_sq, complexity = 0.0, 0.5
        dE, dI, dS, dV = _derivatives(s, d_eta_sq, THETA, params, 0.0, complexity, None)
        s = State(
            E=min(1.0, max(0.0, s.E + dt * dE)),
            I=min(1.0, max(0.0, s.I + dt * dI)),
            S=min(1.0, max(0.001, s.S + dt * dS)),
            V=min(1.0, max(-1.0, s.V + dt * dV)),
        )
        legacy.append(legacy_coherence(s.V, THETA, params))
        manifold.append(manifold_coherence(s))
        Vs.append(s.V)
    return legacy, manifold, Vs


def ringing(series):
    """Count direction reversals after the perturbation ends (overshoot/oscillation)."""
    tail = series[int(len(series) * 30 / 60):]  # after t=30 (recovery phase)
    reversals = 0
    for i in range(2, len(tail)):
        d1, d2 = tail[i - 1] - tail[i - 2], tail[i] - tail[i - 1]
        if d1 * d2 < 0:
            reversals += 1
    return reversals


def rng(series):
    return max(series) - min(series)


print(f"healthy point(default)={HP}  delta_norm_max={DMAX}")
print(f"{'regime':<42}{'range':>10}{'reversals':>12}")
for delta in (0.4, 0.25):
    leg, man, V = run(delta)
    print(f"delta={delta}  LEGACY (V-driven, in-loop)         {rng(leg):>10.3f}{ringing(leg):>12}")
# Regime C: the STABLE delta=0.4 run, but read the manifold readout instead.
leg, man, V = run(0.4)
print(f"delta=0.4  MANIFOLD (readout, out-of-loop)    {rng(man):>10.3f}{ringing(man):>12}")
print()
# Show the actual coherence excursion (min during stress -> baseline) per signal.
leg04, man04, _ = run(0.4)
print(f"legacy   @delta0.4: baseline≈{leg04[0]:.3f}  worst≈{min(leg04):.3f}  swing={rng(leg04):.3f}")
print(f"manifold @delta0.4: baseline≈{man04[0]:.3f}  worst≈{min(man04):.3f}  swing={rng(man04):.3f}")

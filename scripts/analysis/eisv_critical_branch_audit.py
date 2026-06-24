"""Stage-1 red-team: is the coherence-critical branch reachable, and what would
switching it to the manifold readout change?

`status`/`is_critical`/CIRS all gate on `state.coherence < COHERENCE_CRITICAL_THRESHOLD`
(=0.40). `state.coherence` is the legacy V-driven form, which the contraction
pins near 0.49. This harness drives the REAL ODE through a healthy run and a
degrading run and reports, for each, whether the legacy-critical branch ever
fires vs whether the manifold readout (calibrated to per-class measured health)
would — i.e. how much real criticality the current branch is blind to.

Synthetic, not the production corpus (no DB access here): states come from the
real integrator under representative forcing, not real check-ins. Treat as a
reachability/divergence probe, not a production audit.
"""
import math
from governance_core.dynamics import State, _derivatives, compute_equilibrium
from governance_core.coherence import coherence as legacy_coherence
from governance_core.parameters import get_active_params, DEFAULT_THETA
from config.governance_config import (
    get_delta_norm_max, HEALTHY_OPERATING_POINT_BY_CLASS, GovernanceConfig as GC,
)

P, TH = get_active_params(), DEFAULT_THETA
CRIT = GC.COHERENCE_CRITICAL_THRESHOLD  # 0.40


def manifold(E, I, S, cls):
    hp = HEALTHY_OPERATING_POINT_BY_CLASS.get(cls, HEALTHY_OPERATING_POINT_BY_CLASS["default"])
    n = math.sqrt((E-hp[0])**2 + (I-hp[1])**2 + (S-hp[2])**2)
    return 1.0 - max(0.0, min(1.0, n / get_delta_norm_max(cls).value))


def run(label, cls, drift_fn, complexity_fn, dt=0.05, steps=1200):
    s = compute_equilibrium(P, TH, complexity=0.5)
    leg_crit = man_crit = 0
    leg_min, man_min = 1.0, 1.0
    for n in range(steps):
        t = n * dt
        dE, dI, dS, dV = _derivatives(s, drift_fn(t), TH, P, 0.0, complexity_fn(t), None)
        s = State(E=min(1, max(0, s.E+dt*dE)), I=min(1, max(0, s.I+dt*dI)),
                  S=min(1, max(0.001, s.S+dt*dS)), V=min(1, max(-1, s.V+dt*dV)))
        leg = legacy_coherence(s.V, TH, P)
        man = manifold(s.E, s.I, s.S, cls)
        leg_min, man_min = min(leg_min, leg), min(man_min, man)
        leg_crit += leg < CRIT
        man_crit += man < CRIT
    print(f"{label:<34}{cls:<10}leg_min={leg_min:.3f} man_min={man_min:.3f}  "
          f"crit-fires: legacy={leg_crit:>4}  manifold={man_crit:>4}")


print(f"COHERENCE_CRITICAL_THRESHOLD={CRIT}\n")
print(f"{'scenario':<34}{'class':<10}{'signal minima / critical-branch fires (of 1200 steps)'}")
# Healthy: low drift, normal complexity. Should NOT be critical under either.
run("healthy (low drift)", "default", lambda t: 0.0, lambda t: 0.5)
# Sustained degradation: rising drift + high complexity (an agent going off rails).
run("degrading (rising drift)", "default", lambda t: min(0.9, t/40), lambda t: 0.9)
# Severe: max drift + complexity throughout.
run("severe (max drift+cx)", "default", lambda t: 1.0, lambda t: 1.0)
# Same severe case for a wide-envelope class (Watcher) and a tight one (Lumen).
run("severe", "Watcher", lambda t: 1.0, lambda t: 1.0)
run("severe", "Lumen", lambda t: 1.0, lambda t: 1.0)

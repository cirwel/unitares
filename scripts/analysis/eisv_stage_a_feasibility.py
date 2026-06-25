"""Stage-A feasibility (synthetic): does a per-class S setpoint make the manifold
readout thresholdable (healthy agent no longer reads < 0.40 'critical')?

dS = -mu*(S - sigma) + drivers  =>  S* = sigma + drivers/mu.
We pick sigma per class so S* lands on the measured healthy S, then integrate
and report manifold-at-rest. Also reports the residual from E*/I* (which are NOT
moved here) so we see how much of the gap S alone closes.
"""
import math
from governance_core.dynamics import State, _derivatives, compute_equilibrium
from governance_core.parameters import get_active_params, DEFAULT_THETA
from config.governance_config import (
    get_delta_norm_max, HEALTHY_OPERATING_POINT_BY_CLASS, GovernanceConfig as GC,
)

P, TH = get_active_params(), DEFAULT_THETA
CRIT = GC.COHERENCE_CRITICAL_THRESHOLD


def manifold(E, I, S, hp, dmax):
    n = math.sqrt((E-hp[0])**2 + (I-hp[1])**2 + (S-hp[2])**2)
    return 1.0 - max(0.0, min(1.0, n / dmax))


def rest_with_setpoint(sigma, steps=4000, dt=0.05):
    """Integrate to rest with dS modified to decay toward sigma."""
    s = compute_equilibrium(P, TH, complexity=0.5)
    for _ in range(steps):
        dE, dI, dS, dV = _derivatives(s, 0.0, TH, P, 0.0, 0.5, None)
        dS += P.mu * sigma           # -mu*S  ->  -mu*(S - sigma)
        s = State(E=min(1, max(0, s.E+dt*dE)), I=min(1, max(0, s.I+dt*dI)),
                  S=min(1, max(0.001, s.S+dt*dS)), V=min(1, max(-1, s.V+dt*dV)))
    return s


eq = compute_equilibrium(P, TH, complexity=0.5)
print(f"current equilibrium: E={eq.E:.3f} I={eq.I:.3f} S={eq.S:.3f}\n")
print(f"{'class':<12}{'healthyS':>9}{'sigma':>8}{'S_rest':>8}{'manif@rest':>12}"
      f"{'(today)':>9}{'clears .40?':>12}")
for cls, hp in HEALTHY_OPERATING_POINT_BY_CLASS.items():
    dmax = get_delta_norm_max(cls).value
    today = manifold(eq.E, eq.I, eq.S, hp, dmax)
    sigma = hp[2] - eq.S                      # shift S-rest onto measured healthy S
    r = rest_with_setpoint(sigma)
    m = manifold(r.E, r.I, r.S, hp, dmax)
    print(f"{cls:<12}{hp[2]:>9.3f}{sigma:>8.3f}{r.S:>8.3f}{m:>12.3f}{today:>9.3f}"
          f"{('yes' if m>=CRIT else 'NO'):>12}")

# What full E/I/S alignment would give (trivially manifold=1.0) — the ceiling.
print("\nResidual after S-only fix is dominated by E* (0.805 vs healthy ~0.73).")
print("Full attractor alignment (E,I,S) -> manifold-at-rest = 1.0 by construction.")

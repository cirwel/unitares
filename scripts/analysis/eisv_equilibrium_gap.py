"""Where does the ODE's fixed point sit vs. where healthy agents actually live?

1. Faithfulness check: start at compute_equilibrium, integrate with ZERO forcing.
   A true equilibrium must stay put. If it drifts, my harness is wrong.
2. Gap: compare the ODE equilibrium (E*,I*,S*) to the measured healthy
   operating point per class, component by component.
"""
import math
from governance_core.dynamics import State, _derivatives, compute_equilibrium
from governance_core.parameters import get_active_params, DEFAULT_THETA
from config.governance_config import (
    get_healthy_operating_point, get_delta_norm_max, HEALTHY_OPERATING_POINT_BY_CLASS,
)

P = get_active_params()
TH = DEFAULT_THETA


def integrate(s, steps, dt, d_eta_sq, complexity):
    for _ in range(steps):
        dE, dI, dS, dV = _derivatives(s, d_eta_sq, TH, P, 0.0, complexity, None)
        s = State(
            E=min(1.0, max(0.0, s.E + dt * dE)),
            I=min(1.0, max(0.0, s.I + dt * dI)),
            S=min(1.0, max(0.001, s.S + dt * dS)),
            V=min(1.0, max(-1.0, s.V + dt * dV)),
        )
    return s


eq = compute_equilibrium(P, TH, complexity=0.5)
print(f"compute_equilibrium  : E={eq.E:.4f} I={eq.I:.4f} S={eq.S:.4f} V={eq.V:.4f}")

# Faithfulness: zero-forcing from equilibrium should not move (complexity=0.5 matches eq)
settled = integrate(eq, 2000, 0.05, 0.0, 0.5)
drift = math.sqrt((settled.E-eq.E)**2 + (settled.I-eq.I)**2 + (settled.S-eq.S)**2 + (settled.V-eq.V)**2)
print(f"after 100s zero-force : E={settled.E:.4f} I={settled.I:.4f} S={settled.S:.4f} V={settled.V:.4f}  (drift={drift:.5f})")

print(f"\nper-class measured healthy point vs ODE equilibrium (E,I,S):")
print(f"{'class':<20}{'healthy (E,I,S)':<28}{'eq (E,I,S)':<24}{'||Δ||':>8}{'manifold@eq':>13}")
for cls, hp in HEALTHY_OPERATING_POINT_BY_CLASS.items():
    dmax = get_delta_norm_max(cls).value
    dx, dy, dz = eq.E - hp[0], eq.I - hp[1], eq.S - hp[2]
    norm = math.sqrt(dx*dx + dy*dy + dz*dz)
    man = 1.0 - max(0.0, min(1.0, norm / dmax))
    print(f"{cls:<20}({hp[0]:.3f},{hp[1]:.3f},{hp[2]:.3f})        "
          f"({eq.E:.3f},{eq.I:.3f},{eq.S:.3f})   {norm:>7.3f}{man:>13.3f}")

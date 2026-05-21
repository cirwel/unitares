"""v6 ODE discretized transition per spec §2.3.

State: s = (E, I, S, V) in [0,1]^3 x [-1,1].
Parameters (frozen in ode_params.json):
    alpha, beta_E, k, beta_I, gamma_I_linear, mu, lambda_2, kappa, delta.

The gamma_E ||delta-eta||^2 drift term is dropped (spec §2.3); so is lambda_1 in dS.
Coherence C(V) enters as a scalar function of V; spec does not fix its form.
The v6 paper defines C(V, Theta) as a basin-coherence; for prior use we take
the simplest reading C(V) = 1 - |V|, which is in [0,1] and drives integrity
upward when V is balanced. This is pre-registered here as the v7 prior choice.
"""
from __future__ import annotations

import numpy as np


def coherence(V: np.ndarray | float) -> np.ndarray | float:
    """C(V) = 1 - |V|; in [0,1]. Pre-registered simplification of v6 C(V, Theta)."""
    return 1.0 - np.abs(V)


def ode_step(s: np.ndarray, dt: float, p: dict) -> np.ndarray:
    """Single forward Euler step of the v6 ODE.

    s shape: (..., 4) with columns (E, I, S, V).
    Returns s_{t+1} same shape, pre-reflection.
    """
    s = np.asarray(s, dtype=np.float64)
    E = s[..., 0]
    I = s[..., 1]
    S = s[..., 2]
    V = s[..., 3]
    C = coherence(V)

    dE = p["alpha"] * (I - E) - p["beta_E"] * E * S
    dI = -p["k"] * S + p["beta_I"] * C - p["gamma_I_linear"] * I
    dS = -p["mu"] * S - p["lambda_2"] * C
    dV = p["kappa"] * (E - I) - p["delta"] * V

    out = np.stack(
        [E + dE * dt, I + dI * dt, S + dS * dt, V + dV * dt],
        axis=-1,
    )
    return out


def reflect(s: np.ndarray) -> np.ndarray:
    """Moment-matching reflection at boundaries (approximate — clamp + reflect).

    E, I, S in [0, 1]; V in [-1, 1].
    Values outside are reflected back in: x' = 2*bound - x (one-bounce).
    Values still outside after a single bounce are clamped (rare near bounds).
    """
    s = np.asarray(s, dtype=np.float64)
    out = s.copy()
    # E, I, S in [0,1]
    for i in (0, 1, 2):
        x = out[..., i]
        x = np.where(x < 0.0, -x, x)
        x = np.where(x > 1.0, 2.0 - x, x)
        x = np.clip(x, 0.0, 1.0)
        out[..., i] = x
    # V in [-1,1]
    v = out[..., 3]
    v = np.where(v < -1.0, -2.0 - v, v)
    v = np.where(v > 1.0, 2.0 - v, v)
    v = np.clip(v, -1.0, 1.0)
    out[..., 3] = v
    return out


def fx(x: np.ndarray, dt: float, p: dict) -> np.ndarray:
    """UKF transition function: ode_step + reflect."""
    return reflect(ode_step(x, dt, p))

"""Deterministic repository-equation audit; no deployment data or statistical reads."""

from fractions import Fraction as Q
from itertools import product
import json
import numpy as np

m = np.array([0.1, 0.2, 1.0, 0.08])
M = np.diag(m)
R = np.diag(1 / np.sqrt(m))


def jac(E, S, q, gamma=0.25, mu=0.8):
    return np.array(
        [
            [-0.42 - 0.1 * S, 0.42, -0.1 * E, 0],
            [0, -gamma, -0.1, 0.3 * q],
            [0, 0, -mu, -0.05 * q],
            [0.3, -0.3, 0, -0.4],
        ]
    )


# Every entry is affine in independent E,S,q. Convexity of lambda_max
# bounds the whole box by vertex maxima. q=[0,.5] relaxes actual sech domain.
vertices = []
for E, S, q in product([0.0, 1.0], [0.001, 1.0], [0.0, 0.5]):
    J = jac(E, S, q)
    F = M @ J + J.T @ M
    H = R @ F @ R / 2
    vertices.append(
        dict(E=E, S=S, q=q, max_eigenvalue=float(np.linalg.eigvalsh(H)[-1]))
    )
# Exact unnormalized symmetric Gershgorin F+2aM upper bounds.
a = Q(19, 1000)
me, mi, ms, mv = Q(1, 10), Q(1, 5), Q(1), Q(2, 25)
alpha, betaE, gamma, k, mu, kappa, delta, lam, qmax = map(
    Q, [".42", ".1", ".25", ".1", ".8", ".3", ".4", ".05", ".5"]
)
f24 = lambda q: mi * Q(".3") * q - mv * kappa
max24 = max(abs(f24(q)) for q in (Q(0), qmax))
maxrow4 = max(abs(f24(q)) + ms * lam * q for q in (Q(0), qmax))
rows = [
    -2 * me * (alpha + betaE * Q(".001"))
    + 2 * a * me
    + me * alpha
    + me * betaE
    + mv * kappa,
    -2 * mi * gamma + 2 * a * mi + me * alpha + mi * k + max24,
    -2 * ms * mu + 2 * a * ms + me * betaE + mi * k + ms * lam * qmax,
    -2 * mv * delta + 2 * a * mv + mv * kappa + maxrow4,
]
# Explicit sufficient-condition counterexample for the claimed chosen metric.
Jbad = np.array(
    [[-1, 1, 0, 0], [0, -1, -3, 0.05], [0, 0, -1, -0.5], [0.1, -0.1, 0, -1.0]]
)
d = np.array([0.0, 1.0, -1.0, 0.0])
assert all(v < 0 for v in rows)
assert min(1, 1, 1, 1) > max(0.1, 0.1 * 0.5)
assert float(d @ (Jbad + Jbad.T) @ d) > 0
print(
    json.dumps(
        {
            "source_head": "8a888b30fcca783ba76a83334ca85ba287fc7c09",
            "scope": "paper fixed-parameter continuous ODE Jacobian; not deployment, changing feedback, noise, or integrator",
            "corrected_row_upper_bounds_exact": [str(v) for v in rows],
            "corrected_row_upper_bounds": [float(v) for v in rows],
            "worst_vertex": max(vertices, key=lambda v: v["max_eigenvalue"]),
            "all_vertices": vertices,
            "chosen_metric_sufficient_condition_counterexample": {
                "M": "identity",
                "alpha_gamma_mu_delta": 1,
                "beta_E": 0,
                "k": 3,
                "kappa": 0.1,
                "beta_I": 0.1,
                "lambda_2": 1,
                "qmax": 0.5,
                "asserted_condition": "1 > max(0.1,0.05)",
                "direction": d.tolist(),
                "d_F_d": float(d @ (Jbad + Jbad.T) @ d),
            },
        },
        indent=2,
    )
)

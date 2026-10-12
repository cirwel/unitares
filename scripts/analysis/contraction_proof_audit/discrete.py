"""Offline finite-step contraction audit. No server, database, or outcome reads.

Usage: python scripts/analysis/contraction_proof_audit/discrete.py [--repo PATH]
Uses only five pure modules exported by git show from the pinned revision.
The package initializer is deliberately bypassed, avoiding unrelated imports.
"""

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import tempfile
from dataclasses import asdict, replace

import numpy as np

SHA = "6d742273434c6cc8a7cfa10ea4e29e969660e719"
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[3])
REPO = parser.parse_args().repo
# Disposable source export, not a git worktree. No checkout or runtime mutations.
export = tempfile.TemporaryDirectory(prefix="contraction-audit-")
SOURCE = Path(export.name) / "governance_core"
SOURCE.mkdir(parents=True, exist_ok=True)
sys.dont_write_bytecode = True
hashes = {}
for name in ("parameters", "utils", "coherence", "dynamics", "stability"):
    relative = f"governance_core/{name}.py"
    content = subprocess.check_output(
        ["git", "-C", str(REPO), "show", f"{SHA}:{relative}"]
    )
    (SOURCE / f"{name}.py").write_bytes(content)
    hashes[relative] = hashlib.sha256(content).hexdigest()
package = types.ModuleType("governance_core")
package.__path__ = [str(SOURCE)]
sys.modules["governance_core"] = package

# Pin every environment switch consumed by these functions. Never print env.
settings = {
    "UNITARES_I_DYNAMICS": "linear",
    "UNITARES_INTEGRATOR": "rk4",
    "UNITARES_SENSOR_COUPLING": "off",
    "UNITARES_PARAMS_PROFILE": "default",
    "UNITARES_PARAMS_JSON": "{}",
}
os.environ.update(settings)
d = importlib.import_module("governance_core.dynamics")
pmod = importlib.import_module("governance_core.parameters")
stability = importlib.import_module("governance_core.stability")
params = pmod.get_active_params()
theta = pmod.DEFAULT_THETA
assert params.gamma_I == 0.169 and params.mu == 0.5
assert params.barrier_strength == 2.0 and params.barrier_margin == 0.05
metric = np.diag([0.1, 0.2, 1.0, 0.08])
root = np.diag(np.sqrt(np.diag(metric)))
inverse_root = np.diag(1 / np.sqrt(np.diag(metric)))


def vec(state):
    return np.array([state.E, state.I, state.S, state.V])


def distance(a, b):
    v = a - b
    return float(np.sqrt(v @ metric @ v))


def run_case(mode, dt, x, perturbation, parameters=params, setpoint=0.0):
    os.environ["UNITARES_INTEGRATOR"] = mode
    y = x + perturbation
    clipping = []
    original_clip = d.clip

    def recording_clip(value, lo, hi):
        result = original_clip(value, lo, hi)
        if result != value:
            clipping.append({"input": value, "lo": lo, "hi": hi, "output": result})
        return result

    d.clip = recording_clip
    try:
        outputs = [
            vec(
                d.compute_dynamics(
                    d.State(*v),
                    [],
                    theta,
                    parameters,
                    dt=dt,
                    noise_S=0.0,
                    complexity=0.5,
                    sensor_eisv=None,
                    s_setpoint=setpoint,
                )
            )
            for v in (x, y)
        ]
    finally:
        d.clip = original_clip
    before = distance(x, y)
    after = distance(*outputs)
    J = stability.compute_jacobian(d.State(*x), parameters, theta)
    transformed = root @ J @ inverse_root
    return {
        "integrator": mode,
        "dt": dt,
        "params_delta": parameters.delta,
        "barrier_strength": parameters.barrier_strength,
        "s_setpoint": setpoint,
        "state_x": x.tolist(),
        "state_y": y.tolist(),
        "output_x": outputs[0].tolist(),
        "output_y": outputs[1].tolist(),
        "initial_weighted_distance": before,
        "final_weighted_distance": after,
        "distance_ratio": after / before,
        "claimed_exp_minus_alpha_dt": float(np.exp(-0.019 * dt)),
        "continuous_local_metric_symmetric_eigenvalues": np.linalg.eigvalsh(
            0.5 * (transformed + transformed.T)
        ).tolist(),
        "clip_activations_both_steps": clipping,
        "entropy_floor_value": 0.001 + 0.049 * 0.5,
        "entropy_floor_active_at_output": [
            bool(v[2] == 0.001 + 0.049 * 0.5) for v in outputs
        ],
    }


x = np.array([0.5, 0.999, 0.3, 0.0])
epsilon = np.array([0.0, 1e-7, 0.0, 0.0])
cases = [run_case("rk4", dt, x, epsilon) for dt in (0.1, 1.0)]
cases.append(run_case("rk4", 0.1, x, epsilon, replace(params, barrier_strength=0.0)))
cases.extend(
    run_case("rk4", 0.1, x, epsilon, replace(params, delta=delta), setpoint=0.2)
    for delta in (0.15, 0.25, 0.4, 0.5)
)
cases.append(
    run_case(
        "euler", 0.1, np.array([0.5, 0.5, 0.001, 0.0]), np.array([0.0, 0.0, 1e-7, 0.0])
    )
)
assert cases[0]["distance_ratio"] > 4.6
assert not cases[0]["clip_activations_both_steps"]
assert max(cases[0]["continuous_local_metric_symmetric_eigenvalues"]) < 0
assert cases[2]["distance_ratio"] < 1
os.environ["UNITARES_INTEGRATOR"] = "rk4"


def base_step(v):
    return vec(
        d.compute_dynamics(
            d.State(*v),
            [],
            theta,
            params,
            dt=0.1,
            noise_S=0.0,
            complexity=0.5,
            sensor_eisv=None,
            s_setpoint=0.0,
        )
    )


step_jacobians = []
for h in (1e-5, 1e-6, 1e-7, 1e-8):
    derivative = np.column_stack(
        [
            (base_step(x + np.eye(4)[i] * h) - base_step(x - np.eye(4)[i] * h))
            / (2 * h)
            for i in range(4)
        ]
    )
    eigenvalues = np.linalg.eigvals(derivative)
    step_jacobians.append(
        {
            "finite_difference_h": h,
            "eigenvalues_real_imag": [
                [float(v.real), float(v.imag)] for v in eigenvalues
            ],
            "spectral_radius": float(max(abs(eigenvalues))),
        }
    )
result = {
    "schema": "contraction.discrete_counterexample.v1",
    "source_sha": SHA,
    "source_sha256": hashes,
    "numpy_version": np.__version__,
    "pinned_environment": settings,
    "resolved_parameters": asdict(params),
    "theta": asdict(theta),
    "metric_diagonal": np.diag(metric).tolist(),
    "forcing": {"drift": [], "noise_S": 0.0, "complexity": 0.5, "sensor_eisv": None},
    "cases": cases,
    "finite_step_local_jacobian_sensitivity_check": step_jacobians,
    "interpretation": "The dt=.1 default RK4 map expands two interior initial states in the stated fixed diagonal metric, despite a strictly negative continuous-time local symmetric Jacobian at x. No stage/final clipping or entropy-floor activation occurs in that counterexample. This disproves universal stepwise contraction in that metric over the domain; it does not establish trajectory divergence, live incidence, or instability of the full governor. No live data were read.",
}
print(json.dumps(result, indent=2))

export.cleanup()

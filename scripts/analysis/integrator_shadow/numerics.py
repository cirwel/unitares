#!/usr/bin/env python3
"""Run a frozen synthetic numerical shadow against an exported source tree.

This worker imports only five pure governance_core modules, bypassing the
package initializer. It does not contact a server or read operational data.
Each invocation evaluates one source tree; the caller compares worker reports.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import sys
import types

sys.dont_write_bytecode = True

import numpy as np
import scipy
from scipy.integrate import solve_ivp


AXES = ("E", "I", "S", "V")
MODULES = ("parameters", "utils", "coherence", "dynamics", "stability")
MODES = ("euler", "rk4")
TIMESTEPS = (0.1, 0.25, 0.5, 1.0)
UPDATES = 20
REFERENCE_RTOL = 1e-10
REFERENCE_ATOL = 1e-12
METRIC_WEIGHTS = np.array([0.1, 0.2, 1.0, 0.08])
BASE_ENVIRONMENT = {
    "UNITARES_I_DYNAMICS": "linear",
    "UNITARES_PARAMS_PROFILE": "default",
    "UNITARES_PARAMS_JSON": "{}",
    "UNITARES_INTEGRATOR": "rk4",
    "UNITARES_SENSOR_COUPLING": "off",
}
FORCINGS = (
    {
        "name": "sensor_off",
        "sensor_coupling_mode": "off",
        "sensor_eisv": None,
        "delta_eta": [],
        "noise_S": 0.0,
        "complexity": 0.5,
        "s_setpoint": 0.0,
    },
    {
        "name": "sensor_on",
        "sensor_coupling_mode": "on",
        "sensor_eisv": {"E": 0.8, "I": 0.6, "S": 0.4, "V": 0.2},
        "delta_eta": [0.3, 0.4],
        "noise_S": 0.02,
        "complexity": 0.8,
        "s_setpoint": 0.2,
    },
)


def load_source(source_dir: Path):
    """Bind the supplied tree without executing governance_core/__init__.py."""
    module_dir = source_dir / "governance_core"
    hashes = {}
    for name in MODULES:
        path = module_dir / f"{name}.py"
        hashes[f"governance_core/{name}.py"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    if "governance_core" in sys.modules:
        raise RuntimeError("Use a fresh worker process for each source tree")
    sys.dont_write_bytecode = True
    package = types.ModuleType("governance_core")
    package.__path__ = [str(module_dir)]
    sys.modules["governance_core"] = package
    loaded = {
        name: importlib.import_module(f"governance_core.{name}") for name in MODULES
    }
    for name, module in loaded.items():
        if Path(module.__file__).resolve() != (module_dir / f"{name}.py").resolve():
            raise RuntimeError(f"Unexpected import location for {name}")
    return loaded, hashes


def vector(state):
    return np.array([getattr(state, axis) for axis in AXES], dtype=float)


def state_record(values):
    return dict(zip(AXES, map(float, values)))


def scenario_id(face: str, mode: str, dt: float, forcing: dict) -> str:
    return f"{face}__{mode}__dt_{dt:g}__{forcing['name']}"


def error_record(actual, reference):
    if not reference["comparison_eligible"] or not np.all(np.isfinite(actual)):
        return None
    difference = actual - np.array(list(reference["output"].values()))
    return {
        "coordinate_error": state_record(difference),
        "max_abs_error": float(np.max(np.abs(difference))),
        "weighted_l2_error": float(np.sqrt(np.sum(METRIC_WEIGHTS * difference**2))),
    }


class Evaluator:
    def __init__(self, dynamics, params, theta):
        self.d = dynamics
        self.params = params
        self.theta = theta
        self.lower = np.array([getattr(params, f"{axis}_min") for axis in AXES])
        self.upper = np.array([getattr(params, f"{axis}_max") for axis in AXES])

    def sensor(self, forcing):
        value = forcing["sensor_eisv"]
        return None if value is None else self.d.State(**value)

    def actual_step(self, initial, mode, dt, forcing):
        os.environ["UNITARES_INTEGRATOR"] = mode
        os.environ["UNITARES_SENSOR_COUPLING"] = forcing["sensor_coupling_mode"]
        result = self.d.compute_dynamics(
            self.d.State(*initial),
            forcing["delta_eta"],
            self.theta,
            self.params,
            dt=dt,
            noise_S=forcing["noise_S"],
            complexity=forcing["complexity"],
            sensor_eisv=self.sensor(forcing),
            s_setpoint=forcing["s_setpoint"],
        )
        return vector(result)

    def reference_step(self, initial, dt, forcing):
        """Solve the continuous frozen RHS, then apply the external-update floor."""
        drift_sq = sum(value**2 for value in forcing["delta_eta"])
        sensor = self.sensor(forcing)

        def rhs(_time, values):
            return self.d._derivatives(
                self.d.State(*values),
                drift_sq,
                self.theta,
                self.params,
                forcing["noise_S"],
                forcing["complexity"],
                sensor,
                forcing["s_setpoint"],
            )

        solution = solve_ivp(
            rhs,
            (0.0, dt),
            initial,
            method="Radau",
            dense_output=True,
            rtol=REFERENCE_RTOL,
            atol=REFERENCE_ATOL,
        )
        sample_times = np.unique(
            np.concatenate([solution.t, np.linspace(0.0, solution.t[-1], 129)])
        )
        samples = solution.sol(sample_times)
        finite = bool(np.all(np.isfinite(samples)))
        lower_margins = np.min(samples - self.lower[:, None], axis=1)
        upper_margins = np.min(self.upper[:, None] - samples, axis=1)
        in_box = bool(
            finite and np.all(lower_margins >= 0) and np.all(upper_margins >= 0)
        )
        output = solution.y[:, -1].copy()
        before_floor = float(output[2])
        floor = self.params.S_min + 0.049 * forcing["complexity"]
        output[2] = max(output[2], floor)
        eligible = bool(solution.success and in_box)
        return {
            "output": state_record(output),
            "solver_success": bool(solution.success),
            "solver_message": solution.message,
            "nfev": solution.nfev,
            "comparison_eligible": eligible,
            "ineligibility_reason": (
                None
                if eligible
                else "reference_solver_failed"
                if not solution.success
                else "reference_not_inside_configured_box_on_checked_samples"
            ),
            "box_check": {
                "method": "accepted_solver_mesh_plus_129_uniform_dense_output_samples",
                "sample_count": len(sample_times),
                "finite": finite,
                "in_box": in_box,
                "minimum_lower_margin": state_record(lower_margins),
                "minimum_upper_margin": state_record(upper_margins),
                "limitation": "Sampled path check, not a continuous-path enclosure proof.",
            },
            "final_floor": {
                "value": floor,
                "pre_floor_S": before_floor,
                "activated": bool(before_floor < floor),
            },
        }


def run(source_dir: Path):
    os.environ.update(BASE_ENVIRONMENT)
    modules, hashes = load_source(source_dir)
    params = modules["parameters"].get_active_params()
    theta = modules["parameters"].DEFAULT_THETA
    evaluator = Evaluator(modules["dynamics"], params, theta)
    faces = []
    for axis, (low, high) in enumerate(zip(evaluator.lower, evaluator.upper)):
        for side, value in (("lower", low + 1e-4), ("upper", high - 1e-4)):
            initial = np.array([0.5, 0.5, 0.3, 0.0])
            initial[axis] = value
            faces.append((f"{AXES[axis]}_{side}", initial))

    single_steps, trajectories = [], []
    reference_trajectories = []
    for face, initial in faces:
        for dt in TIMESTEPS:
            for forcing in FORCINGS:
                reference_id = f"{face}__dt_{dt:g}__{forcing['name']}"
                reference_state = initial.copy()
                references = []
                chain_eligible = True
                for update in range(1, UPDATES + 1):
                    reference = evaluator.reference_step(reference_state, dt, forcing)
                    chain_eligible = chain_eligible and reference["comparison_eligible"]
                    reference["comparison_eligible"] = chain_eligible
                    if not chain_eligible and reference["ineligibility_reason"] is None:
                        reference["ineligibility_reason"] = (
                            "earlier_reference_step_ineligible"
                        )
                    references.append({"update": update, **reference})
                    reference_state = np.array(list(reference["output"].values()))
                reference_trajectories.append(
                    {
                        "reference_id": reference_id,
                        "initial_state": state_record(initial),
                        "dt_per_update": dt,
                        "forcing": forcing,
                        "steps": references,
                    }
                )
                for mode in MODES:
                    identifier = scenario_id(face, mode, dt, forcing)
                    actual = initial.copy()
                    steps = []
                    for update, reference in enumerate(references, 1):
                        actual = evaluator.actual_step(actual, mode, dt, forcing)
                        steps.append(
                            {
                                "update": update,
                                "elapsed_model_time": update * dt,
                                "output": state_record(actual),
                                "numerical_error": (
                                    None
                                    if np.all(np.isfinite(actual))
                                    else "nonfinite_actual_state"
                                ),
                                "reference_error": error_record(actual, reference),
                            }
                        )
                    common = {
                        "scenario_id": identifier,
                        "reference_id": reference_id,
                        "face": face,
                        "integrator": mode,
                        "dt_per_update": dt,
                        "initial_state": state_record(initial),
                        "forcing": forcing,
                    }
                    single_steps.append({**common, **steps[0]})
                    valid_errors = [
                        s["reference_error"]["max_abs_error"]
                        for s in steps
                        if s["reference_error"] is not None
                    ]
                    trajectories.append(
                        {
                            **common,
                            "updates": UPDATES,
                            "steps": steps,
                            "max_abs_reference_error_over_updates": max(
                                valid_errors, default=None
                            ),
                            "eligible_reference_steps": len(valid_errors),
                        }
                    )

    pairs = []
    x = np.array([0.5, 0.999, 0.3, 0.0])
    y = x + np.array([0.0, 1e-7, 0.0, 0.0])
    before = float(np.sqrt(np.sum(METRIC_WEIGHTS * (x - y) ** 2)))
    for mode in MODES:
        for dt in TIMESTEPS:
            for forcing in FORCINGS:
                outputs = [evaluator.actual_step(v, mode, dt, forcing) for v in (x, y)]
                after = float(
                    np.sqrt(np.sum(METRIC_WEIGHTS * (outputs[0] - outputs[1]) ** 2))
                )
                pairs.append(
                    {
                        "scenario_id": scenario_id(
                            "original_I_pair", mode, dt, forcing
                        ),
                        "integrator": mode,
                        "dt": dt,
                        "forcing": forcing,
                        "initial_x": state_record(x),
                        "initial_y": state_record(y),
                        "output_x": state_record(outputs[0]),
                        "output_y": state_record(outputs[1]),
                        "initial_weighted_distance": before,
                        "final_weighted_distance": after,
                        "distance_ratio": after / before,
                    }
                )

    return {
        "schema": "integrator.offline_shadow.worker.v1",
        "source_module_sha256": hashes,
        "runtime": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "scipy": scipy.__version__,
        },
        "base_environment": BASE_ENVIRONMENT,
        "per_scenario_environment": {
            "UNITARES_INTEGRATOR": "scenario.integrator",
            "UNITARES_SENSOR_COUPLING": "scenario.forcing.sensor_coupling_mode",
        },
        "resolved_parameters": asdict(params),
        "theta": asdict(theta),
        "suite": {
            "near_face_inset": 1e-4,
            "timesteps": TIMESTEPS,
            "modes": MODES,
            "updates_per_trajectory": UPDATES,
            "forcings": FORCINGS,
            "metric_diagonal": METRIC_WEIGHTS.tolist(),
        },
        "reference_configuration": {
            "method": "scipy.solve_ivp.Radau",
            "rtol": REFERENCE_RTOL,
            "atol": REFERENCE_ATOL,
            "rhs": "exported governance_core.dynamics._derivatives",
            "forcing_policy": "Frozen for every external update and throughout each trajectory.",
            "entropy_floor_policy": "Applied once after each complete external update.",
            "errors": "Coordinates and metric distances against the same elapsed-time reference; null when the reference path check is ineligible.",
        },
        "single_steps": single_steps,
        "pair_witnesses": pairs,
        "trajectories": trajectories,
        "reference_trajectories": reference_trajectories,
        "scope": {
            "data": "Deterministic synthetic states only; no live data, database, outcomes or network.",
            "comparison": "Actual exported compute_dynamics, not a reimplementation of either integrator.",
            "limitations": [
                "Frozen-input numerical behavior only; no adaptive controller, history, policy or agent replay.",
                "Radau changes the integration method, but intentionally shares the source RHS; this is not independent validation of the equations.",
                "Reference domain eligibility is sampled, not a formal continuous-path proof.",
                "The two forcing cases also differ in drift, noise, complexity and setpoint; their contrast does not isolate sensor coupling.",
                "No global contraction, decision-quality, live-incidence or production-readiness claim.",
                "Reported errors and ratios have no empirical pass/fail threshold or policy authority.",
            ],
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", required=True, type=Path)
    args = parser.parse_args()
    result = run(args.source_dir.expanduser().resolve())
    # Never emit JavaScript's non-standard NaN/Infinity JSON tokens. Keep any
    # unexpected numerical failure explicit rather than silently dropping it.
    invalid_paths = []

    def finite_json(value, path="report"):
        if isinstance(value, float) and not np.isfinite(value):
            invalid_paths.append(path)
            return None
        if isinstance(value, dict):
            return {
                key: finite_json(item, f"{path}.{key}") for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [
                finite_json(item, f"{path}[{index}]")
                for index, item in enumerate(value)
            ]
        return value

    result = finite_json(result)
    result["nonfinite_values_replaced_with_null"] = invalid_paths
    print(json.dumps(result, allow_nan=False, separators=(",", ":")))


if __name__ == "__main__":
    main()

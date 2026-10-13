"""Offline synthetic shadow comparison; never connects to the live estimator.

Run from a full development environment (requirements-full.txt). Outputs JSON
on stdout or to --output. Git supplies immutable source, not working-tree code.
"""

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile

BASELINE = "6d742273434c6cc8a7cfa10ea4e29e969660e719"
CANDIDATE = "106d45f48e28e00ba4ca35fcae7ab08b4fc2ed01"
EXPORT_PATHS = (
    "governance_core",
    "src",
    "config",
    "tests/__init__.py",
    "tests/eisv_preservation_driver.py",
    "tests/fixtures/eisv_preservation/estimator_trajectories_v0.json",
)


def differences(before, after, path=()):
    """Compare structure and types too; bools must never become numeric deltas."""
    if type(before) is not type(after):
        return [dict(path=list(path), kind="type", before=before, after=after)]
    if isinstance(before, dict):
        result = []
        for key in sorted(before.keys() | after.keys()):
            if key not in before:
                result.append(
                    dict(path=list(path + (key,)), kind="added", after=after[key])
                )
            elif key not in after:
                result.append(
                    dict(path=list(path + (key,)), kind="removed", before=before[key])
                )
            else:
                result.extend(differences(before[key], after[key], path + (key,)))
        return result
    if isinstance(before, list):
        result = []
        if len(before) != len(after):
            result.append(
                dict(
                    path=list(path), kind="length", before=len(before), after=len(after)
                )
            )
        for i, (left, right) in enumerate(zip(before, after)):
            result.extend(differences(left, right, path + (i,)))
        return result
    if isinstance(before, (int, float)) and not isinstance(before, bool):
        if not math.isfinite(before) or not math.isfinite(after):
            raise ValueError("nonfinite value in synthetic fixture comparison")
        if before != after:
            return [
                dict(
                    path=list(path),
                    kind="numeric",
                    before=before,
                    after=after,
                    absolute_difference=abs(before - after),
                )
            ]
    elif before != after:
        return [dict(path=list(path), kind="categorical", before=before, after=after)]
    return []


def summarize_changes(changes):
    numeric = [c for c in changes if c["kind"] == "numeric"]
    categorical = [c for c in changes if c["kind"] == "categorical"]
    structural = [c for c in changes if c["kind"] not in ("numeric", "categorical")]
    return dict(
        total_changed_leaves=len(changes),
        numeric_changes=len(numeric),
        categorical_changes=len(categorical),
        structural_changes=len(structural),
        max_absolute_numeric_change=max(
            (c["absolute_difference"] for c in numeric), default=0
        ),
        largest_numeric_changes=sorted(
            numeric, key=lambda c: c["absolute_difference"], reverse=True
        )[:10],
        categorical_and_structural_changes=categorical + structural,
    )


def summarize_numerics(payloads):
    """Report coverage alongside errors so missing comparisons cannot vanish."""
    baseline = payloads["baseline"]["numerics"]
    candidate = payloads["candidate"]["numerics"]
    for field in (
        "suite",
        "resolved_parameters",
        "theta",
        "reference_configuration",
        "reference_trajectories",
    ):
        if differences(baseline[field], candidate[field]):
            raise ValueError(
                f"numerical comparison inputs or references differ: {field}"
            )
    for field in ("single_steps", "trajectories", "pair_witnesses"):
        ids = [
            [row["scenario_id"] for row in data[field]]
            for data in (baseline, candidate)
        ]
        if ids[0] != ids[1] or len(set(ids[0])) != len(ids[0]):
            raise ValueError(f"numerical scenario coverage differs or repeats: {field}")
    summary = {}
    for label, payload in payloads.items():
        data = payload["numerics"]
        groups = []
        for mode in data["suite"]["modes"]:
            for dt in data["suite"]["timesteps"]:
                singles = [
                    x
                    for x in data["single_steps"]
                    if x["integrator"] == mode and x["dt_per_update"] == dt
                ]
                paths = [
                    x
                    for x in data["trajectories"]
                    if x["integrator"] == mode and x["dt_per_update"] == dt
                ]
                errors = [
                    x["reference_error"]["max_abs_error"]
                    for x in singles
                    if x["reference_error"] is not None
                ]
                path_errors = [
                    x["max_abs_reference_error_over_updates"]
                    for x in paths
                    if x["max_abs_reference_error_over_updates"] is not None
                ]
                groups.append(
                    dict(
                        integrator=mode,
                        dt=dt,
                        single_step_count=len(singles),
                        eligible_single_steps=len(errors),
                        max_single_step_coordinate_error=max(errors, default=None),
                        trajectory_count=len(paths),
                        expected_trajectory_steps=sum(x["updates"] for x in paths),
                        eligible_trajectory_steps=sum(
                            x["eligible_reference_steps"] for x in paths
                        ),
                        max_trajectory_coordinate_error=max(path_errors, default=None),
                    )
                )
        summary[label] = dict(
            groups=groups, nonfinite_values=data["nonfinite_values_replaced_with_null"]
        )
    return summary


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def resolve(repo, revision):
    return git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").decode().strip()


def export(repo, revision, target):
    archive = git(repo, "archive", revision, *EXPORT_PATHS)
    with tarfile.open(fileobj=io.BytesIO(archive)) as stream:
        stream.extractall(target, filter="data")
    return hashlib.sha256(archive).hexdigest()


def run_worker(source, kind):
    scratch = source / "_scratch"
    scratch.mkdir(exist_ok=True)
    # Inherit no credentials, service endpoints, configuration or Python path.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    env.update(
        TMPDIR=str(scratch),
        TMP=str(scratch),
        TEMP=str(scratch),
        PYTHONDONTWRITEBYTECODE="1",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("worker.py")),
            "--source-dir",
            str(source),
            "--kind",
            kind,
        ],
        cwd=source,
        env=env,
        text=True,
        capture_output=True,
        timeout=600,
    )
    if result.returncode:
        raise RuntimeError(f"{kind} worker failed: {result.stderr[-6000:]}")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[3]
    )
    parser.add_argument("--baseline-ref", default=BASELINE)
    parser.add_argument("--candidate-ref", default=CANDIDATE)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixtures-only", action="store_true")
    args = parser.parse_args()
    revisions = {
        key: resolve(args.repo, ref)
        for key, ref in [
            ("baseline", args.baseline_ref),
            ("candidate", args.candidate_ref),
        ]
    }
    source_changes = (
        git(
            args.repo,
            "diff",
            "--name-only",
            revisions["baseline"],
            revisions["candidate"],
            "--",
            *EXPORT_PATHS,
        )
        .decode()
        .splitlines()
    )
    if source_changes != ["governance_core/dynamics.py"]:
        raise ValueError(
            f"comparison requires only the dynamics implementation to differ: {source_changes}"
        )
    payloads = {}
    exports = {}
    with tempfile.TemporaryDirectory(prefix="offline-integrator-shadow-") as folder:
        for label, revision in revisions.items():
            source = Path(folder) / label
            source.mkdir()
            exports[label] = export(args.repo, revision, source)
            payloads[label] = {"preservation": run_worker(source, "preservation")}
            if label == "baseline":
                golden = json.loads(
                    (
                        source
                        / "tests/fixtures/eisv_preservation/estimator_trajectories_v0.json"
                    ).read_text()
                )
                golden.pop("provenance", None)
                baseline_mismatches = differences(
                    golden, payloads[label]["preservation"]
                )
                if baseline_mismatches:
                    raise ValueError(
                        f"preserved source does not reproduce its golden: {baseline_mismatches[:3]}"
                    )
            if not args.fixtures_only:
                payloads[label]["numerics"] = run_worker(source, "numerics")
    changes = differences(
        payloads["baseline"]["preservation"], payloads["candidate"]["preservation"]
    )
    report = dict(
        schema="unitares.offline_integrator_shadow.v1",
        scope="Synthetic frozen fixtures and numerical scenarios only; no live inputs, no outcome reads, no deployment or baseline changes.",
        limitations=[
            "Fixture agreement does not establish live parity or decision quality.",
            "Numerical examples do not certify global nonlinear contraction.",
            "This report carries no pass/fail authority over the registered study.",
        ],
        revisions=revisions,
        archive_sha256=exports,
        exported_paths=list(EXPORT_PATHS),
        source_changes=source_changes,
        python=platform.python_version(),
        baseline_matches_unchanged_golden=True,
        fixture_comparison=summarize_changes(changes),
        fixture_changes=changes,
        results=payloads,
    )
    if not args.fixtures_only:
        report["numerical_summary"] = summarize_numerics(payloads)
    text = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(text)
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "fixture_comparison": report["fixture_comparison"],
                },
                indent=2,
            )
        )
    else:
        print(text, end="")


if __name__ == "__main__":
    main()

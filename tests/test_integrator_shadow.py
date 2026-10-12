"""Offline shadow reporting must expose drift and refuse service access."""

import pytest

from scripts.analysis.integrator_shadow.run import (
    differences,
    run_worker,
    summarize_changes,
)


def test_comparison_includes_structure_types_and_categorical_changes():
    before = {"sequence": [1, True, "proceed"], "removed": 7, "value": 0.5}
    after = {"sequence": [1.1, 1, "pause", 9], "added": 7, "value": 0.500001}
    result = differences(before, after)
    assert {item["kind"] for item in result} == {
        "length",
        "type",
        "categorical",
        "numeric",
        "added",
        "removed",
    }
    summary = summarize_changes(result)
    assert (
        summary["numeric_changes"] == 1
    )  # int -> float is structural, not silently coerced.
    assert summary["categorical_changes"] == 1
    assert summary["structural_changes"] == 5
    assert summary["max_absolute_numeric_change"] == pytest.approx(0.000001)


def test_equal_values_have_no_drift_and_nonfinite_values_are_rejected():
    assert differences({"a": [None, False, 0.5]}, {"a": [None, False, 0.5]}) == []
    with pytest.raises(ValueError, match="nonfinite"):
        differences(float("nan"), float("nan"))


def fake_driver(root, body):
    package = root / "tests"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "eisv_preservation_driver.py").write_text(body)


def test_worker_removes_service_configuration_and_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("UNITARES_PARAMS_JSON", '{"gamma_I": 9}')
    monkeypatch.setenv("DATABASE_URL", "postgresql://should-not-be-inherited")
    fake_driver(
        tmp_path,
        "import os\ndef run_all():\n    return dict(config=os.getenv('UNITARES_PARAMS_JSON'), db=os.getenv('DATABASE_URL'))\n",
    )
    assert run_worker(tmp_path, "preservation") == {"config": None, "db": None}


def test_worker_fails_even_if_driver_swallows_network_denial(tmp_path):
    fake_driver(
        tmp_path,
        """import socket
def run_all():
    try:
        socket.getaddrinfo('example.invalid', 443)
    except Exception:
        pass
    return {'ignored_error': True}
""",
    )
    with pytest.raises(RuntimeError, match="forbidden operations"):
        run_worker(tmp_path, "preservation")


def test_worker_refuses_write_outside_export(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside.json"
    fake_driver(
        source,
        f"""from pathlib import Path
def run_all():
    Path({str(outside)!r}).write_text('bad')
    return {{}}
""",
    )
    with pytest.raises(RuntimeError, match="denied open"):
        run_worker(source, "preservation")
    assert not outside.exists()


def test_worker_metadata_probes_are_emulated_but_other_subprocesses_denied(tmp_path):
    fake_driver(
        tmp_path,
        f"""import subprocess
def run_all():
    result = subprocess.run(['git', '-C', {str(tmp_path)!r}, 'rev-parse', 'HEAD'], capture_output=True, text=True)
    return {{'returncode': result.returncode, 'stdout': result.stdout}}
""",
    )
    assert run_worker(tmp_path, "preservation") == {"returncode": 128, "stdout": ""}
    (tmp_path / "tests/eisv_preservation_driver.py").write_text("""import subprocess
def run_all():
    try:
        subprocess.run(['git', '--version'])
    except Exception:
        pass
    return {}
""")
    with pytest.raises(RuntimeError, match="forbidden operations"):
        run_worker(tmp_path, "preservation")


def test_worker_refuses_relative_directory_descriptor_escape(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "probe.txt"
    fake_driver(
        source,
        f"""import os
def run_all():
    directory = os.open({str(tmp_path)!r}, os.O_RDONLY)
    try:
        target = os.open('probe.txt', os.O_WRONLY | os.O_CREAT, dir_fd=directory)
        os.close(target)
    finally:
        os.close(directory)
    return {{}}
""",
    )
    with pytest.raises(RuntimeError, match="denied open"):
        run_worker(source, "preservation")
    assert not outside.exists()


def test_numerical_summary_keeps_missing_coverage_visible_and_checks_references():
    from copy import deepcopy
    from scripts.analysis.integrator_shadow.run import summarize_numerics

    numerical = dict(
        suite={"modes": ["rk4"], "timesteps": [0.1]},
        resolved_parameters={},
        theta={},
        reference_configuration={},
        reference_trajectories=[{"reference_id": "sample", "value": 0.2}],
        single_steps=[
            dict(
                scenario_id="sample",
                integrator="rk4",
                dt_per_update=0.1,
                reference_error=None,
            )
        ],
        trajectories=[
            dict(
                scenario_id="sample",
                integrator="rk4",
                dt_per_update=0.1,
                updates=20,
                eligible_reference_steps=0,
                max_abs_reference_error_over_updates=None,
            )
        ],
        pair_witnesses=[],
        nonfinite_values_replaced_with_null=["sample"],
    )
    payloads = {
        label: {"numerics": deepcopy(numerical)} for label in ("baseline", "candidate")
    }
    summary = summarize_numerics(payloads)["candidate"]
    assert summary["groups"][0]["expected_trajectory_steps"] == 20
    assert summary["groups"][0]["eligible_trajectory_steps"] == 0
    assert summary["groups"][0]["max_single_step_coordinate_error"] is None
    assert summary["nonfinite_values"] == ["sample"]
    payloads["candidate"]["numerics"]["reference_trajectories"][0]["value"] = 0.3
    with pytest.raises(ValueError, match="references differ"):
        summarize_numerics(payloads)

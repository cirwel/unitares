"""Interpreter transitions must keep the admitted floor and both images covered."""

import importlib.util
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CHECKER_PATH = ROOT / "scripts/diagnostics/check_ci_python_matrix_sync.py"


@pytest.fixture
def runtime_contract(tmp_path, monkeypatch):
    """A complete contract with distinct support and production interpreters."""
    spec = importlib.util.spec_from_file_location(
        "python_runtime_contract_checker", CHECKER_PATH
    )
    assert spec is not None and spec.loader is not None
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)

    paths = {
        "PYPROJECT_PATH": tmp_path / "pyproject.toml",
        "CONSTRAINTS_PATH": tmp_path / "constraints.txt",
        "DOCKERFILE_PATH": tmp_path / "Dockerfile",
        "GLAMA_DOCKERFILE_PATH": tmp_path / "Dockerfile.glama",
        "TEST_WORKFLOW_PATH": tmp_path / "tests.yml",
        "OPS_TEMPLATES_DIR": tmp_path / "ops",
    }
    for attribute, path in paths.items():
        monkeypatch.setattr(checker, attribute, path)
    checker.PYPROJECT_PATH.write_text('[project]\nrequires-python = ">=3.14"\n')
    checker.CONSTRAINTS_PATH.write_text("# production-python: 3.15\n")
    for image in (checker.DOCKERFILE_PATH, checker.GLAMA_DOCKERFILE_PATH):
        image.write_text("FROM python:3.15-slim@sha256:fixture\n")
    checker.OPS_TEMPLATES_DIR.mkdir()
    (checker.OPS_TEMPLATES_DIR / "fixture.plist.template").write_text(
        "<string>/Library/Frameworks/Python.framework/Versions/3.15/bin/python3</string>\n"
    )
    workflow = {
        "jobs": {
            "test_shard": {
                "strategy": {"matrix": {"python-version": ["3.14", "3.15"]}},
                "steps": [
                    {
                        "uses": "actions/setup-python@fixture",
                        "with": {"python-version": "${{ matrix.python-version }}"},
                    }
                ],
            },
        },
    }
    checker.TEST_WORKFLOW_PATH.write_text(yaml.safe_dump(workflow))
    return checker


def test_new_production_interpreter_keeps_the_older_supported_floor(
    runtime_contract, capsys
):
    assert runtime_contract.main() == 0
    output = capsys.readouterr().out
    assert "floor 3.14, production 3.15" in output
    assert "2 Dockerfiles" in output


@pytest.mark.parametrize(
    "missing, message",
    [
        ("3.14", "minimum supported version"),
        ("3.15", "production interpreter"),
    ],
)
def test_each_interpreter_must_actually_remain_in_the_matrix(
    runtime_contract, capsys, missing, message
):
    workflow = yaml.safe_load(runtime_contract.TEST_WORKFLOW_PATH.read_text())
    versions = workflow["jobs"]["test_shard"]["strategy"]["matrix"]["python-version"]
    versions.remove(missing)
    runtime_contract.TEST_WORKFLOW_PATH.write_text(yaml.safe_dump(workflow))

    assert runtime_contract.main() == 1
    assert message in capsys.readouterr().out


def test_glama_only_interpreter_drift_is_rejected(runtime_contract, capsys):
    runtime_contract.GLAMA_DOCKERFILE_PATH.write_text(
        "FROM python:3.14-slim@sha256:fixture\n"
    )
    assert runtime_contract._read_dockerfile_python() == (3, 15)

    assert runtime_contract.main() == 1
    assert "Dockerfile.glama base image is python:3.14" in capsys.readouterr().out


def test_missing_glama_image_fails_closed(runtime_contract, capsys):
    runtime_contract.GLAMA_DOCKERFILE_PATH.unlink()

    assert runtime_contract.main() == 1
    output = capsys.readouterr().out
    assert "Could not read the interpreter contract" in output
    assert "Dockerfile.glama" in output


def test_glama_stages_cannot_name_different_interpreters(runtime_contract, capsys):
    runtime_contract.GLAMA_DOCKERFILE_PATH.write_text(
        "FROM python:3.15-slim AS build\nFROM python:3.14-slim\n"
    )

    assert runtime_contract.main() == 1
    assert (
        "Dockerfile.glama names more than one python base image"
        in capsys.readouterr().out
    )

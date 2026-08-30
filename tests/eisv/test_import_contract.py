"""Compatibility contracts for the EISV package move."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

import src.eisv_format as legacy_formatting
import src.eisv_validator as legacy_validation
import src.eisv.formatting as canonical_formatting
import src.eisv.validation as canonical_validation


PROJECT_ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    (
        (legacy_formatting, canonical_formatting),
        (legacy_validation, canonical_validation),
    ),
)
def test_legacy_modules_alias_canonical_public_api(
    legacy: ModuleType,
    canonical: ModuleType,
) -> None:
    """Old imports resolve to the canonical module and exported objects."""
    assert legacy is canonical
    assert legacy.__all__ == canonical.__all__
    assert canonical.__all__
    assert len(canonical.__all__) == len(set(canonical.__all__))
    for name in canonical.__all__:
        assert getattr(legacy, name) is getattr(canonical, name)


@pytest.mark.parametrize(
    ("legacy_name", "canonical_name", "export_name"),
    (
        ("src.eisv_format", "src.eisv.formatting", "EISVMetrics"),
        (
            "src.eisv_validator",
            "src.eisv.validation",
            "IncompleteEISVError",
        ),
    ),
)
def test_legacy_first_import_aliases_canonical_module_in_fresh_process(
    legacy_name: str,
    canonical_name: str,
    export_name: str,
) -> None:
    """An external legacy-first import gets the canonical module identity."""
    code = (
        f"import {legacy_name} as legacy\n"
        f"import {canonical_name} as canonical\n"
        "assert legacy is canonical\n"
        f"assert legacy.{export_name} is canonical.{export_name}\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert run.returncode == 0, run.stderr


def test_legacy_validation_toggle_shares_canonical_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutating the legacy toggle changes the state read by canonical code."""
    monkeypatch.setattr(legacy_validation, "VALIDATION_ENABLED", False)

    response = {"metrics": {"E": 0.8}}
    assert canonical_validation.VALIDATION_ENABLED is False
    assert canonical_validation.auto_validate_response(response) is response


@pytest.mark.parametrize(
    ("legacy_name", "canonical_name"),
    (
        ("src.eisv_format", "src.eisv.formatting"),
        ("src.eisv_validator", "src.eisv.validation"),
    ),
)
def test_legacy_module_entrypoint_delegates_byte_identically(
    legacy_name: str,
    canonical_name: str,
) -> None:
    """Existing python -m examples keep their output and exit status."""
    runs = []
    for module_name in (legacy_name, canonical_name):
        runs.append(
            subprocess.run(
                [sys.executable, "-m", module_name],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
        )

    legacy_run, canonical_run = runs
    assert legacy_run.returncode == canonical_run.returncode == 0
    assert legacy_run.stdout == canonical_run.stdout
    assert legacy_run.stderr == canonical_run.stderr


def test_legacy_validator_file_entrypoint_delegates_byte_identically() -> None:
    """Preserve the validator example's historical direct-file execution."""
    runs = []
    for relative_path in ("src/eisv_validator.py", "src/eisv/validation.py"):
        runs.append(
            subprocess.run(
                [sys.executable, relative_path],
                cwd=PROJECT_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
        )

    legacy_run, canonical_run = runs
    assert legacy_run.returncode == canonical_run.returncode == 0
    assert legacy_run.stdout == canonical_run.stdout
    assert legacy_run.stderr == canonical_run.stderr

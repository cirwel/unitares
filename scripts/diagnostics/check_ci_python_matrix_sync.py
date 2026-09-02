#!/usr/bin/env python3
"""
Validate Python version sync between pyproject, constraints.txt, and the CI
workflow matrix.

Three facts have to agree:

* ``requires-python`` in pyproject.toml is the floor the package admits.
* ``# production-python: X.Y`` in constraints.txt is the interpreter the
  deployment surfaces actually run (the LaunchAgent, the Docker base image).
  It moves only with a deploy, the same rule as the pins in that file.
* the ``python-version`` matrix in .github/workflows/tests.yml is what the
  suite is exercised on.

The matrix must include the floor and the production interpreter, and must
not include anything below the floor. Until 2026-09-02 only the floor rule
existed, and the suite ran on 3.12 alone while every deployment surface was
on 3.14.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = PROJECT_ROOT / "pyproject.toml"
CONSTRAINTS_PATH = PROJECT_ROOT / "constraints.txt"
TEST_WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "tests.yml"

PRODUCTION_PYTHON_RE = re.compile(r"^#\s*production-python:\s*(\d+\.\d+)\s*$", re.M)


def _parse_version_tuple(version_text: str) -> tuple[int, int]:
    major_text, minor_text = version_text.split(".", 1)
    return int(major_text), int(minor_text)


def _render(version: tuple[int, int]) -> str:
    return f"{version[0]}.{version[1]}"


def _read_requires_python() -> tuple[int, int]:
    pyproject_data = tomllib.loads(PYPROJECT_PATH.read_text())
    requires_python = pyproject_data["project"]["requires-python"].strip()

    match = re.match(r"^>=\s*(\d+\.\d+)$", requires_python)
    if not match:
        raise ValueError(
            f"Unsupported requires-python format: {requires_python!r} "
            "(expected exact form like '>=3.12')"
        )
    return _parse_version_tuple(match.group(1))


def _read_production_python() -> tuple[int, int]:
    """The interpreter production runs, declared once in constraints.txt."""
    matches = PRODUCTION_PYTHON_RE.findall(CONSTRAINTS_PATH.read_text())
    if len(matches) != 1:
        raise ValueError(
            "constraints.txt must declare exactly one '# production-python: X.Y' "
            f"marker (found {len(matches)})"
        )
    return _parse_version_tuple(matches[0])


def _read_ci_matrix_versions() -> list[tuple[int, int]]:
    workflow_text = TEST_WORKFLOW_PATH.read_text()
    match = re.search(r"python-version:\s*\[([^\]]+)\]", workflow_text)
    if not match:
        raise ValueError("Could not find python-version matrix in tests.yml")

    raw_entries = [entry.strip().strip("'\"") for entry in match.group(1).split(",")]
    versions: list[tuple[int, int]] = []
    for entry in raw_entries:
        if not re.match(r"^\d+\.\d+$", entry):
            raise ValueError(f"Unsupported matrix version format: {entry!r}")
        versions.append(_parse_version_tuple(entry))
    return versions


def main() -> int:
    try:
        required_min = _read_requires_python()
        production = _read_production_python()
        ci_versions = _read_ci_matrix_versions()
    except (ValueError, KeyError, OSError) as exc:
        # Fail toward "unknown", never toward "healthy": a marker that cannot
        # be read is a failed check, not a passed one.
        print(f"❌ Could not read the interpreter contract: {exc}")
        return 1

    too_low = [v for v in ci_versions if v < required_min]
    if too_low:
        print(
            "❌ CI matrix includes versions lower than requires-python: "
            f"{too_low} < {required_min}"
        )
        return 1

    if required_min not in ci_versions:
        print(
            "❌ CI matrix must include minimum supported version from pyproject: "
            f"{_render(required_min)}"
        )
        return 1

    if production < required_min:
        print(
            "❌ constraints.txt declares production-python "
            f"{_render(production)} below requires-python {_render(required_min)}"
        )
        return 1

    if production not in ci_versions:
        print(
            "❌ CI matrix must include the production interpreter declared in "
            f"constraints.txt: {_render(production)}"
        )
        return 1

    rendered = ", ".join(_render(version) for version in ci_versions)
    print(
        "✅ CI Python matrix matches the interpreter contract: "
        f">={_render(required_min)}, production {_render(production)}, "
        f"matrix [{rendered}]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

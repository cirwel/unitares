#!/usr/bin/env python3
"""
Validate the interpreter contract: pyproject floor, constraints.txt production
marker, Dockerfile base image, ops LaunchAgent templates, and the CI matrix.

Four facts have to agree, and the CI matrix has to exercise them:

* ``requires-python`` in pyproject.toml is the floor the package admits.
* ``# production-python: X.Y`` in constraints.txt is the operator's declaration
  of the interpreter production runs. It moves only with a deploy, the same
  rule as the pins in that file.
* the Dockerfile base image (``FROM python:X.Y-…``) is the container
  deployment's interpreter and is Dependabot-maintained; it must equal the
  marker, so a base-image bump cannot drift away from the declaration.
* any LaunchAgent template under scripts/ops that hardcodes a framework
  interpreter (``/Library/Frameworks/Python.framework/Versions/X.Y/…``) must
  equal the marker as well.
* the ``python-version`` matrix of the ``test_shard`` job in
  .github/workflows/tests.yml must include the floor and the production
  interpreter, include nothing below the floor, and the job's setup-python
  step must actually consume the matrix (a literal there would run every leg
  on one interpreter while the matrix still reads as covered).

Every read failure is a failed check, never a passed one. Until 2026-09-02 only
the floor rule existed, the matrix was read by a bare regex over the whole
file, and the pytest suite ran on 3.12 alone while every deployment surface was
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
DOCKERFILE_PATH = PROJECT_ROOT / "Dockerfile"
OPS_TEMPLATES_DIR = PROJECT_ROOT / "scripts" / "ops"

SHARD_JOB_ID = "test_shard"
MATRIX_EXPRESSION = "${{ matrix.python-version }}"

# A trailing comment after the version is allowed; the marker is a line in a
# file that is otherwise all prose.
PRODUCTION_PYTHON_RE = re.compile(r"^#\s*production-python:\s*(\d+\.\d+)(?=\s|$)", re.M)
DOCKER_FROM_RE = re.compile(r"^FROM\s+python:(\d+\.\d+)(?![\d])", re.M)
TEMPLATE_INTERPRETER_RE = re.compile(
    r"/Library/Frameworks/Python\.framework/Versions/(\d+\.\d+)/"
)

Version = tuple[int, int]


class ContractError(ValueError):
    """An input could not be read or does not carry the fact it should."""


def _parse_version_tuple(version_text: str) -> Version:
    major_text, minor_text = version_text.split(".", 1)
    return int(major_text), int(minor_text)


def _render(version: Version) -> str:
    return f"{version[0]}.{version[1]}"


def _read_requires_python() -> Version:
    pyproject_data = tomllib.loads(PYPROJECT_PATH.read_text())
    requires_python = pyproject_data["project"]["requires-python"].strip()

    match = re.match(r"^>=\s*(\d+\.\d+)$", requires_python)
    if not match:
        raise ContractError(
            f"Unsupported requires-python format: {requires_python!r} "
            "(expected exact form like '>=3.12')"
        )
    return _parse_version_tuple(match.group(1))


def _read_production_python() -> Version:
    """The interpreter production runs, declared once in constraints.txt."""
    matches = PRODUCTION_PYTHON_RE.findall(CONSTRAINTS_PATH.read_text())
    if len(matches) != 1:
        raise ContractError(
            "constraints.txt must declare exactly one '# production-python: X.Y' "
            f"marker (found {len(matches)})"
        )
    return _parse_version_tuple(matches[0])


def _read_dockerfile_python() -> Version:
    """The base-image interpreter, from every python FROM line (they must agree)."""
    found = {_parse_version_tuple(m) for m in DOCKER_FROM_RE.findall(DOCKERFILE_PATH.read_text())}
    if not found:
        raise ContractError(f"{DOCKERFILE_PATH.name} has no 'FROM python:X.Y…' line")
    if len(found) > 1:
        raise ContractError(
            f"{DOCKERFILE_PATH.name} names more than one python base image: "
            f"{sorted(_render(v) for v in found)}"
        )
    return found.pop()


def _read_template_pythons() -> dict[str, Version]:
    """Framework interpreters hardcoded in ops LaunchAgent templates, by file."""
    versions: dict[str, Version] = {}
    for template in sorted(OPS_TEMPLATES_DIR.glob("*.plist.template")):
        found = {
            _parse_version_tuple(m) for m in TEMPLATE_INTERPRETER_RE.findall(template.read_text())
        }
        if len(found) > 1:
            raise ContractError(
                f"{template.name} hardcodes more than one framework interpreter: "
                f"{sorted(_render(v) for v in found)}"
            )
        if found:
            versions[template.name] = found.pop()
    return versions


def _read_ci_matrix_versions() -> list[Version]:
    """The test_shard matrix, from the parsed workflow, once its consumer is verified."""
    import yaml  # PyYAML is a core dependency (pyproject); the smoke job has it.

    workflow = yaml.safe_load(TEST_WORKFLOW_PATH.read_text())
    try:
        job = workflow["jobs"][SHARD_JOB_ID]
        raw_versions = job["strategy"]["matrix"]["python-version"]
        steps = job["steps"]
    except (KeyError, TypeError) as exc:
        raise ContractError(
            f"tests.yml has no jobs.{SHARD_JOB_ID}.strategy.matrix.python-version ({exc!r})"
        ) from exc
    if not isinstance(raw_versions, list) or not raw_versions:
        raise ContractError(f"jobs.{SHARD_JOB_ID} python-version matrix must be a non-empty list")

    versions: list[Version] = []
    for entry in raw_versions:
        text = str(entry).strip()
        if not re.match(r"^\d+\.\d+$", text):
            raise ContractError(f"Unsupported matrix version format: {entry!r}")
        versions.append(_parse_version_tuple(text))

    consumers = [
        step for step in steps
        if isinstance(step, dict) and str(step.get("uses", "")).startswith("actions/setup-python")
    ]
    if not consumers:
        raise ContractError(f"jobs.{SHARD_JOB_ID} has no actions/setup-python step")
    for step in consumers:
        requested = str((step.get("with") or {}).get("python-version", "")).strip()
        if requested != MATRIX_EXPRESSION:
            raise ContractError(
                f"jobs.{SHARD_JOB_ID}'s setup-python step requests {requested!r}, not "
                f"{MATRIX_EXPRESSION!r}: the matrix would be declared but not run"
            )
    return versions


def main() -> int:
    try:
        required_min = _read_requires_python()
        production = _read_production_python()
        dockerfile = _read_dockerfile_python()
        templates = _read_template_pythons()
        ci_versions = _read_ci_matrix_versions()
    except (ContractError, ImportError, KeyError, OSError, ValueError) as exc:
        # Fail toward "unknown", never toward "healthy": an input that cannot
        # be read is a failed check, not a passed one.
        print(f"❌ Could not read the interpreter contract: {exc}")
        return 1

    too_low = [v for v in ci_versions if v < required_min]
    if too_low:
        print(
            "❌ CI matrix includes versions lower than requires-python: "
            f"{[_render(v) for v in too_low]} < {_render(required_min)}"
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

    if dockerfile != production:
        print(
            f"❌ Dockerfile base image is python:{_render(dockerfile)} but constraints.txt "
            f"declares production-python {_render(production)}; move them together, "
            "with the deploy"
        )
        return 1

    mismatched = {name: v for name, v in templates.items() if v != production}
    if mismatched:
        rendered = ", ".join(f"{name}={_render(v)}" for name, v in sorted(mismatched.items()))
        print(
            "❌ scripts/ops templates hardcode a framework interpreter that is not the "
            f"declared production-python {_render(production)}: {rendered}"
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
        "✅ Interpreter contract holds: "
        f"floor {_render(required_min)}, production {_render(production)} "
        f"(constraints, Dockerfile, {len(templates)} ops template(s)), "
        f"CI matrix [{rendered}] consumed by setup-python"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

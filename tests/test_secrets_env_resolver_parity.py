"""Runtime helpers select only an explicit secrets file, independent of cwd.

The installer may scaffold a neutral template, but creating or discovering a
file does not authorize any runtime to source it. Legacy paths remain useful
regression fixtures: they can be selected explicitly and are otherwise ignored.
All credential values and homes here are synthetic. No live helper is invoked.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NEUTRAL_REL = Path(".config/unitares/secrets.env")
LEGACY_REL = Path(".config/cirwel/secrets.env")

# Files to create, explicit selector (relative to the fake home), selected path.
SCENARIOS = {
    "env_set": ([NEUTRAL_REL, LEGACY_REL, Path("custom/secrets.env")],
                Path("custom/secrets.env"), Path("custom/secrets.env")),
    "env_set_missing_file": ([LEGACY_REL], Path("custom/absent.env"), Path("custom/absent.env")),
    "explicit_legacy": ([NEUTRAL_REL, LEGACY_REL], LEGACY_REL, LEGACY_REL),
    "neutral_only_unselected": ([NEUTRAL_REL], None, None),
    "legacy_only_unselected": ([LEGACY_REL], None, None),
    "both_unselected": ([NEUTRAL_REL, LEGACY_REL], None, None),
    "neither_unselected": ([], None, None),
}


def _prepare(tmp_path, monkeypatch, scenario):
    files, env_rel, expected_rel = SCENARIOS[scenario]
    home = tmp_path / "home"
    home.mkdir()
    for rel in files:
        target = home / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("LEASE_FORCE_RELEASE_TOKEN=fixture-file-token\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LEASE_FORCE_RELEASE_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)  # Repository lookup must not depend on cwd.
    if env_rel is None:
        monkeypatch.delenv("UNITARES_SECRETS_ENV", raising=False)
    else:
        monkeypatch.setenv("UNITARES_SECRETS_ENV", str(home / env_rel))
    return home, home / expected_rel if expected_rel is not None else None


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # Dataclasses need the module registered.
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module


def _doctor():
    return _load(REPO / "scripts/dev/unitares_doctor.py", "_parity_doctor").resolve_secrets_file()


def _deprecate():
    return _load(REPO / "scripts/dev/lease_plane_deprecate.py", "_parity_deprecate")._secrets_env_path()


def _sentinel():
    mod = _load(REPO / "scripts/ops/sentinel-plist.py", "_parity_sentinel")
    return mod._selected_secrets_file(dict(os.environ))


def _integration_helpers():
    # Exercise the credential helpers without importing the integration module:
    # its module-level router probe would contact a real local service.
    path = REPO / "tests/test_lease_plane_force_release_contract.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name in {"_secrets_path", "_read_secrets_var"}]
    assert len(functions) == 2
    namespace = {"os": os, "sys": sys, "Path": Path, "project_root": REPO}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace


PYTHON_RESOLVERS = {
    "doctor": _doctor,
    "deprecation_cli": _deprecate,
    "sentinel_plist": _sentinel,
    "integration_fixture": lambda: _integration_helpers()["_secrets_path"](),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("resolver", sorted(PYTHON_RESOLVERS))
def test_runtime_python_resolvers_agree(tmp_path, monkeypatch, resolver, scenario):
    home, expected = _prepare(tmp_path, monkeypatch, scenario)
    assert Path.home() == home
    assert PYTHON_RESOLVERS[resolver]() == expected


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_installer_destination_is_separate_from_runtime_selection(tmp_path, monkeypatch, scenario):
    home, selected = _prepare(tmp_path, monkeypatch, scenario)
    setup = _load(REPO / "scripts/install/setup.py", "_parity_setup")
    assert setup.resolve_secrets_path(home) == (selected or home / NEUTRAL_REL)
    if selected is None:
        assert all(resolve() is None for resolve in PYTHON_RESOLVERS.values())


@pytest.mark.parametrize("reader", ["deprecation_cli", "integration_fixture"])
def test_direct_environment_token_needs_no_file(tmp_path, monkeypatch, reader):
    _prepare(tmp_path, monkeypatch, "both_unselected")
    monkeypatch.setenv("LEASE_FORCE_RELEASE_TOKEN", "fixture-direct-token")
    if reader == "deprecation_cli":
        actual = _load(REPO / "scripts/dev/lease_plane_deprecate.py", "_parity_token")._read_force_release_token()
    else:
        actual = _integration_helpers()["_read_secrets_var"]("LEASE_FORCE_RELEASE_TOKEN")
    assert actual == "fixture-direct-token"


@pytest.mark.parametrize("scenario", ["both_unselected", "env_set", "env_set_missing_file", "explicit_legacy"])
@pytest.mark.parametrize("reader", ["deprecation_cli", "integration_fixture"])
def test_token_readers_read_only_selected_file(tmp_path, monkeypatch, reader, scenario):
    _, selected = _prepare(tmp_path, monkeypatch, scenario)
    if reader == "deprecation_cli":
        actual = _load(REPO / "scripts/dev/lease_plane_deprecate.py", "_parity_token")._read_force_release_token()
    else:
        actual = _integration_helpers()["_read_secrets_var"]("LEASE_FORCE_RELEASE_TOKEN")
    expected = "fixture-file-token" if selected is not None and selected.exists() else None
    assert actual == expected


# --- shell copies: execute only selection/validation, never service startup ---

_SHELL_ASSIGN = re.compile(r'^(?P<var>SECRETS_FILE|SECRETS)="\$\{UNITARES_SECRETS_ENV:-\}"$', re.MULTILINE)


def _shell_resolver_blocks():
    blocks = {}
    skip_parts = {"deps", "_build", "node_modules", ".venv"}
    for path in sorted([*REPO.glob("elixir/**/*.sh"), *REPO.glob("scripts/**/*.sh")]):
        if not path.is_file() or skip_parts & set(path.relative_to(REPO).parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        match = _SHELL_ASSIGN.search(text)
        if match:
            lines = text[match.start():].splitlines()
            end = next(i for i, line in enumerate(lines) if line.strip() == "fi")
            blocks[str(path.relative_to(REPO))] = (match.group("var"), "\n".join(lines[:end + 1]))
    return blocks


SHELL_BLOCKS = _shell_resolver_blocks()


def test_every_beam_start_script_carries_explicit_selection():
    starts = {str(p.relative_to(REPO)) for p in REPO.glob("elixir/*/scripts/start.sh")}
    assert len(starts) >= 5
    assert starts <= SHELL_BLOCKS.keys(), f"start.sh without explicit selector: {starts - SHELL_BLOCKS.keys()}"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
@pytest.mark.parametrize("script", sorted(SHELL_BLOCKS))
def test_shell_selection_ignores_unselected_files(tmp_path, monkeypatch, script, scenario):
    home, expected = _prepare(tmp_path, monkeypatch, scenario)
    var, block = SHELL_BLOCKS[script]
    env = {"HOME": str(home), "PATH": os.defpath}
    if expected is not None:
        env["UNITARES_SECRETS_ENV"] = str(expected)
    result = subprocess.run(
        ["bash", "-c", f'set -euo pipefail\n{block}\nprintf "%s" "${var}"\n'],
        env=env, cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    if expected is not None and not expected.exists() and "absolute, readable regular file" in block:
        assert result.returncode == 1
        assert "absolute, readable regular file" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == (str(expected) if expected is not None else "")

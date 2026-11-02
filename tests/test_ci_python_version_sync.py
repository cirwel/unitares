"""The early CI check must enforce the declared floor, not an obsolete literal."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def version_check(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "ci_python_version_sync", ROOT / "scripts/dev/check_ci_python_version_sync.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.14"\n')
    (tmp_path / ".python-version").write_text("3.15\n")
    # Keep the real interpreter and pytest process untouched.
    module.sys = SimpleNamespace(version_info=(3, 13, 0), version="3.13.0")
    return module


def test_unsupported_interpreter_is_rejected_before_dev_default_warning(
    version_check, capsys
):
    assert version_check.main() == 1
    output = capsys.readouterr().out
    assert "Python 3.14+ required" in output
    assert "passed" not in output


def test_supported_floor_is_allowed_beside_newer_dev_default(version_check, capsys):
    class Version(tuple):
        major = 3
        minor = 14

    version_check.sys.version_info = Version((3, 14, 8))
    assert version_check.main() == 0
    output = capsys.readouterr().out
    assert "WARNING: .python-version says 3.15, running 3.14" in output
    assert "Python version check passed" in output


def test_floor_changes_are_read_from_package_metadata(version_check, capsys):
    (version_check.PROJECT_ROOT / "pyproject.toml").write_text(
        '[project]\nrequires-python = ">=3.15"\n'
    )
    version_check.sys.version_info = (3, 14, 8)
    assert version_check.main() == 1
    assert "Python 3.15+ required" in capsys.readouterr().out


@pytest.mark.parametrize(
    "metadata", [None, "invalid toml", '[project]\nrequires-python = "~=3.14"']
)
def test_unreadable_or_unsupported_floor_fails_closed(version_check, capsys, metadata):
    path = version_check.PROJECT_ROOT / "pyproject.toml"
    if metadata is None:
        path.unlink()
    else:
        path.write_text(metadata)
    assert version_check.main() == 1
    assert "cannot read the package Python floor" in capsys.readouterr().out

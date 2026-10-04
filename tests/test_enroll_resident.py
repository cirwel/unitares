"""S19: enrollment CLI argument validation and warning emission.

Tests the pure-validation paths of ``scripts/ops/enroll_resident.py`` (UUID,
label, executable validators) and the ``_emit_user_writable_warning`` helper.
DB-touching paths are exercised via the CLI's ``--dry-run`` flag, which
validates input and emits the warning without touching PostgreSQL — this
keeps the unit tests dependency-free.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

# Load the CLI module by file path since `scripts/ops/` is not a package.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = PROJECT_ROOT / "scripts" / "ops" / "enroll_resident.py"

_spec = importlib.util.spec_from_file_location("enroll_resident", CLI_PATH)
assert _spec and _spec.loader
enroll_resident = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(enroll_resident)


# -----------------------------------------------------------------------------
# Argument validators
# -----------------------------------------------------------------------------


def test_validate_uuid_accepts_canonical_form() -> None:
    canonical = "f92dcea8-4786-412a-a0eb-362c273382f5"
    assert enroll_resident._validate_uuid(canonical) == canonical


def test_validate_uuid_rejects_non_uuid() -> None:
    with pytest.raises(argparse.ArgumentTypeError, match="valid UUID"):
        enroll_resident._validate_uuid("not-a-uuid")


def test_validate_label_accepts_unitares_residents() -> None:
    for label in (
        "com.unitares.sentinel",
        "com.unitares.vigil",
        "com.unitares.chronicler",
    ):
        assert enroll_resident._validate_label(label) == label


def test_validate_label_rejects_non_reverse_dns() -> None:
    for bad in ("Sentinel", "com unitares sentinel", "", "no-dot-here"):
        with pytest.raises(argparse.ArgumentTypeError, match="reverse-DNS"):
            enroll_resident._validate_label(bad)


def test_validate_executable_requires_absolute_path(tmp_path: Path) -> None:
    rel = "./relative_binary"
    with pytest.raises(argparse.ArgumentTypeError, match="absolute"):
        enroll_resident._validate_executable(rel)


def test_validate_executable_requires_existing_file(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(argparse.ArgumentTypeError, match="does not exist"):
        enroll_resident._validate_executable(str(missing))


def test_validate_executable_requires_executable_bit(tmp_path: Path) -> None:
    not_x = tmp_path / "binary"
    not_x.write_bytes(b"")
    not_x.chmod(0o644)  # no x bit
    with pytest.raises(argparse.ArgumentTypeError, match="not executable"):
        enroll_resident._validate_executable(str(not_x))


def test_validate_executable_accepts_real_binary(tmp_path: Path) -> None:
    binary = tmp_path / "binary"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    result = enroll_resident._validate_executable(str(binary))
    assert result == str(binary.resolve())


# -----------------------------------------------------------------------------
# Warning emission
# -----------------------------------------------------------------------------


def _fake_framework(tmp_path: Path) -> tuple[Path, Path]:
    version = tmp_path / "Python.framework" / "Versions" / "3.14"
    launcher = version / "bin" / "python3.14"
    app = version / "Resources" / "Python.app" / "Contents" / "MacOS" / "Python"
    for binary in (launcher, app):
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
    (version / "bin" / "python3").symlink_to("python3.14")
    return version / "bin" / "python3", app


def test_validate_executable_maps_framework_launcher_to_app_binary(tmp_path: Path) -> None:
    """proc_pidpath reports Python.app for a framework launcher; enroll that."""
    launcher, app = _fake_framework(tmp_path)
    assert enroll_resident._validate_executable(str(launcher)) == str(app.resolve())


def test_validate_executable_leaves_non_framework_python_alone(tmp_path: Path) -> None:
    binary = tmp_path / "opt" / "bin" / "python3"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    assert enroll_resident._validate_executable(str(binary)) == str(binary.resolve())


def test_validate_executable_keeps_launcher_when_app_binary_missing(tmp_path: Path) -> None:
    launcher, app = _fake_framework(tmp_path)
    app.unlink()
    assert enroll_resident._validate_executable(str(launcher)) == str(launcher.resolve())


def test_validate_executable_rejects_missing_launcher_inside_framework(tmp_path: Path) -> None:
    """A typo'd launcher must not be remapped to a real app binary and enrolled."""
    launcher, _app = _fake_framework(tmp_path)
    typo = launcher.parent / "python-typo"
    with pytest.raises(argparse.ArgumentTypeError, match="does not exist"):
        enroll_resident._validate_executable(str(typo))


def test_emit_warning_fires_on_user_writable_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = tmp_path / "binary"
    binary.write_bytes(b"")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    is_writable = enroll_resident._emit_user_writable_warning(str(binary))
    captured = capsys.readouterr()

    assert is_writable is True
    assert "DEPLOYMENT-RISK WARNING" in captured.err
    assert "same-UID-writable" in captured.err
    assert str(binary) in captured.err


def test_emit_warning_silent_for_root_owned_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Root-owned paths produce no warning and return False."""
    if os.geteuid() == 0:
        pytest.skip("running as root cannot test non-root unwritable path")
    is_writable = enroll_resident._emit_user_writable_warning("/usr/bin/env")
    captured = capsys.readouterr()

    assert is_writable is False
    assert "DEPLOYMENT-RISK WARNING" not in captured.err


# -----------------------------------------------------------------------------
# End-to-end via --dry-run (no DB)
# -----------------------------------------------------------------------------


def test_dry_run_user_writable_returns_one(tmp_path: Path) -> None:
    """--dry-run + user-writable executable + no --allow-user-writable: rc=1."""
    binary = tmp_path / "binary"
    binary.write_bytes(b"")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "--agent-id", "f92dcea8-4786-412a-a0eb-362c273382f5",
            "--launchd-label", "com.unitares.sentinel",
            "--executable", str(binary),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "DEPLOYMENT-RISK WARNING" in result.stderr
    assert "[dry-run]" in result.stderr


def test_dry_run_user_writable_with_allow_returns_zero(tmp_path: Path) -> None:
    """--dry-run + user-writable + --allow-user-writable: warning still fires, rc=0."""
    binary = tmp_path / "binary"
    binary.write_bytes(b"")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "--agent-id", "f92dcea8-4786-412a-a0eb-362c273382f5",
            "--launchd-label", "com.unitares.sentinel",
            "--executable", str(binary),
            "--dry-run",
            "--allow-user-writable",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "DEPLOYMENT-RISK WARNING" in result.stderr  # warning is always written


def test_invalid_uuid_rejected_with_argparse_error(tmp_path: Path) -> None:
    binary = tmp_path / "binary"
    binary.write_bytes(b"")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)

    result = subprocess.run(
        [
            sys.executable,
            str(CLI_PATH),
            "--agent-id", "not-a-uuid",
            "--launchd-label", "com.unitares.sentinel",
            "--executable", str(binary),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
    )
    # argparse exits with code 2 on argument errors.
    assert result.returncode == 2
    assert "valid UUID" in result.stderr

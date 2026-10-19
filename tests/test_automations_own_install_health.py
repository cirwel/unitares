"""The census checks only a deployment's explicit install expectation.

These tests call the read-only check directly, never the real census collectors.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def census_mod(monkeypatch, tmp_path):
    for name in ("UNITARES_AUTOMATION_INSTALL_SOURCE", "UNITARES_AUTOMATION_INSTALL_PATH"):
        monkeypatch.delenv(name, raising=False)
    module_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "ops" / "unitares-automations"
    )
    loader = importlib.machinery.SourceFileLoader("unitares_automations", str(module_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    sys.modules["unitares_automations"] = module
    loader.exec_module(module)
    monkeypatch.setattr(module, "HOME", tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    return module


def _source(tmp_path):
    source = tmp_path / "checkout" / "unitares-automations"
    source.parent.mkdir()
    source.write_text("#!/usr/bin/env python3\n")
    return source


def _installed(tmp_path):
    installed = tmp_path / ".local/bin/unitares-automations"
    installed.parent.mkdir(parents=True)
    return installed


def _warnings(census_mod):
    warnings: list[str] = []
    census_mod.check_own_install_health(warnings)
    return warnings


@pytest.mark.parametrize("kind", ["missing", "copy", "symlink", "broken"])
def test_no_expectation_accepts_any_install_layout(census_mod, tmp_path, kind):
    installed = _installed(tmp_path)
    if kind == "copy":
        installed.write_text("#!/usr/bin/env python3\n")
    elif kind == "symlink":
        installed.symlink_to(_source(tmp_path))
    elif kind == "broken":
        installed.symlink_to(tmp_path / "missing")
    assert _warnings(census_mod) == []


def test_install_path_alone_does_not_enable_check(census_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("UNITARES_AUTOMATION_INSTALL_PATH", str(tmp_path / "missing"))
    assert _warnings(census_mod) == []


def test_copy_is_flagged_when_symlink_is_required(census_mod, tmp_path, monkeypatch):
    source = _source(tmp_path)
    installed = _installed(tmp_path)
    installed.write_bytes(source.read_bytes())
    monkeypatch.setenv("UNITARES_AUTOMATION_INSTALL_SOURCE", str(source))
    assert any("hand-installed copy" in warning for warning in _warnings(census_mod))
    assert installed.read_bytes() == source.read_bytes()
    assert not installed.is_symlink()


def test_expected_symlink_is_silent(census_mod, tmp_path, monkeypatch):
    source = _source(tmp_path)
    _installed(tmp_path).symlink_to(source)
    monkeypatch.setenv("UNITARES_AUTOMATION_INSTALL_SOURCE", str(source))
    assert _warnings(census_mod) == []


def test_different_target_is_flagged_without_repair(census_mod, tmp_path, monkeypatch):
    source = _source(tmp_path)
    wrong_target = tmp_path / "old-tool"
    wrong_target.write_text("old tool\n")
    installed = _installed(tmp_path)
    installed.symlink_to(wrong_target)
    monkeypatch.setenv("UNITARES_AUTOMATION_INSTALL_SOURCE", str(source))
    assert any("not the configured source" in warning for warning in _warnings(census_mod))
    assert installed.resolve() == wrong_target


@pytest.mark.parametrize("broken", [False, True])
def test_missing_or_broken_expected_install_is_flagged(census_mod, tmp_path, monkeypatch, broken):
    source = _source(tmp_path)
    installed = _installed(tmp_path)
    if broken:
        installed.symlink_to(tmp_path / "absent-source")
    monkeypatch.setenv("UNITARES_AUTOMATION_INSTALL_SOURCE", str(source))
    assert any("missing or a broken symlink" in warning for warning in _warnings(census_mod))
    assert not installed.exists()


def test_missing_expected_source_is_flagged(census_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("UNITARES_AUTOMATION_INSTALL_SOURCE", str(tmp_path / "absent"))
    assert any("configured install source" in warning for warning in _warnings(census_mod))


def test_custom_install_paths_expand_home(census_mod, tmp_path, monkeypatch):
    source = _source(tmp_path)
    installed = tmp_path / "bin" / "census"
    installed.parent.mkdir()
    installed.symlink_to(source)
    monkeypatch.setenv("UNITARES_AUTOMATION_INSTALL_SOURCE", "~/checkout/unitares-automations")
    monkeypatch.setenv("UNITARES_AUTOMATION_INSTALL_PATH", "~/bin/census")
    assert _warnings(census_mod) == []

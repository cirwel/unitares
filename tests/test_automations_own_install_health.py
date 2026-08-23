"""Tests for the census's self-check on its own PATH install.

`~/.local/bin/unitares-automations` must stay a symlink to this repo's
`scripts/ops/unitares-automations`, never a hand-installed copy -- a copy
silently drifts behind every fix landed here with no measured symptom to
surface it (found 2026-08-22, three commits / 89 changed lines behind,
undetected for days). The check lives in the tool's own collection pass
rather than a separate watchdog, so these tests exercise it directly.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture()
def census_mod():
    module_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "ops" / "unitares-automations"
    )
    loader = importlib.machinery.SourceFileLoader("unitares_automations", str(module_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    sys.modules["unitares_automations"] = module
    loader.exec_module(module)
    return module


def test_no_install_present_is_silent(census_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(census_mod, "HOME", tmp_path / "empty-home")
    warnings: list[str] = []
    census_mod.check_own_install_health(warnings)
    assert warnings == []


def test_hand_installed_copy_is_flagged(census_mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    installed = home / ".local/bin/unitares-automations"
    installed.parent.mkdir(parents=True)
    installed.write_text("#!/usr/bin/env python3\n")
    installed.chmod(0o755)
    monkeypatch.setattr(census_mod, "HOME", home)
    warnings: list[str] = []
    census_mod.check_own_install_health(warnings)
    assert any("hand-installed copy, not a symlink" in w for w in warnings), warnings


def test_symlink_to_canonical_is_silent(census_mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    canonical = home / ".local/share/cirwel/unitares-ops/scripts/ops/unitares-automations"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("#!/usr/bin/env python3\n")
    canonical.chmod(0o755)
    installed = home / ".local/bin/unitares-automations"
    installed.parent.mkdir(parents=True)
    installed.symlink_to(canonical)
    monkeypatch.setattr(census_mod, "HOME", home)
    warnings: list[str] = []
    census_mod.check_own_install_health(warnings)
    assert warnings == []


def test_symlink_to_somewhere_else_is_flagged(census_mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    canonical = home / ".local/share/cirwel/unitares-ops/scripts/ops/unitares-automations"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("#!/usr/bin/env python3\n")
    wrong_target = home / "elsewhere" / "unitares-automations"
    wrong_target.parent.mkdir(parents=True)
    wrong_target.write_text("#!/usr/bin/env python3\n")
    installed = home / ".local/bin/unitares-automations"
    installed.parent.mkdir(parents=True)
    installed.symlink_to(wrong_target)
    monkeypatch.setattr(census_mod, "HOME", home)
    warnings: list[str] = []
    census_mod.check_own_install_health(warnings)
    assert any("symlinks to" in w and "not the canonical" in w for w in warnings), warnings

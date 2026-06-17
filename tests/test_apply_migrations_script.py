"""Unit tests for scripts/dev/apply_migrations.py.

The script's I/O (psql subprocess calls) touches the live machine, which is
brittle in CI. These tests exercise the pure planning logic — ``compute_plan``,
which decides what is pending vs. drifted — using fake registry dicts.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = REPO_ROOT / "scripts" / "dev"
SCRIPT = SCRIPT_DIR / "apply_migrations.py"


@pytest.fixture(scope="module")
def mod():
    # The script does `from unitares_doctor import ...` at top level; make that
    # resolvable the same way running it as a script (sys.path[0]) would.
    sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("apply_migrations", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["apply_migrations"] = module
    spec.loader.exec_module(module)
    return module


def test_clean_registry_has_no_pending_or_drift(mod):
    expected = {1: "a", 2: "b"}
    actual = {1: "a", 2: "b"}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual)
    assert pending == []
    assert mismatches == []
    assert unexpected == []


def test_pending_is_source_versions_absent_from_db(mod):
    expected = {1: "a", 2: "b", 3: "c"}
    actual = {1: "a", 2: "b"}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual)
    assert pending == [3]
    assert mismatches == []
    assert unexpected == []


def test_pending_is_sorted(mod):
    expected = {1: "a", 5: "e", 3: "c", 2: "b"}
    actual = {1: "a"}
    pending, _, _ = mod.compute_plan(expected, actual)
    assert pending == [2, 3, 5]


def test_name_mismatch_is_flagged_not_pending(mod):
    expected = {1: "a", 2: "renamed"}
    actual = {1: "a", 2: "old_name"}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual)
    assert pending == []
    assert mismatches == [2]
    assert unexpected == []


def test_db_version_with_no_source_file_is_unexpected(mod):
    expected = {1: "a"}
    actual = {1: "a", 99: "mystery"}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual)
    assert unexpected == [99]
    assert pending == []
    assert mismatches == []


def test_known_exception_is_not_reported_unexpected(mod):
    expected = {1: "a"}
    actual = {1: "a", 18: "progress flat telemetry tables"}
    exceptions = {18: "progress flat telemetry tables"}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual, exceptions)
    assert unexpected == []
    assert pending == []
    assert mismatches == []


def test_empty_db_makes_all_source_pending(mod):
    expected = {1: "a", 2: "b"}
    actual: dict[int, str] = {}
    pending, mismatches, unexpected = mod.compute_plan(expected, actual)
    assert pending == [1, 2]
    assert mismatches == []
    assert unexpected == []

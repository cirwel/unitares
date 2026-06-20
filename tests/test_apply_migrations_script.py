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


# ── --check preflight gate ───────────────────────────────────────────────────
# --check must exit non-zero on ANYTHING the DB-is-not-ready-for-this-code: a
# pending migration (which the default dry-run treats as exit 0), a name
# mismatch, or a DB version with no source file. It exits 0 only when fully in
# sync. We drive main() with stubbed registry I/O so no live DB is needed.


def _run_check(mod, monkeypatch, expected, actual):
    monkeypatch.setattr(mod, "_source_schema_migrations", lambda _root: expected)
    monkeypatch.setattr(mod, "query_applied", lambda _url: actual)
    monkeypatch.setattr(mod, "KNOWN_SCHEMA_MIGRATION_EXCEPTIONS", {})
    return mod.main(["--check", "--db-url", "postgresql://x/y"])


def test_check_passes_when_in_sync(mod, monkeypatch):
    assert _run_check(mod, monkeypatch, {1: "a", 2: "b"}, {1: "a", 2: "b"}) == 0


def test_check_fails_on_pending(mod, monkeypatch):
    # The default dry-run returns 0 here; --check must block instead.
    assert _run_check(mod, monkeypatch, {1: "a", 2: "b"}, {1: "a"}) == 1


def test_check_fails_on_name_mismatch(mod, monkeypatch):
    assert _run_check(mod, monkeypatch, {1: "a", 2: "renamed"}, {1: "a", 2: "old"}) == 1


def test_check_fails_on_unexpected_db_version(mod, monkeypatch):
    assert _run_check(mod, monkeypatch, {1: "a"}, {1: "a", 99: "mystery"}) == 1

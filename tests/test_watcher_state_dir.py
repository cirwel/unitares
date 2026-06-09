"""Tests for the checkout-independent Watcher state dir + legacy migration.

Regression coverage for the dashboard-zeroes bug: the Watcher agent (writer)
ran from the dev checkout while http_api (reader) ran from the deploy worktree,
so each resolved a different ``data/watcher`` and the panel read zeroes. The
fix anchors state under ``~/.unitares`` (or ``UNITARES_WATCHER_DATA_DIR``) so
writer and reader always agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import agents.watcher._util as util


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Each test starts with the module path cache and migration flag clear."""
    monkeypatch.setattr(util, "_state_dir_cache", None)
    monkeypatch.setattr(util, "_legacy_migration_done", False)
    monkeypatch.delenv("UNITARES_WATCHER_DATA_DIR", raising=False)
    yield


class TestResolution:
    def test_default_is_home_anchored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert util.watcher_state_dir() == tmp_path / ".unitares" / "watcher"

    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(tmp_path / "custom"))
        assert util.watcher_state_dir() == tmp_path / "custom"

    def test_env_override_expands_user(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", "~/elsewhere")
        assert util.watcher_state_dir() == tmp_path / "elsewhere"

    def test_result_is_cached(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(tmp_path / "a"))
        first = util.watcher_state_dir()
        # Changing the env after first resolution must not move the dir.
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(tmp_path / "b"))
        assert util.watcher_state_dir() == first

    def test_resolution_has_no_filesystem_side_effects(self, monkeypatch, tmp_path):
        target = tmp_path / "never_created"
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(target))
        util.watcher_state_dir()
        assert not target.exists()


class TestMigration:
    def _point_legacy(self, monkeypatch, legacy: Path, target: Path):
        monkeypatch.setattr(util, "_LEGACY_STATE_DIR", legacy)
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(target))

    def test_copies_existing_state_files(self, monkeypatch, tmp_path):
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "findings.jsonl").write_text('{"pattern":"P001"}\n')
        (legacy / "dedup.json").write_text("{}")
        target = tmp_path / "home" / "watcher"
        self._point_legacy(monkeypatch, legacy, target)

        util.migrate_legacy_watcher_state()

        assert (target / "findings.jsonl").read_text() == '{"pattern":"P001"}\n'
        assert (target / "dedup.json").read_text() == "{}"

    def test_does_not_delete_source(self, monkeypatch, tmp_path):
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "findings.jsonl").write_text("x\n")
        target = tmp_path / "home" / "watcher"
        self._point_legacy(monkeypatch, legacy, target)

        util.migrate_legacy_watcher_state()

        assert (legacy / "findings.jsonl").exists()

    def test_does_not_clobber_existing_target(self, monkeypatch, tmp_path):
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "findings.jsonl").write_text("OLD\n")
        target = tmp_path / "home" / "watcher"
        target.mkdir(parents=True)
        (target / "findings.jsonl").write_text("NEW\n")
        self._point_legacy(monkeypatch, legacy, target)

        util.migrate_legacy_watcher_state()

        assert (target / "findings.jsonl").read_text() == "NEW\n"

    def test_noop_when_legacy_absent(self, monkeypatch, tmp_path):
        legacy = tmp_path / "does_not_exist"
        target = tmp_path / "home" / "watcher"
        self._point_legacy(monkeypatch, legacy, target)

        util.migrate_legacy_watcher_state()

        assert not target.exists()

    def test_runs_filesystem_work_once_per_process(self, monkeypatch, tmp_path):
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "findings.jsonl").write_text("v1\n")
        target = tmp_path / "home" / "watcher"
        self._point_legacy(monkeypatch, legacy, target)

        util.migrate_legacy_watcher_state()
        # A later legacy change is ignored — migration already ran this process.
        (legacy / "dedup.json").write_text("{}")
        util.migrate_legacy_watcher_state()

        assert not (target / "dedup.json").exists()

"""The reader's side of the Watcher state-location contract.

Core does not ship or import a Watcher. A deployment that runs one writes
``findings.jsonl`` under ``~/.unitares/watcher`` (or
``UNITARES_WATCHER_DATA_DIR``), and a deployment-owned route pack reads it
through ``src.watcher_state_reader``. These tests pin what that module promises
to such a pack; the writer keeps its own tests wherever it lives.
"""

from __future__ import annotations

import pytest

import src.watcher_state_reader as reader


@pytest.fixture(autouse=True)
def _reset_reader(monkeypatch, tmp_path):
    monkeypatch.setattr(reader, "_state_dir_cache", None)
    monkeypatch.setattr(reader, "_legacy_migration_done", False)
    monkeypatch.setattr(reader, "_LEGACY_STATE_DIR", tmp_path / "legacy-absent")
    monkeypatch.delenv("UNITARES_WATCHER_DATA_DIR", raising=False)
    yield


class TestResolution:
    def test_default_is_home_anchored(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert reader.watcher_state_dir() == tmp_path / ".unitares" / "watcher"

    def test_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(tmp_path / "custom"))
        assert reader.watcher_state_dir() == tmp_path / "custom"

    def test_env_override_expands_user(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", "~/elsewhere")
        assert reader.watcher_state_dir() == tmp_path / "elsewhere"

    def test_resolution_has_no_filesystem_side_effects(self, monkeypatch, tmp_path):
        target = tmp_path / "never_created"
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(target))
        reader.watcher_state_dir()
        assert not target.exists()


class TestFindingsPath:
    def test_reads_the_shared_file_when_it_has_data(self, monkeypatch, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "findings.jsonl").write_text('{"fingerprint": "a"}\n')
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(shared))
        assert reader.watcher_findings_path() == shared / "findings.jsonl"

    def test_absent_store_resolves_to_the_shared_path(self, monkeypatch, tmp_path):
        """No Watcher deployed: the path is still the shared one, and the
        caller sees a missing file, not an exception."""
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(tmp_path / "none"))
        path = reader.watcher_findings_path()
        assert path == tmp_path / "none" / "findings.jsonl"
        assert not path.exists()

    def test_legacy_file_is_read_while_the_shared_one_is_empty(self, monkeypatch, tmp_path):
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "findings.jsonl").write_text('{"fingerprint": "old"}\n')
        shared = tmp_path / "shared"
        shared.mkdir()
        (shared / "findings.jsonl").write_text("")
        monkeypatch.setattr(reader, "_LEGACY_STATE_DIR", legacy)
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(shared))
        assert reader.watcher_findings_path() == legacy / "findings.jsonl"

    def test_legacy_state_is_copied_once_into_an_empty_shared_dir(self, monkeypatch, tmp_path):
        legacy = tmp_path / "legacy"
        legacy.mkdir()
        (legacy / "findings.jsonl").write_text('{"fingerprint": "old"}\n')
        (legacy / "dedup.json").write_text("{}")
        shared = tmp_path / "shared"
        monkeypatch.setattr(reader, "_LEGACY_STATE_DIR", legacy)
        monkeypatch.setenv("UNITARES_WATCHER_DATA_DIR", str(shared))
        assert reader.watcher_findings_path() == shared / "findings.jsonl"
        assert (shared / "findings.jsonl").read_text() == '{"fingerprint": "old"}\n'
        assert (shared / "dedup.json").exists()
        assert (legacy / "findings.jsonl").exists()

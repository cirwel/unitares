from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "client" / "session_cache.py"


def _run(args: list[str], workspace: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--workspace", str(workspace)],
        text=True,
        capture_output=True,
        check=False,
    )


def test_path_uses_sanitized_slot_for_session_cache(tmp_path: Path) -> None:
    result = _run(["path", "session", "--slot", "../../codex run"], tmp_path)

    assert result.returncode == 0
    assert result.stdout.strip().endswith(".unitares/session-______codex_run.json")


def test_set_session_rejects_slotless_write(tmp_path: Path) -> None:
    result = _run(
        ["set", "session", "--json", '{"uuid": "agent-1"}'],
        tmp_path,
    )

    assert result.returncode == 2
    assert "refusing slotless session write" in result.stderr
    assert not (tmp_path / ".unitares" / "session.json").exists()


def test_set_session_allows_explicit_shared_write(tmp_path: Path) -> None:
    result = _run(
        ["set", "session", "--allow-shared", "--json", '{"uuid": "agent-1"}'],
        tmp_path,
    )

    assert result.returncode == 0
    assert (tmp_path / ".unitares" / "session.json").exists()


def test_set_session_rejects_non_empty_continuity_token(tmp_path: Path) -> None:
    result = _run(
        [
            "set", "session", "--slot", "codex-1",
            "--json", '{"uuid": "agent-1", "continuity_token": "v1.token"}',
        ],
        tmp_path,
    )

    assert result.returncode == 2
    assert "non-empty continuity_token" in result.stderr
    assert not (tmp_path / ".unitares" / "session-codex-1.json").exists()


def test_set_session_allows_empty_token_and_writes_0600(tmp_path: Path) -> None:
    result = _run(
        [
            "set", "session", "--slot", "codex-1", "--stamp",
            "--json", '{"uuid": "agent-1", "continuity_token": "", "schema_version": 2}',
        ],
        tmp_path,
    )

    path = tmp_path / ".unitares" / "session-codex-1.json"
    assert result.returncode == 0
    payload = json.loads(path.read_text())
    assert payload["continuity_token"] == ""
    assert payload["schema_version"] == 2
    assert "updated_at" in payload
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_list_returns_newest_lineage_entries_and_surfaces_flat_legacy(tmp_path: Path) -> None:
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    (cache_dir / "session.json").write_text(json.dumps({
        "uuid": "legacy-agent",
        "updated_at": "2026-04-15T00:00:00+00:00",
    }))

    older = _run(
        ["set", "session", "--slot", "older", "--json", '{"uuid": "older-agent"}'],
        tmp_path,
    )
    newer = _run(
        ["set", "session", "--slot", "newer", "--json", '{"uuid": "newer-agent"}'],
        tmp_path,
    )
    assert older.returncode == 0
    assert newer.returncode == 0
    (cache_dir / "session-older.json").write_text(json.dumps({
        "uuid": "older-agent",
        "updated_at": "2026-04-20T00:00:00+00:00",
    }))
    (cache_dir / "session-newer.json").write_text(json.dumps({
        "uuid": "newer-agent",
        "client_session_id": "sid-newer",
        "updated_at": "2026-04-21T00:00:00+00:00",
    }))

    listed = _run(["list"], tmp_path)

    assert listed.returncode == 0
    entries = json.loads(listed.stdout)
    assert [entry["slot"] for entry in entries] == ["newer", "older", None]
    assert entries[0]["parent_agent_id"] == "newer-agent"
    assert entries[0]["prior_client_session_id"] == "sid-newer"
    assert "uuid" not in entries[0]
    assert "client_session_id" not in entries[0]


def test_merge_strips_legacy_token_but_rejects_incoming_token(tmp_path: Path) -> None:
    cache_dir = tmp_path / ".unitares"
    cache_dir.mkdir()
    path = cache_dir / "session-codex-legacy.json"
    path.write_text(json.dumps({
        "uuid": "legacy-agent",
        "continuity_token": "v1.legacy",
    }))

    stamp = _run(
        [
            "set", "session", "--slot", "codex-legacy", "--merge", "--stamp",
            "--json", '{"client_session_id": "sid-legacy"}',
        ],
        tmp_path,
    )
    assert stamp.returncode == 0
    assert "[V1_LEGACY_STRIP]" in stamp.stderr
    cached = json.loads(path.read_text())
    assert cached["uuid"] == "legacy-agent"
    assert cached["client_session_id"] == "sid-legacy"
    assert "continuity_token" not in cached

    rejected = _run(
        [
            "set", "session", "--slot", "codex-legacy", "--merge",
            "--json", '{"continuity_token": "v1.new"}',
        ],
        tmp_path,
    )
    assert rejected.returncode == 2
    assert "non-empty continuity_token" in rejected.stderr


def test_codex_command_docs_do_not_teach_flat_cache_writes() -> None:
    commands_dir = Path(__file__).resolve().parents[1] / "commands"
    docs = {
        path.name: path.read_text(encoding="utf-8")
        for path in commands_dir.glob("*.md")
    }

    for name, text in docs.items():
        assert "session_cache.py get session" not in text, name
        assert "scripts/client/session_cache.py set session --merge --stamp" not in text, name

    start = docs["governance-start.md"]
    assert "session_cache.py list" in start
    assert "set session --slot <client_session_id-or-codex-session-id>" in start
    assert "do not write `continuity_token`" in start

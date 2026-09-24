"""sync-plugin-skills.sh writes the skills fingerprint into the plugin mirror.

unitares does not commit skills/SKILLS_MANIFEST.sha256 (derived data whose
aggregate line made unrelated skill PRs conflict). The plugin's CI still checks
its mirror against a synced manifest, so the sync must produce one, computed
from canonical, and `--check` must treat a missing or wrong one as drift.
These tests run the real script against a throwaway canonical tree and mirror.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("sync-plugin-skills.sh", "skills_manifest.py", "skills_direction_guard.py")

pytestmark = pytest.mark.skipif(
    shutil.which("rsync") is None or shutil.which("git") is None,
    reason="needs rsync and git",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True,
    )


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    canon = tmp_path / "unitares"
    (canon / "scripts" / "dev").mkdir(parents=True)
    for name in SCRIPTS:
        shutil.copy2(REPO_ROOT / "scripts" / "dev" / name, canon / "scripts" / "dev" / name)
    skill = canon / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text('---\nname: demo\nlast_verified: "2026-09-24"\n---\n# Demo\n')
    _git(canon, "init", "-q")
    _git(canon, "add", "skills", "scripts")
    _git(canon, "commit", "-q", "-m", "canon")

    plugin = tmp_path / "plugin"
    (plugin / "skills").mkdir(parents=True)
    (plugin / "skills" / ".keep").write_text("")
    _git(plugin, "init", "-q")
    _git(plugin, "add", "skills")
    _git(plugin, "commit", "-q", "-m", "plugin")
    return canon, plugin


def _sync(canon: Path, plugin: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(canon / "scripts" / "dev" / "sync-plugin-skills.sh"), *args],
        capture_output=True, text=True,
        env={**os.environ, "UNITARES_PLUGIN_REPO": str(plugin)},
    )


def _expected(canon: Path) -> str:
    return subprocess.run(
        ["python3", str(canon / "scripts" / "dev" / "skills_manifest.py")],
        capture_output=True, text=True, check=True,
    ).stdout


def test_sync_writes_the_manifest_into_the_mirror(trees):
    canon, plugin = trees
    result = _sync(canon, plugin)
    assert result.returncode == 0, result.stderr + result.stdout
    written = plugin / "skills" / "SKILLS_MANIFEST.sha256"
    assert written.read_text() == _expected(canon)
    assert (plugin / "skills" / "demo" / "SKILL.md").exists()
    assert not (canon / "skills" / "SKILLS_MANIFEST.sha256").exists()

    check = _sync(canon, plugin, "--check")
    assert check.returncode == 0, check.stdout
    assert "in sync" in check.stdout


def test_check_reports_a_missing_or_stale_manifest(trees):
    canon, plugin = trees
    assert _sync(canon, plugin).returncode == 0
    manifest = plugin / "skills" / "SKILLS_MANIFEST.sha256"

    manifest.unlink()
    check = _sync(canon, plugin, "--check")
    assert check.returncode == 1
    assert "SKILLS_MANIFEST.sha256 is missing or does not match" in check.stdout

    manifest.write_text("# aggregate: 0\n")
    assert _sync(canon, plugin, "--check").returncode == 1


def test_a_stray_canonical_manifest_is_never_mirrored(trees):
    canon, plugin = trees
    (canon / "skills" / "SKILLS_MANIFEST.sha256").write_text("stale junk\n")
    assert _sync(canon, plugin).returncode == 0
    assert (plugin / "skills" / "SKILLS_MANIFEST.sha256").read_text() == _expected(canon)

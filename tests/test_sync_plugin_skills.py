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
SCRIPTS = (
    "sync-plugin-skills.sh", "skills_manifest.py", "skills_direction_guard.py",
    "check_plugin_attestation_rule.py",
)

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
    # The direction guard imports the shared attestation rule from src/.
    (canon / "src").mkdir()
    (canon / "src" / "__init__.py").write_text("")
    shutil.copy2(REPO_ROOT / "src" / "skill_attestations.py", canon / "src" / "skill_attestations.py")
    skill = canon / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text('---\nname: demo\nlast_verified: "2026-09-24"\n---\n# Demo\n')
    _git(canon, "init", "-q")
    _git(canon, "add", "skills", "scripts", "src")
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


# A plugin freshness checker whose attestation rule is the pre-port "newest
# record wins" reading, which canonical's rule disagrees with.
_DRIFTED_CHECKER = """\
import hashlib, json
from pathlib import Path

def skill_text_digest(skill_md):
    return hashlib.sha256(Path(skill_md).read_bytes()).hexdigest()[:16]

def attested_date(skills_dir, name, skill_digest):
    adir = Path(skills_dir) / ".attestations" / name
    for path in sorted(adir.glob("*.json"), reverse=True) if adir.is_dir() else []:
        try:
            data = json.loads(path.read_text())
        except ValueError:
            continue
        if isinstance(data, dict) and isinstance(data.get("source_digests"), dict):
            v = data.get("verified_date")
            return v if isinstance(v, str) and v else None
    return None
"""


# A faithful port of canonical's rule, as the plugin carries it.
_PORTED_CHECKER = """\
import hashlib, json
from pathlib import Path

def skill_text_digest(skill_md):
    return hashlib.sha256(Path(skill_md).read_bytes()).hexdigest()[:16]

def attested_date(skills_dir, name, skill_digest):
    adir = Path(skills_dir) / ".attestations" / name
    records = []
    for path in sorted(adir.glob("*.json"), reverse=True) if adir.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("source_digests"), dict):
            records.append(data)
    certified = [r for r in records if r.get("skill_digest") == skill_digest]
    date = None
    for r in certified or records[:1]:
        v = r.get("verified_date")
        if isinstance(v, str) and v and (date is None or v > date):
            date = v
    return date
"""


def test_a_drifted_plugin_checker_fails_check_but_not_the_sync(trees):
    canon, plugin = trees
    (plugin / "scripts").mkdir()
    (plugin / "scripts" / "_check_freshness.py").write_text(_DRIFTED_CHECKER)

    result = _sync(canon, plugin)
    assert result.returncode == 5, result.stderr + result.stdout
    assert "disagrees with canonical's attestation rule" in result.stderr
    # The skills mirror is still written: the checker port is separate work.
    assert (plugin / "skills" / "SKILLS_MANIFEST.sha256").read_text() == _expected(canon)

    # Mirror in sync, checker drifted: its own exit code, so ship.sh can tell
    # it from mirror drift (which a re-sync fixes and this does not).
    check = _sync(canon, plugin, "--check")
    assert check.returncode == 5
    assert "in sync" in check.stdout
    assert "disagrees with canonical's attestation rule" in check.stderr


def test_apply_mode_checks_the_rule_against_the_mirror_it_wrote(trees):
    # This checker agrees everywhere except on skill "live" once it has
    # attestations, which only the sync itself brings over. A parity result
    # taken before the write would miss it.
    canon, plugin = trees
    live = canon / "skills" / "live"
    live.mkdir()
    (live / "SKILL.md").write_text('---\nname: live\nlast_verified: "2026-09-24"\n---\n# Live\n')
    adir = canon / "skills" / ".attestations" / "live"
    adir.mkdir(parents=True)
    (adir / "20260924T000000000000Z-00000000.json").write_text(
        '{"verified_date": "2026-09-24", "source_digests": {}}'
    )
    _git(canon, "add", "skills")
    _git(canon, "commit", "-q", "-m", "live")
    (plugin / "scripts").mkdir()
    (plugin / "scripts" / "_check_freshness.py").write_text(_PORTED_CHECKER + """
_ported = attested_date
def attested_date(skills_dir, name, skill_digest):
    if name == "live" and (Path(skills_dir) / ".attestations" / name).is_dir():
        return "1999-01-01"
    return _ported(skills_dir, name, skill_digest)
""")

    result = _sync(canon, plugin)
    assert result.returncode == 5, result.stderr + result.stdout
    assert "synced skill live" in result.stderr


def test_a_checker_that_crashes_warns_without_failing(trees):
    canon, plugin = trees
    assert _sync(canon, plugin).returncode == 0
    (plugin / "scripts").mkdir()
    (plugin / "scripts" / "_check_freshness.py").write_text(_PORTED_CHECKER + """
def attested_date(skills_dir, name, skill_digest):
    raise RuntimeError("boom")
""")
    check = _sync(canon, plugin, "--check")
    assert check.returncode == 0, check.stderr
    assert "could not compare" in check.stderr and "RuntimeError: boom" in check.stderr

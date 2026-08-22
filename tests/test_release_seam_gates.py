"""The seam gates: changelog coverage and per-series version drift.

Both scripts read the repository through `git`, so they are exercised the way
CI runs them — as subprocesses against a real (throwaway) repository — rather
than by importing internals and stubbing the parts under test.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COVERAGE = REPO_ROOT / "scripts/ci/changelog_coverage.py"
DRIFT = REPO_ROOT / "scripts/ci/release_series_drift.py"

CHANGELOG_HEAD = "# Changelog\n\n---\n\n## [Unreleased]\n\n_Nothing yet._\n\n---\n\n"


def _run(repo: Path, script: Path, *args: str) -> subprocess.CompletedProcess:
    target = repo / "scripts" / "ci" / script.name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(script.read_text(encoding="utf-8"), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(target), *args],
        capture_output=True, text=True, cwd=repo,
    )


class Repo:
    """A throwaway git repository built commit by commit."""

    def __init__(self, path: Path):
        self.path = path
        self._git("init", "-q", "-b", "master")
        self._git("config", "user.email", "test@example.invalid")
        self._git("config", "user.name", "Test")

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def write(self, relative: str, content: str) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def commit(self, subject: str, files: dict[str, str] | None = None) -> None:
        for relative, content in (files or {}).items():
            self.write(relative, content)
        if not files:
            # Every commit needs content; a marker file keeps subjects distinct
            # without touching any path a series watches.
            self.write("log.txt", subject)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", subject)

    def tag(self, name: str) -> None:
        self._git("tag", "-a", name, "-m", name)


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path)
    r.write("VERSION", "1.0.0\n")
    r.commit("chore: seed", {"VERSION": "1.0.0\n", "docs/CHANGELOG.md": CHANGELOG_HEAD})
    r.tag("v1.0.0")
    return r


def _entry(version: str, body: str) -> str:
    return f"{CHANGELOG_HEAD}## [{version}] - 2026-01-01\n\n{body}\n\n---\n\n## [1.0.0] - 2025-12-01\n\n_seed_\n"


# --- changelog coverage -----------------------------------------------------


def test_skips_when_the_version_is_already_tagged(repo: Repo):
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 0
    assert "already tagged" in result.stdout


def test_skips_when_no_entry_exists_for_the_pending_version(repo: Repo):
    repo.commit("feat: something (#10)")
    repo.commit("chore: bump", {"VERSION": "1.1.0\n"})
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 0
    assert "not a release PR" in result.stdout


def test_fails_and_names_every_uncited_merge(repo: Repo):
    repo.commit("feat: alpha (#10)")
    repo.commit("fix: beta (#11)")
    repo.commit("fix: gamma (#12)")
    repo.commit("chore: release prep", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "- **things:** alpha only (#10)"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "covers 1/3" in result.stdout
    assert "#11" in result.stdout and "#12" in result.stdout
    assert "#10" not in result.stdout.split("not named in the entry:")[1]


def test_passes_when_every_merge_is_cited(repo: Repo):
    repo.commit("feat: alpha (#10)")
    repo.commit("fix: beta (#11)")
    repo.commit("chore: release prep", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "- **things:** alpha and beta (#10, #11)"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 0
    assert "covers 2/2" in result.stdout


def test_a_declared_exemption_is_honored_and_reported(repo: Repo):
    repo.commit("feat: alpha (#10)")
    repo.commit("chore(deps): internal churn (#11)")
    repo.commit("chore: release prep", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry(
            "1.1.0",
            "<!-- changelog-coverage-exempt: 11 -->\n\n- **things:** alpha (#10)",
        ),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 0
    assert "exempted by declaration: #11" in result.stdout


def test_release_bookkeeping_never_has_to_cite_itself(repo: Repo):
    """A `chore(release)` merge cannot appear in the entry it creates."""
    repo.commit("feat: alpha (#10)")
    repo.commit("chore(release): 1.1.0 version bump and changelog (#11)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "- **things:** alpha (#10)"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 0
    assert "covers 1/1" in result.stdout


def test_list_mode_reports_the_same_gaps_without_failing(repo: Repo):
    repo.commit("feat: alpha (#10)")
    repo.commit("fix: beta (#11)")
    repo.commit("chore: release prep", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "- **things:** alpha (#10)"),
    })
    result = _run(repo.path, COVERAGE, "--list")
    assert result.returncode == 0
    assert "#11" in result.stdout


# --- series drift -----------------------------------------------------------


SDK_PYPROJECT = '[project]\nname = "unitares-sdk"\nversion = "{version}"\n'


def _with_sdk(repo: Repo, version: str) -> None:
    repo.commit("chore: seed sdk", {"agents/sdk/pyproject.toml": SDK_PYPROJECT.format(version=version)})
    repo.tag(f"sdk-v{version}")


def test_clean_when_no_series_has_moved(repo: Repo):
    _with_sdk(repo, "0.1.0")
    repo.commit("docs: unrelated")
    result = _run(repo.path, DRIFT)
    assert result.returncode == 0
    assert "level with its tag" in result.stdout


def test_blocks_a_release_when_the_sdk_moved_without_a_bump(repo: Repo):
    _with_sdk(repo, "0.1.0")
    repo.commit("feat(sdk): new method (#10)", {
        "agents/sdk/pyproject.toml": SDK_PYPROJECT.format(version="0.1.0"),
        "agents/sdk/src/client.py": "def delegate(): ...\n",
    })
    repo.commit("chore: release prep", {"VERSION": "1.1.0\n"})
    result = _run(repo.path, DRIFT)
    assert result.returncode == 1
    assert "unitares-sdk" in result.stdout
    assert "cannot reach these changes" in result.stdout


def test_an_already_bumped_series_is_not_blocking(repo: Repo):
    _with_sdk(repo, "0.1.0")
    repo.commit("feat(sdk): new method (#10)", {
        "agents/sdk/pyproject.toml": SDK_PYPROJECT.format(version="0.2.0"),
    })
    repo.commit("chore: release prep", {"VERSION": "1.1.0\n"})
    result = _run(repo.path, DRIFT)
    assert result.returncode == 0
    assert "already bumped" in result.stdout


def test_drift_is_advisory_off_a_release_tree(repo: Repo):
    """VERSION already tagged means this is ordinary work, not a release."""
    _with_sdk(repo, "0.1.0")
    repo.commit("feat(sdk): new method (#10)", {
        "agents/sdk/pyproject.toml": SDK_PYPROJECT.format(version="0.1.0"),
        "agents/sdk/src/client.py": "def delegate(): ...\n",
    })
    result = _run(repo.path, DRIFT)
    assert result.returncode == 0
    assert "advisory" in result.stdout


def test_a_changed_skills_bundle_asks_for_a_plugin_re_cut(repo: Repo):
    repo.commit("docs(skills): correct margin guidance (#10)", {
        "skills/governance-fundamentals/SKILL.md": "margin is a threshold distance\n",
    })
    repo.commit("chore: release prep", {"VERSION": "1.1.0\n"})
    result = _run(repo.path, DRIFT)
    assert result.returncode == 1
    assert "plugin skills bundle" in result.stdout
    assert "re-cut" in result.stdout

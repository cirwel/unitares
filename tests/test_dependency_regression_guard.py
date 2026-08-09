"""Guard against a change that silently lowers an already-merged dependency.

The motivating case is a Dependabot branch opened before its neighbours merged:
its lockfile predates what master ships, so merging does the advertised bump
AND rolls everything else back. `git merge` reports CLEAN for this — the branch
never touched those lines relative to its own base — so no conflict blocks it.

Observed on #1499, #1502 and #1517 (2026-08). #1517 was the clean-merge variant
and would have reverted 34 packages.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root / "scripts" / "dev"))

from check_dependency_regression import (  # noqa: E402
    find_regressions,
    parse_version,
)


class TestParseVersion:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1.2.3", (1, 2, 3)),
            ("^30.0.1", (30, 0, 1)),  # range operators stripped
            ("~8.2.0", (8, 2, 0)),
            (">=4.1.1", (4, 1, 1)),
            ("  1.0.0  ", (1, 0, 0)),
            ("2.0", (2, 0)),
            ("1.2.3-beta.1", (1, 2, 3)),  # prerelease suffix dropped
            ("1.2.3+build5", (1, 2, 3)),
        ],
    )
    def test_parses_numeric_forms(self, raw, expected):
        assert parse_version(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        ["", "*", "latest", "git+https://example.com/x.git", "workspace:*", None],
    )
    def test_returns_none_for_unparseable(self, raw):
        """Unparseable versions are skipped, never guessed at.

        A false failure on an exotic version string would train people to
        ignore this check, which costs more than the case it would catch.
        """
        assert parse_version(raw) is None

    def test_ordering_is_numeric_not_lexicographic(self):
        """The bug this catches lives in the tens: 30.0.1 -> 30.0.0, 8.2.1 -> 8.1.5."""
        assert parse_version("8.2.1") > parse_version("8.1.5")
        assert parse_version("1.33.0") > parse_version("1.4.0")  # not string order
        assert parse_version("30.0.1") > parse_version("30.0.0")


def _write_lock(path: Path, versions: dict[str, str]) -> None:
    path.write_text(
        json.dumps(
            {
                "lockfileVersion": 3,
                "packages": {
                    "": {"name": "x"},
                    **{f"node_modules/{n}": {"version": v} for n, v in versions.items()},
                },
            }
        )
    )


def _write_manifest(path: Path, deps: dict[str, str]) -> None:
    path.write_text(json.dumps({"name": "x", "devDependencies": deps}))


class TestFindRegressions:
    """`find_regressions` reads base from git and head from the worktree.

    These drive it through a real git repo so the git plumbing is exercised
    rather than mocked — the base-ref read is where a silent failure would
    turn the guard into a no-op that always passes.
    """

    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        import subprocess

        def git(*args):
            subprocess.run(
                ["git", *args], cwd=tmp_path, check=True, capture_output=True
            )

        git("init", "-b", "main")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        (tmp_path / "dashboard").mkdir()
        monkeypatch.chdir(tmp_path)
        return tmp_path, git

    def test_downgrade_is_reported(self, repo):
        path, git = repo
        lock = path / "dashboard" / "package-lock.json"
        _write_lock(lock, {"vite": "8.2.1", "jsdom": "30.0.1"})
        git("add", "-A")
        git("commit", "-m", "base")

        # The stale-base shape: lock regenerated against an older master.
        _write_lock(lock, {"vite": "8.1.5", "jsdom": "30.0.0"})

        found = find_regressions("HEAD", ["dashboard/package-lock.json"])
        by_pkg = {item["package"]: item for item in found}
        assert set(by_pkg) == {"vite", "jsdom"}
        assert by_pkg["vite"]["base"] == "8.2.1"
        assert by_pkg["vite"]["head"] == "8.1.5"

    def test_upgrade_passes(self, repo):
        path, git = repo
        lock = path / "dashboard" / "package-lock.json"
        _write_lock(lock, {"js-yaml": "4.3.0"})
        git("add", "-A")
        git("commit", "-m", "base")

        _write_lock(lock, {"js-yaml": "4.3.1"})

        assert find_regressions("HEAD", ["dashboard/package-lock.json"]) == []

    def test_addition_and_removal_pass(self, repo):
        """Only direction is guarded. Adding or dropping a dep is deliberate."""
        path, git = repo
        lock = path / "dashboard" / "package-lock.json"
        _write_lock(lock, {"keep": "1.0.0", "dropped": "2.0.0"})
        git("add", "-A")
        git("commit", "-m", "base")

        _write_lock(lock, {"keep": "1.0.0", "added": "3.0.0"})

        assert find_regressions("HEAD", ["dashboard/package-lock.json"]) == []

    def test_declared_range_downgrade_is_reported(self, repo):
        """package.json ranges count too — #1517 moved ^30.0.1 back to ^30.0.0."""
        path, git = repo
        manifest = path / "dashboard" / "package.json"
        _write_manifest(manifest, {"jsdom": "^30.0.1"})
        git("add", "-A")
        git("commit", "-m", "base")

        _write_manifest(manifest, {"jsdom": "^30.0.0"})

        found = find_regressions("HEAD", ["dashboard/package.json"])
        assert [item["package"] for item in found] == ["jsdom"]

    def test_missing_manifest_is_not_a_regression(self, repo):
        """Safe on trees without these files — the check must not fail closed."""
        path, git = repo
        (path / "README").write_text("x")
        git("add", "-A")
        git("commit", "-m", "base")

        assert find_regressions("HEAD", ["dashboard/package-lock.json"]) == []

    def test_transitive_packages_are_covered(self, repo):
        """#1517's revert reached 34 packages, mostly transitive.

        A guard that only read direct dependencies would have reported 2 and
        called the rest clean.
        """
        path, git = repo
        lock = path / "dashboard" / "package-lock.json"
        _write_lock(lock, {"rolldown": "1.2.3", "lightningcss": "1.33.0"})
        git("add", "-A")
        git("commit", "-m", "base")

        _write_lock(lock, {"rolldown": "1.1.5", "lightningcss": "1.32.0"})

        found = {item["package"] for item in find_regressions(
            "HEAD", ["dashboard/package-lock.json"]
        )}
        assert found == {"rolldown", "lightningcss"}

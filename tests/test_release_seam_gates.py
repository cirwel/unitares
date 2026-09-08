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

    def merge_pr(self, number: int, subject: str, files: dict[str, str]) -> None:
        """Land a change the way GitHub's "Create a merge commit" button does.

        The suite could not express this before, which is why it passed while
        the gate was blind to thirteen real merges: every fixture was a linear
        chain of squash-shaped subjects, so the tests validated the script
        against the one merge method the script assumed.
        """
        base = self._git("rev-parse", "HEAD")
        self._git("checkout", "-q", "-b", f"pr-{number}")
        self.commit(subject, files)
        self._git("checkout", "-q", "master")
        self._git("merge", "--no-ff", "-q", f"pr-{number}",
                  "-m", f"Merge pull request #{number} from cirwel/pr-{number}")
        assert self._git("rev-parse", "HEAD") != base


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


def test_fails_and_names_every_uncited_merge(repo: Repo):
    repo.commit("feat: alpha (#10)")
    repo.commit("fix: beta (#11)")
    repo.commit("fix: gamma (#12)")
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "- **things:** alpha only (#10)"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "1 of 3 cited" in result.stdout
    assert "#11" in result.stdout and "#12" in result.stdout


def test_passes_when_every_merge_is_cited(repo: Repo):
    repo.commit("feat: alpha (#10)")
    repo.commit("fix: beta (#11)")
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "- **things:** alpha and beta (#10, #11)"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 0
    assert "2 of 2 cited" in result.stdout


def test_a_declared_exemption_with_a_valid_reason_is_honored(repo: Repo):
    repo.commit("feat: alpha (#10)")
    repo.commit("chore(deps): internal churn (#11)")
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry(
            "1.1.0",
            "<!-- changelog-coverage-exempt: #11 no-user-effect -->\n\n- **things:** alpha (#10)",
        ),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 0
    assert "1 declared exempt" in result.stdout


def test_an_exemption_without_a_recognized_reason_is_rejected(repo: Repo):
    repo.commit("feat: alpha (#10)")
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry(
            "1.1.0", "<!-- changelog-coverage-exempt: #10 because-i-said-so -->"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "not one of" in result.stdout


def test_the_script_never_prints_a_paste_ready_exemption(repo: Repo):
    """Printing the waiver made declaring one feel like satisfying the tool."""
    repo.commit("feat: alpha (#10)")
    repo.commit("fix: beta (#11)")
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "_nothing_"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "changelog-coverage-exempt: 10, 11" not in result.stdout
    assert "#NNNN reason" in result.stdout


def test_a_merge_commit_pull_request_is_counted(repo: Repo):
    """The v2.19.0 bug: thirteen merge-commit PRs invisible to the denominator."""
    repo.merge_pr(10, "feat: alpha", {"a.txt": "a\n"})
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "_nothing cited_"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "1 merge-commit" in result.stdout
    assert "#10" in result.stdout


def test_a_merge_commit_pull_request_can_be_cited(repo: Repo):
    repo.merge_pr(10, "feat: alpha", {"a.txt": "a\n"})
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "- **things:** alpha (#10)"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 0
    assert "1 of 1 cited" in result.stdout


def test_an_unattributable_change_fails_rather_than_shrinking_the_denominator(repo: Repo):
    """A direct push carries no reference. Measuring a smaller set is the bug."""
    repo.commit("hotfix pushed straight to master")
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "_nothing to cite_"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "carry no pull request reference" in result.stdout


def test_a_release_tree_with_no_entry_fails_instead_of_skipping(repo: Repo):
    """Previously asserted as a skip — the suite blessed its own false pass."""
    repo.commit("feat: alpha (#10)")
    repo.commit("chore(release): bump (#99)", {"VERSION": "1.1.0\n"})
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "must carry its entry" in result.stderr


def test_a_stray_tag_on_an_unreachable_commit_does_not_stand_the_gate_down(repo: Repo):
    repo.commit("feat: alpha (#10)")
    repo._git("checkout", "-q", "-b", "scratch")
    repo.commit("wip")
    repo.tag("v1.1.0")
    repo._git("checkout", "-q", "master")
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "_nothing_"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "not an ancestor of HEAD" in result.stderr


def test_release_bookkeeping_never_has_to_cite_itself(repo: Repo):
    """A `chore(release)` merge cannot appear in the entry it creates."""
    repo.commit("feat: alpha (#10)")
    repo.commit("chore(release): 1.1.0 version bump and changelog (#11)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "- **things:** alpha (#10)"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 0
    assert "1 of 1 cited" in result.stdout


def _release(repo: Repo, cited: str = "- **things:** alpha (#10)") -> None:
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", cited),
    })


def test_an_inert_dependency_bump_is_excluded_but_counted(repo: Repo):
    """An action pin, a digest and a dev-tooling manifest move nothing a reader
    of the entry can observe; the merge proves it and no line is required."""
    repo.commit("feat: alpha (#10)")
    repo.commit("build(deps): bump actions/checkout from 4 to 5 (#12)",
                {".github/workflows/ci.yml": "uses: actions/checkout@v5\n"})
    repo.commit("build(deps-dev): bump vitest from 4.1.11 to 5.0.0 in /dashboard (#13)",
                {"dashboard/package.json": '{"devDependencies": {"vitest": "5.0.0"}}\n',
                 "dashboard/package-lock.json": "{}\n"})
    repo.commit("build(deps): bump python from `cae66f2` to `cad9a2c` (#14)",
                {"Dockerfile": "FROM python@sha256:cad9a2c\n"})
    _release(repo)
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 0, result.stdout
    assert "1 of 1 cited" in result.stdout
    assert "3 dependency bump(s) excluded" in result.stdout
    assert "0 dependency bump(s) held" in result.stdout


def test_a_major_move_of_a_runtime_pin_is_held_to_a_citation(repo: Repo):
    """`build(deps): bump mcp from 1.29.0 to 2.1.1 (#2050)` renamed
    Tool.inputSchema under exactly this subject shape. A major move of a
    runtime manifest is a change the entry must name."""
    repo.commit("feat: alpha (#10)")
    repo.commit("build(deps): bump mcp from 1.29.0 to 2.1.1 (#15)",
                {"constraints.txt": "mcp==2.1.1\n"})
    _release(repo)
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "#15" in result.stdout
    assert "held: major move of a runtime pin (constraints.txt)" in result.stdout
    assert "1 dependency bump(s) held" in result.stdout
    # Citing it clears the gate like any other change.
    _release(repo, "- **things:** alpha (#10)\n- **deps:** mcp 2 (#15)")
    assert _run(repo.path, COVERAGE).returncode == 0


def test_a_same_major_move_of_a_runtime_pin_is_excluded(repo: Repo):
    repo.commit("feat: alpha (#10)")
    repo.commit("build(deps): bump mcp from 2.1.1 to 2.2.0 (#16)",
                {"constraints.txt": "mcp==2.2.0\n"})
    repo.commit("build(deps): update numpy requirement from <2,>=1.26 to >=1.26,<2.1 (#17)",
                {"pyproject.toml": "numpy>=1.26,<2.1\n"})
    _release(repo)
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 0, result.stdout
    assert "2 dependency bump(s) excluded" in result.stdout


def test_a_group_bump_of_runtime_pins_cannot_prove_itself(repo: Repo):
    """A group subject names no versions, so a runtime manifest move under it
    is held: the subject is the only version evidence the gate reads."""
    repo.commit("feat: alpha (#10)")
    repo.commit("build(deps): bump the pip group across 1 directory with 2 updates (#18)",
                {"constraints.txt": "mcp==2.2.0\nnumpy==2.1.0\n"})
    _release(repo)
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "held: moves a runtime pin (constraints.txt) and the subject does not show the versions" in result.stdout


def test_a_bump_that_touches_source_is_an_ordinary_change(repo: Repo):
    """The subject shape buys nothing when the diff reaches past the pins."""
    repo.commit("feat: alpha (#10)")
    repo.commit("build(deps): bump mcp from 2.1.1 to 2.2.0 (#19)",
                {"constraints.txt": "mcp==2.2.0\n", "src/tool_schemas.py": "# adapted\n"})
    _release(repo)
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "held: touches src/tool_schemas.py, which is not a dependency pin" in result.stdout


def test_an_ordinary_build_change_still_needs_a_citation(repo: Repo):
    """Only the dependabot shape is excluded; `build:` alone is a real change."""
    repo.commit("feat: alpha (#10)")
    repo.commit("build: switch the package backend to hatchling (#14)")
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry("1.1.0", "- **things:** alpha (#10)"),
    })
    result = _run(repo.path, COVERAGE)
    assert result.returncode == 1
    assert "#14" in result.stdout
    assert "0 dependency bump(s) excluded" in result.stdout


def test_list_mode_reports_the_same_gaps_without_failing(repo: Repo):
    repo.commit("feat: alpha (#10)")
    repo.commit("fix: beta (#11)")
    repo.commit("chore(release): 1.1.0 (#99)", {
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
    repo.commit("chore(release): 1.1.0 (#99)", {"VERSION": "1.1.0\n"})
    result = _run(repo.path, DRIFT)
    assert result.returncode == 1
    assert "unitares-sdk" in result.stdout
    assert "cannot reach these changes" in result.stdout


def test_a_readme_only_touch_does_not_block_the_sdk_series(repo: Repo):
    """agents/sdk/README.md is the PyPI project page, but the server-tag
    cross-reference line it carries updates on every server release
    regardless of SDK code movement. That alone must not read as an
    unbumped SDK change."""
    _with_sdk(repo, "0.1.0")
    repo.commit("chore(release): 1.1.0 (#99)", {
        "agents/sdk/README.md": "pin: v1.1.0\n",
        "VERSION": "1.1.0\n",
    })
    result = _run(repo.path, DRIFT)
    assert result.returncode == 0
    assert "level with its tag" in result.stdout


def test_a_real_sdk_change_alongside_a_readme_touch_still_blocks(repo: Repo):
    """The README exclusion must not shadow a genuine, concurrent SDK change."""
    _with_sdk(repo, "0.1.0")
    repo.commit("feat(sdk): new method (#10)", {
        "agents/sdk/pyproject.toml": SDK_PYPROJECT.format(version="0.1.0"),
        "agents/sdk/src/client.py": "def delegate(): ...\n",
        "agents/sdk/README.md": "pin: v1.1.0\n",
    })
    repo.commit("chore(release): 1.1.0 (#99)", {"VERSION": "1.1.0\n"})
    result = _run(repo.path, DRIFT)
    assert result.returncode == 1
    assert "unitares-sdk" in result.stdout


def test_only_a_provably_forward_bump_clears_the_series(repo: Repo):
    """Equal, older, malformed, and unreadable all used to read as "bumped"."""
    for declared, why in [("0.1.0", "equal"), ("0.1", "equal under semver"),
                          ("0.0.9", "a downgrade"), ("banana", "not a version")]:
        target = repo.path.parent / f"drift-{declared.replace('.', '_')}"
        target.mkdir(parents=True, exist_ok=True)
        r = Repo(target)
        r.write("VERSION", "1.0.0\n")
        r.commit("chore: seed", {"VERSION": "1.0.0\n"})
        r.tag("v1.0.0")
        _with_sdk(r, "0.1.0")
        r.commit("feat(sdk): new method (#10)", {
            "agents/sdk/pyproject.toml": SDK_PYPROJECT.format(version=declared),
            "agents/sdk/src/client.py": "def delegate(): ...\n",
        })
        r.commit("chore(release): 1.1.0 (#99)", {"VERSION": "1.1.0\n"})
        result = _run(r.path, DRIFT)
        assert result.returncode == 1, f"{declared} ({why}) should block"


def test_a_deleted_version_file_blocks_rather_than_clears(repo: Repo):
    _with_sdk(repo, "0.1.0")
    repo.commit("feat(sdk): new method (#10)", {"agents/sdk/src/client.py": "x\n"})
    (repo.path / "agents/sdk/pyproject.toml").unlink()
    repo._git("add", "-u")
    repo._git("commit", "-q", "-m", "chore: drop the version file (#11)")
    repo.commit("chore(release): 1.1.0 (#99)", {"VERSION": "1.1.0\n"})
    result = _run(repo.path, DRIFT)
    assert result.returncode == 1


def test_a_declared_plugin_recut_clears_the_skills_series(repo: Repo):
    """Without this the gate was unsatisfiable: correct mirroring stayed red."""
    repo.commit("docs(skills): seed", {"skills/x/SKILL.md": "seed\n"})
    repo.commit("docs(skills): correct margin guidance (#10)", {
        "skills/x/SKILL.md": "corrected\n"})
    repo.commit("chore(release): 1.1.0 (#99)", {
        "VERSION": "1.1.0\n",
        "docs/CHANGELOG.md": _entry(
            "1.1.0", "<!-- plugin-bundle-recut: v0.4.14 -->\n\n- **skills:** corrected (#10)"),
    })
    result = _run(repo.path, DRIFT)
    assert result.returncode == 0
    assert "carried by plugin v0.4.14" in result.stdout


def test_an_already_bumped_series_is_not_blocking(repo: Repo):
    _with_sdk(repo, "0.1.0")
    repo.commit("feat(sdk): new method (#10)", {
        "agents/sdk/pyproject.toml": SDK_PYPROJECT.format(version="0.2.0"),
    })
    repo.commit("chore(release): 1.1.0 (#99)", {"VERSION": "1.1.0\n"})
    result = _run(repo.path, DRIFT)
    assert result.returncode == 0
    assert "newer than the published 0.1.0" in result.stdout


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
    repo.commit("chore(release): 1.1.0 (#99)", {"VERSION": "1.1.0\n"})
    result = _run(repo.path, DRIFT)
    assert result.returncode == 1
    assert "plugin skills bundle" in result.stdout
    assert "plugin-bundle-recut" in result.stdout


# --- published claims -------------------------------------------------------


CLAIMS = REPO_ROOT / "scripts/ci/published_claims.py"

COMPAT = "| `unitares-sdk` | `{v}` | Install with `pip install unitares-sdk=={v}`. |\n"
SITE = "Python SDK {v}\n\n```bash\npython -m pip install unitares-sdk=={v}\n```\n"


def _claims_repo(repo: Repo, advertised: str, tags: list[str], site: str | None = None) -> None:
    repo.write("COMPATIBILITY.md", COMPAT.format(v=advertised))
    repo.write("docs/public-site/index.md", SITE.format(v=site or advertised))
    repo.commit("docs: advertise the SDK", {})
    for tag in tags:
        repo._git("tag", "-a", tag, "-m", tag)


def test_an_advertised_version_with_a_tag_passes(repo: Repo):
    _claims_repo(repo, "0.2.0", ["sdk-v0.1.0", "sdk-v0.2.0"])
    result = _run(repo.path, CLAIMS)
    assert result.returncode == 0
    assert "0.2.0 is published" in result.stdout


def test_an_advertised_version_with_no_tag_fails(repo: Repo):
    """The live 2026-08-21 bug: pyproject bumped, docs followed, PyPI did not."""
    _claims_repo(repo, "0.2.1", ["sdk-v0.1.0", "sdk-v0.2.0"])
    result = _run(repo.path, CLAIMS)
    assert result.returncode == 1
    assert "no sdk-v0.2.1 tag" in result.stdout


def test_surfaces_advertising_different_versions_fail(repo: Repo):
    _claims_repo(repo, "0.2.0", ["sdk-v0.1.0", "sdk-v0.2.0"], site="0.1.0")
    result = _run(repo.path, CLAIMS)
    assert result.returncode == 1
    assert "advertise different" in result.stdout


def test_missing_tags_fail_rather_than_skip(repo: Repo):
    """"I could not check" must not share an exit code with "the claim is fine"."""
    _claims_repo(repo, "0.2.0", [])
    result = _run(repo.path, CLAIMS)
    assert result.returncode == 1
    assert "cannot verify" in result.stderr

"""scripts/dev/open-plugin-skill-sync-pr.sh keeps the plugin's skill mirror current.

The plugin's ``skills/`` is a byte mirror of ``unitares/skills/``, but nothing
ran the sync and the plugin's CI only checks the mirror against its own
manifest, so on 2026-09-25/26 it fell behind twice unnoticed. This script is
the one implementation the GitHub workflow and an operator's deploy wrapper
share. These tests run it against real git fixtures: a unitares origin with a
stand-in sync script, a bare plugin origin it pushes to, and a fake ``gh``.

What they pin: in sync means no branch and no PR; behind means one commit on
the one automation branch and exactly one PR; a later run updates that PR; a
hand-opened sync PR makes it step aside; a branch that moved mid-run is not
overwritten; and every run removes the worktrees it made and nothing else.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "dev" / "open-plugin-skill-sync-pr.sh"
BRANCH = "auto/plugin-skill-sync"

pytestmark = pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")

# The stand-in mirrors skills/ the way the real sync script does. --checksum
# matters as much here as there: both worktrees are created in the same second,
# so size+mtime cannot see a change between two same-length file versions.
STUB_SYNC = """#!/usr/bin/env bash
[ -n "${FAIL_SYNC:-}" ] && { echo boom >&2; exit 3; }
SRC="$(cd "$(dirname "$0")/../.." && pwd)/skills"
mkdir -p "$UNITARES_PLUGIN_REPO/skills"
rsync -a --checksum --delete --exclude /SKILLS_MANIFEST.sha256 "$SRC/" "$UNITARES_PLUGIN_REPO/skills/"
"""


# Commits in the fixtures and in the script under test need an identity and no
# signing, on a CI runner as much as locally -- passed explicitly, never by
# mutating this process's environment.
GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "commit.gpgsign",
    "GIT_CONFIG_VALUE_0": "false",
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=GIT_ENV
    ).stdout.strip()


def _write_exec(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Fixture:
    def __init__(self, root: Path, plugin_branch: str = "master"):
        self.root = root
        self.env = dict(GIT_ENV)

        self.u_origin = root / "unitares-origin"
        (self.u_origin / "skills" / "alpha").mkdir(parents=True)
        _git(root, "init", "-q", "--initial-branch=master", str(self.u_origin))
        (self.u_origin / "skills" / "alpha" / "SKILL.md").write_text("alpha v1\n")
        _write_exec(self.u_origin / "scripts" / "dev" / "sync-plugin-skills.sh", STUB_SYNC)
        _git(self.u_origin, "add", "-A")
        _git(self.u_origin, "commit", "-qm", "alpha v1")
        self.unitares = root / "unitares"
        _git(root, "clone", "-q", str(self.u_origin), str(self.unitares))

        self.p_bare = root / "plugin-origin.git"
        _git(root, "init", "-q", "--bare", f"--initial-branch={plugin_branch}", str(self.p_bare))
        seed = root / "plugin-seed"
        _git(root, "clone", "-q", str(self.p_bare), str(seed))
        (seed / "skills" / "alpha").mkdir(parents=True)
        (seed / "skills" / "alpha" / "SKILL.md").write_text("alpha v1\n")
        (seed / "skills" / "SKILLS_MANIFEST.sha256").write_text("manifest\n")
        _git(seed, "add", "-A")
        _git(seed, "commit", "-qm", "seed")
        _git(seed, "push", "-q", "origin", f"HEAD:{plugin_branch}")
        self.plugin = root / "plugin"
        _git(root, "clone", "-q", str(self.p_bare), str(self.plugin))
        _git(self.plugin, "remote", "set-head", "origin", plugin_branch)

        self.stub = root / "gh-stub"
        self.stub.mkdir()
        for name in ("open_pr", "other_pr", "calls", "on_search"):
            (self.stub / name).write_text("")
        _write_exec(self.stub / "gh", f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >>"{self.stub}/calls"
case "$*" in
  "pr list"*"--head"*) cat "{self.stub}/open_pr" ;;
  "pr list"*"--search"*) bash "{self.stub}/on_search"; cat "{self.stub}/other_pr" ;;
  "pr create"*) echo "https://example.invalid/pull/7" ;;
  "pr edit"*) if [ -n "${{EDIT_FAIL:-}}" ]; then echo "edit refused" >&2; exit 1; fi ;;
esac
""")
        self.wt = root / "wt"

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess:
        env = {
            **self.env,
            "UNITARES_REPO_DIR": str(self.unitares),
            "UNITARES_PLUGIN_REPO": str(self.plugin),
            "SKILL_SYNC_WT_ROOT": str(self.wt),
            "SKILL_SYNC_GH": str(self.stub / "gh"),
            **extra,
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env
        )

    def bump(self, text: str) -> None:
        (self.u_origin / "skills" / "alpha" / "SKILL.md").write_text(text + "\n")
        _git(self.u_origin, "commit", "-qam", text)

    def remote_branch(self) -> str:
        r = subprocess.run(
            ["git", "-C", str(self.p_bare), "rev-parse", "-q", "--verify", f"refs/heads/{BRANCH}"],
            capture_output=True, text=True, env=GIT_ENV,
        )
        return r.stdout.strip()

    def branch_file(self, path: str) -> str:
        return _git(self.p_bare, "show", f"{BRANCH}:{path}")

    def calls(self, prefix: str) -> list[str]:
        return [c for c in (self.stub / "calls").read_text().splitlines() if c.startswith(prefix)]

    def leftovers(self) -> list[str]:
        made = sorted(p.name for p in self.wt.iterdir()) if self.wt.exists() else []
        trees = _git(self.unitares, "worktree", "list").splitlines()[1:]
        trees += _git(self.plugin, "worktree", "list").splitlines()[1:]
        return made + trees


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return Fixture(tmp_path)


def _out(proc: subprocess.CompletedProcess) -> str:
    return proc.stdout + proc.stderr


def test_in_sync_reports_and_creates_nothing(fx):
    proc = fx.run()
    assert proc.returncode == 0, _out(proc)
    assert "in sync" in _out(proc)
    assert fx.remote_branch() == ""
    assert fx.leftovers() == []


def test_dry_run_names_the_drift_and_pushes_nothing(fx):
    fx.bump("alpha v2")
    proc = fx.run("--dry-run")
    assert proc.returncode == 0, _out(proc)
    assert "BEHIND" in _out(proc) and "alpha" in _out(proc)
    assert "1 file(s)" in _out(proc), "the mirror-only manifest must not be counted"
    assert fx.remote_branch() == ""
    assert fx.calls("pr create") == []
    assert fx.leftovers() == []


def test_behind_commits_one_branch_and_opens_one_pr(fx):
    fx.bump("alpha v2")
    proc = fx.run()
    assert proc.returncode == 0, _out(proc)
    assert fx.branch_file("skills/alpha/SKILL.md") == "alpha v2"
    assert fx.branch_file("skills/SKILLS_MANIFEST.sha256") == "manifest", "manifest must survive"
    creates = fx.calls("pr create")
    assert len(creates) == 1 and "--base master" in creates[0]
    assert fx.leftovers() == []


def test_later_run_updates_the_open_pr_instead_of_opening_another(fx):
    fx.bump("alpha v2")
    assert fx.run().returncode == 0
    (fx.stub / "open_pr").write_text("7\n")
    fx.bump("alpha v3")
    proc = fx.run()
    assert proc.returncode == 0, _out(proc)
    assert "updated #7" in _out(proc)
    assert fx.branch_file("skills/alpha/SKILL.md") == "alpha v3"
    assert len(fx.calls("pr create")) == 1
    edits = fx.calls("pr edit")
    assert len(edits) == 1 and edits[0].startswith("pr edit 7 ")
    # The body names the new source commit, not the one the PR was opened for.
    new_sha = _git(fx.u_origin, "rev-parse", "--short=8", "HEAD")
    assert "--body" in edits[0] and new_sha in edits[0]


def test_failed_pr_update_is_reported_not_claimed(fx):
    fx.bump("alpha v2")
    assert fx.run().returncode == 0
    (fx.stub / "open_pr").write_text("7\n")
    fx.bump("alpha v3")
    proc = fx.run(EDIT_FAIL="1")
    assert proc.returncode == 1, _out(proc)
    assert "could not update #7" in _out(proc) and "edit refused" in _out(proc)
    assert "updated #7" not in _out(proc)


def test_default_branch_is_asked_of_the_remote(tmp_path):
    # A plugin whose default branch is not master, with no origin/HEAD in the
    # clone (as in a CI checkout): the PR must target the real default.
    fx = Fixture(tmp_path, plugin_branch="main")
    _git(fx.plugin, "remote", "set-head", "origin", "--delete")
    fx.bump("alpha v2")
    proc = fx.run()
    assert proc.returncode == 0, _out(proc)
    creates = fx.calls("pr create")
    assert len(creates) == 1 and "--base main" in creates[0]


def test_hand_opened_sync_pr_makes_it_step_aside(fx):
    (fx.stub / "other_pr").write_text("151\n")
    fx.bump("alpha v2")
    proc = fx.run()
    assert proc.returncode == 2, _out(proc)
    assert "#151" in _out(proc)
    assert fx.remote_branch() == ""
    assert fx.leftovers() == []


def test_branch_moved_mid_run_is_not_overwritten(fx):
    # Another run pushes the automation branch after this one looked at it and
    # before it pushes: the compare-and-swap must refuse, not replace.
    fx.bump("alpha v2")
    racer = fx.root / "racer"
    _git(fx.root, "clone", "-q", str(fx.p_bare), str(racer))
    (racer / "RACE").write_text("x\n")
    _git(racer, "add", "RACE")
    _git(racer, "commit", "-qm", "race")
    (fx.stub / "on_search").write_text(
        f'git -C "{racer}" push -q origin "HEAD:refs/heads/{BRANCH}" >/dev/null 2>&1\n'
    )
    proc = fx.run()
    raced = _git(racer, "rev-parse", "HEAD")
    assert proc.returncode == 1, _out(proc)
    assert "refused" in _out(proc)
    assert fx.remote_branch() == raced, "the racer's push must survive"
    assert fx.calls("pr create") == []
    assert fx.leftovers() == []


def test_failing_sync_exits_1_and_cleans_up(fx):
    fx.bump("alpha v2")
    proc = fx.run(FAIL_SYNC="1")
    assert proc.returncode == 1, _out(proc)
    assert "failed" in _out(proc)
    assert fx.leftovers() == []


def test_missing_checkout_is_a_clean_skip(fx):
    proc = fx.run(UNITARES_REPO_DIR=str(fx.root / "nope"))
    assert proc.returncode == 1, _out(proc)
    assert "no git checkout" in _out(proc)
    assert fx.leftovers() == []

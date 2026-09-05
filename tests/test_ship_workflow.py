"""Tests for scripts/dev/ship.sh delivery routing."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SHIP_SOURCE = PROJECT_ROOT / "scripts" / "dev" / "ship.sh"
SHIP_WATCHER_HELPER_SOURCE = (
    PROJECT_ROOT / "scripts" / "dev" / "_ship_watcher_fingerprints.py"
)


def run(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )


@pytest.fixture
def ship_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    script_path = repo / "scripts" / "dev" / "ship.sh"
    script_path.parent.mkdir(parents=True)
    shutil.copy2(SHIP_SOURCE, script_path)
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
    shutil.copy2(
        SHIP_WATCHER_HELPER_SOURCE,
        script_path.parent / "_ship_watcher_fingerprints.py",
    )

    run(["git", "init", "-q", "-b", "main"], repo)
    run(["git", "config", "user.email", "t@t"], repo)
    run(["git", "config", "user.name", "t"], repo)
    run(["git", "config", "commit.gpgsign", "false"], repo)
    (repo / "README.md").write_text("seed\n")
    run(["git", "add", "README.md"], repo)
    run(["git", "commit", "-q", "-m", "seed"], repo)

    origin = tmp_path / "origin.git"
    run(["git", "init", "--bare", "-q", str(origin)], tmp_path)
    run(["git", "remote", "add", "origin", str(origin)], repo)
    run(["git", "push", "-q", "-u", "origin", "main"], repo)
    return repo


def stage_file(repo: Path, relative_path: str) -> None:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{relative_path}\n")
    run(["git", "add", relative_path], repo)


def ship_plan(repo: Path, *options: str) -> dict[str, str]:
    cmd = [
        str(repo / "scripts" / "dev" / "ship.sh"),
        "--plan",
        *options,
        "test: change",
    ]
    result = run(cmd, repo)
    parsed: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, value = line.split("=", 1)
        parsed[key] = value
    return parsed


def write_watcher_finding(
    findings: Path, repo: Path, relative_path: str, fp: str
) -> None:
    findings.parent.mkdir(parents=True, exist_ok=True)
    findings.write_text(
        json.dumps(
            {
                "file": str(repo / relative_path),
                "fingerprint": fp,
                "status": "surfaced",
            }
        )
        + "\n"
    )


def install_existing_pr_gh(
    fake_bin: Path,
    *,
    capture: Path,
    body: str,
    labels: str = "",
    is_draft: bool = True,
    auto_merge: bool = False,
    fail_edit: bool = False,
    label_installed: bool = True,
    fail_label_lookup: bool = False,
) -> dict[str, str]:
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        "printf 'CALL' >> \"$GH_CAPTURE\"\n"
        'for arg in "$@"; do printf \' <%s>\' "$arg" >> "$GH_CAPTURE"; done\n'
        "printf '\\n' >> \"$GH_CAPTURE\"\n"
        'if [ "$1" = pr ] && [ "$2" = view ]; then\n'
        '  case "$4" in\n'
        "    url) printf '%s\\n' https://github.test/pull/7 ;;\n"
        "    body) printf '%s\\n' \"$EXISTING_PR_BODY\" ;;\n"
        "    labels) printf '%s\\n' \"$EXISTING_PR_LABELS\" ;;\n"
        "    isDraft) printf '%s\\n' \"$EXISTING_PR_IS_DRAFT\" ;;\n"
        "    autoMergeRequest) printf '%s\\n' \"$EXISTING_PR_AUTO_MERGE\" ;;\n"
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = label ] && [ "$2" = list ]; then\n'
        '  if [ "$GH_LABEL_LOOKUP_FAIL" = 1 ]; then exit 23; fi\n'
        "  printf '%s\\n' \"$MERGE_AUTO_LABEL_COUNT\"\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = pr ] && [ "$2" = edit ] && '
        '[ "$GH_EDIT_FAIL" = 1 ]; then exit 19; fi\n'
        "exit 0\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GH_CAPTURE": str(capture),
        "EXISTING_PR_BODY": body,
        "EXISTING_PR_LABELS": labels,
        "EXISTING_PR_IS_DRAFT": "true" if is_draft else "false",
        "EXISTING_PR_AUTO_MERGE": "true" if auto_merge else "false",
        "GH_EDIT_FAIL": "1" if fail_edit else "0",
        "GH_LABEL_LOOKUP_FAIL": "1" if fail_label_lookup else "0",
        "MERGE_AUTO_LABEL_COUNT": "1" if label_installed else "0",
    }


def run_direct_ship(repo: Path, *, env: dict[str, str]) -> tuple[str, str]:
    result = run(
        [
            str(repo / "scripts" / "dev" / "ship.sh"),
            "--direct",
            "test: change",
        ],
        repo,
        env=env,
    )
    message = run(["git", "log", "-1", "--format=%B"], repo).stdout
    return result.stdout, message


def test_auto_routes_runtime_changes_to_draft_pr_branch(ship_repo: Path) -> None:
    stage_file(ship_repo, "src/mcp_server.py")

    plan = ship_plan(ship_repo)

    assert plan["kind"] == "runtime"
    assert plan["branch"] == "main"
    assert plan["delivery"] == "draft_pr"
    assert plan["force_auto_branch"] == "1"
    assert plan["autonomous_queue"] == "1"


def test_auto_routes_detached_non_runtime_changes_to_draft_pr(ship_repo: Path) -> None:
    run(["git", "checkout", "--detach", "-q"], ship_repo)
    stage_file(ship_repo, "docs/workflow-note.md")

    plan = ship_plan(ship_repo)

    assert plan["kind"] == "other"
    assert plan["branch"] == "(detached)"
    assert plan["delivery"] == "draft_pr"
    assert plan["force_auto_branch"] == "1"
    assert plan["autonomous_queue"] == "1"


def test_auto_routes_feature_branch_docs_to_draft_pr(ship_repo: Path) -> None:
    # Draft PR for everything: non-runtime work on a named feature branch opens
    # a draft PR on that branch rather than direct-pushing
    # (docs/operations/github-workflow-conventions.md).
    run(["git", "checkout", "-q", "-b", "docs/workflow-note"], ship_repo)
    stage_file(ship_repo, "docs/workflow-note.md")

    plan = ship_plan(ship_repo)

    assert plan["kind"] == "other"
    assert plan["branch"] == "docs/workflow-note"
    assert plan["delivery"] == "draft_pr"
    assert plan["force_auto_branch"] == "0"
    assert plan["autonomous_queue"] == "1"


def test_explicit_direct_opts_out_on_feature_branch(ship_repo: Path) -> None:
    # --direct is the escape hatch from draft-PR-for-everything, for
    # docs/tests-only pushes on a named feature branch.
    run(["git", "checkout", "-q", "-b", "docs/workflow-note"], ship_repo)
    stage_file(ship_repo, "docs/workflow-note.md")

    plan = ship_plan(ship_repo, "--direct")

    assert plan["kind"] == "other"
    assert plan["branch"] == "docs/workflow-note"
    assert plan["delivery"] == "direct"
    assert plan["force_auto_branch"] == "0"


def test_explicit_draft_pr_uses_current_feature_branch(ship_repo: Path) -> None:
    run(["git", "checkout", "-q", "-b", "codex/workflow-note"], ship_repo)
    stage_file(ship_repo, "docs/workflow-note.md")

    plan = ship_plan(ship_repo, "--draft-pr")

    assert plan["kind"] == "other"
    assert plan["branch"] == "codex/workflow-note"
    assert plan["delivery"] == "draft_pr"
    assert plan["force_auto_branch"] == "0"
    assert plan["autonomous_queue"] == "0"


def test_auto_merge_flag_is_safe_queued_draft_alias(ship_repo: Path) -> None:
    run(["git", "checkout", "-q", "-b", "codex/workflow-note"], ship_repo)
    stage_file(ship_repo, "docs/workflow-note.md")

    plan = ship_plan(ship_repo, "--auto-merge")

    assert plan["mode"] == "auto_merge"
    assert plan["delivery"] == "draft_pr"
    assert plan["autonomous_queue"] == "1"


def test_auto_merge_alias_mints_branch_from_detached_head(ship_repo: Path) -> None:
    stage_file(ship_repo, "docs/workflow-note.md")
    run(["git", "checkout", "-q", "--detach"], ship_repo)

    plan = ship_plan(ship_repo, "--auto-merge")

    assert plan["delivery"] == "draft_pr"
    assert plan["force_auto_branch"] == "1"
    assert plan["autonomous_queue"] == "1"


@pytest.mark.parametrize("option", [None, "--auto-merge"])
def test_queued_pr_body_declares_autonomous_queue_intent(
    ship_repo: Path,
    tmp_path: Path,
    option: str | None,
) -> None:
    run(["git", "checkout", "-q", "-b", "codex/workflow-note"], ship_repo)
    stage_file(ship_repo, "docs/workflow-note.md")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "gh-args.txt"
    gh = fake_bin / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'printf \'%s\\n\' "$@" >> "$GH_CAPTURE"\n'
        'if [ "$1" = pr ] && [ "$2" = view ]; then exit 1; fi\n'
        'if [ "$1" = pr ] && [ "$2" = create ]; then '
        "echo https://github.test/pull/1; exit 0; fi\n"
        "exit 0\n"
    )
    gh.chmod(gh.stat().st_mode | stat.S_IXUSR)

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "GH_CAPTURE": str(capture),
    }
    command = [str(ship_repo / "scripts" / "dev" / "ship.sh")]
    if option:
        command.append(option)
    command.append("test: queued change")
    result = run(command, ship_repo, env=env)

    arguments = capture.read_text()
    assert "--draft" in arguments
    assert "<!-- unitares-merge-intent: autonomous -->" in arguments
    if option == "--auto-merge":
        assert "--auto-merge is deprecated" in result.stderr


@pytest.mark.parametrize("existing_body", ["", "Legacy draft description"])
def test_default_ship_queues_existing_manual_or_legacy_pr(
    ship_repo: Path,
    tmp_path: Path,
    existing_body: str,
) -> None:
    run(["git", "checkout", "-q", "-b", "codex/existing-pr"], ship_repo)
    stage_file(ship_repo, "docs/existing.md")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "gh-calls.txt"
    env = install_existing_pr_gh(
        fake_bin,
        capture=capture,
        body=existing_body,
    )

    result = run(
        [str(ship_repo / "scripts" / "dev" / "ship.sh"), "test: existing PR"],
        ship_repo,
        env=env,
    )

    calls = capture.read_text()
    assert "CALL <pr> <edit> <--add-label> <merge:auto>" in calls
    assert "CALL <pr> <edit> <--body>" not in calls
    assert "added autonomous queue label" in result.stdout
    assert "https://github.test/pull/7" in result.stdout


def test_existing_queued_pr_is_not_edited_again(
    ship_repo: Path,
    tmp_path: Path,
) -> None:
    run(["git", "checkout", "-q", "-b", "codex/already-queued"], ship_repo)
    stage_file(ship_repo, "docs/existing.md")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "gh-calls.txt"
    env = install_existing_pr_gh(
        fake_bin,
        capture=capture,
        body="Queued already\n\n<!-- unitares-merge-intent: autonomous -->",
    )

    run(
        [str(ship_repo / "scripts" / "dev" / "ship.sh"), "test: queued PR"],
        ship_repo,
        env=env,
    )

    assert "CALL <pr> <edit>" not in capture.read_text()


def test_explicit_draft_unqueues_existing_pr_without_clobbering_body(
    ship_repo: Path,
    tmp_path: Path,
) -> None:
    run(["git", "checkout", "-q", "-b", "codex/manual-existing"], ship_repo)
    stage_file(ship_repo, "docs/existing.md")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "gh-calls.txt"
    env = install_existing_pr_gh(
        fake_bin,
        capture=capture,
        body="Keep this prose\n\n<!-- unitares-merge-intent: autonomous -->",
        labels="merge:auto",
    )

    run(
        [
            str(ship_repo / "scripts" / "dev" / "ship.sh"),
            "--draft-pr",
            "test: manual existing PR",
        ],
        ship_repo,
        env=env,
    )

    calls = capture.read_text()
    assert "CALL <pr> <edit> <--body> <Keep this prose" in calls
    assert "CALL <pr> <edit> <--remove-label> <merge:auto>" in calls


def test_explicit_open_pr_unqueues_disarms_and_marks_existing_draft_ready(
    ship_repo: Path,
    tmp_path: Path,
) -> None:
    run(["git", "checkout", "-q", "-b", "codex/open-existing"], ship_repo)
    stage_file(ship_repo, "docs/existing.md")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "gh-calls.txt"
    env = install_existing_pr_gh(
        fake_bin,
        capture=capture,
        body="Keep this prose\n\n<!-- unitares-merge-intent: autonomous -->",
        labels="merge:auto",
        is_draft=True,
        auto_merge=True,
    )

    run(
        [
            str(ship_repo / "scripts" / "dev" / "ship.sh"),
            "--open-pr",
            "test: manual ready PR",
        ],
        ship_repo,
        env=env,
    )

    calls = capture.read_text()
    assert "CALL <pr> <edit> <--body> <Keep this prose" in calls
    assert "CALL <pr> <edit> <--remove-label> <merge:auto>" in calls
    assert "CALL <pr> <merge> <--disable-auto>" in calls
    assert "CALL <pr> <ready>" in calls


def test_default_queue_preserves_existing_maintainer_ready_and_arm_state(
    ship_repo: Path,
    tmp_path: Path,
) -> None:
    run(["git", "checkout", "-q", "-b", "codex/ready-existing"], ship_repo)
    stage_file(ship_repo, "docs/existing.md")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "gh-calls.txt"
    env = install_existing_pr_gh(
        fake_bin,
        capture=capture,
        body="Ready legacy PR",
        is_draft=False,
        auto_merge=True,
    )

    run(
        [str(ship_repo / "scripts" / "dev" / "ship.sh"), "test: queue safely"],
        ship_repo,
        env=env,
    )

    calls = capture.read_text()
    assert "CALL <pr> <merge> <--disable-auto>" not in calls
    assert "CALL <pr> <ready> <--undo>" not in calls
    assert "CALL <pr> <edit> <--add-label> <merge:auto>" in calls


def test_existing_pr_queue_update_failure_fails_delivery(
    ship_repo: Path,
    tmp_path: Path,
) -> None:
    run(["git", "checkout", "-q", "-b", "codex/edit-failure"], ship_repo)
    stage_file(ship_repo, "docs/existing.md")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "gh-calls.txt"
    env = install_existing_pr_gh(
        fake_bin,
        capture=capture,
        body="Manual draft",
        fail_edit=True,
    )

    with pytest.raises(subprocess.CalledProcessError) as exc:
        run(
            [
                str(ship_repo / "scripts" / "dev" / "ship.sh"),
                "test: queue update fails",
            ],
            ship_repo,
            env=env,
        )

    assert exc.value.returncode == 19
    assert "CALL <pr> <edit> <--add-label> <merge:auto>" in capture.read_text()


def test_existing_pr_without_bootstrap_label_is_reported_and_left_unqueued(
    ship_repo: Path,
    tmp_path: Path,
) -> None:
    run(["git", "checkout", "-q", "-b", "codex/missing-label"], ship_repo)
    stage_file(ship_repo, "docs/existing.md")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "gh-calls.txt"
    env = install_existing_pr_gh(
        fake_bin,
        capture=capture,
        body="Manual draft",
        label_installed=False,
    )

    result = run(
        [str(ship_repo / "scripts" / "dev" / "ship.sh"), "test: queue later"],
        ship_repo,
        env=env,
    )

    calls = capture.read_text()
    assert "CALL <label> <list>" in calls
    assert "CALL <pr> <edit> <--add-label> <merge:auto>" not in calls
    assert "left unqueued: merge:auto label not installed" in result.stderr
    assert "https://github.test/pull/7" in result.stdout


def test_existing_pr_label_lookup_failure_fails_delivery(
    ship_repo: Path,
    tmp_path: Path,
) -> None:
    run(["git", "checkout", "-q", "-b", "codex/label-lookup-failure"], ship_repo)
    stage_file(ship_repo, "docs/existing.md")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "gh-calls.txt"
    env = install_existing_pr_gh(
        fake_bin,
        capture=capture,
        body="Manual draft",
        fail_label_lookup=True,
    )

    with pytest.raises(subprocess.CalledProcessError) as exc:
        run(
            [str(ship_repo / "scripts" / "dev" / "ship.sh"), "test: lookup fails"],
            ship_repo,
            env=env,
        )

    assert exc.value.returncode == 23
    calls = capture.read_text()
    assert "CALL <label> <list>" in calls
    assert "CALL <pr> <edit> <--add-label> <merge:auto>" not in calls


def test_stage_all_plan_classifies_dirty_worktree_without_staging(
    ship_repo: Path,
) -> None:
    path = ship_repo / "src" / "mcp_server.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("runtime\n")

    plan = ship_plan(ship_repo, "--stage-all")

    assert plan["kind"] == "runtime"
    assert plan["delivery"] == "draft_pr"
    assert plan["force_auto_branch"] == "1"
    assert plan["stage_all"] == "1"

    status = run(["git", "status", "--porcelain=v1"], ship_repo)
    staged = run(["git", "diff", "--cached", "--name-only"], ship_repo)
    assert "?? src/" in status.stdout
    assert staged.stdout == ""


def test_direct_ship_reads_shared_watcher_dir_for_commit_trailer(
    ship_repo: Path,
    tmp_path: Path,
) -> None:
    stage_file(ship_repo, "agents/foo.py")

    watcher_dir = tmp_path / "watcher"
    write_watcher_finding(
        watcher_dir / "findings.jsonl",
        ship_repo,
        "agents/foo.py",
        "abc123",
    )

    env = {
        **os.environ,
        "UNITARES_WATCHER_DATA_DIR": str(watcher_dir),
    }
    stdout, message = run_direct_ship(ship_repo, env=env)

    assert "[ship] appended Watcher-Findings trailer: abc123" in stdout
    assert "Watcher-Findings: abc123" in message


def test_direct_ship_reads_default_shared_watcher_dir_for_commit_trailer(
    ship_repo: Path,
    tmp_path: Path,
) -> None:
    stage_file(ship_repo, "agents/default_path.py")

    home = tmp_path / "home"
    write_watcher_finding(
        home / ".unitares" / "watcher" / "findings.jsonl",
        ship_repo,
        "agents/default_path.py",
        "default123",
    )

    env = {**os.environ, "HOME": str(home)}
    env.pop("UNITARES_WATCHER_DATA_DIR", None)
    stdout, message = run_direct_ship(ship_repo, env=env)

    assert "[ship] appended Watcher-Findings trailer: default123" in stdout
    assert "Watcher-Findings: default123" in message


def test_direct_ship_falls_back_to_legacy_watcher_dir_for_commit_trailer(
    ship_repo: Path,
    tmp_path: Path,
) -> None:
    stage_file(ship_repo, "agents/legacy_path.py")

    home = tmp_path / "home-without-shared-state"
    write_watcher_finding(
        ship_repo / "data" / "watcher" / "findings.jsonl",
        ship_repo,
        "agents/legacy_path.py",
        "legacy123",
    )

    env = {**os.environ, "HOME": str(home)}
    env.pop("UNITARES_WATCHER_DATA_DIR", None)
    stdout, message = run_direct_ship(ship_repo, env=env)

    assert "[ship] appended Watcher-Findings trailer: legacy123" in stdout
    assert "Watcher-Findings: legacy123" in message

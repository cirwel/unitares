"""Tests for the merge-loss guard CI scripts (scripts/ci/).

Each guard is exercised end-to-end as a subprocess against a stub `gh`
binary on PATH, because the guards' whole job is orchestrating gh calls
and exit codes. The positive (guard fires) cases matter most: a guard
whose only tested property is "does not false-positive" can be a silent
no-op and pass — that exact failure shipped once in the client-side
pre-push guard (macOS has no `timeout`; every test passed by doing
nothing).
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts" / "ci"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

STUB_GH = """#!/usr/bin/env bash
d="$STUB_DATA"
printf '%s\\n' "$*" >> "$d/calls.log"
case "$*" in
  "label create"*) exit 0 ;;
  "pr list"*)
    [ -f "$d/pr_list.fail" ] && exit 1
    cat "$d/pr_list.json" 2>/dev/null || echo "[]" ;;
  "issue list"*)
    cat "$d/issue_list.json" 2>/dev/null || echo "[]" ;;
  "issue create"*) echo "https://github.com/example/repo/issues/999" ;;
  "issue comment"*|"issue edit"*|"issue close"*) exit 0 ;;
  "api repos/"*"/compare/"*)
    [ -f "$d/compare.fail" ] && exit 1
    cat "$d/compare.json" 2>/dev/null || exit 1 ;;
  "api repos/"*"/events?per_page=100&page=1")
    cat "$d/events_page1.json" 2>/dev/null || echo "[]" ;;
  "api repos/"*"/events?per_page=100&page="*) echo "[]" ;;
  "api repos/"*"/timeline?per_page=100&page=1")
    n=$(printf '%s' "$*" | sed -E 's|.*/issues/([0-9]+)/timeline.*|\\1|')
    cat "$d/timeline_$n.json" 2>/dev/null || echo "[]" ;;
  "api repos/"*"/timeline?per_page=100&page="*) echo "[]" ;;
  *) echo "stub gh: unhandled: $*" >&2; exit 64 ;;
esac
"""


@pytest.fixture
def guard_env(tmp_path):
    """PATH-front stub gh + isolated data dir + step-summary file."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    gh_path = stub_dir / "gh"
    gh_path.write_text(STUB_GH)
    gh_path.chmod(gh_path.stat().st_mode | stat.S_IEXEC)

    data = tmp_path / "data"
    data.mkdir()
    summary = tmp_path / "summary.md"
    summary.touch()

    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["STUB_DATA"] = str(data)
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    env["GITHUB_REPOSITORY"] = "example/repo"
    return env, data, summary


def run_guard(script: str, env: dict, **extra_env) -> subprocess.CompletedProcess:
    env = {**env, **{k: str(v) for k, v in extra_env.items()}}
    return subprocess.run(
        ["python3", str(SCRIPTS / script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def calls(data: Path) -> str:
    log = data / "calls.log"
    return log.read_text() if log.exists() else ""


# --- orphan_push_guard ------------------------------------------------------


def test_orphan_push_fires_on_merged_pr(guard_env):
    env, data, summary = guard_env
    (data / "pr_list.json").write_text(json.dumps(
        [{"number": 10, "state": "MERGED", "mergedAt": "2026-08-01T00:00:00Z", "url": "u"}]
    ))
    (data / "compare.json").write_text(json.dumps({
        "ahead_by": 1,
        "commits": [{"sha": "a" * 40, "commit": {"message": "fix: the lost work"}}],
    }))
    proc = run_guard("orphan_push_guard.py", env, BRANCH="claude/dead-branch", PUSHED_SHA="b" * 40)
    assert proc.returncode == 1, proc.stderr
    assert "issue create" in calls(data)
    assert "cherry-pick " + "a" * 40 in calls(data)
    assert "ORPHAN PUSH" in summary.read_text()


def test_orphan_push_comments_on_existing_finding(guard_env):
    env, data, _ = guard_env
    (data / "pr_list.json").write_text(json.dumps(
        [{"number": 10, "state": "CLOSED", "mergedAt": None, "url": "u"}]
    ))
    (data / "compare.json").write_text(json.dumps({"ahead_by": 0, "commits": []}))
    (data / "issue_list.json").write_text(json.dumps(
        [{"number": 55, "body": "<!-- finding-fingerprint: orphan-push-guard claude/dead-branch -->\nold"}]
    ))
    proc = run_guard("orphan_push_guard.py", env, BRANCH="claude/dead-branch", PUSHED_SHA="b" * 40)
    assert proc.returncode == 1
    assert "issue comment 55" in calls(data)
    assert "issue create" not in calls(data)


def test_orphan_push_allows_open_pr(guard_env):
    env, data, _ = guard_env
    (data / "pr_list.json").write_text(json.dumps([
        {"number": 10, "state": "MERGED", "mergedAt": "2026-08-01T00:00:00Z", "url": "u"},
        {"number": 12, "state": "OPEN", "mergedAt": None, "url": "u"},
    ]))
    proc = run_guard("orphan_push_guard.py", env, BRANCH="claude/live", PUSHED_SHA="b" * 40)
    assert proc.returncode == 0
    assert "issue create" not in calls(data)


def test_orphan_push_allows_pre_pr_branch(guard_env):
    env, data, _ = guard_env
    (data / "pr_list.json").write_text("[]")
    proc = run_guard("orphan_push_guard.py", env, BRANCH="claude/new", PUSHED_SHA="b" * 40)
    assert proc.returncode == 0
    assert "issue create" not in calls(data)


def test_orphan_push_degrades_visibly_on_api_failure(guard_env):
    env, data, summary = guard_env
    (data / "pr_list.fail").touch()
    proc = run_guard("orphan_push_guard.py", env, BRANCH="claude/x", PUSHED_SHA="b" * 40)
    assert proc.returncode == 0  # fail-open
    assert "::warning::" in proc.stdout
    assert "DEGRADED" in summary.read_text()  # ...but never silent


# --- merge_content_check ----------------------------------------------------


def _push_event(branch: str, head: str) -> dict:
    return {"type": "PushEvent", "payload": {"ref": f"refs/heads/{branch}", "head": head}}


def test_content_check_verifies_matching_head(guard_env):
    env, data, summary = guard_env
    (data / "events_page1.json").write_text(json.dumps([_push_event("claude/b", "c" * 40)]))
    proc = run_guard(
        "merge_content_check.py", env,
        PR_NUMBER=7, HEAD_BRANCH="claude/b", HEAD_SHA="c" * 40,
    )
    assert proc.returncode == 0
    assert "verified" in summary.read_text()


def test_content_check_fires_when_last_push_missing_from_merge(guard_env):
    env, data, summary = guard_env
    (data / "events_page1.json").write_text(json.dumps([_push_event("claude/b", "d" * 40)]))
    (data / "compare.json").write_text(json.dumps({
        "status": "ahead",
        "html_url": "cmp",
        "commits": [{"sha": "d" * 40, "commit": {"message": "the dropped commit"}}],
    }))
    proc = run_guard(
        "merge_content_check.py", env,
        PR_NUMBER=1610, HEAD_BRANCH="claude/b", HEAD_SHA="c" * 40,
    )
    assert proc.returncode == 1, proc.stderr
    assert "issue create" in calls(data)
    assert "CONTENT MISSING" in summary.read_text()


def test_content_check_accepts_contained_push(guard_env):
    env, data, _ = guard_env
    (data / "events_page1.json").write_text(json.dumps([_push_event("claude/b", "d" * 40)]))
    (data / "compare.json").write_text(json.dumps({"status": "behind", "commits": []}))
    proc = run_guard(
        "merge_content_check.py", env,
        PR_NUMBER=7, HEAD_BRANCH="claude/b", HEAD_SHA="c" * 40,
    )
    assert proc.returncode == 0
    assert "issue create" not in calls(data)


def test_content_check_degrades_visibly_outside_events_window(guard_env):
    env, data, summary = guard_env
    (data / "events_page1.json").write_text("[]")
    proc = run_guard(
        "merge_content_check.py", env,
        PR_NUMBER=7, HEAD_BRANCH="claude/gone", HEAD_SHA="c" * 40,
    )
    assert proc.returncode == 0
    assert "DEGRADED" in summary.read_text()
    assert "content-unverified" in summary.read_text()


# --- automerge_disarm_detector ----------------------------------------------


def test_disarm_detector_reports_disarmed_pr(guard_env):
    env, data, summary = guard_env
    (data / "pr_list.json").write_text(json.dumps([
        {"number": 20, "title": "stranded one", "isDraft": False, "autoMergeRequest": None, "url": "u20"},
        {"number": 21, "title": "armed one", "isDraft": False, "autoMergeRequest": {"enabledAt": "x"}, "url": "u21"},
        {"number": 22, "title": "draft one", "isDraft": True, "autoMergeRequest": None, "url": "u22"},
    ]))
    (data / "timeline_20.json").write_text(json.dumps([
        {"event": "auto_merge_enabled", "created_at": "2026-08-01T00:00:00Z"},
        {"event": "auto_merge_disabled", "created_at": "2026-08-02T00:00:00Z"},
    ]))
    proc = run_guard("automerge_disarm_detector.py", env)
    assert proc.returncode == 0, proc.stderr
    log = calls(data)
    assert "issue create" in log
    assert "#20" in log
    assert "#21" not in log  # currently armed: not a finding
    # never-armed PRs (no auto-merge events) are the draft-contract normal state
    assert "timeline" in log  # it actually looked


def test_disarm_detector_closes_tracker_when_clear(guard_env):
    env, data, _ = guard_env
    (data / "pr_list.json").write_text(json.dumps([
        {"number": 30, "title": "never armed", "isDraft": False, "autoMergeRequest": None, "url": "u"},
    ]))
    (data / "timeline_30.json").write_text("[]")
    (data / "issue_list.json").write_text(json.dumps(
        [{"number": 60, "body": "<!-- finding-fingerprint: automerge-disarm-detector -->\nold"}]
    ))
    proc = run_guard("automerge_disarm_detector.py", env)
    assert proc.returncode == 0
    assert "issue close 60" in calls(data)


def test_disarm_detector_updates_tracker_in_place(guard_env):
    env, data, _ = guard_env
    (data / "pr_list.json").write_text(json.dumps([
        {"number": 20, "title": "still stranded", "isDraft": False, "autoMergeRequest": None, "url": "u"},
    ]))
    (data / "timeline_20.json").write_text(json.dumps(
        [{"event": "auto_merge_disabled", "created_at": "2026-08-02T00:00:00Z"}]
    ))
    (data / "issue_list.json").write_text(json.dumps(
        [{"number": 61, "body": "<!-- finding-fingerprint: automerge-disarm-detector -->\nold"}]
    ))
    proc = run_guard("automerge_disarm_detector.py", env)
    assert proc.returncode == 0
    log = calls(data)
    assert "issue edit 61" in log
    assert "issue create" not in log


# --- workflow files ---------------------------------------------------------


@pytest.mark.parametrize(
    "workflow", ["orphan-push-guard.yml", "merge-content-check.yml", "automerge-disarm.yml"]
)
def test_workflow_parses_and_is_github_token_only(workflow):
    text = (WORKFLOWS / workflow).read_text()
    parsed = yaml.safe_load(text)
    assert "jobs" in parsed
    # execution-cost policy: no metered API keys anywhere near these guards
    assert "ANTHROPIC" not in text and "OPENAI" not in text
    assert "github.token" in text or workflow == "automerge-disarm.yml"

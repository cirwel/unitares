"""Tests for the merge-loss guard CI scripts (scripts/ci/).

Each guard is exercised end-to-end as a subprocess against a stub `gh`
binary on PATH, because the guards' whole job is orchestrating gh calls
and exit codes. The positive (guard fires) cases matter most: a guard
whose only tested property is "does not false-positive" can be a silent
no-op and pass — that exact failure shipped once in the client-side
pre-push guard (macOS has no `timeout`; every test passed by doing
nothing). The false-positive cases matter second-most: a guard that
alarms on hygiene trains readers to ignore it.

These are behavioral tests against stubbed responses; the response
SHAPES were separately ground-truthed against the live GitHub API on
2026-08-28 (state values, events payload, timeline ordering, compare
statuses, autoMergeRequest nullability).
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
  "api repos/"*"/timeline?per_page=100&page="*)
    n=$(printf '%s' "$*" | sed -E 's|.*/issues/([0-9]+)/timeline.*|\\1|')
    p=$(printf '%s' "$*" | sed -E 's|.*page=([0-9]+).*|\\1|')
    if [ -f "$d/timeline_repeat_$n" ]; then
      cat "$d/timeline_$n.json"
    elif [ "$p" = "1" ]; then
      cat "$d/timeline_$n.json" 2>/dev/null || echo "[]"
    else
      echo "[]"
    fi ;;
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


MERGED_PR = {
    "number": 10, "state": "MERGED", "mergedAt": "2026-08-01T00:00:00Z",
    "headRefOid": "e" * 40, "url": "u",
}


# --- orphan_push_guard ------------------------------------------------------


def test_orphan_push_fires_on_merged_pr_with_new_commits(guard_env):
    env, data, summary = guard_env
    (data / "pr_list.json").write_text(json.dumps([MERGED_PR]))
    (data / "compare.json").write_text(json.dumps({
        "status": "ahead",
        "commits": [{"sha": "a" * 40, "commit": {"message": "fix: the lost work"}}],
    }))
    proc = run_guard("orphan_push_guard.py", env, BRANCH="claude/dead-branch", PUSHED_SHA="b" * 40)
    assert proc.returncode == 1, proc.stderr
    log = calls(data)
    assert "issue create" in log
    # squash-safety: the orphan set is anchored on the merged PR's head,
    # never on an ancestry diff against the default branch
    assert f"compare/{'e' * 40}...{'b' * 40}" in log
    assert "cherry-pick " + "a" * 40 in log
    assert "ORPHAN PUSH" in summary.read_text()


def test_orphan_push_prunable_content_is_not_an_alarm(guard_env):
    # The audit's PRUNABLE class: dead branch, but the push adds nothing
    # beyond what the PR landed. Hygiene — must NOT red-X or file.
    env, data, summary = guard_env
    (data / "pr_list.json").write_text(json.dumps([MERGED_PR]))
    (data / "compare.json").write_text(json.dumps({"status": "behind", "commits": []}))
    proc = run_guard("orphan_push_guard.py", env, BRANCH="claude/dead-branch", PUSHED_SHA="b" * 40)
    assert proc.returncode == 0, proc.stderr
    assert "issue create" not in calls(data)
    assert "PRUNABLE" in summary.read_text()


def test_orphan_push_closed_unmerged_compares_against_default_branch(guard_env):
    # Nothing was squashed for a CLOSED-unmerged PR, so the whole unlanded
    # tail is the finding and the anchor is the default branch.
    env, data, _ = guard_env
    (data / "pr_list.json").write_text(json.dumps(
        [{"number": 10, "state": "CLOSED", "mergedAt": None, "headRefOid": "e" * 40, "url": "u"}]
    ))
    (data / "compare.json").write_text(json.dumps({
        "status": "ahead",
        "commits": [{"sha": "a" * 40, "commit": {"message": "unlanded work"}}],
    }))
    proc = run_guard(
        "orphan_push_guard.py", env,
        BRANCH="claude/dead-branch", PUSHED_SHA="b" * 40, DEFAULT_BRANCH="master",
    )
    assert proc.returncode == 1
    assert f"compare/master...{'b' * 40}" in calls(data)


def test_orphan_push_comments_on_existing_finding(guard_env):
    env, data, _ = guard_env
    (data / "pr_list.json").write_text(json.dumps([MERGED_PR]))
    (data / "compare.json").write_text(json.dumps({
        "status": "ahead",
        "commits": [{"sha": "a" * 40, "commit": {"message": "more lost work"}}],
    }))
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
        MERGED_PR,
        {"number": 12, "state": "OPEN", "mergedAt": None, "headRefOid": "f" * 40, "url": "u"},
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


def test_orphan_push_compare_failure_still_alarms_as_indeterminate(guard_env):
    env, data, _ = guard_env
    (data / "pr_list.json").write_text(json.dumps([MERGED_PR]))
    (data / "compare.fail").touch()
    proc = run_guard("orphan_push_guard.py", env, BRANCH="claude/dead", PUSHED_SHA="b" * 40)
    assert proc.returncode == 1  # dead-branch push is anomalous regardless
    assert "INDETERMINATE" in calls(data)  # ...but the recipe must not blind cherry-pick


def test_orphan_push_degrades_visibly_on_api_failure(guard_env):
    env, data, summary = guard_env
    (data / "pr_list.fail").touch()
    proc = run_guard("orphan_push_guard.py", env, BRANCH="claude/x", PUSHED_SHA="b" * 40)
    assert proc.returncode == 0  # fail-open
    assert "::warning::" in proc.stdout
    assert "DEGRADED" in summary.read_text()  # ...but never silent


# --- merge_content_check ----------------------------------------------------


def _push_event(branch: str, head: str, created_at: str = "2026-08-01T00:00:00Z") -> dict:
    return {
        "type": "PushEvent",
        "created_at": created_at,
        "payload": {"ref": f"refs/heads/{branch}", "head": head},
    }


def test_content_check_no_contradiction_on_matching_head(guard_env):
    env, data, summary = guard_env
    (data / "events_page1.json").write_text(json.dumps([_push_event("claude/b", "c" * 40)]))
    proc = run_guard(
        "merge_content_check.py", env,
        PR_NUMBER=7, HEAD_BRANCH="claude/b", HEAD_SHA="c" * 40,
        MERGED_AT="2026-08-02T00:00:00Z",
    )
    assert proc.returncode == 0
    assert "no contradiction" in summary.read_text()


def test_content_check_picks_newest_push_by_created_at_not_array_order(guard_env):
    # Live finding 2026-08-28: the events array is id-ordered and within-page
    # created_at inversions are common. The NEWER push (later created_at,
    # later array position) must win; picking the first array match would
    # wrongly "verify" against the older, contained push.
    env, data, summary = guard_env
    (data / "events_page1.json").write_text(json.dumps([
        _push_event("claude/b", "d" * 40, "2026-08-01T10:00:00Z"),  # older, first in array
        _push_event("claude/b", "f" * 40, "2026-08-01T12:00:00Z"),  # newest push
    ]))
    (data / "compare.json").write_text(json.dumps({
        "status": "ahead",
        "html_url": "cmp",
        "commits": [{"sha": "f" * 40, "commit": {"message": "the dropped commit"}}],
    }))
    proc = run_guard(
        "merge_content_check.py", env,
        PR_NUMBER=7, HEAD_BRANCH="claude/b", HEAD_SHA="d" * 40,
        MERGED_AT="2026-08-02T00:00:00Z",
    )
    assert proc.returncode == 1, proc.stderr
    assert f"compare/{'d' * 40}...{'f' * 40}" in calls(data)
    assert "CONTENT MISSING" in summary.read_text()


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
        MERGED_AT="2026-08-02T00:00:00Z",
    )
    assert proc.returncode == 1, proc.stderr
    assert "issue create" in calls(data)
    assert "CONTENT MISSING" in summary.read_text()


def test_content_check_routes_post_merge_push_to_orphan_finding(guard_env):
    # A push AFTER the merge is the orphan mode, not a merge-content loss:
    # it must not be labeled as the #1610 mode, and it must dedup into the
    # orphan-push-guard finding (covers legacy branches without the push guard).
    env, data, summary = guard_env
    (data / "events_page1.json").write_text(json.dumps(
        [_push_event("claude/b", "d" * 40, "2026-08-02T01:00:00Z")]  # after merge
    ))
    proc = run_guard(
        "merge_content_check.py", env,
        PR_NUMBER=7, HEAD_BRANCH="claude/b", HEAD_SHA="c" * 40,
        MERGED_AT="2026-08-02T00:00:00Z",
    )
    assert proc.returncode == 1, proc.stderr
    log = calls(data)
    assert "issue create" in log
    assert "orphan-push-guard claude/b" in log  # guard-1's fingerprint, shared
    assert "1610" not in log  # no false causal attribution
    assert "POST-MERGE PUSH" in summary.read_text()


def test_content_check_accepts_contained_push(guard_env):
    env, data, _ = guard_env
    (data / "events_page1.json").write_text(json.dumps([_push_event("claude/b", "d" * 40)]))
    (data / "compare.json").write_text(json.dumps({"status": "behind", "commits": []}))
    proc = run_guard(
        "merge_content_check.py", env,
        PR_NUMBER=7, HEAD_BRANCH="claude/b", HEAD_SHA="c" * 40,
        MERGED_AT="2026-08-02T00:00:00Z",
    )
    assert proc.returncode == 0
    assert "issue create" not in calls(data)


def test_content_check_degrades_visibly_outside_events_window(guard_env):
    env, data, summary = guard_env
    (data / "events_page1.json").write_text("[]")
    proc = run_guard(
        "merge_content_check.py", env,
        PR_NUMBER=7, HEAD_BRANCH="claude/gone", HEAD_SHA="c" * 40,
        MERGED_AT="2026-08-02T00:00:00Z",
    )
    assert proc.returncode == 0
    assert "DEGRADED" in summary.read_text()
    assert "content-unverified" in summary.read_text()


# --- automerge_disarm_detector ----------------------------------------------


def test_disarm_detector_reports_disarmed_pr(guard_env):
    env, data, summary = guard_env
    (data / "pr_list.json").write_text(json.dumps([
        {"number": 20, "title": "stranded one", "isDraft": False, "autoMergeRequest": None, "labels": [], "url": "u20"},
        {"number": 21, "title": "armed one", "isDraft": False, "autoMergeRequest": {"enabledAt": "x"}, "labels": [], "url": "u21"},
        {"number": 22, "title": "draft one", "isDraft": True, "autoMergeRequest": None, "labels": [], "url": "u22"},
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


def test_disarm_detector_honors_hold_label(guard_env):
    env, data, summary = guard_env
    (data / "pr_list.json").write_text(json.dumps([
        {"number": 20, "title": "deliberate hold", "isDraft": False, "autoMergeRequest": None,
         "labels": [{"name": "automerge-hold"}], "url": "u"},
    ]))
    proc = run_guard("automerge_disarm_detector.py", env)
    assert proc.returncode == 0
    log = calls(data)
    assert "timeline" not in log  # suppressed before the timeline read
    assert "issue create" not in log


def test_disarm_detector_truncated_timeline_is_unverifiable_not_healthy(guard_env):
    # A timeline longer than the page cap must land in UNVERIFIABLE — a
    # stale auto_merge_enabled inside the read window must not win.
    env, data, _ = guard_env
    (data / "pr_list.json").write_text(json.dumps([
        {"number": 23, "title": "very long PR", "isDraft": False, "autoMergeRequest": None, "labels": [], "url": "u"},
    ]))
    full_page = [{"event": "commented", "created_at": "2026-08-01T00:00:00Z"}] * 99
    full_page.append({"event": "auto_merge_enabled", "created_at": "2026-08-01T01:00:00Z"})
    (data / "timeline_23.json").write_text(json.dumps(full_page))  # always 100 items
    (data / "timeline_repeat_23").touch()  # stub serves it for every page
    proc = run_guard("automerge_disarm_detector.py", env)
    assert proc.returncode == 0, proc.stderr
    log = calls(data)
    assert "issue create" in log
    assert "UNKNOWN" in log  # listed as unverifiable in the tracking issue


def test_disarm_detector_closes_tracker_when_clear(guard_env):
    env, data, _ = guard_env
    (data / "pr_list.json").write_text(json.dumps([
        {"number": 30, "title": "never armed", "isDraft": False, "autoMergeRequest": None, "labels": [], "url": "u"},
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
        {"number": 20, "title": "still stranded", "isDraft": False, "autoMergeRequest": None, "labels": [], "url": "u"},
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
    # execution-cost policy: GITHUB_TOKEN wired, no metered API keys
    assert "github.token" in text
    assert "ANTHROPIC" not in text and "OPENAI" not in text

"""pr-babysitter.sh keeps exactly one maintainer-approved PR armed at a time.

The script is driven against a fake `gh` on PATH. Reads (`gh pr list`,
`gh pr view`, `gh api .../commits/<base>`, `gh api .../timeline`) answer from
fixture files; every other call is recorded, so the tests assert exactly what
the script would have mutated.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts/ops/pr-babysitter.sh"
LABEL = "approved-to-merge"
RETRIED = "merge-retried"

FAKE_GH = r"""#!/usr/bin/env bash
d="$FAKE_GH_DIR"
case "$1 $2" in
  "pr list") cat "$d/prs.json"; exit 0 ;;
  "pr view")
    f="$d/state_${5//\//_}_$3"
    [ -f "$f" ] && { cat "$f"; exit 0; }
    exit 1 ;;
esac
if [ "$1" = "api" ]; then
  case "$*" in
    */timeline*)
      n=$(sed -E 's#.*issues/([0-9]+)/timeline.*#\1#' <<<"$*")
      cat "$d/timeline_$n.json" 2>/dev/null || echo '[]'
      exit 0 ;;
    */commits/*) cat "$d/base_date"; exit 0 ;;
  esac
fi
echo "$*" >> "$d/calls.log"
if [ -f "$d/fail" ]; then
  while IFS= read -r pattern; do
    [ -n "$pattern" ] && [[ "$*" == *"$pattern"* ]] && exit 1
  done < "$d/fail"
fi
exit 0
"""

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="pr-babysitter.sh needs bash and jq",
)


def _iso(minutes_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _check(name: str, conclusion: str = "SUCCESS", run: int = 100, status: str = "COMPLETED") -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "detailsUrl": f"https://github.com/o/r/actions/runs/{run}/job/{run * 10}",
    }


def _pr(
    number: int,
    *,
    labels: tuple[str, ...] = (LABEL,),
    draft: bool = False,
    armed_min_ago: float | None = None,
    mergeable: str = "MERGEABLE",
    state: str = "BEHIND",
    base: str = "master",
    checks: list[dict] | None = None,
    body: str = "",
) -> dict:
    return {
        "number": number,
        "isDraft": draft,
        "mergeable": mergeable,
        "mergeStateStatus": state,
        "autoMergeRequest": (
            {"mergeMethod": "SQUASH", "enabledAt": _iso(armed_min_ago)} if armed_min_ago is not None else None
        ),
        "baseRefName": base,
        "labels": [{"name": label} for label in labels],
        "statusCheckRollup": checks if checks is not None else [_check("test")],
        "body": body,
    }


def _timeline(labelled_min_ago: float | None = 60, pushed_min_ago: float | None = 120, updater_min_ago=None) -> list:
    events: list[dict] = []
    if pushed_min_ago is not None:
        events.append(
            {"event": "committed", "committer": {"name": "Kenny", "date": _iso(pushed_min_ago)}, "message": "fix: x"}
        )
    if updater_min_ago is not None:
        # GitHub's updater / `gh pr update-branch`: never counts as a push.
        events.append(
            {
                "event": "committed",
                "committer": {"name": "GitHub", "date": _iso(updater_min_ago)},
                "message": "Merge branch 'master' into claude/x",
            }
        )
    if labelled_min_ago is not None:
        events.append({"event": "labeled", "label": {"name": LABEL}, "created_at": _iso(labelled_min_ago)})
    return events


def _run(
    tmp_path: Path,
    prs: list[dict],
    *,
    timelines: dict[int, list] | None = None,
    base_idle_min: float = 1,
    states: dict[str, str] | None = None,
    fail: tuple[str, ...] = (),
    expect_rc: int = 0,
    **env: str,
) -> tuple[list[str], str]:
    d = tmp_path / "gh"
    d.mkdir(exist_ok=True)
    gh = d / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    (d / "prs.json").write_text(json.dumps(prs))
    (d / "base_date").write_text(_iso(base_idle_min) + "\n")
    (d / "calls.log").write_text("")
    (d / "fail").write_text("\n".join(fail) + "\n")
    timelines = timelines or {}
    for pr in prs:
        (d / f"timeline_{pr['number']}.json").write_text(json.dumps(timelines.get(pr["number"], _timeline())))
    for key, value in (states or {}).items():
        repo, number = key.split("#")
        (d / f"state_{repo.replace('/', '_')}_{number}").write_text(value + "\n")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            **os.environ,
            "PATH": f"{d}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_DIR": str(d),
            "PR_BABYSITTER_REPO": "o/r",
            **env,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expect_rc, result.stdout + result.stderr
    return [line for line in (d / "calls.log").read_text().splitlines() if line], result.stdout


def _arm(n: int) -> str:
    return f"pr merge {n} -R o/r --auto --squash"


# --- the queue -----------------------------------------------------------------


def test_arms_in_approval_order_not_pr_number(tmp_path: Path) -> None:
    calls, _ = _run(
        tmp_path,
        [_pr(10), _pr(12), _pr(11)],
        timelines={10: _timeline(30), 12: _timeline(90), 11: _timeline(60)},
    )
    assert calls == [_arm(12)]


def test_never_arms_unlabelled_draft_or_stacked_prs(tmp_path: Path) -> None:
    calls, _ = _run(
        tmp_path,
        [_pr(5, labels=()), _pr(6, draft=True), _pr(7, labels=("automerge-hold",)), _pr(8, base="claude/parent")],
    )
    assert calls == []


def test_arming_never_asks_gh_to_delete_a_branch(tmp_path: Path) -> None:
    # The repo deletes merged branches itself; gh's --delete-branch would also
    # touch a local branch in launchd's working directory, the deploy tree.
    calls, _ = _run(tmp_path, [_pr(1)])
    assert calls == [_arm(1)] and "--delete-branch" not in calls[0]


def test_a_push_after_approval_makes_it_stale(tmp_path: Path) -> None:
    calls, out = _run(
        tmp_path,
        [_pr(1), _pr(2)],
        timelines={1: _timeline(labelled_min_ago=60, pushed_min_ago=30), 2: _timeline(50)},
    )
    assert calls == [_arm(2)]
    assert "#1 has a commit from" in out and "re-apply approved-to-merge" in out


def test_base_update_merges_do_not_make_approval_stale(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(1)], timelines={1: _timeline(60, 120, updater_min_ago=10)})
    assert calls == [_arm(1)]


def test_merge_after_an_open_pr_waits(tmp_path: Path) -> None:
    calls, out = _run(
        tmp_path,
        [_pr(1, body="Merge after #7 lands."), _pr(2, body="merge after owner/plugin#165")],
        timelines={1: _timeline(90), 2: _timeline(60)},
        states={"o/r#7": "OPEN", "owner/plugin#165": "OPEN"},
    )
    assert calls == []
    assert "#1 waits on o/r#7 (OPEN)" in out
    assert "#2 waits on owner/plugin#165 (OPEN)" in out


def test_merge_after_a_merged_pr_proceeds(tmp_path: Path) -> None:
    calls, _ = _run(
        tmp_path, [_pr(1, body="Merge after owner/plugin#165.")], states={"owner/plugin#165": "MERGED"}
    )
    assert calls == [_arm(1)]


def test_an_unreadable_dependency_fails_closed(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(1, body="merge after #9")])
    assert calls == []
    assert "UNREADABLE" in out


def test_unknown_mergeability_holds_the_order(tmp_path: Path) -> None:
    # Right after a merge GitHub is still computing; a later PR must not jump ahead.
    calls, _ = _run(
        tmp_path, [_pr(1, mergeable="UNKNOWN"), _pr(2)], timelines={1: _timeline(90), 2: _timeline(60)}
    )
    assert calls == []


def test_conflicting_is_skipped_to_the_next(tmp_path: Path) -> None:
    calls, out = _run(
        tmp_path, [_pr(1, mergeable="CONFLICTING"), _pr(2)], timelines={1: _timeline(90), 2: _timeline(60)}
    )
    assert calls == [_arm(2)]
    assert "#1 queued but CONFLICTING" in out


def test_a_run_waiting_for_approval_is_skipped_not_rerun(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(1, state="BLOCKED", checks=[_check("test", "ACTION_REQUIRED")])])
    assert calls == []
    assert "ACTION_REQUIRED" in out


def test_pending_checks_do_not_block_arming(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(8, state="BLOCKED", checks=[_check("test", "", status="IN_PROGRESS")])])
    assert calls == [_arm(8)]


# --- failed checks ---------------------------------------------------------------


def test_failed_on_an_up_to_date_head_is_marked_then_rerun_once(tmp_path: Path) -> None:
    checks = [
        _check("test", "FAILURE", run=77),
        _check("smoke", "FAILURE", run=77),
        _check("scope", "TIMED_OUT", run=78),
        _check("validate", "SUCCESS", run=79),
    ]
    calls, _ = _run(
        tmp_path,
        [_pr(9, state="BLOCKED", checks=checks), _pr(10)],
        timelines={9: _timeline(90), 10: _timeline(60)},
    )
    assert calls == [
        f"pr edit 9 -R o/r --add-label {RETRIED}",
        "run rerun 77 --failed -R o/r",
        "run rerun 78 --failed -R o/r",
        # The retrying PR does not hold up the rest of the queue.
        _arm(10),
    ]


def test_failed_on_a_behind_head_is_armed_for_a_fresh_run(tmp_path: Path) -> None:
    # Re-running the stale head is wasted: GitHub re-runs everything on update.
    calls, _ = _run(tmp_path, [_pr(9, state="BEHIND", checks=[_check("test", "FAILURE")])])
    assert calls == [_arm(9)]


def test_failed_while_its_run_is_still_going_waits(tmp_path: Path) -> None:
    checks = [_check("smoke", "FAILURE", run=77), _check("shard", "", run=77, status="IN_PROGRESS")]
    calls, _ = _run(tmp_path, [_pr(9, state="BLOCKED", checks=checks)])
    assert calls == []


def test_failed_after_its_retry_is_skipped(tmp_path: Path) -> None:
    calls, out = _run(
        tmp_path, [_pr(9, labels=(LABEL, RETRIED), state="BLOCKED", checks=[_check("test", "FAILURE")])]
    )
    assert calls == []
    assert "still failing after one retry" in out


def test_no_rerun_without_the_marker(tmp_path: Path) -> None:
    # If the marker cannot land (label missing, API error), re-running would repeat every tick.
    calls, out = _run(
        tmp_path, [_pr(9, state="BLOCKED", checks=[_check("test", "FAILURE")])], fail=("--add-label",)
    )
    assert calls == [f"pr edit 9 -R o/r --add-label {RETRIED}"]
    assert "not re-running" in out


def test_a_retry_that_started_nothing_is_not_spent(tmp_path: Path) -> None:
    calls, out = _run(
        tmp_path, [_pr(9, state="BLOCKED", checks=[_check("test", "FAILURE", run=77)])], fail=("run rerun",)
    )
    assert calls == [
        f"pr edit 9 -R o/r --add-label {RETRIED}",
        "run rerun 77 --failed -R o/r",
        f"pr edit 9 -R o/r --remove-label {RETRIED}",
    ]
    assert "retry not spent" in out


def test_failing_status_context_without_a_run_is_skipped(tmp_path: Path) -> None:
    context = {"__typename": "StatusContext", "context": "ext", "state": "ERROR", "targetUrl": "https://ci.example/1"}
    calls, out = _run(tmp_path, [_pr(9, state="BLOCKED", checks=[context])])
    assert calls == []
    assert "not Actions runs" in out


def test_a_failed_arm_stops_the_tick(tmp_path: Path) -> None:
    # A failed call may still have armed it; arming the next could leave two armed.
    calls, out = _run(
        tmp_path, [_pr(1), _pr(2)], timelines={1: _timeline(90), 2: _timeline(60)}, fail=("pr merge 1",)
    )
    assert calls == [_arm(1)]
    assert "#1 arm failed" in out


# --- the slot -------------------------------------------------------------------


def test_an_armed_pr_holds_the_queue_even_when_armed_by_hand(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, labels=(), armed_min_ago=5, state="BLOCKED"), _pr(4)])
    assert calls == []


def test_an_approved_armed_pr_that_conflicts_is_disarmed_and_the_queue_moves(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(3, armed_min_ago=20, mergeable="CONFLICTING"), _pr(4)])
    assert calls == ["pr merge 3 -R o/r --disable-auto", _arm(4)]
    assert "#3 armed but CONFLICTING" in out


def test_an_approved_armed_pr_that_failed_on_its_head_is_disarmed(tmp_path: Path) -> None:
    calls, _ = _run(
        tmp_path, [_pr(3, armed_min_ago=20, state="BLOCKED", checks=[_check("test", "FAILURE")]), _pr(4)]
    )
    assert calls == ["pr merge 3 -R o/r --disable-auto", _arm(4)]


def test_an_armed_pr_failed_on_a_stale_head_keeps_the_slot(tmp_path: Path) -> None:
    calls, _ = _run(
        tmp_path, [_pr(3, armed_min_ago=20, state="BEHIND", checks=[_check("test", "FAILURE")]), _pr(4)]
    )
    assert calls == []


def test_a_hand_armed_pr_is_never_disarmed(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, labels=(), armed_min_ago=20, mergeable="CONFLICTING"), _pr(4)])
    assert calls == [_arm(4)]


def test_a_failed_disarm_stops_the_tick(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, armed_min_ago=20, mergeable="CONFLICTING"), _pr(4)], fail=("--disable-auto",))
    assert calls == ["pr merge 3 -R o/r --disable-auto"]


def test_behind_holder_is_left_to_github_inside_the_grace(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, armed_min_ago=30, state="BEHIND")], base_idle_min=5)
    assert calls == []


def test_grace_counts_from_the_arming_when_that_is_later(tmp_path: Path) -> None:
    # Armed a minute ago against a base that has been still for an hour.
    calls, _ = _run(tmp_path, [_pr(3, armed_min_ago=1, state="BEHIND")], base_idle_min=60)
    assert calls == []


def test_behind_holder_is_updated_once_both_have_sat_idle(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, armed_min_ago=30, state="BEHIND"), _pr(4)], base_idle_min=20)
    assert calls == ["pr update-branch 3 -R o/r"]


def test_a_long_hold_is_logged(tmp_path: Path) -> None:
    _, out = _run(tmp_path, [_pr(3, armed_min_ago=120, state="BLOCKED")])
    assert "#3 has held the queue for 12" in out


# --- plumbing -------------------------------------------------------------------


def test_dry_run_mutates_nothing(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(10)], PR_QUEUE_DRY_RUN="1")
    assert calls == []
    assert "dry-run: gh pr merge 10" in out


def test_gh_list_failure_does_nothing(tmp_path: Path) -> None:
    d = tmp_path / "gh"
    d.mkdir()
    gh = d / "gh"
    gh.write_text('#!/usr/bin/env bash\n[ "$1 $2" = "pr list" ] && exit 1\necho "$*" >> "$FAKE_GH_DIR/calls.log"\n')
    gh.chmod(0o755)
    (d / "calls.log").write_text("")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "PATH": f"{d}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_DIR": str(d)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert (d / "calls.log").read_text() == ""

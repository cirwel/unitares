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
    */pulls/*/commits*)
      n=$(sed -E 's#.*pulls/([0-9]+)/commits.*#\1#' <<<"$*")
      cat "$d/commits_$n.json" 2>/dev/null || echo '[]'
      exit 0 ;;
    */compare/*)
      sha="${2##*...}"
      echo "$2" >> "$d/compares.log"
      f="$d/compare_$sha.json"
      [ -f "$f" ] && { cat "$f"; exit 0; }
      [ -f "$d/compare_default_missing" ] && exit 1
      printf '{"files":[{"filename":"f","status":"modified","sha":"b%s","patch":"@@ -1 +1 @@\\n+%s"}]}' "$sha" "$sha"
      exit 0 ;;
    */issues/*/comments*)
      n=$(sed -E 's#.*issues/([0-9]+)/comments.*#\1#' <<<"$*")
      [ -f "$d/comments_$n.fail" ] && exit 1
      cat "$d/comments_$n.txt" 2>/dev/null
      exit 0 ;;
    */contents/*)
      [ -f "$d/manifest_remote.tsv" ] || exit 1
      base64 < "$d/manifest_remote.tsv" | tr -d '\n'; echo
      exit 0 ;;
    */timeline*)
      n=$(sed -E 's#.*issues/([0-9]+)/timeline.*#\1#' <<<"$*")
      [ -f "$d/timeline_$n.fail" ] && exit 1
      cat "$d/timeline_$n.json" 2>/dev/null || echo '[]'
      exit 0 ;;
    */commits/*)
      # A later read in the same tick may see master moved (base_moved).
      if [ -f "$d/base_moved" ] && [ -f "$d/base_read_once" ]; then cat "$d/base_moved"; else cat "$d/base_date"; fi
      : > "$d/base_read_once"
      exit 0 ;;
  esac
fi
printf '%s\n' "${*//$'\n'/ }" >> "$d/calls.log"
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
    state: str = "BLOCKED",
    base: str = "master",
    checks: list[dict] | None = None,
    body: str = "",
    head: str | None = None,
    review: str | None = "SUCCESS",
    fork: bool = False,
) -> dict:
    rollup = list(checks) if checks is not None else [_check("test")]
    if review is not None:
        rollup.append(_check("review", review, run=900))
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
        "statusCheckRollup": rollup,
        "body": body,
        "headRefOid": head or f"sha{number}",
        "isCrossRepository": fork,
    }


def _timeline(labelled_min_ago: float | None = 10, pushed_min_ago: float | None = 20, updater_min_ago=None) -> list:
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


def _empty_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.tsv"
    if not path.exists():
        path.write_text("# path<TAB>symbol_regex<TAB>why\n")
    return path


def _run(
    tmp_path: Path,
    prs: list[dict],
    *,
    timelines: dict[int, list] | None = None,
    base_idle_min: float = 1,
    states: dict[str, str] | None = None,
    fail: tuple[str, ...] = (),
    compares: dict[str, dict | None] | None = None,
    timeline_fails: tuple[int, ...] = (),
    arms: dict[int, float] | None = None,
    expect_rc: int = 0,
    **env: str,
) -> tuple[list[str], str]:
    d = tmp_path / "gh"
    d.mkdir(exist_ok=True)
    gh = d / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    (d / "prs.json").write_text(json.dumps(prs))
    (d / "base_read_once").unlink(missing_ok=True)
    (d / "base_date").write_text(_iso(base_idle_min) + "\n")
    (d / "calls.log").write_text("")
    (d / "fail").write_text("\n".join(fail) + "\n")
    timelines = timelines or {}
    for pr in prs:
        (d / f"timeline_{pr['number']}.json").write_text(json.dumps(timelines.get(pr["number"], _timeline())))
    for stale in d.glob("timeline_*.fail"):
        stale.unlink()
    for number in timeline_fails:
        (d / f"timeline_{number}.fail").write_text("")
    for sha, payload in (compares or {}).items():
        target = d / f"compare_{sha}.json"
        # None: GitHub will not return it (unknown SHA, API error).
        target.write_text("not json" if payload is None else json.dumps(payload))
    state_file = tmp_path / "state" / "approvals"
    if arms:
        # Arms this script made: the arming time GitHub reports must match.
        state_file.parent.mkdir(exist_ok=True)
        with (tmp_path / "state" / "approvals.arms").open("a") as handle:
            for number, minutes_ago in arms.items():
                handle.write(f"{number} {_iso(minutes_ago)}\n")
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
            "PR_QUEUE_STATE_FILE": str(state_file),
            "PR_QUEUE_NOTIFY": "0",
            "PR_QUEUE_SENSITIVITY_MANIFEST": str(_empty_manifest(tmp_path)),
            **env,
        } if "PR_QUEUE_SENSITIVITY_MANIFEST_UNSET" not in env else {
            **{k: v for k, v in os.environ.items() if k != "PR_QUEUE_SENSITIVITY_MANIFEST"},
            "PATH": f"{d}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_DIR": str(d),
            "PR_BABYSITTER_REPO": "o/r",
            "PR_QUEUE_STATE_FILE": str(state_file),
            "PR_QUEUE_NOTIFY": "0",
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expect_rc, result.stdout + result.stderr
    return [line for line in (d / "calls.log").read_text().splitlines() if line], result.stdout


def _arm(n: int, head: str | None = None) -> str:
    return f"pr merge {n} -R o/r --auto --squash --match-head-commit {head or f'sha{n}'}"



# --- the queue -----------------------------------------------------------------


def test_arms_in_approval_order_not_pr_number(tmp_path: Path) -> None:
    calls, _ = _run(
        tmp_path,
        [_pr(10), _pr(12), _pr(11)],
        timelines={10: _timeline(5), 12: _timeline(12), 11: _timeline(8)},
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
        timelines={1: _timeline(labelled_min_ago=10, pushed_min_ago=5), 2: _timeline(8)},
    )
    assert calls == [_arm(2)]
    assert "#1 has a commit from" in out and "re-apply approved-to-merge" in out


def test_base_update_merges_do_not_make_approval_stale(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(1)], timelines={1: _timeline(10, 20, updater_min_ago=5)})
    assert calls == [_arm(1)]


def test_merge_after_an_open_pr_waits(tmp_path: Path) -> None:
    calls, out = _run(
        tmp_path,
        [_pr(1, body="Merge after #7 lands."), _pr(2, body="merge after owner/plugin#165")],
        timelines={1: _timeline(12), 2: _timeline(8)},
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
        tmp_path, [_pr(1, mergeable="UNKNOWN"), _pr(2)], timelines={1: _timeline(12), 2: _timeline(8)}
    )
    assert calls == []


def test_conflicting_is_skipped_to_the_next(tmp_path: Path) -> None:
    calls, out = _run(
        tmp_path, [_pr(1, mergeable="CONFLICTING"), _pr(2)], timelines={1: _timeline(12), 2: _timeline(8)}
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
        timelines={9: _timeline(12), 10: _timeline(8)},
    )
    assert calls == [
        f"pr edit 9 -R o/r --add-label {RETRIED}",
        "run rerun 77 --failed -R o/r",
        "run rerun 78 --failed -R o/r",
        # The retrying PR does not hold up the rest of the queue.
        _arm(10),
    ]


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


def test_a_failed_rollback_says_the_retry_is_spent(tmp_path: Path) -> None:
    _, out = _run(
        tmp_path,
        [_pr(9, state="BLOCKED", checks=[_check("test", "FAILURE", run=77)])],
        fail=("run rerun", "--remove-label"),
    )
    assert "could not be removed; remove it by hand" in out
    assert "retry not spent" not in out


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
        tmp_path, [_pr(1), _pr(2)], timelines={1: _timeline(12), 2: _timeline(8)}, fail=("pr merge 1",)
    )
    assert calls == [_arm(1)]
    assert "#1 arm failed" in out


# --- the slot -------------------------------------------------------------------


def test_an_armed_pr_holds_the_queue_even_when_armed_by_hand(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, labels=(), armed_min_ago=5, state="BLOCKED"), _pr(4)])
    assert calls == []


def test_an_approved_armed_pr_that_conflicts_is_disarmed_and_the_queue_moves(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(3, armed_min_ago=20, mergeable="CONFLICTING"), _pr(4)], arms={3: 20})
    assert calls == ["pr merge 3 -R o/r --disable-auto", _arm(4)]
    assert "#3 armed but CONFLICTING" in out


def test_an_approved_armed_pr_that_failed_on_its_head_is_disarmed(tmp_path: Path) -> None:
    calls, _ = _run(
        tmp_path, [_pr(3, armed_min_ago=20, state="BLOCKED", checks=[_check("test", "FAILURE")]), _pr(4)],
        arms={3: 20},
    )
    assert calls == ["pr merge 3 -R o/r --disable-auto", _arm(4)]


def test_an_armed_pr_failed_on_a_stale_head_keeps_the_slot(tmp_path: Path) -> None:
    calls, _ = _run(
        tmp_path, [_pr(3, armed_min_ago=20, state="BEHIND", checks=[_check("test", "FAILURE")]), _pr(4)]
    )
    assert calls == []


def test_a_hand_armed_pr_is_never_disarmed_and_keeps_the_slot_while_conflicting(tmp_path: Path) -> None:
    # Arming #4 would leave two armed the moment #3's conflict is resolved.
    calls, _ = _run(tmp_path, [_pr(3, labels=(), armed_min_ago=20, mergeable="CONFLICTING"), _pr(4)])
    assert calls == []


def test_an_approved_armed_pr_parked_for_approval_is_disarmed(tmp_path: Path) -> None:
    calls, _ = _run(
        tmp_path, [_pr(3, armed_min_ago=20, state="BLOCKED", checks=[_check("review", "ACTION_REQUIRED")]), _pr(4)],
        arms={3: 20},
    )
    assert calls == ["pr merge 3 -R o/r --disable-auto", _arm(4)]


def test_a_zero_arming_time_is_not_a_decades_long_hold(tmp_path: Path) -> None:
    pr = _pr(3, armed_min_ago=5, state="BLOCKED")
    pr["autoMergeRequest"]["enabledAt"] = "0001-01-01T00:00:00Z"
    _, out = _run(tmp_path, [pr])
    assert "held the queue" not in out


def test_a_failed_disarm_stops_the_tick(tmp_path: Path) -> None:
    calls, _ = _run(
        tmp_path, [_pr(3, armed_min_ago=20, mergeable="CONFLICTING"), _pr(4)], fail=("--disable-auto",), arms={3: 20}
    )
    assert calls == ["pr merge 3 -R o/r --disable-auto"]


def test_behind_holder_is_left_to_github_inside_the_grace(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, armed_min_ago=30, state="BEHIND")], base_idle_min=1)
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


# --- the approved head ------------------------------------------------------------


def test_first_sight_pins_the_head_and_arms_that_head(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(1, head="aaa")])
    assert calls == [_arm(1, "aaa")]
    pins = (tmp_path / "state" / "approvals").read_text().split()
    assert pins[:2] == ["1", "aaa"] and len(pins) == 4


def _files(*entries: tuple) -> dict:
    return {"files": [dict(zip(("filename", "status", "sha", "patch"), e)) for e in entries]}


CHANGE_A = _files(("f", "modified", "blob1", "@@ -1,3 +1,3 @@ def g():\n ctx\n-old\n+new"))
# The same change after a clean base update: new blob id, line numbers, and a
# context line that master changed nearby.
CHANGE_A_REBASED = _files(("f", "modified", "blob9", "@@ -40,3 +40,3 @@ def h():\n ctx2\n-old\n+new"))
CHANGE_B = _files(("f", "modified", "blob2", "@@ -1,3 +1,4 @@ def g():\n ctx\n-old\n+new\n+sneaky"))
BINARY_A = _files(("logo.png", "modified", "blobA", None))
BINARY_B = _files(("logo.png", "modified", "blobB", None))


def _two_ticks(tmp_path: Path, first: dict, second: dict | None):
    # Tick 1: #3 (hand-armed) holds the slot, so #1 is only pinned at aaa.
    tl = _timeline(10, 20)
    _run(tmp_path, [_pr(3, labels=(), armed_min_ago=5, state="BLOCKED"), _pr(1, head="aaa")],
         timelines={1: tl}, compares={"aaa": first})
    # Tick 2: the slot is free and #1's head has moved to bbb.
    return _run(tmp_path, [_pr(1, head="bbb")], timelines={1: tl}, compares={"aaa": first, "bbb": second})


def test_a_clean_base_update_keeps_the_approval(tmp_path: Path) -> None:
    calls, _ = _two_ticks(tmp_path, CHANGE_A, CHANGE_A_REBASED)
    assert calls == [_arm(1, "bbb")]


def test_a_changed_diff_makes_the_approval_stale(tmp_path: Path) -> None:
    # Whatever the new commit claims to be (committer "GitHub", "Merge branch"
    # subject), the change it carries is not the one approved.
    calls, out = _two_ticks(tmp_path, CHANGE_A, CHANGE_B)
    assert calls == []
    assert "#1 changed since its approval at aaa" in out


def test_a_changed_binary_file_makes_the_approval_stale(tmp_path: Path) -> None:
    # A binary diff has no patch; its content identity is the blob SHA.
    calls, _ = _two_ticks(tmp_path, BINARY_A, BINARY_B)
    assert calls == []


def test_the_fingerprint_is_read_at_the_captured_head(tmp_path: Path) -> None:
    # Never the PR's current ref, which can move between the list and the read.
    _run(tmp_path, [_pr(1, head="aaa")])
    reads = (tmp_path / "gh" / "compares.log").read_text().split()
    assert reads and set(reads) == {"repos/o/r/compare/master...aaa"}


def test_a_compare_that_may_be_truncated_approves_nothing(tmp_path: Path) -> None:
    many = _files(*[(f"f{i}", "modified", f"b{i}", "+x") for i in range(300)])
    calls, out = _run(tmp_path, [_pr(1, head="aaa")], compares={"aaa": many})
    assert calls == []
    assert "cannot record what was approved" in out


def _armed_then_moved(tmp_path: Path, second: dict | None, timeline_fails: tuple[int, ...] = ()):
    # Tick 1 arms #3 at aaa; tick 2 sees it still armed, head now bbb.
    tl = _timeline(10, 20)
    first_calls, _ = _run(tmp_path, [_pr(3, head="aaa")], timelines={3: tl}, compares={"aaa": CHANGE_A})
    assert first_calls == [_arm(3, "aaa")]
    return _run(
        tmp_path,
        # Armed moments ago, by tick 1: GitHub's arming time matches the record.
        [_pr(3, head="bbb", armed_min_ago=0.3, state="BLOCKED"), _pr(4)],
        timelines={3: tl, 4: _timeline(8, 20)},
        compares={"aaa": CHANGE_A, "bbb": second},
        timeline_fails=timeline_fails,
    )


def test_a_push_to_an_armed_pr_disarms_it(tmp_path: Path) -> None:
    # --match-head-commit binds only the arming; a later push would merge.
    calls, out = _armed_then_moved(tmp_path, CHANGE_B)
    assert calls == ["pr merge 3 -R o/r --disable-auto", _arm(4)]
    assert "#3 armed but its head changed since the approval" in out


def test_a_base_update_to_an_armed_pr_keeps_it_armed(tmp_path: Path) -> None:
    calls, _ = _armed_then_moved(tmp_path, CHANGE_A_REBASED)
    assert calls == []


def test_an_armed_pr_whose_new_head_cannot_be_read_is_disarmed(tmp_path: Path) -> None:
    calls, _ = _armed_then_moved(tmp_path, None)
    assert calls[0] == "pr merge 3 -R o/r --disable-auto"


def test_an_unreadable_timeline_disarms_rather_than_trusting_a_moved_head(tmp_path: Path) -> None:
    calls, _ = _armed_then_moved(tmp_path, CHANGE_B, timeline_fails=(3,))
    assert calls[0] == "pr merge 3 -R o/r --disable-auto"


def test_an_armed_labelled_pr_with_no_pin_is_left_alone(tmp_path: Path) -> None:
    # Armed by hand after labelling, or the state was lost: the maintainer's act.
    calls, _ = _run(tmp_path, [_pr(3, head="bbb", armed_min_ago=3, state="BLOCKED"), _pr(4)])
    assert calls == []


def test_an_unreadable_diff_after_the_head_moved_approves_nothing(tmp_path: Path) -> None:
    calls, out = _two_ticks(tmp_path, CHANGE_A, None)
    assert calls == []
    assert "diff unreadable" in out


def test_an_unreadable_diff_at_first_sight_records_nothing(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(1, head="aaa")], compares={"aaa": None})
    assert calls == []
    assert "cannot record what was approved" in out
    assert not (tmp_path / "state" / "approvals").exists()


def test_a_reapplied_label_pins_the_new_head(tmp_path: Path) -> None:
    _two_ticks(tmp_path, CHANGE_A, CHANGE_B)  # stale after the change
    # The maintainer re-applies the label: a newer label event pins afresh.
    calls, _ = _run(tmp_path, [_pr(1, head="bbb")], timelines={1: _timeline(2, 20)}, compares={"bbb": CHANGE_B})
    assert calls == [_arm(1, "bbb")]


def test_an_old_label_with_no_pin_must_be_reapplied(tmp_path: Path) -> None:
    # The script was down, or its state was lost: the label no longer says
    # which head was approved.
    calls, out = _run(tmp_path, [_pr(1)], timelines={1: _timeline(60, 120)})
    assert calls == []
    assert "was never pinned and is 60m old" in out
    assert not (tmp_path / "state" / "approvals").exists()


def test_an_unreadable_state_file_approves_nothing(tmp_path: Path) -> None:
    state = tmp_path / "state" / "approvals"
    state.mkdir(parents=True)  # a directory: exists, but awk cannot read it as a file
    calls, out = _run(tmp_path, [_pr(1)])
    assert calls == []
    assert "approval state unreadable" in out


def test_a_label_is_pinned_even_while_another_pr_holds_the_slot(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, labels=(), armed_min_ago=30, state="BLOCKED"), _pr(4, head="eee")])
    assert calls == []
    assert (tmp_path / "state" / "approvals").read_text().split()[:2] == ["4", "eee"]


def test_dry_run_records_no_pin(tmp_path: Path) -> None:
    _run(tmp_path, [_pr(1)], PR_QUEUE_DRY_RUN="1")
    assert not (tmp_path / "state" / "approvals").exists()


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
    mutations = [c for c in (d / "calls.log").read_text().splitlines()
                 if c.startswith(("pr merge", "pr edit", "pr comment", "pr update-branch", "run rerun"))]
    assert mutations == []


# --- withdrawn approval -------------------------------------------------------


def test_removing_the_label_from_a_pr_the_script_armed_disarms_it(tmp_path: Path) -> None:
    first, _ = _run(tmp_path, [_pr(3, head="aaa")])
    assert first == [_arm(3, "aaa")]
    calls, out = _run(tmp_path, [_pr(3, labels=(), head="aaa", armed_min_ago=0.2, state="BLOCKED"), _pr(4)])
    assert calls == ["pr merge 3 -R o/r --disable-auto", _arm(4)]
    assert "#3 armed but its approved-to-merge label was removed" in out


def test_a_pr_the_maintainer_rearmed_by_hand_later_is_left_alone(tmp_path: Path) -> None:
    arms = tmp_path / "state" / "approvals.arms"
    arms.parent.mkdir(parents=True)
    arms.write_text("3 2020-01-01T00:00:00Z\n")  # the script's arm, long ago
    calls, _ = _run(tmp_path, [_pr(3, labels=(), armed_min_ago=5, state="BLOCKED"), _pr(4)])
    assert calls == []  # holds the slot as a hand-armed PR


def test_a_dry_run_records_no_arm(tmp_path: Path) -> None:
    _run(tmp_path, [_pr(3)], PR_QUEUE_DRY_RUN="1")
    assert not (tmp_path / "state" / "approvals.arms").exists()


def test_a_labelled_pr_armed_by_hand_is_never_disarmed(tmp_path: Path) -> None:
    # Labelled, but the maintainer armed it: no recorded arm, so it is theirs.
    calls, _ = _run(tmp_path, [_pr(3, armed_min_ago=20, mergeable="CONFLICTING"), _pr(4)])
    assert calls == []


def test_an_arm_that_cannot_be_recorded_is_rolled_back(tmp_path: Path) -> None:
    (tmp_path / "state" / "approvals.arms").mkdir(parents=True)  # unwritable as a file
    calls, out = _run(tmp_path, [_pr(1, head="aaa")])
    assert calls == [_arm(1, "aaa"), "pr merge 1 -R o/r --disable-auto"]
    assert "could not be recorded" in out



# --- the review gate ------------------------------------------------------------


@pytest.mark.parametrize("state", ["NEUTRAL", "FAILURE", "ACTION_REQUIRED"])
def test_a_pr_whose_review_did_not_pass_is_not_armed(tmp_path: Path, state: str) -> None:
    # NEUTRAL is the review gate's "unreviewed"; review is not a required check,
    # so without this an agent's label would merge an unreviewed PR.
    calls, _ = _run(tmp_path, [_pr(1, review=None, checks=[_check("test"), _check("review", state, run=5)]), _pr(2)],
                      timelines={1: _timeline(12), 2: _timeline(8)})
    assert _arm(1) not in calls and calls[-1] == _arm(2)


def test_a_pr_with_no_review_check_yet_holds_the_order(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(1, review=None), _pr(2)], timelines={1: _timeline(12), 2: _timeline(8)})
    assert calls == []
    assert "#1 waiting on review=MISSING" in out


def test_a_pending_review_holds_the_order(tmp_path: Path) -> None:
    pending = _pr(1, review=None, checks=[_check("review", "", status="IN_PROGRESS")])
    calls, out = _run(tmp_path, [pending, _pr(2)], timelines={1: _timeline(12), 2: _timeline(8)})
    assert calls == []
    assert "review=PENDING" in out


def test_required_checks_are_configurable(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(1, review=None)], PR_QUEUE_REQUIRED_CHECKS="")
    assert calls == [_arm(1)]


@pytest.mark.parametrize("state", ["NEUTRAL", "FAILURE"])
def test_a_script_armed_pr_whose_review_stops_passing_is_disarmed(tmp_path: Path, state: str) -> None:
    pr = _pr(3, review=None, armed_min_ago=20, state="BLOCKED", checks=[_check("test"), _check("review", state, run=5)])
    calls, out = _run(tmp_path, [pr, _pr(4)], arms={3: 20})
    assert calls == ["pr merge 3 -R o/r --disable-auto", _arm(4)]
    assert f"review={state} is not passing" in out


@pytest.mark.parametrize("checks", [[_check("test")], [_check("test"), _check("review", "", status="IN_PROGRESS")]])
def test_a_review_not_yet_passing_disarms_but_keeps_the_pr_first(tmp_path: Path, checks: list) -> None:
    # For about a minute after GitHub updates the branch, review is missing or
    # pending. Disarm (review is not branch-protected), but arm nothing else.
    pr = _pr(3, review=None, armed_min_ago=20, state="BLOCKED", checks=checks)
    calls, out = _run(tmp_path, [pr, _pr(4)], arms={3: 20})
    assert calls == ["pr merge 3 -R o/r --disable-auto"]
    assert "nothing else armed this tick" in out


def test_a_hand_armed_pr_whose_review_stops_passing_is_left_alone(tmp_path: Path) -> None:
    pr = _pr(3, review=None, armed_min_ago=20, state="BLOCKED", checks=[_check("review", "NEUTRAL")])
    calls, _ = _run(tmp_path, [pr, _pr(4)])
    assert calls == []



def test_an_up_to_date_pr_is_only_armed(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(1, state="BLOCKED")])
    assert calls == [_arm(1)]


# --- update before arming ---------------------------------------------------------


def test_a_behind_head_of_queue_is_updated_unarmed_and_keeps_its_place(tmp_path: Path) -> None:
    # Arming first would leave auto-merge on across a head nothing has checked.
    calls, out = _run(tmp_path, [_pr(1, state="BEHIND"), _pr(2, state="BLOCKED")],
                      timelines={1: _timeline(12), 2: _timeline(8)})
    assert calls == ["pr update-branch 1 -R o/r"]
    assert "#1 updating before arming" in out


def test_failed_on_a_behind_head_is_updated_for_a_fresh_run(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(9, state="BEHIND", checks=[_check("test", "FAILURE")])])
    assert calls == ["pr update-branch 9 -R o/r"]


def test_a_failed_update_is_retried_next_tick(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(1, state="BEHIND"), _pr(2, state="BLOCKED")],
                      timelines={1: _timeline(12), 2: _timeline(8)}, fail=("update-branch",))
    assert calls == ["pr update-branch 1 -R o/r"]
    assert "retried next tick" in out


def test_update_then_revalidate_then_arm(tmp_path: Path) -> None:
    tl = _timeline(10, 20)
    # Tick 1: behind, so it is updated, not armed.
    calls, _ = _run(tmp_path, [_pr(1, head="aaa", state="BEHIND")], timelines={1: tl},
                    compares={"aaa": CHANGE_A})
    assert calls == ["pr update-branch 1 -R o/r"]
    # Tick 2: the update moved the head; review is being re-evaluated. Hold.
    calls, out = _run(tmp_path, [_pr(1, head="bbb", review=None)], timelines={1: tl},
                      compares={"aaa": CHANGE_A, "bbb": CHANGE_A_REBASED})
    assert calls == []
    assert "review=MISSING" in out
    # Tick 3: the new head's content matches the approval and review passed.
    calls, _ = _run(tmp_path, [_pr(1, head="bbb")], timelines={1: tl},
                    compares={"aaa": CHANGE_A, "bbb": CHANGE_A_REBASED})
    assert calls == [_arm(1, "bbb")]


def test_a_script_armed_holder_left_behind_is_disarmed_before_updating(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(3, armed_min_ago=30, state="BEHIND")], base_idle_min=20, arms={3: 30})
    assert calls == ["pr merge 3 -R o/r --disable-auto", "pr update-branch 3 -R o/r"]
    assert "disarming to update" in out


# --- notices on the PR -------------------------------------------------------------


def _notices(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("pr comment")]


def test_a_conflicting_queued_pr_gets_one_notice(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(1, mergeable="CONFLICTING", head="aaa")], PR_QUEUE_NOTIFY="1")
    notices = _notices(calls)
    assert len(notices) == 1
    assert "<!-- pr-queue-notice conflicting aaa -->" in notices[0]
    assert "remove the `approved-to-merge` label, then add it" in notices[0]


def test_a_notice_already_on_the_pr_is_not_repeated(tmp_path: Path) -> None:
    d = tmp_path / "gh"
    d.mkdir(parents=True)
    (d / "comments_1.txt").write_text("<!-- pr-queue-notice conflicting aaa -->\n**Merge queue:** skipped\n")
    calls, _ = _run(tmp_path, [_pr(1, mergeable="CONFLICTING", head="aaa")], PR_QUEUE_NOTIFY="1")
    assert _notices(calls) == []


def test_a_new_head_gets_a_fresh_notice(tmp_path: Path) -> None:
    d = tmp_path / "gh"
    d.mkdir(parents=True)
    (d / "comments_1.txt").write_text("<!-- pr-queue-notice conflicting aaa -->\n")
    calls, _ = _run(tmp_path, [_pr(1, mergeable="CONFLICTING", head="bbb")], PR_QUEUE_NOTIFY="1")
    assert len(_notices(calls)) == 1


def test_an_unreviewed_pr_is_told_to_run_the_review(tmp_path: Path) -> None:
    state = "NEUTRAL"
    pr = _pr(1, review=None, checks=[_check("test"), _check("review", state, run=5)])
    calls, _ = _run(tmp_path, [pr], PR_QUEUE_NOTIFY="1")
    notices = _notices(calls)
    assert notices and "review.sh" in notices[0] and f"review={state}" in notices[0]


def test_retries_exhausted_names_the_failing_checks(tmp_path: Path) -> None:
    pr = _pr(9, labels=(LABEL, RETRIED), checks=[_check("smoke", "FAILURE")])
    calls, _ = _run(tmp_path, [pr], PR_QUEUE_NOTIFY="1")
    notices = _notices(calls)
    assert notices and "smoke still failing" in notices[0] and "merge-retried" in notices[0]


def test_a_changed_approval_is_told_to_renew(tmp_path: Path) -> None:
    tl = _timeline(10, 20)  # one timeline for both ticks: the pin is keyed on the label time
    # Tick 1: #3 holds the slot, so #1 is only pinned at aaa.
    _run(tmp_path, [_pr(3, labels=(), armed_min_ago=5), _pr(1, head="aaa")],
         timelines={1: tl}, compares={"aaa": CHANGE_A})
    # Tick 2: #1's content changed.
    calls, _ = _run(tmp_path, [_pr(1, head="ccc")], timelines={1: tl},
                    compares={"aaa": CHANGE_A, "ccc": CHANGE_B}, PR_QUEUE_NOTIFY="1")
    notices = _notices(calls)
    assert notices and "remove the `approved-to-merge` label, then add it" in notices[0]


def test_transient_waits_post_no_notice(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(1, review=None)], PR_QUEUE_NOTIFY="1")
    assert _notices(calls) == []


def test_an_unreadable_comment_list_posts_nothing(tmp_path: Path) -> None:
    # Without the existing comments the dedupe cannot work: post nothing
    # rather than risk a notice on every tick.
    d = tmp_path / "gh"
    d.mkdir(parents=True)
    (d / "comments_1.fail").write_text("")
    calls, _ = _run(tmp_path, [_pr(1, mergeable="CONFLICTING")], PR_QUEUE_NOTIFY="1")
    assert _notices(calls) == []


# --- operator-only PRs --------------------------------------------------------------


def test_a_governance_sensitive_pr_is_never_armed(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(1, labels=(LABEL, "governance-sensitive")), _pr(2)],
                      timelines={1: _timeline(12), 2: _timeline(8)}, PR_QUEUE_NOTIFY="1")
    assert _arm(1) not in calls and calls[-1] == _arm(2)
    notices = _notices(calls)
    assert len(notices) == 1 and "operator merges it by hand" in notices[0]


def test_a_script_armed_pr_that_becomes_governance_sensitive_is_disarmed(tmp_path: Path) -> None:
    pr = _pr(3, labels=(LABEL, "governance-sensitive"), armed_min_ago=20)
    calls, out = _run(tmp_path, [pr, _pr(4)], arms={3: 20})
    assert calls == ["pr merge 3 -R o/r --disable-auto", _arm(4)]
    assert "only the operator merges" in out


def test_operator_only_labels_are_configurable(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(1, labels=(LABEL, "governance-sensitive"))], PR_QUEUE_OPERATOR_ONLY_LABELS="")
    assert calls == [_arm(1)]


# --- sensitivity, checked by the queue itself ------------------------------------------


def _manifest(tmp_path: Path, *rows: str) -> None:
    (tmp_path / "manifest.tsv").write_text("# header\n" + "".join(r + "\n" for r in rows))


def test_a_whole_file_sensitive_entry_is_never_armed_even_unlabelled(tmp_path: Path) -> None:
    # CI's label is best-effort (a fork's token cannot apply it); the queue checks itself.
    _manifest(tmp_path, "f\t-\tanti-gaming test")
    calls, out = _run(tmp_path, [_pr(1, head="aaa"), _pr(2, head="bbb")],
                      timelines={1: _timeline(12), 2: _timeline(8)},
                      compares={"aaa": CHANGE_A, "bbb": _files(("g", "modified", "b1", "+x"))})
    assert calls == [_arm(2, "bbb")]
    assert "#1 touches a governance-sensitive surface (f)" in out


def test_a_symbol_entry_matches_only_changed_lines(tmp_path: Path) -> None:
    hit = _files(("f", "modified", "b1", "@@ -1 +1 @@\n-RISK_APPROVE_THRESHOLD = 0.3\n+RISK_APPROVE_THRESHOLD = 0.9"))
    # The symbol only in an unchanged context line does not count, as in CI.
    miss = _files(("f", "modified", "b2", "@@ -9 +9 @@\n ctx RISK_APPROVE_THRESHOLD\n-a\n+b"))
    for sub, compare, expected in (("hit", hit, []), ("miss", miss, [_arm(1, "aaa")])):
        d = tmp_path / sub
        d.mkdir()
        _manifest(d, "f\tRISK_APPROVE_THRESHOLD\trisk line")
        calls, _ = _run(d, [_pr(1, head="aaa")], compares={"aaa": compare})
        assert calls == expected, sub


def test_a_sensitive_file_with_no_patch_fails_closed(tmp_path: Path) -> None:
    _manifest(tmp_path, "f\tSOME_CONSTANT\twhy")
    calls, out = _run(tmp_path, [_pr(1, head="aaa")], compares={"aaa": _files(("f", "modified", "b1", None))})
    assert calls == []
    assert "no patch to check" in out


def test_a_missing_manifest_fails_closed(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(1)], PR_QUEUE_SENSITIVITY_MANIFEST=str(tmp_path / "gone.tsv"))
    assert calls == []
    assert "manifest could not be read" in out


def test_a_fork_pr_is_never_armed(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(1, fork=True), _pr(2)], timelines={1: _timeline(12), 2: _timeline(8)})
    assert calls == [_arm(2)]
    assert "#1 is labelled a fork" in out or "a fork" in out



def test_a_script_armed_pr_is_disarmed_the_moment_it_is_seen_behind(tmp_path: Path) -> None:
    # No grace: GitHub's updater can move the head within a minute, and the
    # arm must not outlive the head it validated.
    calls, _ = _run(tmp_path, [_pr(3, armed_min_ago=30, state="BEHIND")], base_idle_min=0, arms={3: 30})
    assert calls == ["pr merge 3 -R o/r --disable-auto", "pr update-branch 3 -R o/r"]


def test_a_hand_armed_pr_just_behind_is_left_inside_the_grace(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, labels=(), armed_min_ago=30, state="BEHIND")], base_idle_min=0)
    assert calls == []


def test_by_default_the_manifest_is_read_from_the_base_branch(tmp_path: Path) -> None:
    # The deploy checkout can lag master; CI judges against master's manifest.
    d = tmp_path / "gh"
    d.mkdir(parents=True)
    (d / "manifest_remote.tsv").write_text("# rules\nf\t-\tanti-gaming test\n")
    calls, out = _run(tmp_path, [_pr(1, head="aaa")], compares={"aaa": CHANGE_A},
                      PR_QUEUE_SENSITIVITY_MANIFEST_UNSET="1")
    assert calls == []
    assert "governance-sensitive surface (f)" in out


def test_an_unreadable_remote_manifest_fails_closed(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(1)], PR_QUEUE_SENSITIVITY_MANIFEST_UNSET="1")
    assert calls == []
    assert "manifest could not be read" in out


def test_master_moving_mid_tick_arms_nothing(tmp_path: Path) -> None:
    # The PR data describes the base as it stood when the tick began.
    d = tmp_path / "gh"
    d.mkdir(parents=True)
    (d / "base_moved").write_text("newbase\n")
    calls, out = _run(tmp_path, [_pr(1)])
    assert calls == []
    assert "master moved during this tick" in out


"""pr-babysitter.sh keeps exactly one labelled PR armed at a time.

The script is driven against a fake `gh` on PATH: `gh pr list` and
`gh api .../commits/<base>` answer from fixtures, and every other call is
recorded so the tests can assert what the script would have mutated.
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

FAKE_GH = r"""#!/usr/bin/env bash
case "$1 $2" in
  "pr list") cat "$FAKE_GH_PRS"; exit 0 ;;
  "api repos/"*) echo "$FAKE_GH_BASE_DATE"; exit 0 ;;
esac
echo "$*" >> "$FAKE_GH_CALLS"
if [ "$1 $2" = "pr merge" ] && [ -n "${FAKE_GH_FAIL_MERGE:-}" ]; then
  case " $FAKE_GH_FAIL_MERGE " in *" $3 "*) exit 1 ;; esac
fi
exit 0
"""

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None or shutil.which("bash") is None,
    reason="pr-babysitter.sh needs bash and jq",
)


def _check(name: str, conclusion: str = "SUCCESS", run: int = 100) -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "detailsUrl": f"https://github.com/o/r/actions/runs/{run}/job/{run * 10}",
    }


def _pr(
    number: int,
    *,
    labels: tuple[str, ...] = ("queue",),
    draft: bool = False,
    armed: bool = False,
    mergeable: str = "MERGEABLE",
    state: str = "BEHIND",
    checks: list[dict] | None = None,
) -> dict:
    return {
        "number": number,
        "isDraft": draft,
        "mergeable": mergeable,
        "mergeStateStatus": state,
        "autoMergeRequest": {"mergeMethod": "SQUASH"} if armed else None,
        "baseRefName": "master",
        "labels": [{"name": label} for label in labels],
        "statusCheckRollup": checks if checks is not None else [_check("test")],
    }


def _iso(minutes_ago: int) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _run(tmp_path: Path, prs: list[dict], *, base_idle_min: int = 1, **env) -> tuple[list[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    fixture = tmp_path / "prs.json"
    fixture.write_text(json.dumps(prs))
    calls = tmp_path / "calls.log"
    calls.write_text("")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_PRS": str(fixture),
            "FAKE_GH_CALLS": str(calls),
            "FAKE_GH_BASE_DATE": _iso(base_idle_min),
            "PR_BABYSITTER_REPO": "o/r",
            **env,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return [line for line in calls.read_text().splitlines() if line], result.stdout


def test_arms_only_the_lowest_numbered_queued_pr(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(12), _pr(10), _pr(11)])
    assert calls == ["pr merge 10 -R o/r --auto --squash --delete-branch"]


def test_never_arms_an_unlabelled_or_draft_pr(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(5, labels=()), _pr(6, draft=True), _pr(7, labels=("other",))])
    assert calls == []


def test_an_armed_pr_holds_the_queue(tmp_path: Path) -> None:
    # Arming even a PR the maintainer armed by hand counts as in flight: a
    # second armed PR is exactly the parallel CI re-run the queue exists to stop.
    calls, _ = _run(tmp_path, [_pr(3, labels=(), armed=True, state="BLOCKED"), _pr(4)])
    assert calls == []


def test_a_conflicted_armed_pr_does_not_hold_the_queue(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, armed=True, mergeable="CONFLICTING"), _pr(4)])
    assert calls == ["pr merge 4 -R o/r --auto --squash --delete-branch"]


def test_skips_conflicting_and_unknown_to_the_next_mergeable(tmp_path: Path) -> None:
    calls, out = _run(
        tmp_path,
        [_pr(1, mergeable="CONFLICTING"), _pr(2, mergeable="UNKNOWN"), _pr(3)],
    )
    assert calls == ["pr merge 3 -R o/r --auto --squash --delete-branch"]
    assert "#1 queued but CONFLICTING" in out


def test_pending_checks_do_not_block_arming(tmp_path: Path) -> None:
    pending = {"__typename": "CheckRun", "name": "test", "status": "IN_PROGRESS", "conclusion": ""}
    calls, _ = _run(tmp_path, [_pr(8, checks=[pending])])
    assert calls == ["pr merge 8 -R o/r --auto --squash --delete-branch"]


def test_failed_check_is_rerun_once_and_marked(tmp_path: Path) -> None:
    checks = [
        _check("test", "FAILURE", run=77),
        _check("smoke", "FAILURE", run=77),
        _check("scope", "TIMED_OUT", run=78),
        _check("validate", "SUCCESS", run=79),
    ]
    calls, _ = _run(tmp_path, [_pr(9, checks=checks), _pr(10)])
    assert calls == [
        "run rerun 77 --failed -R o/r",
        "run rerun 78 --failed -R o/r",
        "pr edit 9 -R o/r --add-label queue-retried",
        # The retrying PR does not hold up the rest of the queue.
        "pr merge 10 -R o/r --auto --squash --delete-branch",
    ]


def test_failed_check_after_retry_is_skipped_not_rerun(tmp_path: Path) -> None:
    checks = [_check("test", "FAILURE", run=77)]
    calls, out = _run(tmp_path, [_pr(9, labels=("queue", "queue-retried"), checks=checks)])
    assert calls == []
    assert "still failing after one retry" in out


def test_failing_status_context_without_a_run_is_skipped(tmp_path: Path) -> None:
    context = {"__typename": "StatusContext", "context": "ext", "state": "ERROR", "targetUrl": "https://ci.example/1"}
    calls, out = _run(tmp_path, [_pr(9, checks=[context])])
    assert calls == []
    assert "not Actions runs" in out


def test_a_failed_arm_falls_through_to_the_next(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(1), _pr(2)], FAKE_GH_FAIL_MERGE="1")
    assert calls == [
        "pr merge 1 -R o/r --auto --squash --delete-branch",
        "pr merge 2 -R o/r --auto --squash --delete-branch",
    ]


def test_behind_armed_pr_is_left_to_github_inside_the_grace(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, armed=True, state="BEHIND")], base_idle_min=5)
    assert calls == []


def test_behind_armed_pr_is_updated_once_the_base_has_sat_idle(tmp_path: Path) -> None:
    calls, _ = _run(tmp_path, [_pr(3, armed=True, state="BEHIND")], base_idle_min=20)
    assert calls == ["pr update-branch 3 -R o/r"]


def test_only_the_in_flight_pr_is_ever_updated(tmp_path: Path) -> None:
    # The old babysitter updated every armed+BEHIND PR; with one in flight,
    # nothing else is armed, and unarmed PRs are GitHub's or the draft sweep's.
    calls, _ = _run(
        tmp_path,
        [_pr(3, armed=True, state="BEHIND"), _pr(4, state="BEHIND"), _pr(5, draft=True, state="BEHIND")],
        base_idle_min=30,
    )
    assert calls == ["pr update-branch 3 -R o/r"]


def test_dry_run_mutates_nothing(tmp_path: Path) -> None:
    calls, out = _run(tmp_path, [_pr(10)], PR_QUEUE_DRY_RUN="1")
    assert calls == []
    assert "dry-run: gh pr merge 10" in out


def test_gh_list_failure_does_nothing(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/usr/bin/env bash\n[ "$1 $2" = "pr list" ] && exit 1\necho "$*" >> "$FAKE_GH_CALLS"\n')
    gh.chmod(0o755)
    calls = tmp_path / "calls.log"
    calls.write_text("")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "FAKE_GH_CALLS": str(calls)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert calls.read_text() == ""

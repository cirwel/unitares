"""Repository-level security invariants for GitHub Actions workflows."""

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
DRAFT_BASE_REFRESH = WORKFLOW_DIR / "draft-base-refresh.yml"
EXTERNAL_USES = re.compile(
    r"^\s*(?:-\s*)?uses:\s+(?P<action>[^\s@]+)@(?P<ref>[^\s#]+)"
    r"(?:\s+#\s*(?P<label>\S+))?\s*$"
)


def test_external_actions_are_pinned_to_full_commit_shas():
    failures: list[str] = []

    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
        for line_number, line in enumerate(
            workflow.read_text(encoding="utf-8").splitlines(), 1
        ):
            match = EXTERNAL_USES.match(line)
            if not match:
                continue
            action = match.group("action")
            if action.startswith("./"):
                continue
            ref = match.group("ref")
            label = match.group("label")
            if not re.fullmatch(r"[0-9a-f]{40}", ref):
                failures.append(
                    f"{workflow.relative_to(REPO_ROOT)}:{line_number}: "
                    f"{action}@{ref} is not a full commit SHA"
                )
            if not label:
                failures.append(
                    f"{workflow.relative_to(REPO_ROOT)}:{line_number}: "
                    "pinned action lacks a human-readable release comment"
                )

    assert not failures, "\n".join(failures)


def _draft_refresh_script() -> str:
    workflow = yaml.safe_load(DRAFT_BASE_REFRESH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["refresh"]["steps"]
    return next(step["run"] for step in steps if step["name"].startswith("Merge base"))


def test_draft_refresh_revalidates_every_live_write_condition_before_push():
    script = _draft_refresh_script()
    live_query = 'gh pr view "$num" --repo "$GITHUB_REPOSITORY"'
    assert live_query in script
    assert "--json state,isDraft,updatedAt,labels,headRefOid" in script
    assert script.index(live_query) < script.index("if ! git push")

    for guard in (
        '"$live_state" != "OPEN"',
        '"$live_draft" != "true"',
        'index("no-base-refresh")',
        '"$live_updated" != "$updated"',
        '"$live_oid" != "$oid"',
    ):
        assert guard in script, f"draft refresh does not revalidate {guard}"


def test_draft_refresh_push_is_exact_oid_cas_and_failure_continues_sweep():
    script = _draft_refresh_script()
    guarded_push = re.search(r"if ! git push \\\n(?P<command>.*?); then(?P<failure>.*?)\n\s*fi", script, re.DOTALL)
    assert guarded_push, "refresh push must be guarded rather than aborting the sweep"
    command = guarded_push.group("command")
    assert '--force-with-lease="refs/heads/$head:$oid"' in command
    assert "--force-if-includes" in command
    assert 'origin "HEAD:refs/heads/$head"' in command
    assert "continue" in guarded_push.group("failure")


# --- The push credential and the post-push CI check (PR #2250, 2026-09-16) ---
#
# A head pushed with GITHUB_TOKEN gets its pull_request runs created in the
# approval-required state, so the refreshed draft is less mergeable than the
# stale head it replaced. The workflow therefore pushes only with the
# DRAFT_BASE_REFRESH_TOKEN secret, withholds every push without it, and after
# each push requires a Tests run to exist on the new head. The tests below run
# the workflow's own `run` block under bash against stand-ins for gh, git,
# date and sleep, so the tested script is the shipped one.

FAKE_GH = r"""#!/usr/bin/env bash
printf 'gh %s\n' "$*" >> "$FAKE_LOG"
filter=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  if [ "${args[$i]}" = "--jq" ]; then filter="${args[$((i + 1))]}"; fi
done
case "$1 $2" in
  "pr list")   src="$FAKE_DIR/prs.json" ;;
  "pr view")   src="$FAKE_DIR/pr-$3.json" ;;
  "repo view") src="$FAKE_DIR/repo.json" ;;
  "api "*)
    if [ "${FAKE_API_EXIT:-0}" != 0 ]; then exit "$FAKE_API_EXIT"; fi
    case "$2" in
      */actions/workflows/*/runs\?event=pull_request\&head_sha=*) src="$FAKE_DIR/runs.json" ;;
      *) echo "fake gh: unexpected api path $2" >&2; exit 1 ;;
    esac ;;
  *) echo "fake gh: unexpected call: $*" >&2; exit 1 ;;
esac
if [ -n "$filter" ]; then jq -r "$filter" "$src"; else cat "$src"; fi
"""

FAKE_GIT = r"""#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "$FAKE_LOG"
all="$*"
case "$1" in
  config|fetch|checkout) exit 0 ;;
  merge) if [ "$2" = "--abort" ]; then exit 0; fi; exit "${FAKE_MERGE_EXIT:-0}" ;;
  rev-parse)
    case "$all" in
      *"--short HEAD") printf '%s\n' "${FAKE_NEW_OID:0:7}" ;;
      *origin/*) ref="${all##*origin/}"; ref="${ref%%^*}"; cat "$FAKE_DIR/oid-${ref//\//_}" ;;
      *HEAD) printf '%s\n' "$FAKE_NEW_OID" ;;
      *) echo "fake git: unexpected rev-parse: $all" >&2; exit 1 ;;
    esac ;;
  push) printf '%s\n' "$all" >> "$FAKE_PUSH_LOG"; exit "${FAKE_PUSH_EXIT:-0}" ;;
  *) echo "fake git: unexpected call: $all" >&2; exit 1 ;;
esac
"""

# GNU date stand-in (macOS date has no -d): `date -u +%s` and `date -u -d <iso> +%s`.
FAKE_DATE = r"""#!/usr/bin/env bash
if [ "$1" = "-u" ] && [ "$2" = "-d" ]; then
  exec python3 - "$3" <<'PY'
import datetime, sys
try:
    stamp = datetime.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00"))
except ValueError:
    sys.exit(1)
print(int(stamp.timestamp()))
PY
fi
exec python3 -c 'import time; print(int(time.time()))'
"""

FAKE_SLEEP = "#!/usr/bin/env bash\nexit 0\n"

NEW_OID = "f" * 40


def _refresh_step() -> dict:
    workflow = yaml.safe_load(DRAFT_BASE_REFRESH.read_text(encoding="utf-8"))
    return next(
        step for step in workflow["jobs"]["refresh"]["steps"]
        if step["name"].startswith("Merge base")
    )


def _draft(number, head, *, oid, idle_hours=48, state="BEHIND", labels=(), base="master"):
    touched = datetime.now(timezone.utc) - timedelta(hours=idle_hours)
    return {
        "number": number,
        "isDraft": True,
        "headRefName": head,
        "headRepositoryOwner": {"login": "cirwel"},
        "baseRefName": base,
        "mergeStateStatus": state,
        "labels": [{"name": label} for label in labels],
        "updatedAt": touched.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "headRefOid": oid,
    }


def _run_refresh(tmp_path, *, prs, runs=(), env=None, push_exit=0, api_exit=0, default_branch="master"):
    if shutil.which("jq") is None or shutil.which("bash") is None:
        pytest.skip("the refresh script needs bash and jq")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("gh", FAKE_GH), ("git", FAKE_GIT), ("date", FAKE_DATE), ("sleep", FAKE_SLEEP)):
        tool = bin_dir / name
        tool.write_text(body, encoding="utf-8")
        tool.chmod(0o755)
    fake_dir = tmp_path / "fake"
    fake_dir.mkdir()
    (fake_dir / "prs.json").write_text(json.dumps(prs), encoding="utf-8")
    for pr in prs:
        live = {key: pr[key] for key in ("isDraft", "updatedAt", "labels", "headRefOid")}
        live["state"] = "OPEN"
        (fake_dir / f"pr-{pr['number']}.json").write_text(json.dumps(live), encoding="utf-8")
        (fake_dir / f"oid-{pr['headRefName'].replace('/', '_')}").write_text(pr["headRefOid"], encoding="utf-8")
    (fake_dir / "runs.json").write_text(json.dumps({"workflow_runs": list(runs)}), encoding="utf-8")
    (fake_dir / "repo.json").write_text(
        json.dumps({"defaultBranchRef": {"name": default_branch}}), encoding="utf-8"
    )
    summary = tmp_path / "summary.md"
    summary.touch()
    push_log = tmp_path / "push.log"
    full_env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "GITHUB_REPOSITORY": "cirwel/unitares",
        "GH_TOKEN": "fake",
        "DRY_RUN": "false",
        "PR_NUMBER": "",
        "QUIET_HOURS": "12",
        "PUSH_TOKEN_CONFIGURED": "true",
        "CI_WORKFLOW": "tests.yml",
        # One poll, no waiting: `sleep` is a no-op here anyway.
        "CI_WAIT_SECONDS": "0",
        "GITHUB_STEP_SUMMARY": str(summary),
        "FAKE_DIR": str(fake_dir),
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_PUSH_LOG": str(push_log),
        "FAKE_PUSH_EXIT": str(push_exit),
        "FAKE_API_EXIT": str(api_exit),
        "FAKE_NEW_OID": NEW_OID,
        **(env or {}),
    }
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.run(
        ["bash", "-c", _refresh_step()["run"]],
        cwd=work,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    pushes = push_log.read_text(encoding="utf-8").splitlines() if push_log.exists() else []
    return proc, pushes, summary.read_text(encoding="utf-8")


def _push_line(head, oid):
    return (
        f"push --force-with-lease=refs/heads/{head}:{oid} --force-if-includes "
        f"origin HEAD:refs/heads/{head}"
    )


QUEUED = ({"status": "queued", "conclusion": None},)
PARKED = ({"status": "completed", "conclusion": "action_required"},)


def test_draft_refresh_push_credential_is_the_secret_and_the_token_stays_read_only():
    workflow = yaml.safe_load(DRAFT_BASE_REFRESH.read_text(encoding="utf-8"))
    assert workflow["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
        "actions": "read",
    }, "GITHUB_TOKEN must not be able to push: a gate bug then cannot strand a head"
    checkout = workflow["jobs"]["refresh"]["steps"][0]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"]["token"] == "${{ secrets.DRAFT_BASE_REFRESH_TOKEN || github.token }}"
    env = _refresh_step()["env"]
    assert env["GH_TOKEN"] == "${{ github.token }}", "reads stay on GITHUB_TOKEN"
    assert env["PUSH_TOKEN_CONFIGURED"] == "${{ secrets.DRAFT_BASE_REFRESH_TOKEN != '' }}"
    assert env["CI_WORKFLOW"] == "tests.yml"


def test_draft_refresh_withholds_every_push_without_the_secret_and_fails(tmp_path):
    proc, pushes, summary = _run_refresh(
        tmp_path,
        prs=[_draft(1, "codex/one", oid="a" * 40)],
        runs=QUEUED,
        env={"PUSH_TOKEN_CONFIGURED": "false"},
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert pushes == []
    assert "withheld: would push" in proc.stdout
    assert "::error::1 draft(s) are behind and idle but were not refreshed: DRAFT_BASE_REFRESH_TOKEN" in proc.stdout
    assert "withheld (DRAFT_BASE_REFRESH_TOKEN not configured): 1" in summary


def test_draft_refresh_without_the_secret_and_nothing_to_refresh_stays_green(tmp_path):
    proc, pushes, _ = _run_refresh(
        tmp_path,
        prs=[_draft(1, "codex/one", oid="a" * 40, state="CLEAN")],
        env={"PUSH_TOKEN_CONFIGURED": "false"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert pushes == []
    assert "::warning::DRAFT_BASE_REFRESH_TOKEN is not configured" in proc.stdout


def test_draft_refresh_dry_run_lists_without_pushing_or_failing(tmp_path):
    proc, pushes, summary = _run_refresh(
        tmp_path,
        prs=[_draft(1, "codex/one", oid="a" * 40)],
        env={"DRY_RUN": "true", "PUSH_TOKEN_CONFIGURED": "false"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert pushes == []
    assert "dry-run: would push" in proc.stdout
    assert "would refresh: 1" in summary


def test_draft_refresh_pushes_with_the_cas_and_confirms_a_queued_ci_run(tmp_path):
    proc, pushes, summary = _run_refresh(
        tmp_path, prs=[_draft(7, "codex/one", oid="a" * 40)], runs=QUEUED
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert pushes == [_push_line("codex/one", "a" * 40)]
    assert f"::notice::PR #7: refreshed to {NEW_OID[:7]}; its tests.yml run is queued." in proc.stdout
    assert "refreshed: 1" in summary


def test_draft_refresh_fails_and_stops_pushing_when_the_head_is_parked_awaiting_approval(tmp_path):
    proc, pushes, summary = _run_refresh(
        tmp_path,
        prs=[_draft(1, "codex/one", oid="a" * 40), _draft(2, "claude/two", oid="b" * 40)],
        runs=PARKED,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert pushes == [_push_line("codex/one", "a" * 40)], "the second head must not be pushed"
    assert "::error::PR #1: the refreshed head" in proc.stdout
    assert "parked in approval-required CI" in proc.stdout
    assert "hold: an earlier push in this sweep did not establish CI" in proc.stdout
    assert "did not get a tests.yml run" in summary


def test_draft_refresh_fails_when_no_ci_run_appears_on_the_pushed_head(tmp_path):
    proc, pushes, _ = _run_refresh(tmp_path, prs=[_draft(1, "codex/one", oid="a" * 40)], runs=())
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert pushes == [_push_line("codex/one", "a" * 40)]
    assert "no tests.yml pull_request run could be confirmed" in proc.stdout
    assert "(absent)" in proc.stdout


def test_draft_refresh_fails_closed_when_the_actions_api_cannot_be_read(tmp_path):
    proc, pushes, _ = _run_refresh(
        tmp_path, prs=[_draft(1, "codex/one", oid="a" * 40)], runs=QUEUED, api_exit=1
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert pushes == [_push_line("codex/one", "a" * 40)]
    assert "(unreadable)" in proc.stdout


def test_draft_refresh_pr_number_input_limits_the_sweep_to_that_pr(tmp_path):
    proc, pushes, _ = _run_refresh(
        tmp_path,
        prs=[_draft(1, "codex/one", oid="a" * 40), _draft(2, "claude/two", oid="b" * 40)],
        runs=QUEUED,
        env={"PR_NUMBER": "2"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert pushes == [_push_line("claude/two", "b" * 40)]
    assert "PR #1" not in proc.stdout


def test_draft_refresh_rejects_a_malformed_pr_number_before_touching_anything(tmp_path):
    proc, pushes, _ = _run_refresh(
        tmp_path, prs=[_draft(1, "codex/one", oid="a" * 40)], runs=QUEUED, env={"PR_NUMBER": "2; rm"}
    )
    assert proc.returncode == 1
    assert pushes == []
    assert "::error::pr_number must be a PR number or empty" in proc.stdout


def test_draft_refresh_names_a_non_default_base_as_unverified_instead_of_failing(tmp_path):
    proc, pushes, _ = _run_refresh(
        tmp_path, prs=[_draft(1, "codex/one", oid="a" * 40, base="develop")], runs=()
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert pushes == [_push_line("codex/one", "a" * 40)]
    assert "unverified: tests.yml runs on pull_request only for PRs into master" in proc.stdout


def test_draft_refresh_push_lease_rejection_still_holds_rather_than_aborting(tmp_path):
    proc, pushes, summary = _run_refresh(
        tmp_path,
        prs=[_draft(1, "codex/one", oid="a" * 40), _draft(2, "claude/two", oid="b" * 40)],
        runs=QUEUED,
        push_exit=1,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(pushes) == 2, "a rejected lease on one head must not stop the sweep"
    assert proc.stdout.count("hold: push lease rejected") == 2
    assert "refreshed: 0" in summary

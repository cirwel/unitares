"""Repository-level security invariants for GitHub Actions workflows."""

import re
from pathlib import Path

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

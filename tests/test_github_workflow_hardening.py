"""Repository-level security invariants for GitHub Actions workflows."""

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
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

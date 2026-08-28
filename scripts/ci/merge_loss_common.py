"""Shared plumbing for the merge-loss guards.

Used by orphan_push_guard.py, merge_content_check.py, and
automerge_disarm_detector.py. All three exist because commit loss on this
repo has never looked like a failure: the push succeeds, the PR page says
MERGED, checks are green — and work is gone anyway. Each guard makes one
specific silent loss mode loud, server-side, so it binds every pusher
(any agent harness, any terminal) rather than one client's hook chain.

Design rule shared by all three: DEGRADED IS NEVER SILENT. The guards fail
open on API errors — a broken guard must not block delivery — but a
degraded run must say so on the workflow surface (a `::warning::`
annotation plus a step-summary line), because instrumentation that fails
toward "healthy" is this repo's named recurring failure mode (see the
INDETERMINATE class in stranded_work_audit.py).

GITHUB_TOKEN-only — no metered APIs (execution-cost policy).
"""

from __future__ import annotations

import json
import os
import subprocess

GH_TIMEOUT_SECONDS = 60
FINDING_LABEL = "ci-finding"


def gh(*args: str) -> str:
    """Run a gh CLI command, returning stdout. Raises on failure/timeout."""
    proc = subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT_SECONDS,
    )
    return proc.stdout


def gh_json(*args: str):
    return json.loads(gh(*args))


def summary(line: str) -> None:
    """Append a line to the job summary (and stdout for the run log)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n\n")
    print(line)


def degraded(guard: str, why: str) -> int:
    """Report a degraded (fail-open) run visibly and return exit code 0."""
    print(f"::warning::{guard} degraded: {why} — this run verified nothing")
    summary(f"**{guard}: DEGRADED** — {why}. Fail-open by design; a degraded run is not a verification.")
    return 0


def fingerprint_marker(slug: str) -> str:
    """Hidden dedup marker, same pattern as stranded-work.yml / surface-findings.yml."""
    return f"<!-- finding-fingerprint: {slug} -->"


def ensure_finding_label(repo: str) -> None:
    """Self-provision the ci-finding label; already-exists is fine."""
    try:
        gh(
            "label", "create", FINDING_LABEL, "-R", repo,
            "--description", "Opened by a CI audit/collector",
            "--color", "D93F0B",
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass


def find_open_finding(repo: str, marker: str) -> int | None:
    """Return the number of the open ci-finding issue carrying `marker`, if any.

    Matches the marker against issue bodies in-process rather than through
    GitHub search: branch names contain `/` and markers contain `:`, both of
    which the search tokenizer mangles.
    """
    issues = gh_json(
        "issue", "list", "-R", repo,
        "--label", FINDING_LABEL,
        "--state", "open",
        "--json", "number,body",
        "--limit", "200",
    )
    for issue in issues:
        if marker in (issue.get("body") or ""):
            return issue["number"]
    return None


def file_or_comment_finding(repo: str, marker: str, title: str, body: str) -> None:
    """Create the finding issue, or comment on the existing one (dedup by marker)."""
    ensure_finding_label(repo)
    existing = find_open_finding(repo, marker)
    full_body = f"{marker}\n{body}"
    if existing is not None:
        gh("issue", "comment", str(existing), "-R", repo, "--body", full_body)
    else:
        gh(
            "issue", "create", "-R", repo,
            "--title", title,
            "--label", FINDING_LABEL,
            "--body", full_body,
        )

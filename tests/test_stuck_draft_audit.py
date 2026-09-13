"""Tests for ``scripts/dev/stuck_draft_audit.py``.

The audit exists because readiness in this repo is agent-declared and requires
"CI green, review round joined", while no review bot exists — so a green draft
whose session ended has no exit that is not a person noticing it. These tests
pin the classification, and pin the two boundaries the audit must not cross:
it never confuses "could not read" with "nothing there", and it reports rather
than acts.
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "stuck_draft_audit.py"

spec = importlib.util.spec_from_file_location("stuck_draft_audit", SCRIPT)
assert spec and spec.loader
audit_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_mod)

NOW = datetime(2026, 9, 13, 18, 0, tzinfo=timezone.utc)


def _iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z")


def _pr(*, checks, mergeable="MERGEABLE", idle=24.0, age=48.0, number=1):
    return {
        "number": number,
        "title": "t",
        "headRefName": "b",
        "author": {"login": "someone"},
        "createdAt": _iso(age),
        "updatedAt": _iso(idle),
        "mergeable": mergeable,
        "statusCheckRollup": checks,
    }


GREEN = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
RED = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
RUNNING = [{"status": "IN_PROGRESS", "conclusion": None}]


def _classify(pr, threads=0, quiet=12.0):
    return audit_mod.classify(pr, threads, quiet, NOW)


# --- check rollup reduction --------------------------------------------------


def test_green_rollup_is_green():
    assert audit_mod.check_state(_pr(checks=GREEN)) == "green"


def test_failure_is_red():
    assert audit_mod.check_state(_pr(checks=GREEN + RED)) == "red"


def test_skipped_and_neutral_are_not_failures():
    """This repo's conditional Elixir jobs skip on most PRs.

    Counting a skip as unfinished would park every PR in CI-PENDING forever,
    which would make the audit silently report nothing — the failure mode it
    exists to prevent.
    """
    checks = GREEN + [
        {"status": "COMPLETED", "conclusion": "SKIPPED"},
        {"status": "COMPLETED", "conclusion": "NEUTRAL"},
    ]
    assert audit_mod.check_state(_pr(checks=checks)) == "green"


def test_running_check_is_pending():
    assert audit_mod.check_state(_pr(checks=GREEN + RUNNING)) == "pending"


def test_no_checks_is_pending_not_green():
    """Absence of checks is not evidence of passing checks."""
    assert audit_mod.check_state(_pr(checks=[])) == "pending"


def test_legacy_commit_status_shape_is_handled():
    assert audit_mod.check_state(_pr(checks=[{"state": "FAILURE"}])) == "red"
    assert audit_mod.check_state(_pr(checks=[{"state": "SUCCESS"}])) == "green"


# --- classification ----------------------------------------------------------


def test_green_idle_no_threads_is_unblocked():
    f = _classify(_pr(checks=GREEN))
    assert f["class"] == "UNBLOCKED"
    assert "readiness decision" in f["reason"]


def test_recently_touched_is_in_flight_even_when_green():
    """Draft means 'still working — hands off' while the owner is live."""
    f = _classify(_pr(checks=GREEN, idle=1.0))
    assert f["class"] == "IN-FLIGHT"


def test_conflict_outranks_everything():
    f = _classify(_pr(checks=GREEN, mergeable="CONFLICTING"))
    assert f["class"] == "CONFLICTED"


def test_red_ci_outranks_review_state():
    f = _classify(_pr(checks=RED), threads=3)
    assert f["class"] == "CI-RED"


def test_open_threads_are_review_open():
    f = _classify(_pr(checks=GREEN), threads=2)
    assert f["class"] == "REVIEW-OPEN"
    assert "waiting on the author" in f["reason"]


def test_pending_checks_are_not_unblocked():
    assert _classify(_pr(checks=RUNNING))["class"] == "CI-PENDING"


def test_unreadable_threads_are_never_reported_as_clean():
    """None means 'could not read', which must not collapse into 'no threads'.

    A degraded audit that reports healthy is this repo's named recurring
    failure mode, so the indeterminate case gets its own class and never
    reaches UNBLOCKED.
    """
    f = _classify(_pr(checks=GREEN), threads=None)
    assert f["class"] != "UNBLOCKED"
    assert f["class"] == "CI-PENDING"
    assert "indeterminate" in f["reason"]


# --- backlog profile ---------------------------------------------------------


def test_age_profile_separates_unblocked_from_all():
    findings = [
        _classify(_pr(checks=GREEN, age=100, number=1)),
        _classify(_pr(checks=RED, age=50, number=2)),
        _classify(_pr(checks=GREEN, idle=1.0, age=2, number=3)),
    ]
    p = audit_mod.age_profile(findings)
    assert p["all_open_drafts"]["count"] == 3
    assert p["reported"]["count"] == 2, "IN-FLIGHT is excluded from reported"
    assert p["unblocked"]["count"] == 1
    assert p["unblocked"]["oldest_age_hours"] == 100


def test_age_profile_handles_empty():
    assert audit_mod.age_profile([])["all_open_drafts"] == {"count": 0}


# --- the boundary the audit must not cross -----------------------------------


def test_audit_never_mutates_a_pull_request():
    """It reports; it does not flip drafts ready, merge, or comment.

    CLAUDE.md reserves readiness to the owning agent and merging to the
    maintainer. A script doing either would be the "silent green button" that
    section explicitly forbids. If this test ever fails, the audit has taken
    authority it was never granted.
    """
    source = SCRIPT.read_text()
    for forbidden in (
        "pr ready",
        "pr merge",
        "pr comment",
        "pr review",
        "pr edit",
        "--merge",
        "mergePullRequest",
        "markPullRequestReadyForReview",
    ):
        assert forbidden not in source, f"audit script contains mutating call: {forbidden!r}"


def test_only_read_permissions_are_used():
    """Every gh invocation in the script is a read."""
    source = SCRIPT.read_text()
    assert '"gh", "pr", "list"' in source
    assert '"gh", "api", "graphql"' in source
    # -f query=... is a GraphQL read; no mutation keyword may appear.
    assert "mutation" not in source

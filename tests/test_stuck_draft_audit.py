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
import json
import subprocess
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
        "requiredStatusChecks": [{"context": "tests", "app_id": None}],
        "headRefOid": "sampled-head",
        "reviewDecision": "APPROVED",
        "reviewRequests": [],
    }


GREEN = [{"name": "tests", "status": "COMPLETED", "conclusion": "SUCCESS"}]
RED = [{"name": "tests", "status": "COMPLETED", "conclusion": "FAILURE"}]
RUNNING = [{"name": "tests", "status": "IN_PROGRESS", "conclusion": None}]


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
    assert audit_mod.check_state(_pr(checks=[{"context": "tests", "state": "SUCCESS"}])) == "green"


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


def test_requested_reviewer_is_review_open_even_without_threads():
    pr = _pr(checks=GREEN)
    pr["reviewRequests"] = [{"login": "reviewer"}]
    f = _classify(pr, threads=0)
    assert f["class"] == "REVIEW-OPEN"
    assert "requested reviewer" in f["reason"]


@pytest.mark.parametrize("requests", [None, "unreadable", {}])
def test_unreadable_review_requests_are_unknown(requests):
    pr = _pr(checks=GREEN)
    pr["reviewRequests"] = requests
    f = _classify(pr, threads=0)
    assert f["class"] == "UNKNOWN"
    assert "review requests" in f["reason"]


def test_missing_review_requests_are_unknown():
    pr = _pr(checks=GREEN)
    del pr["reviewRequests"]
    assert _classify(pr, threads=0)["class"] == "UNKNOWN"


def test_empty_review_requests_may_proceed():
    assert _classify(_pr(checks=GREEN), threads=0)["class"] == "UNBLOCKED"


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


@pytest.mark.parametrize("mergeable", ["UNKNOWN", "", None, "future-enum"])
def test_unknown_mergeability_is_not_unblocked(mergeable):
    finding = _classify(_pr(checks=GREEN, mergeable=mergeable))
    assert finding["class"] == "UNKNOWN"
    assert "mergeability" in finding["reason"]


@pytest.mark.parametrize("decision", ["CHANGES_REQUESTED", "REVIEW_REQUIRED"])
def test_review_decision_blocks_even_without_threads(decision):
    pr = _pr(checks=GREEN)
    pr["reviewDecision"] = decision
    assert _classify(pr, threads=0)["class"] == "REVIEW-OPEN"


def test_missing_review_decision_is_unknown():
    pr = _pr(checks=GREEN)
    del pr["reviewDecision"]
    assert _classify(pr)["class"] == "UNKNOWN"


@pytest.mark.parametrize("policy", [None, [], [
    {"context": "tests", "app_id": None}, {"context": "never-started", "app_id": None},
]])
def test_green_subset_cannot_establish_required_ci(policy):
    pr = _pr(checks=GREEN)
    pr["requiredStatusChecks"] = policy
    assert _classify(pr)["class"] == "CI-PENDING"


@pytest.mark.parametrize("check", [
    {"name": "tests", "status": "COMPLETED", "conclusion": None},
    {"name": "tests", "status": "COMPLETED", "conclusion": "STALE"},
    {"context": "tests", "state": "EXPECTED"},
    {"name": "tests"},
])
def test_unknown_check_outcomes_are_pending(check):
    assert audit_mod.check_state(_pr(checks=[check])) == "pending"


def test_required_policy_unions_classic_protection_and_rulesets(monkeypatch):
    calls = []

    def run(*args):
        calls.append(args)
        if args[2] == "graphql":
            return json.dumps({"data": {"repository": {"ref": {
                "branchProtectionRule": {"requiredStatusChecks": [{"context": "tests", "app": {"databaseId": 123}}]},
            }}}})
        return json.dumps([
            [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "tests", "integration_id": 456}],
            }}],
            [{"type": "required_status_checks", "parameters": {
                "required_status_checks": [{"context": "security"}],
            }}],
        ])

    monkeypatch.setattr(audit_mod, "run", run)
    assert audit_mod.required_checks("owner/repo", "feature/base") == [
        {"context": "tests", "app_id": 123},
        {"context": "tests", "app_id": 456},
        {"context": "security", "app_id": None},
    ]
    assert calls[-1][-1].endswith("feature%2Fbase")
    assert "--paginate" in calls[-1] and "--slurp" in calls[-1]


@pytest.mark.parametrize("failure", ["api", "partial", "malformed", "null"])
def test_unreadable_required_policy_remains_unknown(monkeypatch, failure):
    def run(*args):
        if failure == "api":
            raise subprocess.CalledProcessError(1, args)
        if failure == "partial":
            return json.dumps({"errors": [{"message": "denied"}], "data": {}})
        if failure == "null":
            return "null"
        return "not-json"

    monkeypatch.setattr(audit_mod, "run", run)
    assert audit_mod.required_checks("owner/repo", "master") is None


def test_ruleset_failure_does_not_accept_classic_subset(monkeypatch):
    def run(*args):
        if args[2] == "graphql":
            return json.dumps({"data": {"repository": {"ref": {
                "branchProtectionRule": {"requiredStatusChecks": [{"context": "tests", "app": None}]},
            }}}})
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(audit_mod, "run", run)
    assert audit_mod.required_checks("owner/repo", "master") is None


def test_unprotected_base_declares_no_ci_policy(monkeypatch):
    monkeypatch.setattr(audit_mod, "run", lambda *args: json.dumps(
        {"data": {"repository": {"ref": {"branchProtectionRule": None}}}}
        if args[2] == "graphql" else [[]]
    ))
    assert audit_mod.required_checks("owner/repo", "feature/base") == []


def test_truncated_review_threads_are_unknown(monkeypatch):
    monkeypatch.setattr(audit_mod, "run", lambda *args: json.dumps({"data": {
        "repository": {"pullRequest": {"reviewThreads": {
            "nodes": [{"isResolved": True}] * 100,
            "pageInfo": {"hasNextPage": True},
        }}},
    }}))
    assert audit_mod.unresolved_threads("owner/repo", 1) is None


def test_audit_loads_required_policy_once_per_base(monkeypatch):
    prs = [_pr(checks=GREEN, number=n) for n in (1, 2)]
    for pr in prs:
        del pr["requiredStatusChecks"]
        pr["baseRefName"] = "master"
    monkeypatch.setattr(audit_mod, "open_drafts", lambda repo: prs)
    monkeypatch.setattr(audit_mod, "unresolved_threads", lambda repo, number: 0)
    monkeypatch.setattr(audit_mod, "head_check_rollup", lambda repo, oid: GREEN)
    calls = []

    def policy(repo, base):
        calls.append((repo, base))
        return [{"context": "tests", "app_id": None}, {"context": "missing", "app_id": None}]

    monkeypatch.setattr(audit_mod, "required_checks", policy)
    assert {f["class"] for f in audit_mod.audit("owner/repo", 0)} == {"CI-PENDING"}
    assert calls == [("owner/repo", "master")]


@pytest.mark.parametrize("rule_type", ["workflows", "code_scanning", "required_deployments"])
def test_rules_not_expressible_as_check_contexts_remain_unknown(monkeypatch, rule_type):
    monkeypatch.setattr(audit_mod, "run", lambda *args: json.dumps(
        {"data": {"repository": {"ref": {
            "branchProtectionRule": {"requiredStatusChecks": [{"context": "tests", "app": None}]},
        }}}}
        if args[2] == "graphql" else [[{"type": rule_type}]]
    ))
    assert audit_mod.required_checks("owner/repo", "master") is None


@pytest.mark.parametrize("source", ["classic", "ruleset"])
@pytest.mark.parametrize("actual_app", [456, None])
def test_wrong_or_unknown_app_cannot_satisfy_required_check(monkeypatch, source, actual_app):
    monkeypatch.setattr(audit_mod, "run", lambda *args: json.dumps(
        {"data": {"repository": {"ref": {"branchProtectionRule": {
            "requiredStatusChecks": [{"context": "tests", "app": {"databaseId": 123}}]
            if source == "classic" else [],
        }}}}}
        if args[2] == "graphql" else [[{"type": "required_status_checks", "parameters": {
            "required_status_checks": [{"context": "tests", "integration_id": 123}],
        }}]] if source == "ruleset" else [[]]
    ))
    pr = _pr(checks=[{**GREEN[0], "app_id": actual_app}])
    pr["requiredStatusChecks"] = audit_mod.required_checks("owner/repo", "master")
    assert pr["requiredStatusChecks"] == [{"context": "tests", "app_id": 123}]
    assert _classify(pr)["class"] == "CI-PENDING"


def test_matching_app_satisfies_required_check():
    pr = _pr(checks=[{**GREEN[0], "app_id": 123}])
    pr["requiredStatusChecks"] = [{"context": "tests", "app_id": 123}]
    assert _classify(pr)["class"] == "UNBLOCKED"


def test_check_rollup_reads_app_and_pins_exact_head(monkeypatch):
    calls = []

    def run(*args):
        calls.append(args)
        return json.dumps({"data": {"repository": {"object": {"statusCheckRollup": {"contexts": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [{**GREEN[0], "checkSuite": {"app": {"databaseId": 123}}},
                      {"context": "legacy", "state": "SUCCESS"}],
        }}}}}})

    monkeypatch.setattr(audit_mod, "run", run)
    checks = audit_mod.head_check_rollup("owner/repo", "sampled-head")
    assert checks[0]["app_id"] == 123
    assert checks[1]["context"] == "legacy"
    assert "oid=sampled-head" in calls[0]


@pytest.mark.parametrize("failure", ["api", "partial", "truncated", "malformed"])
def test_unreadable_enriched_rollup_remains_unknown(monkeypatch, failure):
    def run(*args):
        if failure == "api":
            raise subprocess.CalledProcessError(1, args)
        if failure == "partial":
            return json.dumps({"errors": [{"message": "denied"}], "data": {}})
        if failure == "malformed":
            return "null"
        return json.dumps({"data": {"repository": {"object": {"statusCheckRollup": {"contexts": {
            "pageInfo": {"hasNextPage": True}, "nodes": GREEN,
        }}}}}})

    monkeypatch.setattr(audit_mod, "run", run)
    assert audit_mod.head_check_rollup("owner/repo", "sampled-head") is None


def test_audit_uses_enriched_head_checks_not_name_only_rollup(monkeypatch):
    pr = _pr(checks=GREEN)
    pr["baseRefName"] = "master"
    monkeypatch.setattr(audit_mod, "open_drafts", lambda repo: [pr])
    monkeypatch.setattr(audit_mod, "unresolved_threads", lambda repo, number: 0)
    monkeypatch.setattr(audit_mod, "required_checks", lambda repo, base: [{"context": "tests", "app_id": 123}])
    calls = []

    def checks(repo, oid):
        calls.append(oid)
        return [{**GREEN[0], "app_id": 456}]

    monkeypatch.setattr(audit_mod, "head_check_rollup", checks)
    assert audit_mod.audit("owner/repo", 0)[0]["class"] == "CI-PENDING"
    assert calls == ["sampled-head"]

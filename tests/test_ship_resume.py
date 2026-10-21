"""Delivery resumes only from live, owned-head evidence; no GitHub writes in tests."""

from copy import deepcopy
import json
import subprocess
from unittest.mock import Mock

import pytest

from scripts.dev import _ship_resume as ship


AGENT = "00000000-0000-4000-8000-000000000042"
HEAD = "a" * 40


@pytest.fixture
def delivery(monkeypatch):
    pr = {
        "number": 42, "url": "https://example.invalid/pr/42", "state": "OPEN",
        "body": f"Classification: fix\n\nUNITARES agent: {AGENT}\n",
        "headRefName": "codex/workflow", "headRefOid": HEAD,
        "baseRefName": "master", "baseRefOid": "b" * 40,
        "isDraft": True, "isCrossRepository": False, "labels": [], "comments": [],
        "statusCheckRollup": [{"__typename": "CheckRun", "name": "test",
                               "status": "COMPLETED", "conclusion": "SUCCESS"}],
    }
    calls = []
    events = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        if args == ("git", "branch", "--show-current"):
            return "codex/workflow\n"
        if args == ("git", "rev-parse", "HEAD"):
            return HEAD + "\n"
        if args[:3] == ("git", "status", "--porcelain=v1"):
            return ""
        if args[:2] == ("git", "show"):
            if args[2].endswith("review_policy.json"):
                return json.dumps({"second_family_paths": ["scripts/dev/review_gate.py"]})
            return "tests/test_policy.py\tENFORCE\toperator boundary\n"
        if args[:2] == ("git", "diff") and "--name-only" in args:
            return "docs/dev/change.md\0"
        if args[:2] == ("git", "diff"):
            return ""
        if args[:2] == ("gh", "api"):
            if "--method" in args:
                body = args[args.index("--raw-field") + 1].removeprefix("body=")
                stamp = "2026-10-21T00:00:02Z" if "renew-confirm" in body else "2026-10-21T00:00:00Z"
                comment = {"body": body, "author": {"login": "app/delivery-bot"}, "createdAt": stamp}
                pr["comments"].append(comment)
                return json.dumps({"body": body, "user": {"login": "delivery-bot[bot]"},
                                   "created_at": comment["createdAt"]})
            if "/comments?" in args[2]:
                return json.dumps([[{"author_association": "COLLABORATOR", **c} for c in pr["comments"]]])
            if pr["labels"] and not events:
                events.append({"id": 1, "event": "labeled", "label": {"name": "approved-to-merge"}})
            return json.dumps([events])
        if args[:3] == ("gh", "pr", "edit") and "--body" in args:
            pr["body"] = args[args.index("--body") + 1]
            return ""
        if args[:3] == ("gh", "pr", "edit"):
            event = "unlabeled" if "--remove-label" in args else "labeled"
            events.append({"id": max((e["id"] for e in events), default=0) + 1,
                           "event": event, "label": {"name": "approved-to-merge"},
                           "actor": {"login": "delivery-bot[bot]"}, "created_at": "2026-10-21T00:00:01Z"})
            pr["labels"] = [] if event == "unlabeled" else [{"name": "approved-to-merge"}]
        if args[:3] == ("gh", "pr", "ready"):
            pr["isDraft"] = False
        if args[:3] == ("gh", "pr", "comment"):
            pr["comments"].append({"body": args[args.index("--body") + 1]})
        if args[:3] == ("gh", "pr", "checks"):
            return json.dumps([{"name": "test", "bucket": "pass"}])
        if args[:3] == ("gh", "run", "list"):
            return json.dumps([{"databaseId": 2, "workflowDatabaseId": 10,
                                "status": "completed", "conclusion": "success"}])
        return ""
    monkeypatch.setattr(ship, "run", run)
    monkeypatch.setattr(ship, "snapshot", lambda: deepcopy(pr))
    monkeypatch.setattr(ship.shutil, "which", lambda _: "/installed/unitares-github")
    monkeypatch.setenv("UNITARES_AGENT_UUID", AGENT)
    return pr, calls


def execute(**kwargs):
    ship.resume(reviewer="claude", wait=False, plan=False, **kwargs)


def writes(calls):
    return [args for args, _ in calls if args[:2] == ("gh", "pr")
            and args[2] in {"ready", "edit", "comment"}]


def test_resume_joins_evidence_then_queues_without_commit_push_or_full_tests(delivery):
    _, calls = delivery
    execute()
    commands = [args for args, _ in calls]
    assert ("review", "--provider-outage-exit", "--respect-provider-cooldown", "--reviewer", "claude") in commands
    assert commands.index(("git", "prepr")) < commands.index(("gh", "pr", "ready", "42"))
    assert commands[-1] == ("git", "closeout", "--strict")
    assert any("--add-label" in args for args in writes(calls))
    assert not any("commit" in args or "push" in args or "test-cache.sh" in args for args in commands)


@pytest.mark.parametrize("field,value,reason", [
    ("body", "Classification: fix\nUNITARES agent: 00000000-0000-0000-0000-000000000000", "ownership"),
    ("headRefOid", "c" * 40, "head differ"),
    ("isCrossRepository", True, "same-repository"),
    ("baseRefName", "parent-feature", "targeting master"),
    ("labels", [{"name": "governance-sensitive"}], "hold"),
    ("body", f"Classification: capability\nUNITARES agent: {AGENT}", "operator ruling"),
])
def test_resume_preserves_ownership_and_reserved_gates(delivery, field, value, reason):
    pr, calls = delivery
    pr[field] = value
    with pytest.raises(ship.Parked, match=reason):
        execute()
    assert not writes(calls)


def test_changed_head_during_review_never_becomes_ready(delivery, monkeypatch):
    pr, calls = delivery
    moved = deepcopy(pr)
    moved["headRefOid"] = "d" * 40
    monkeypatch.setattr(ship, "snapshot", Mock(side_effect=[deepcopy(pr), moved]))
    with pytest.raises(ship.Parked, match="head differ"):
        execute()
    assert not writes(calls)


def test_base_move_requires_reinspection(delivery, monkeypatch):
    pr, calls = delivery
    moved = deepcopy(pr)
    moved["baseRefOid"] = "c" * 40
    monkeypatch.setattr(ship, "snapshot", Mock(side_effect=[deepcopy(pr), moved]))
    with pytest.raises(ship.Parked, match="base moved"):
        execute()
    assert not writes(calls)


def test_incomplete_independent_review_does_not_queue(delivery, monkeypatch):
    _, calls = delivery
    original = ship.run
    def run(*args, **kwargs):
        if args[0] == "review":
            raise ship.Parked("second model family still required")
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    with pytest.raises(ship.Parked, match="second model family"):
        execute()
    assert not writes(calls)


def test_dirty_work_is_never_committed_by_resume(delivery, monkeypatch):
    _, calls = delivery
    original = ship.run
    def run(*args, **kwargs):
        if args[:2] == ("git", "status"):
            return " M src/changed.py\n"
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    with pytest.raises(ship.Parked, match="Commit and push"):
        execute()
    assert not writes(calls)


def test_missing_identity_parks_without_remote_calls(delivery, monkeypatch):
    _, calls = delivery
    monkeypatch.delenv("UNITARES_AGENT_UUID")
    with pytest.raises(ship.Parked, match="bound UNITARES identity"):
        execute()
    assert not calls


@pytest.mark.parametrize("status,conclusion", [("IN_PROGRESS", ""), ("COMPLETED", "ACTION_REQUIRED")])
def test_pending_ci_and_open_review_findings_park(delivery, status, conclusion):
    pr, calls = delivery
    pr["statusCheckRollup"][0].update(status=status, conclusion=conclusion)
    with pytest.raises(ship.Parked, match="CI is pending or failing"):
        execute()
    assert not writes(calls)


def test_missing_aggregate_does_not_hide_queued_workflow(delivery, monkeypatch):
    _, calls = delivery
    original = ship.run
    def run(*args, **kwargs):
        if args[:3] == ("gh", "run", "list"):
            return json.dumps([{"databaseId": 2, "workflowDatabaseId": 10,
                                "status": "queued", "conclusion": ""}])
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    with pytest.raises(ship.Parked, match="workflow is queued"):
        execute()
    assert not writes(calls)


def test_trusted_base_manifest_blocks_unlabelled_reserved_change(delivery, monkeypatch):
    _, calls = delivery
    original = ship.run
    def run(*args, **kwargs):
        if args[:2] == ("git", "diff") and "-U0" in args:
            return "-ENFORCE = True\n+ENFORCE = False\n"
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    with pytest.raises(ship.Parked, match="Operator merge required"):
        execute()
    assert not writes(calls)


def test_runtime_identity_change_requires_explicit_operator_decision(delivery, monkeypatch):
    _, calls = delivery
    monkeypatch.setattr(ship, "changed_paths", lambda _: ["src/mcp_handlers/identity/handlers.py"])
    with pytest.raises(ship.Parked, match="operator decisions stay explicit"):
        execute()
    assert not writes(calls)


def test_registered_scientific_rule_never_queues_as_a_docs_fix(delivery, monkeypatch):
    _, calls = delivery
    monkeypatch.setattr(ship, "changed_paths", lambda _: ["docs/proposals/registered/eisv-outcome-grounding-stop-rule-v0.md"])
    with pytest.raises(ship.Parked, match="operator decisions stay explicit"):
        execute()
    assert not writes(calls)


def test_interrupted_renewal_can_resume_its_exact_journalled_removal(delivery, monkeypatch):
    pr, calls = delivery
    pr["labels"] = [{"name": "approved-to-merge"}]
    original = ship.run
    failed = False
    def run(*args, **kwargs):
        nonlocal failed
        if "--add-label" in args and not failed:
            failed = True
            raise ship.CommandFailed(args, Mock(returncode=1, stdout="", stderr="transient network error"))
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    with pytest.raises(ship.CommandFailed):
        execute()
    assert not pr["labels"]
    assert any("prior_event=1" in c["body"] for c in pr["comments"])
    execute()
    assert pr["labels"] == [{"name": "approved-to-merge"}]
    assert sum("--remove-label" in args for args in writes(calls)) == 1


def test_later_external_veto_does_not_match_renewal_journal(delivery, monkeypatch):
    pr, calls = delivery
    pr["comments"] = [{"body": ship.intent_marker(pr, AGENT, 0),
                       "author": {"login": "app/delivery-bot"}, "createdAt": "2026-10-21T00:00:00Z"}]
    original = ship.run
    def run(*args, **kwargs):
        if args[:2] == ("gh", "api"):
            return json.dumps([[{"id": i, "event": event, "label": {"name": "approved-to-merge"},
                                 "actor": {"login": "delivery-bot[bot]"}, "created_at": "2026-10-21T00:00:01Z"}
                                for i, event in [(1, "unlabeled"), (2, "labeled"), (3, "unlabeled")]]])
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    with pytest.raises(ship.Parked, match="approval was removed"):
        execute()
    assert not writes(calls)


def test_event_api_lag_keeps_a_recoverable_pre_removal_intent(delivery, monkeypatch):
    pr, calls = delivery
    pr["labels"] = [{"name": "approved-to-merge"}]
    original = ship.run
    hidden = False
    def run(*args, **kwargs):
        nonlocal hidden
        if args[:2] == ("gh", "api") and "/events?" in args[2] and not pr["labels"] and not hidden:
            hidden = True
            return "[[]]"
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    with pytest.raises(ship.Parked, match="not yet visible"):
        execute()
    assert not pr["labels"] and any("renew-intent" in c["body"] for c in pr["comments"])
    execute()
    assert pr["labels"] == [{"name": "approved-to-merge"}]
    assert sum("--remove-label" in args for args in writes(calls)) == 1


def test_lag_before_removal_parks_without_changing_approval(delivery, monkeypatch):
    pr, calls = delivery
    pr["labels"] = [{"name": "approved-to-merge"}]
    original = ship.label_events
    reads = 0
    def label_events(value):
        nonlocal reads
        reads += 1
        if reads == 3:
            return [{"id": 2, "event": "unlabeled"}]
        return original(value)
    monkeypatch.setattr(ship, "label_events", label_events)
    with pytest.raises(ship.Parked, match="not consistent"):
        execute()
    assert not writes(calls)
    assert pr["labels"] == [{"name": "approved-to-merge"}]


def test_failed_removal_intent_cannot_override_later_same_actor_veto(delivery, monkeypatch):
    pr, calls = delivery
    pr["labels"] = [{"name": "approved-to-merge"}]
    original = ship.run
    def run(*args, **kwargs):
        if "--remove-label" in args:
            raise ship.CommandFailed(args, Mock(returncode=1, stdout="", stderr="removal failed"))
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    with pytest.raises(ship.CommandFailed):
        execute()
    assert not any("renew-confirm" in c["body"] for c in pr["comments"])
    original("gh", "pr", "edit", "42", "--remove-label", "approved-to-merge", remote=True)
    with pytest.raises(ship.Parked, match="approval was removed"):
        execute()
    assert not any("--add-label" in args for args in writes(calls))


def test_another_actors_removal_never_uses_our_intent(delivery):
    pr, _ = delivery
    pr["comments"] = [{"body": ship.intent_marker(pr, AGENT, 0),
                       "author": {"login": "app/delivery-bot"}, "createdAt": "2026-10-21T00:00:00Z"}]
    events = [{"id": 1, "event": "unlabeled", "actor": {"login": "operator"},
               "created_at": "2026-10-21T00:00:01Z"}]
    assert not ship.own_renewal(pr, AGENT, events)


def test_quoted_intent_in_review_text_is_not_a_renewal_record(delivery):
    pr, _ = delivery
    pr["comments"] = [{"body": "Review example:\n" + ship.intent_marker(pr, AGENT, 0),
                       "author": {"login": "app/delivery-bot"}, "createdAt": "2026-10-21T00:00:00Z"}]
    events = [{"id": 1, "event": "unlabeled", "actor": {"login": "delivery-bot[bot]"},
               "created_at": "2026-10-21T00:00:01Z"}]
    assert not ship.own_renewal(pr, AGENT, events)


def test_cancelled_push_check_does_not_hide_successful_pr_workflows(delivery):
    pr, _ = delivery
    pr["statusCheckRollup"].append({"__typename": "CheckRun", "name": "push guard",
                                   "status": "COMPLETED", "conclusion": "CANCELLED"})
    execute()


def test_unlabel_history_survives_push_and_missing_old_receipt(delivery, monkeypatch):
    _, calls = delivery
    original = ship.run
    def run(*args, **kwargs):
        if args[:2] == ("gh", "api"):
            return json.dumps([[{"id": 2, "event": "unlabeled", "label": {"name": "approved-to-merge"}}]])
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    with pytest.raises(ship.Parked, match="across subsequent pushes"):
        execute()
    assert not writes(calls)


def test_old_head_receipt_and_removed_label_stay_held(delivery):
    pr, calls = delivery
    pr["comments"] = [{"body": f"<!-- unitares-ship-ready head={'c' * 40} agent={AGENT} -->"}]
    with pytest.raises(ship.Parked, match="approval was removed"):
        execute()
    assert not writes(calls)


@pytest.mark.parametrize("mandatory", [False, True])
def test_advisory_outage_exception_keeps_mandatory_family_gate(delivery, monkeypatch, mandatory):
    pr, calls = delivery
    original = ship.run
    def run(*args, **kwargs):
        if args[0] == "review":
            raise ship.CommandFailed(args, Mock(returncode=4, stdout="[review] UNREVIEWED: provider unavailable", stderr=""))
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    monkeypatch.setattr(ship, "mandatory_review", lambda _: mandatory)
    if mandatory:
        with pytest.raises(ship.CommandFailed):
            execute()
        assert not writes(calls)
    else:
        execute()
        assert any(args[2] == "ready" for args in writes(calls))
        assert any("Advisory review unavailable" in " ".join(args) for args in writes(calls))
        assert "Advisory review unavailable" in pr["body"]
        assert pr["body"].rstrip().endswith(f"UNITARES agent: {AGENT}")


def test_pending_review_is_not_an_advisory_outage(delivery, monkeypatch):
    _, calls = delivery
    original = ship.run
    def run(*args, **kwargs):
        if args[0] == "review":
            raise ship.CommandFailed(args, Mock(returncode=2, stdout="[review] native review pending; provider unavailable", stderr=""))
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    with pytest.raises(ship.CommandFailed):
        execute()
    assert not writes(calls)


def test_advisory_exception_receipt_is_not_reposted(delivery, monkeypatch):
    pr, calls = delivery
    pr["body"] = f"<!-- unitares-ship-unreviewed head={HEAD} agent={AGENT} -->\n" + pr["body"]
    pr["isDraft"] = False
    pr["labels"] = [{"name": "approved-to-merge"}]
    pr["comments"] = [{"body": f"<!-- unitares-ship-ready head={HEAD} agent={AGENT} -->"},
                      {"body": f"<!-- unitares-ship-unreviewed head={HEAD} agent={AGENT} -->"}]
    original = ship.run
    def run(*args, **kwargs):
        if args[0] == "review":
            raise ship.CommandFailed(args, Mock(returncode=4, stdout="[review] provider unavailable", stderr=""))
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    execute()
    assert not writes(calls)


def test_resuming_recorded_readiness_is_idempotent(delivery):
    pr, calls = delivery
    pr["isDraft"] = False
    pr["labels"] = [{"name": "approved-to-merge"}]
    pr["comments"] = [{"body": f"<!-- unitares-ship-ready head={HEAD} agent={AGENT} -->"}]
    execute()
    assert not writes(calls)


def test_stale_label_is_renewed_once_for_validated_head(delivery):
    pr, calls = delivery
    pr["labels"] = [{"name": "approved-to-merge"}]
    execute()
    edits = [args for args in writes(calls) if args[2] == "edit"]
    assert "--remove-label" in edits[0] and "--add-label" in edits[1]


def test_removed_queue_approval_is_not_automatically_restored(delivery):
    pr, calls = delivery
    pr["comments"] = [{"body": f"<!-- unitares-ship-ready head={HEAD} agent={AGENT} -->"}]
    with pytest.raises(ship.Parked, match="approval was removed"):
        execute()
    assert not writes(calls)


def test_approval_removed_during_validation_is_left_removed(delivery, monkeypatch):
    pr, calls = delivery
    pr["labels"] = [{"name": "approved-to-merge"}]
    held = deepcopy(pr)
    held["labels"] = []
    monkeypatch.setattr(ship, "snapshot", Mock(side_effect=[deepcopy(pr), deepcopy(pr), held]))
    with pytest.raises(ship.Parked, match="removed during validation"):
        execute()
    assert not writes(calls)


def test_plan_does_not_start_review_or_mutate_pr(delivery):
    _, calls = delivery
    ship.resume(reviewer=None, wait=False, plan=True)
    assert not writes(calls)
    assert not any(args[0] == "review" for args, _ in calls)


def test_remote_commands_use_installed_app_runner(monkeypatch):
    monkeypatch.setattr(ship.shutil, "which", lambda _: "/installed/unitares-github")
    command = Mock(return_value=Mock(returncode=0, stdout="ok", stderr=""))
    monkeypatch.setattr(ship.subprocess, "run", command)
    ship.run("git", "prepr", remote=True, read_only=True)
    assert command.call_args.args[0] == ["unitares-github", "--read-only", "--", "git", "prepr"]


def test_missing_app_runner_does_not_fall_back_to_human_login(monkeypatch):
    monkeypatch.setattr(ship.shutil, "which", lambda _: None)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    command = Mock()
    monkeypatch.setattr(ship.subprocess, "run", command)
    with pytest.raises(ship.Parked, match="refusing implicit gh login"):
        ship.run("gh", "pr", "ready", "42", remote=True)
    command.assert_not_called()


def test_renaming_reserved_document_keeps_its_source_in_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-q", "-b", "master")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "commit.gpgsign", "false")
    git("config", "diff.renames", "true")
    source = tmp_path / "docs/proposals/registered/stop-rule.md"
    source.parent.mkdir(parents=True)
    source.write_text("Registered stop thresholds.\n")
    git("add", ".")
    git("commit", "-qm", "registered rule")
    base = git("rev-parse", "HEAD")
    target = tmp_path / "docs/operations/stop-rule.md"
    target.parent.mkdir(parents=True)
    git("mv", str(source), str(target))
    git("commit", "-qm", "move rule")
    assert set(ship.changed_paths({"baseRefOid": base})) == {
        "docs/proposals/registered/stop-rule.md", "docs/operations/stop-rule.md",
    }


def test_unavailable_historical_commit_never_uses_advisory_exception(delivery, monkeypatch):
    _, calls = delivery
    original = ship.run
    def run(*args, **kwargs):
        if args[0] == "review":
            raise ship.CommandFailed(args, Mock(returncode=2,
                stdout="[review] UNREVIEWED: review history is incomplete: historical commit unavailable", stderr=""))
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    with pytest.raises(ship.CommandFailed):
        execute()
    assert not writes(calls)


def test_missing_base_is_fetched_before_policy_inspection(delivery, monkeypatch):
    _, calls = delivery
    original = ship.run
    fetched = False
    def run(*args, **kwargs):
        nonlocal fetched
        if args[:2] == ("git", "cat-file") and not fetched:
            raise ship.CommandFailed(args, Mock(returncode=1, stdout="", stderr="missing base"))
        if args[:2] == ("git", "fetch"):
            fetched = True
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    execute()
    fetch = next(i for i, (args, _) in enumerate(calls) if args[:2] == ("git", "fetch"))
    policy = next(i for i, (args, _) in enumerate(calls) if args[:2] == ("git", "show"))
    assert fetched and fetch < policy
    assert calls[fetch][1] == {"remote": True, "read_only": True}


def test_missing_host_tools_park_before_review_spending(delivery, monkeypatch):
    _, calls = delivery
    monkeypatch.setattr(ship.shutil, "which", lambda _: None)
    with pytest.raises(ship.Parked, match="Missing required host tool"):
        execute()
    assert not any(args[0] in {"review", "./scripts/dev/review.sh"} for args, _ in calls)
    assert not writes(calls)


def test_post_readiness_failure_reports_approval_remains(delivery, monkeypatch, capsys):
    pr, _ = delivery
    pr["isDraft"], pr["labels"] = False, [{"name": "approved-to-merge"}]
    monkeypatch.setattr(ship, "resume", lambda **kw: (_ for _ in ()).throw(ship.Parked("receipt failed")))
    monkeypatch.setattr("sys.argv", ["ship-resume"])
    assert ship.main() == 2
    assert "already ready and approved" in capsys.readouterr().err


@pytest.mark.parametrize("labelled", [False, True])
def test_untrusted_ready_marker_cannot_hold_delivery_or_skip_renewal(delivery, labelled):
    pr, calls = delivery
    pr["comments"] = [{"body": f"<!-- unitares-ship-ready head={HEAD} agent={AGENT} -->",
                       "author_association": "NONE", "user": {"type": "User", "id": 7}}]
    if labelled:
        pr["labels"] = [{"name": "approved-to-merge"}]
    execute()
    assert any("--add-label" in args for args in writes(calls))
    assert any("--remove-label" in args for args in writes(calls)) == labelled


def test_untrusted_advisory_marker_cannot_suppress_receipt(delivery, monkeypatch):
    pr, calls = delivery
    pr["comments"] = [{"body": f"<!-- unitares-ship-unreviewed head={HEAD} agent={AGENT} -->",
                       "author_association": "NONE"}]
    original = ship.run
    def run(*args, **kwargs):
        if args[0] == "review":
            raise ship.CommandFailed(args, Mock(returncode=4, stdout="provider unavailable", stderr=""))
        return original(*args, **kwargs)
    monkeypatch.setattr(ship, "run", run)
    execute()
    assert any(args[:3] == ("gh", "pr", "comment") and "Advisory review unavailable" in " ".join(args)
               for args in writes(calls))

"""Regression tests for scripts/dev/stranded_work_audit.py."""

from scripts.dev import stranded_work_audit as audit


def test_unmerged_patch_commits_returns_only_positive_patch_ids(monkeypatch):
    monkeypatch.setattr(
        audit,
        "run",
        lambda *cmd: "+ aaa first\n- bbb landed\n+ ccc second\n",
    )

    assert audit.unmerged_patch_commits("topic", "merged-head") == ["aaa", "ccc"]


def test_sensitive_path_contract():
    assert audit._is_sensitive_path("db/postgres/migrations/064_fix.sql")
    assert audit._is_sensitive_path("scripts/ops/restart_service.sh")
    assert audit._is_sensitive_path("config/com.unitares.worker.plist")
    assert audit._is_sensitive_path("scripts/templates/worker.plist.template")
    assert not audit._is_sensitive_path("docs/proposals/worker.md")


def test_audit_forces_review_for_recent_or_sensitive_dangling_work(monkeypatch):
    branches = ["active", "recent", "sensitive", "stale"]
    ages = {"active": 3, "recent": 8, "sensitive": 40, "stale": 40}
    sensitive = {
        "active": [],
        "recent": [],
        "sensitive": ["db/postgres/migrations/064_fix.sql"],
        "stale": [],
    }
    monkeypatch.setattr(audit, "run", lambda *cmd: "branch-sha\n")
    monkeypatch.setattr(audit, "remote_branches", lambda: branches)
    monkeypatch.setattr(audit, "newest_pr", lambda repo, branch: None)
    monkeypatch.setattr(audit, "tip_age_days", lambda branch: ages[branch])
    monkeypatch.setattr(audit, "unmerged_patch_count", lambda branch: 1)
    monkeypatch.setattr(audit, "sensitive_paths", lambda branch: sensitive[branch])

    findings = {
        finding["branch"]: finding for finding in audit.audit("cirwel/unitares", 7, 14)
    }

    assert "active" not in findings
    assert findings["recent"]["class"] == "DANGLING-REVIEW"
    assert "recent tip" in findings["recent"]["detail"]
    assert findings["sensitive"]["class"] == "DANGLING-REVIEW"
    assert "064_fix.sql" in findings["sensitive"]["detail"]
    assert findings["stale"]["class"] == "DANGLING-STALE"


def test_historical_master_match_prevents_false_stranded_alarm(monkeypatch):
    monkeypatch.setattr(audit, "_paths_touched_by", lambda commits: ["src/example.py"])
    monkeypatch.setattr(audit, "run", lambda *cmd: "newer\nlanded\n")

    def fake_succeeds(*cmd):
        # Current master evolved, but commit `landed` exactly matched the branch.
        return cmd[:4] == ("git", "diff", "--quiet", "landed")

    monkeypatch.setattr(audit, "succeeds", fake_succeeds)

    assert audit._end_state_differs("topic", ["post-merge-commit"]) is False


def test_missing_historical_match_keeps_stranded_alarm(monkeypatch):
    monkeypatch.setattr(audit, "_paths_touched_by", lambda commits: ["src/example.py"])
    monkeypatch.setattr(audit, "run", lambda *cmd: "candidate\n")
    monkeypatch.setattr(audit, "succeeds", lambda *cmd: False)

    assert audit._end_state_differs("topic", ["post-merge-commit"]) is True

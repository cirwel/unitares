"""Regression tests for scripts/dev/stranded_work_audit.py."""

import subprocess

import pytest

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
    monkeypatch.setattr(audit, "unmerged_patch_commits", lambda branch: ["unique"])
    monkeypatch.setattr(audit, "_end_state_differs", lambda branch, commits: True)
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


@pytest.fixture
def dangling_history(tmp_path, monkeypatch):
    """A two-commit branch whose combined patch can land by squash elsewhere."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")

    def git(*args):
        return subprocess.run(
            ["git", "-c", "user.name=Audit fixture", "-c",
             "user.email=audit@example.invalid", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=/dev/null", *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init", "--initial-branch=master")
    path = tmp_path / "example.txt"
    path.write_text("first\nsecond\nthird\n")
    git("add", ".")
    git("commit", "-m", "Base")
    git("update-ref", "refs/remotes/origin/master", "HEAD")
    git("checkout", "-b", "topic")
    monkeypatch.setattr(audit, "remote_branches", lambda: ["topic"])
    monkeypatch.setattr(audit, "tip_age_days", lambda branch: 10)
    return git, path


@pytest.mark.parametrize("closed_pr", [False, True])
@pytest.mark.parametrize("evolved", [False, True])
@pytest.mark.parametrize("deletion", [False, True])
def test_dangling_squash_landing_is_prunable(
    dangling_history, monkeypatch, closed_pr, evolved, deletion,
):
    git, path = dangling_history
    monkeypatch.setattr(
        audit, "newest_pr",
        lambda repo, branch: {"number": 17, "state": "CLOSED"} if closed_pr else None,
    )
    for index, content in enumerate(
        ("second\nthird\n", "third\n") if deletion
        else ("first\nsecond\nthird\nfourth\n", "first\nsecond\nthird\nfourth\nfifth\n")
    ):
        path.write_text(content)
        git("commit", "-am", f"Topic change {index}")
    git("update-ref", "refs/remotes/origin/topic", "HEAD")
    git("checkout", "master")
    git("merge", "--squash", "topic")
    git("commit", "-m", "Land combined patch via another branch")
    if evolved:
        path.write_text("Later master evolution\n")
        git("commit", "-am", "Evolve after landing")
    git("update-ref", "refs/remotes/origin/master", "HEAD")

    # Patch-id comparison alone misses both commits after a squash landing.
    assert len(audit.unmerged_patch_commits("topic")) == 2
    findings = audit.audit("example/repo", 7, 14)

    assert len(findings) == 1
    assert findings[0]["class"] == "PRUNABLE"
    assert findings[0]["reason"] == "no_pr_route"
    assert "exact changed-file state is accounted for" in findings[0]["detail"]


@pytest.mark.parametrize("history_failure", [False, True])
def test_unproven_dangling_work_keeps_alarm(
    dangling_history, monkeypatch, history_failure,
):
    git, path = dangling_history
    path.write_text("Unlanded work\n")
    git("commit", "-am", "Unlanded change")
    git("update-ref", "refs/remotes/origin/topic", "HEAD")
    monkeypatch.setattr(audit, "newest_pr", lambda repo, branch: None)
    monkeypatch.setattr(
        audit, "content_presence",
        lambda commits: {"present": 1, "total": 1, "pct": 100.0,
                         "commits_sampled": 1, "commits_total": 1},
    )
    if history_failure:
        real_run = audit.run

        def failing_history(*cmd):
            if cmd[:3] == ("git", "log", "--format=%H"):
                raise subprocess.CalledProcessError(1, cmd)
            return real_run(*cmd)

        monkeypatch.setattr(audit, "run", failing_history)

    findings = audit.audit("example/repo", 7, 14)

    assert len(findings) == 1
    assert findings[0]["class"] == "DANGLING-REVIEW"
    assert "100.0%" in findings[0]["detail"]


def test_historical_master_match_prevents_false_stranded_alarm(monkeypatch):
    monkeypatch.setattr(audit, "_paths_touched_by", lambda commits: ["src/example.py"])

    def fake_run(*cmd):
        if cmd[:2] == ("git", "merge-base"):
            return "fork\n"
        assert "--ancestry-path" in cmd
        assert f"--max-count={audit._MAX_LANDING_CANDIDATES}" in cmd
        assert "fork..origin/master" in cmd
        return "newer\nlanded\n"

    monkeypatch.setattr(audit, "run", fake_run)

    def fake_succeeds(*cmd):
        # Current master evolved, but commit `landed` exactly matched the branch.
        return cmd[:4] == ("git", "diff", "--quiet", "landed")

    monkeypatch.setattr(audit, "succeeds", fake_succeeds)

    assert audit._end_state_differs("topic", ["post-merge-commit"]) is False


@pytest.mark.parametrize("pr_state", [None, "CLOSED", "MERGED"])
def test_unlanded_rollback_matching_pre_fork_master_keeps_alarm(
    dangling_history, monkeypatch, pr_state,
):
    git, path = dangling_history
    old_content = path.read_text()
    git("checkout", "master")
    path.write_text("New master state\n")
    git("commit", "-am", "Advance master before the branch forks")
    fork = git("rev-parse", "HEAD")
    git("update-ref", "refs/remotes/origin/master", "HEAD")
    git("checkout", "-B", "topic", "master")
    path.write_text(old_content)
    git("commit", "-am", "Unlanded rollback to the old state")
    git("update-ref", "refs/remotes/origin/topic", "HEAD")
    monkeypatch.setattr(
        audit, "newest_pr", lambda repo, branch: {
            "number": 17, "state": pr_state, "headRefOid": fork,
        } if pr_state else None,
    )

    assert len(audit.unmerged_patch_commits("topic")) == 1
    assert audit._end_state_differs("topic", audit.unmerged_patch_commits("topic"))
    expected = "STRANDED" if pr_state == "MERGED" else "DANGLING-REVIEW"
    assert audit.audit("example/repo", 7, 14)[0]["class"] == expected


def test_net_zero_branch_delta_is_accounted_for(dangling_history, monkeypatch):
    git, path = dangling_history
    original = path.read_text()
    path.write_text("Temporary branch change\n")
    git("commit", "-am", "Branch change")
    path.write_text(original)
    git("commit", "-am", "Undo branch change")
    git("update-ref", "refs/remotes/origin/topic", "HEAD")
    git("checkout", "master")
    path.write_text("Independent master evolution\n")
    git("commit", "-am", "Evolve master")
    git("update-ref", "refs/remotes/origin/master", "HEAD")
    monkeypatch.setattr(audit, "newest_pr", lambda repo, branch: None)

    assert len(audit.unmerged_patch_commits("topic")) == 2
    assert audit.audit("example/repo", 7, 14)[0]["class"] == "PRUNABLE"


def test_exhausted_landing_search_budget_keeps_finding(dangling_history, monkeypatch):
    git, path = dangling_history
    for index in range(2):
        path.write_text(f"Branch change {index}\n")
        git("commit", "-am", f"Topic change {index}")
    git("update-ref", "refs/remotes/origin/topic", "HEAD")
    git("checkout", "master")
    git("merge", "--squash", "topic")
    git("commit", "-m", "Squash landing")
    path.write_text("Later master evolution\n")
    git("commit", "-am", "Evolve master")
    git("update-ref", "refs/remotes/origin/master", "HEAD")
    monkeypatch.setattr(audit, "newest_pr", lambda repo, branch: None)
    monkeypatch.setattr(audit, "_MAX_LANDING_CANDIDATES", 1)

    # The valid landing is beyond the scan budget, so no landing is claimed.
    assert audit.audit("example/repo", 7, 14)[0]["class"] == "DANGLING-REVIEW"


def test_absent_changed_file_evidence_keeps_finding(monkeypatch):
    monkeypatch.setattr(audit, "_paths_touched_by", lambda commits: [])

    assert audit._end_state_differs("topic", ["empty-commit"]) is True


def test_missing_historical_match_keeps_stranded_alarm(monkeypatch):
    monkeypatch.setattr(audit, "_paths_touched_by", lambda commits: ["src/example.py"])
    monkeypatch.setattr(audit, "run", lambda *cmd: "candidate\n")
    monkeypatch.setattr(audit, "succeeds", lambda *cmd: False)

    assert audit._end_state_differs("topic", ["post-merge-commit"]) is True


def test_absent_merged_head_is_indeterminate_not_stranded(monkeypatch):
    """The false-alarm mechanism behind the 2026-08-19 audit run.

    GitHub deletes the head branch on merge, and the workflow fetches only
    `refs/heads/*`, so a merged PR's head object is absent in CI. The old code
    then dropped the `since` limit and compared the WHOLE branch, so every
    commit the PR squashed reported as unlanded and the branch read STRANDED.
    All five STRANDED entries in the 2026-08-19 issue were this.

    A false STRANDED tells the reader to re-land landed work, which means
    branch surgery on a merged-PR branch — the operation that lost two pushed
    commits that same day. When the head cannot be fetched the honest answer is
    "cannot tell", not the alarm.
    """
    monkeypatch.setattr(audit, "run", lambda *cmd: "branch-sha\n")
    monkeypatch.setattr(audit, "remote_branches", lambda: ["topic"])
    monkeypatch.setattr(
        audit,
        "newest_pr",
        lambda repo, branch: {
            "number": 1498,
            "state": "MERGED",
            "headRefOid": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        },
    )
    monkeypatch.setattr(audit, "tip_age_days", lambda branch: 30)
    # The head cannot be resolved even after a fetch attempt.
    monkeypatch.setattr(audit, "resolve_merged_head", lambda sha, pr: None)
    # If the limit were dropped, this would fire and report STRANDED.
    monkeypatch.setattr(
        audit,
        "unmerged_patch_commits",
        lambda branch, since=None: ["squashed-commit"],
    )
    monkeypatch.setattr(audit, "_end_state_differs", lambda branch, commits: True)

    findings = audit.audit("cirwel/unitares", 7, 14)

    assert len(findings) == 1
    assert findings[0]["class"] == "INDETERMINATE"
    assert findings[0]["class"] != "STRANDED"
    assert "could not be fetched" in findings[0]["detail"]


def test_resolve_merged_head_fetches_the_pull_ref_before_giving_up(monkeypatch):
    """`refs/pull/N/head` outlives the deleted branch; a bare-sha fetch may not."""
    attempted = []
    present = {"value": False}

    monkeypatch.setattr(audit, "_known_object", lambda sha: present["value"])

    def fake_succeeds(*cmd):
        attempted.append(cmd)
        if cmd[-1] == "refs/pull/1498/head":
            present["value"] = True
            return True
        return False

    monkeypatch.setattr(audit, "succeeds", fake_succeeds)

    assert audit.resolve_merged_head("abc123", 1498) == "abc123"
    assert attempted[0][-1] == "refs/pull/1498/head"


def test_resolve_merged_head_short_circuits_when_already_present(monkeypatch):
    """No network call when the object is already local."""
    monkeypatch.setattr(audit, "_known_object", lambda sha: True)
    monkeypatch.setattr(
        audit,
        "succeeds",
        lambda *cmd: pytest.fail("should not fetch an object we already have"),
    )

    assert audit.resolve_merged_head("abc123", 1498) == "abc123"


class TestContentPresence:
    """The triage hint: how much of a commit's added content is in master.

    `_end_state_differs` proves a landing by exact file match somewhere in
    master's history. When a fix landed and the file kept evolving, no such
    point exists, so the branch stays an alarm with nothing to distinguish
    "superseded" from "lost". This reading fills that gap — and must never
    fill it by retiring the alarm itself.
    """

    def test_reports_the_share_of_added_lines_found_in_master(self, monkeypatch):
        monkeypatch.setattr(
            audit, "_added_lines",
            lambda commit: {"a.py": ["landed line one here", "missing line two here"]})
        monkeypatch.setattr(audit, "_master_file", lambda path: "landed line one here\n")
        result = audit.content_presence(["c1"])
        assert result["present"] == 1
        assert result["total"] == 2
        assert result["pct"] == 50.0

    def test_absent_file_in_master_reads_as_zero_present(self, monkeypatch):
        monkeypatch.setattr(
            audit, "_added_lines", lambda commit: {"gone.py": ["some substantive line"]})
        monkeypatch.setattr(audit, "_master_file", lambda path: None)
        assert audit.content_presence(["c1"])["pct"] == 0.0

    def test_no_substantive_additions_is_none_not_zero_percent(self, monkeypatch):
        # A pure deletion or rename adds nothing to look for. Rendering that as
        # "0% already in master" would read as a maximally alarming finding.
        monkeypatch.setattr(audit, "_added_lines", lambda commit: {"a.py": []})
        assert audit.content_presence(["c1"]) is None

    def test_no_commits_is_none(self):
        assert audit.content_presence([]) is None

    def test_sampling_is_capped_and_says_so(self, monkeypatch):
        monkeypatch.setattr(
            audit, "_added_lines", lambda commit: {"a.py": ["a substantive added line"]})
        monkeypatch.setattr(audit, "_master_file", lambda path: "")
        result = audit.content_presence([f"c{i}" for i in range(50)])
        assert result["commits_sampled"] == audit._MAX_SAMPLED_COMMITS
        assert result["commits_total"] == 50

    def test_git_failure_degrades_to_none_not_to_a_number(self, monkeypatch):
        def boom(commit):
            raise subprocess.CalledProcessError(1, "git")
        monkeypatch.setattr(audit, "_added_lines", boom)
        assert audit.content_presence(["c1"]) is None


class TestAnnotateDoesNotVote:
    """The hint annotates a decided finding; it must not change the class."""

    def test_class_is_untouched_by_a_high_presence_reading(self, monkeypatch):
        monkeypatch.setattr(
            audit, "content_presence",
            lambda commits: {"present": 100, "total": 100, "pct": 100.0,
                             "commits_sampled": 1, "commits_total": 1})
        finding = audit._annotate(
            {"branch": "b", "class": "STRANDED", "detail": "d"}, ["c1"])
        assert finding["class"] == "STRANDED"
        assert "100.0% of 100 added line(s) already in master" in finding["detail"]

    def test_a_none_reading_adds_nothing(self, monkeypatch):
        monkeypatch.setattr(audit, "content_presence", lambda commits: None)
        finding = audit._annotate(
            {"branch": "b", "class": "STRANDED", "detail": "d"}, ["c1"])
        assert finding["detail"] == "d"
        assert "content_presence" not in finding


class TestPostMergePushRate:
    """The tally separates how often the mechanism fires from what it cost.

    A post-merge push strands work or doesn't depending on whether the push
    carried anything new — the same event, two outcomes. Counting only
    STRANDED counts the bad rolls and reads as the exposure.
    """

    def test_counts_the_event_across_all_three_outcomes(self):
        rate = audit.post_merge_push_rate([
            {"class": "STRANDED", "reason": "post_merge_push"},
            {"class": "PRUNABLE", "reason": "post_merge_push"},
            {"class": "PRUNABLE", "reason": "post_merge_push"},
            {"class": "INDETERMINATE", "reason": "post_merge_push"},
        ])
        assert rate["fired"] == 4
        assert rate["carried_stranded_work"] == 1
        assert rate["carried_nothing_new"] == 2
        assert rate["undetermined"] == 1

    def test_hygiene_is_excluded_from_the_denominator(self):
        # A branch nobody re-pushed to, and one that never had a PR, did not
        # reach this event. Folding them in would inflate `fired`.
        rate = audit.post_merge_push_rate([
            {"class": "STRANDED", "reason": "post_merge_push"},
            {"class": "PRUNABLE", "reason": "merged_head_never_advanced"},
            {"class": "PRUNABLE", "reason": "no_pr_route"},
        ])
        assert rate["fired"] == 1
        assert rate["hygiene_not_this_mechanism"] == 2

    def test_no_findings_is_all_zeroes(self):
        rate = audit.post_merge_push_rate([])
        assert rate["fired"] == 0
        assert rate["carried_stranded_work"] == 0

    def test_a_finding_without_a_reason_is_not_counted(self):
        # Older callers, or a class added without a reason, must not silently
        # land in the tally as if the mechanism had fired.
        assert audit.post_merge_push_rate([{"class": "STRANDED"}])["fired"] == 0

    def test_the_tally_does_not_alter_findings(self):
        findings = [{"class": "STRANDED", "reason": "post_merge_push"}]
        audit.post_merge_push_rate(findings)
        assert findings == [{"class": "STRANDED", "reason": "post_merge_push"}]


def test_every_finding_path_sets_a_reason():
    """A finding without a `reason` is invisible to post_merge_push_rate.

    The tally counts by `reason`, so a new class added without one lands
    nowhere and shrinks `fired` — the instrument under-reporting how often the
    mechanism fires, while still looking like it ran. That is the failure this
    module exists to prevent, so pin it structurally rather than trusting the
    next editor to remember.
    """
    import inspect
    body = inspect.getsource(audit.audit)
    assert body.count('"class":') == body.count('"reason":'), (
        "a finding in audit() sets a class without a reason; it will be "
        "excluded from post_merge_push_rate and undercount the mechanism"
    )

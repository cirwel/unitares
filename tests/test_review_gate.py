"""Tests for scripts/dev/review_gate.py — the review record and its CI status.

The load-bearing property is the diff key: it must survive a base merge that
does not touch the PR's files (or draft-base-refresh would void every review),
and it must change when the PR's own content does (or a stale review would
pass a new diff)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "review_gate.py"
_spec = importlib.util.spec_from_file_location("review_gate", SCRIPT)
rg = importlib.util.module_from_spec(_spec)
sys.modules["review_gate"] = rg
_spec.loader.exec_module(rg)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "master")
    _git(r, "config", "user.email", "t@example.invalid")
    _git(r, "config", "user.name", "t")
    (r / "a.txt").write_text("a\n")
    (r / "b.txt").write_text("b\n")
    _git(r, "add", ".")
    _git(r, "commit", "-q", "-m", "base")
    _git(r, "checkout", "-q", "-b", "feature")
    (r / "a.txt").write_text("a changed\n")
    _git(r, "commit", "-q", "-am", "feature change")
    monkeypatch.chdir(r)
    return r


def test_key_survives_a_base_merge_that_does_not_touch_the_pr(repo):
    before = rg.diff_key("master", "HEAD")
    _git(repo, "checkout", "-q", "master")
    (repo / "b.txt").write_text("b moved on master\n")
    _git(repo, "commit", "-q", "-am", "master moves")
    _git(repo, "checkout", "-q", "feature")
    assert rg.diff_key("master", "HEAD") == before  # base moved, not merged
    _git(repo, "merge", "-q", "--no-edit", "master")
    assert rg.diff_key("master", "HEAD") == before  # base merged in


def test_key_changes_when_the_pr_content_changes(repo):
    before = rg.diff_key("master", "HEAD")
    (repo / "a.txt").write_text("a changed again\n")
    _git(repo, "commit", "-q", "-am", "more")
    assert rg.diff_key("master", "HEAD") != before


def test_key_ignores_local_diff_config(repo):
    before = rg.diff_key("master", "HEAD")
    _git(repo, "config", "diff.noprefix", "true")
    _git(repo, "config", "diff.algorithm", "histogram")
    _git(repo, "config", "diff.renames", "copies")
    assert rg.diff_key("master", "HEAD") == before


@pytest.mark.parametrize("text,expected", [
    ("looks fine\nVERDICT: CLEAN\n", ("CLEAN", 0)),
    ("1. x.py:3 bad\nVERDICT: FINDINGS(1)", ("FINDINGS", 1)),
    ("**VERDICT: FINDINGS(12)**", ("FINDINGS", 12)),
    # The prompt quotes both forms; only the reviewer's last line counts.
    ("VERDICT: CLEAN\nor\nVERDICT: FINDINGS(2)\n", ("FINDINGS", 2)),
    ("no verdict here", None),
    ("the VERDICT: CLEAN is inline, not a line", None),
    # Codex round 2 on #2318: trailing content after the verdict must void it.
    ("VERDICT: CLEAN\nactually, x.py:9 has a flaw\n", None),
    ("VERDICT: CLEAN\n\n  \n", ("CLEAN", 0)),
    # Codex round 7 on #2318: FINDINGS(0) would otherwise pend forever.
    ("VERDICT: FINDINGS(0)", ("CLEAN", 0)),
])
def test_parse_verdict(text, expected):
    assert rg.parse_verdict(text) == expected


def _comment(rec, association="OWNER", url="u", text="1. fixed in abc\n2. rebutted: x"):
    return {"author_association": association, "html_url": url,
            "body": rg.render_marker(rec) + "\n" + text}


def test_marker_roundtrip():
    rec = rg.Record("k" * 64, "FINDINGS", 3, True, "codex")
    got = rg.parse_record(rg.render_marker(rec) + "\nbody")
    assert (got.key, got.verdict, got.findings, got.disposed, got.reviewer) == \
        (rec.key, "FINDINGS", 3, True, "codex")


def test_latest_matching_record_wins_and_other_keys_are_ignored():
    k = "k" * 64
    comments = [
        _comment(rg.Record(k, "FINDINGS", 2, False, "codex"), url="first"),
        _comment(rg.Record("other" * 12, "CLEAN", 0, False, "codex"), url="stale"),
        _comment(rg.Record(k, "FINDINGS", 2, True, "codex"), url="disposed"),
    ]
    got = rg.latest_matching(comments, k)
    assert got.url == "disposed" and got.status()[0] == "success"


def test_untrusted_authors_cannot_post_a_record():
    k = "k" * 64
    comments = [_comment(rg.Record(k, "CLEAN", 0, False, "x"), association="NONE"),
                _comment(rg.Record(k, "CLEAN", 0, False, "x"), association="CONTRIBUTOR")]
    assert rg.latest_matching(comments, k) is None


@pytest.mark.parametrize("verdict,disposed,state", [
    ("CLEAN", False, "success"),
    ("FINDINGS", True, "success"),
    ("FINDINGS", False, "pending"),
    ("FAILED", False, "pending"),
])
def test_status_mapping(verdict, disposed, state):
    assert rg.Record("k", verdict, 1, disposed, "codex").status()[0] == state


def test_reviewer_is_the_other_model():
    assert rg.default_reviewer("codex/fix-x") == "claude"
    assert rg.default_reviewer("claude/fix-x") == "codex"
    assert rg.default_reviewer("kenny/fix-x") == "codex"


@pytest.mark.parametrize("text,n,ok", [
    ("", 1, False),
    ("   \n", 1, False),
    ("1. fixed in abc123", 1, True),
    ("1. fixed\n", 2, False),
    ("1) fixed\n2: rebutted, the caller checks it", 2, True),
    ("#1. fixed\n#2. fixed", 2, True),
])
def test_dispositions_must_cover_every_finding(text, n, ok):
    assert rg.dispositions_complete(text, n) is ok


def test_an_empty_dispositions_record_does_not_clear_the_gate():
    # Codex's finding on #2318: a disposed=1 marker over an empty body turned
    # the check green. CI must judge the body, not trust the marker.
    k = "k" * 64
    comments = [_comment(rg.Record(k, "FINDINGS", 1, True, "codex"), text="")]
    assert rg.latest_matching(comments, k).status()[0] == "pending"


def test_a_later_clean_on_the_same_diff_does_not_clear_open_findings():
    # Codex round 3 on #2318: re-rolling the reviewer (or `record`) until it
    # says CLEAN must not drop a finding that was never fixed or disposed.
    k = "k" * 64
    comments = [
        _comment(rg.Record(k, "FINDINGS", 1, False, "codex"), url="findings"),
        _comment(rg.Record(k, "CLEAN", 0, False, "claude"), url="clean"),
    ]
    got = rg.latest_matching(comments, k)
    assert got.url == "findings" and got.status()[0] == "pending"


def test_disposed_findings_then_clean_is_clean():
    k = "k" * 64
    comments = [
        _comment(rg.Record(k, "FINDINGS", 1, False, "codex")),
        _comment(rg.Record(k, "FINDINGS", 1, True, "codex"), text="1. rebutted: x"),
        _comment(rg.Record(k, "CLEAN", 0, False, "codex"), url="clean"),
    ]
    assert rg.latest_matching(comments, k).url == "clean"


def test_one_disposition_answers_one_findings_record():
    # Codex round 4 on #2318: disposing a later FINDINGS(2) must not clear an
    # earlier, unanswered FINDINGS(1) on the same diff.
    k = "k" * 64
    comments = [
        _comment(rg.Record(k, "FINDINGS", 1, False, "codex"), url="first"),
        _comment(rg.Record(k, "FINDINGS", 2, False, "claude"), url="second"),
        _comment(rg.Record(k, "FINDINGS", 2, True, "claude"), url="disp2",
                 text="1. fixed\n2. rebutted: y"),
    ]
    got = rg.latest_matching(comments, k)
    assert got.url == "first" and got.status()[0] == "pending"
    comments.append(_comment(rg.Record(k, "FINDINGS", 1, True, "codex"),
                             url="disp1", text="1. fixed"))
    assert rg.latest_matching(comments, k).status()[0] == "success"


def test_a_disposition_with_the_wrong_count_answers_nothing():
    k = "k" * 64
    comments = [
        _comment(rg.Record(k, "FINDINGS", 2, False, "codex")),
        _comment(rg.Record(k, "FINDINGS", 1, True, "codex"), text="1. fixed"),
    ]
    assert rg.latest_matching(comments, k).status()[0] == "pending"


def test_a_stale_last_message_is_not_posted_as_this_runs_verdict(tmp_path, monkeypatch):
    # Codex round 7 on #2318: a prior codex CLEAN in the cache must not be
    # read back as a later claude run's output.
    (tmp_path / "last-message.txt").write_text("VERDICT: CLEAN\n")

    class FakeProc:
        pid = 0
        def wait(self, timeout=None):
            return 0
    monkeypatch.setattr(rg.subprocess, "Popen", lambda *a, **k: FakeProc())
    text, note = rg.run_reviewer("claude", "p", tmp_path, 5)
    assert note == "exit 0" and rg.parse_verdict(text) is None


def test_key_handles_a_non_utf8_file_name(repo):
    # Codex round 9 on #2318: decoding git's output as text crashed here.
    # Built in the index, not on disk: APFS refuses non-UTF-8 names outright.
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                          input=b"x\n", capture_output=True, check=True).stdout.strip()
    subprocess.run([b"git", b"-C", bytes(repo), b"update-index", b"--add",
                    b"--cacheinfo", b"100644," + blob + b",bad-\xff.txt"], check=True)
    _git(repo, "commit", "-q", "-m", "odd name")
    assert len(rg.diff_key("master", "HEAD")) == 64


def test_a_reviewer_that_cannot_start_is_a_failure_not_an_exception(tmp_path, monkeypatch):
    # Codex round 10 on #2318: a missing CLI raised instead of recording FAILED.
    def boom(*a, **k):
        raise FileNotFoundError("codex")
    monkeypatch.setattr(rg.subprocess, "Popen", boom)
    text, note = rg.run_reviewer("codex", "p", tmp_path, 5)
    assert note.startswith("could not start codex")


def test_a_failed_rerun_does_not_hide_open_findings():
    # Codex round 11 on #2318: FAILED after FINDINGS made dispose unreachable.
    k = "k" * 64
    comments = [
        _comment(rg.Record(k, "FINDINGS", 1, False, "codex"), url="findings"),
        _comment(rg.Record(k, "FAILED", 0, False, "claude"), url="failed"),
    ]
    got = rg.latest_matching(comments, k)
    assert got.url == "findings" and got.verdict == "FINDINGS" and not got.disposed


def test_reviewer_input_survives_a_non_utf8_file_name(repo):
    # Codex round 12 on #2318: diff_text decoded strictly and crashed.
    blob = subprocess.run(["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
                          input=b"x\n", capture_output=True, check=True).stdout.strip()
    subprocess.run([b"git", b"-C", bytes(repo), b"update-index", b"--add",
                    b"--cacheinfo", b"100644," + blob + b",bad-\xff.txt"], check=True)
    _git(repo, "commit", "-q", "-m", "odd name")
    _git(repo, "config", "core.quotePath", "false")
    assert "bad-" in rg.diff_text("master", "HEAD")


def _pr(n, login="cirwel", draft=False, updated="2026-09-19T00:00:00Z"):
    return {"number": n, "isDraft": draft, "author": {"login": login},
            "updatedAt": updated, "headRefOid": "h", "headRefName": "b", "baseRefName": "master"}


def test_sweep_covers_quiet_drafts_but_respects_trust_and_explicit_holds():
    from datetime import datetime, timezone
    now = datetime(2026, 9, 19, 1, 0, tzinfo=timezone.utc).timestamp()
    prs = [
        _pr(1),                                        # owner, ready, quiet: yes
        _pr(2, login="app/dependabot"),                # dependabot: yes
        _pr(3, draft=True),                            # drafts need review to become ready
        _pr(4, login="stranger"),                      # outside author: a human first
        _pr(5, updated="2026-09-19T00:55:00Z"),        # pushed 5 min ago: not yet
        {**_pr(6, draft=True), "labels": [{"name": "no-auto-review"}]},
        {**_pr(7), "labels": [{"name": "no-auto-review"}]},
        _pr(8, draft=True, updated="2026-09-19T00:55:00Z"),
    ]
    got = [p["number"] for p in rg.sweep_candidates(prs, "cirwel", now, 15 * 60)]
    assert got == [1, 2, 3]


@pytest.mark.parametrize("verdict,expected", [("CLEAN", 0), ("FINDINGS", 1), ("FAILED", 2)])
def test_foreground_joins_running_review_and_returns_its_result(repo, monkeypatch, verdict, expected):
    key = "k" * 64
    monkeypatch.setattr(rg, "_resolve", lambda args: (1, "owner/repo", key, "codex/change"))
    comments = []
    monkeypatch.setattr(rg, "pr_comments", lambda *args: comments)
    monkeypatch.setattr(rg, "_review_locked", lambda *args: pytest.fail("duplicated running review"))
    with rg.review_lock(key) as background:
        assert background.held

        def finish_review(seconds):
            comments.append(_comment(rg.Record(key, verdict, 1, False, "claude")))
            background.__exit__()

        monkeypatch.setattr(rg.time, "sleep", finish_review)
        args = SimpleNamespace(reviewer=None, fresh=False, budget=10)
        assert rg.cmd_review(args) == expected


def test_join_timeout_does_not_claim_review_completion(repo, monkeypatch, capsys):
    key = "k" * 64
    monkeypatch.setattr(rg, "_resolve", lambda args: (1, "owner/repo", key, "codex/change"))
    with rg.review_lock(key):
        assert rg.cmd_review(SimpleNamespace(reviewer=None, fresh=False, budget=0)) == rg.UNREVIEWED
    assert "no completed review joined" in capsys.readouterr().out


def test_review_returns_findings_text_to_the_working_agent(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rg, "diff_text", lambda *args: "diff")
    monkeypatch.setattr(rg, "git", lambda *args: "abcd")
    finding = "1. file.py:3 loses the result on timeout.\nVERDICT: FINDINGS(1)"
    monkeypatch.setattr(rg, "run_reviewer", lambda *args: (finding, "exit 0"))
    records = []
    monkeypatch.setattr(rg, "post_record", lambda *args: records.append(args))
    assert rg._review_locked(SimpleNamespace(base="master", budget=10), 1, "k", "claude") == 1
    assert finding in capsys.readouterr().out
    assert records[0][1].verdict == "FINDINGS"


def test_failed_reviewer_exposes_cause_and_an_alternative(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(rg, "diff_text", lambda *args: "diff")
    monkeypatch.setattr(rg, "git", lambda *args: "abcd")
    monkeypatch.setattr(rg, "run_reviewer", lambda *args: ("Weekly limit reached", "exit 1"))
    records = []
    monkeypatch.setattr(rg, "post_record", lambda *args: records.append(args))
    assert rg._review_locked(SimpleNamespace(base="master", budget=10), 1, "k", "claude") == rg.UNREVIEWED
    output = capsys.readouterr().out
    assert "Weekly limit reached" in output and "record <file>" in output
    assert records[0][1].verdict == "FAILED"


def test_sweep_dry_run_reports_draft_without_launching_or_claiming_empty(monkeypatch, capsys):
    monkeypatch.setattr(rg, "repo_slug", lambda: "cirwel/repo")
    def listing(*args):
        assert "labels" in args[-1]
        return [_pr(3, draft=True)]
    monkeypatch.setattr(rg, "gh_json", listing)
    monkeypatch.setattr(rg, "git", lambda *args: "h")
    monkeypatch.setattr(rg, "diff_key", lambda *args: "k")
    monkeypatch.setattr(rg, "pr_comments", lambda *args: [])
    monkeypatch.setattr(rg, "review_lock", lambda *args: SimpleNamespace(holder_alive=lambda: False))
    monkeypatch.setattr(rg.subprocess, "run", lambda *a, **kw: pytest.fail("launched in dry run"))
    args = SimpleNamespace(quiet_minutes=15, dry_run=True)
    assert rg.cmd_sweep(args) == 0
    output = capsys.readouterr().out
    assert "PR #3" in output and "1 review candidate(s)" in output
    assert "no reviews to start" not in output


def test_review_lock_is_exclusive_and_releases(repo):
    with rg.review_lock("k" * 64) as first:
        assert first.held
        with rg.review_lock("k" * 64) as second:
            assert not second.held and second.holder_alive()
    with rg.review_lock("k" * 64) as again:
        assert again.held


def test_a_lock_is_released_when_its_holder_dies(repo):
    # Codex on #2319: the pid-file lock let two processes both "take over".
    # A flock is released by the kernel when the holder exits, even by kill.
    lock = rg.review_lock("k" * 64)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR);"
         "fcntl.flock(fd,fcntl.LOCK_EX);print('held',flush=True);time.sleep(60)",
         str(lock.path)], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == "held"
    with rg.review_lock("k" * 64) as got:
        assert not got.held and got.holder_alive()
    holder.kill()
    holder.wait()
    with rg.review_lock("k" * 64) as got:
        assert got.held


def test_failed_runs_counts_only_failed_records_for_the_key():
    k = "k" * 64
    comments = [
        _comment(rg.Record(k, "FAILED", 0, False, "codex")),
        _comment(rg.Record(k, "FAILED", 0, False, "codex")),
        _comment(rg.Record("x" * 64, "FAILED", 0, False, "codex")),
        _comment(rg.Record(k, "FAILED", 0, False, "codex"), association="NONE"),
    ]
    assert rg.failed_runs(comments, k) == 2
    assert rg.failed_runs(comments, k, "claude") == 0


def test_quota_failure_falls_back_then_skips_provider_across_diffs(repo, monkeypatch, capsys):
    calls, records = [], []
    def reviewer(provider, prompt, out_dir, budget):
        calls.append(provider)
        return (("You've hit your weekly limit", "exit 1") if provider == "claude"
                else ("Independent review complete.\nVERDICT: CLEAN", "exit 0"))
    monkeypatch.setattr(rg, "run_reviewer", reviewer)
    monkeypatch.setattr(rg, "post_record", lambda *args: records.append(args[1]))
    args = SimpleNamespace(base="master", budget=30, reviewer=None)
    assert rg.review_with_fallback(args, 1, "first", "claude") == 0
    assert calls == ["claude", "codex"]
    assert [rec.verdict for rec in records] == ["FAILED", "CLEAN"]
    calls.clear()
    assert rg.review_with_fallback(args, 2, "second", "claude") == 0
    assert calls == ["codex"]
    assert "skipping claude: quota cooldown" in capsys.readouterr().out


def test_findings_stop_fallback(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(rg, "_review_locked", lambda *args: calls.append(args[-1]) or 1)
    assert rg.review_with_fallback(SimpleNamespace(budget=30), 1, "k", "claude") == 1
    assert calls == ["claude"]


def test_exhaustion_of_one_provider_does_not_block_the_other(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(rg, "_review_locked", lambda *args: calls.append(args[-1]) or 0)
    args = SimpleNamespace(budget=30, failed_providers={"claude"})
    assert rg.review_with_fallback(args, 1, "k", "claude") == 0
    assert calls == ["codex"]


def test_both_unavailable_return_explicit_unreviewed(repo, monkeypatch, capsys):
    for provider in ("claude", "codex"):
        rg.remember_unavailable(provider, "rate limit", "exit 1")
    monkeypatch.setattr(rg, "_review_locked", lambda *args: pytest.fail("ignored cooldown"))
    assert rg.review_with_fallback(SimpleNamespace(budget=30), 1, "k", "claude") == 2
    assert "UNREVIEWED" in capsys.readouterr().out


def test_explicit_provider_retry_recovers_and_clears_cooldown(repo, monkeypatch):
    rg.remember_unavailable("codex", "weekly limit", "exit 1")
    calls = []
    def reviewer(provider, *args):
        calls.append(provider)
        return "VERDICT: CLEAN", "exit 0"
    monkeypatch.setattr(rg, "run_reviewer", reviewer)
    monkeypatch.setattr(rg, "post_record", lambda *args: None)
    args = SimpleNamespace(base="master", budget=30, reviewer="codex")
    assert rg.review_with_fallback(args, 1, "k", "codex") == 0
    assert calls == ["codex"] and rg.provider_cooldown("codex") is None


def test_provider_cooldown_expires(repo, monkeypatch):
    monkeypatch.setattr(rg.time, "time", lambda: 100)
    rg.remember_unavailable("claude", "weekly limit", "exit 1")
    monkeypatch.setattr(rg.time, "time", lambda: 100 + rg.PROVIDER_COOLDOWN_S)
    assert rg.provider_cooldown("claude") is None


def test_same_model_fresh_review_is_allowed(repo, monkeypatch):
    monkeypatch.setattr(rg, "_resolve", lambda args: (1, "o/r", "k", "codex/change"))
    monkeypatch.setattr(rg, "pr_comments", lambda *args: [])
    monkeypatch.setattr(rg, "review_with_fallback", lambda *args: 0)
    assert rg.cmd_review(SimpleNamespace(reviewer="codex", budget=30, fresh=False)) == 0


def test_record_requires_explicit_independence(monkeypatch):
    monkeypatch.setattr(rg, "_resolve", lambda args: (1, "o/r", "k", "codex/change"))
    with pytest.raises(SystemExit, match="--independent"):
        rg.cmd_record(SimpleNamespace(independent=False))

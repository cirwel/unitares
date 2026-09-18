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
    ("FAILED", False, "failure"),
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

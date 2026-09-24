"""Insert-time dedupe: one unresolved finding per piece of code.

Triage of 93 unresolved Watcher findings, 2026-09-24: identical code in N
worktrees produced N findings (one ``envelope_step.py`` except block was
flagged 4 times), and edits above a flagged block produced line-shifted
duplicates (5 in one worktree), because the fingerprint carries the absolute
path and the line number.

The fingerprint formula is unchanged, since the backlog is keyed by it. Instead
``persist_findings`` records a finding that repeats an unresolved one (same
pattern, same line hash, same repo-relative path) as dismissed/``dup`` with a
``duplicate_of`` pointer. The row is kept; it is never surfaced, escalated or
mirrored; and because nobody adjudicated it, it emits no resolution outcome
and does not count as a dismissal anywhere Watcher's precision is computed.
The fold only lasts while the canonical finding is unresolved: closing it
for any reason other than a verdict about the code reopens the copy.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

import agents.watcher.agent as A
import agents.watcher.findings as F
from agents.watcher._util import hash_line_content
from agents.watcher.calibration import precision_by_pattern_and_class
from src.http_api import _watcher_summary_from_rows


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    state = tmp_path / "watcher-state"
    state.mkdir()
    monkeypatch.setattr(F, "STATE_DIR", state)
    monkeypatch.setattr(F, "FINDINGS_FILE", state / "findings.jsonl")
    monkeypatch.setattr(F, "DEDUP_FILE", state / "dedup.json")
    monkeypatch.setattr(A, "FINDINGS_FILE", state / "findings.jsonl")


@pytest.fixture()
def no_outcomes(monkeypatch):
    """Record every path that could turn a finding into an outcome or event."""
    calls: list[str] = []

    def _record(name):
        def _stub(*args, **kwargs):
            calls.append(name)
        return _stub

    monkeypatch.setattr(F, "post_finding", _record("post_finding"))
    monkeypatch.setattr(A, "_post_resolution_event", _record("_post_resolution_event"))
    monkeypatch.setattr(A, "_emit_resolution_outcome", _record("_emit_resolution_outcome"))
    monkeypatch.setattr(A, "build_resolution_outcome_args", _record("build_resolution_outcome_args"))
    import agents.common.resolution_outcome as R
    monkeypatch.setattr(R, "build_resolution_outcome_args", _record("common_build_resolution_outcome_args"))
    return calls


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


SOURCE = (
    "def handle(envelope):\n"
    "    try:\n"
    "        step(envelope)\n"
    "    except Exception:\n"
    "        pass\n"
    "    return envelope\n"
)


@pytest.fixture()
def worktrees(tmp_path):
    """A real repo and one linked worktree holding the same file."""
    main = tmp_path / "repo"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "config", "user.email", "t@t.t")
    _git(main, "config", "user.name", "t")
    _git(main, "config", "commit.gpgsign", "false")
    (main / "src").mkdir()
    (main / "src" / "envelope_step.py").write_text(SOURCE)
    _git(main, "add", "src/envelope_step.py")
    _git(main, "commit", "-q", "-m", "seed")
    other = tmp_path / "wt-other"
    _git(main, "worktree", "add", "-q", "-b", "other", str(other))
    return main, other


def _detect(path: Path, line: int, pattern: str = "P006", severity: str = "medium") -> F.Finding:
    """Build a Finding the way scan_file does, from the file as it is now."""
    lines = {i: text for i, text in enumerate(path.read_text().splitlines(), start=1)}
    finding = F.Finding(
        pattern=pattern,
        file=str(path),
        line=line,
        hint="silent swallow",
        severity=severity,
        detected_at="2026-09-24T00:00:00Z",
        model_used="test",
    )
    finding.line_content_hash = hash_line_content(lines.get(line, ""))
    finding.line_content = lines.get(line, "").strip()
    finding.context_hash = A._context_hash(line, lines)
    finding.fingerprint = finding.compute_fingerprint()
    return finding


def _rows() -> list[dict]:
    return [json.loads(x) for x in F.FINDINGS_FILE.read_text().splitlines() if x.strip()]


def _unresolved() -> list[dict]:
    return [r for r in _rows() if r.get("status", "open") in ("open", "surfaced")]


# ---------------------------------------------------------------------------
# Fingerprint formula is untouched
# ---------------------------------------------------------------------------


def test_fingerprint_formula_unchanged(tmp_path):
    path = tmp_path / "x.py"
    finding = F.Finding(
        pattern="P006", file=str(path), line=5, hint="h", severity="medium",
        detected_at="t", model_used="m", line_content_hash="abc",
        context_hash="ignored", repo_relpath="also/ignored.py",
    )
    key = f"P006|{path.resolve().as_posix()}|5|abc"
    assert finding.compute_fingerprint() == hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Duplicates are recorded, not surfaced
# ---------------------------------------------------------------------------


def test_same_code_in_another_worktree_is_recorded_as_dup(worktrees, no_outcomes):
    main, other = worktrees
    first = _detect(main / "src" / "envelope_step.py", 5)
    second = _detect(other / "src" / "envelope_step.py", 5)
    assert first.fingerprint != second.fingerprint

    assert F.persist_findings([first]) == [first]
    assert F.persist_findings([second]) == []

    rows = {r["fingerprint"]: r for r in _rows()}
    assert set(rows) == {first.fingerprint, second.fingerprint}
    assert [r["fingerprint"] for r in _unresolved()] == [first.fingerprint]
    dup = rows[second.fingerprint]
    assert dup["status"] == "dismissed"
    assert dup["resolution_reason"] == "dup"
    assert dup["resolved_by"] == F.AUTO_DEDUP_RESOLVER
    assert dup["duplicate_of"] == first.fingerprint
    assert dup["file"] == str(other / "src" / "envelope_step.py")
    assert dup["line_content"] == "pass"
    assert dup["dismissed_at"]
    assert F.is_auto_duplicate(dup)
    assert no_outcomes == []


def test_four_worktrees_leave_one_unresolved(worktrees, tmp_path):
    main, _ = worktrees
    paths = [main / "src" / "envelope_step.py"]
    for name in ("wt-b", "wt-c", "wt-d"):
        wt = tmp_path / name
        _git(main, "worktree", "add", "-q", "-b", name, str(wt))
        paths.append(wt / "src" / "envelope_step.py")
    for path in paths:
        F.persist_findings([_detect(path, 5)])
    assert len(_rows()) == 4
    assert len(_unresolved()) == 1


def test_block_shifted_by_an_edit_above_is_a_dup(worktrees):
    main, _ = worktrees
    path = main / "src" / "envelope_step.py"
    first = _detect(path, 5)
    F.persist_findings([first])

    path.write_text("import os\n" * 5 + SOURCE)
    shifted = _detect(path, 10)
    assert shifted.fingerprint != first.fingerprint

    assert F.persist_findings([shifted]) == []
    [canonical] = _unresolved()
    assert canonical["fingerprint"] == first.fingerprint
    # The one unresolved finding now points at where the code is.
    assert (canonical["line"], canonical["first_line"]) == (10, 5)
    assert {r["fingerprint"]: r for r in _rows()}[shifted.fingerprint]["duplicate_of"] == first.fingerprint


def test_resolving_a_shifted_finding_does_not_reopen_its_own_copy(worktrees):
    main, _ = worktrees
    path = main / "src" / "envelope_step.py"
    first = _detect(path, 5)
    F.persist_findings([first])
    path.write_text("import os\n" * 5 + SOURCE)
    F.persist_findings([_detect(path, 10)])
    path.write_text("import os\n" * 5 + SOURCE.replace("        pass\n", "        raise\n"))
    assert F.update_finding_status(first.fingerprint, "confirmed", emit_resolution_event=False) == 0
    assert _unresolved() == []


def test_second_identical_one_liner_elsewhere_is_not_a_dup(worktrees):
    """`pass` repeats; a different surrounding block is a different site."""
    main, _ = worktrees
    path = main / "src" / "envelope_step.py"
    path.write_text(
        SOURCE
        + "def other():\n"
        "    try:\n"
        "        unrelated()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    first = _detect(path, 5)
    second = _detect(path, 11)
    assert first.line_content_hash == second.line_content_hash
    F.persist_findings([first])
    assert F.persist_findings([second]) == [second]
    assert len(_unresolved()) == 2


def test_other_block_is_not_a_dup_after_the_original_was_fixed(worktrees):
    """The line hash matches and the original line moved on, but the
    surrounding code differs: the context hash keeps them apart."""
    main, _ = worktrees
    path = main / "src" / "envelope_step.py"
    tail = (
        "def other():\n"
        "    try:\n"
        "        unrelated()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    path.write_text(SOURCE + tail)
    F.persist_findings([_detect(path, 5)])
    path.write_text(SOURCE.replace("        pass\n", "        log_it()\n") + tail)
    second = _detect(path, 11)
    assert F.persist_findings([second]) == [second]


def test_copy_pasted_block_is_not_a_dup_while_the_original_is_in_place(worktrees):
    """Identical context, but the first site has not moved: two sites."""
    main, _ = worktrees
    path = main / "src" / "envelope_step.py"
    path.write_text(SOURCE + "\n\n\n" + SOURCE + "\n\n\n")
    first = _detect(path, 5)
    second = _detect(path, 14)
    assert first.context_hash == second.context_hash
    F.persist_findings([first])
    assert F.persist_findings([second]) == [second]
    assert len(_unresolved()) == 2


def test_legacy_row_without_context_matches_only_at_the_same_line(worktrees):
    main, other = worktrees
    legacy = _detect(main / "src" / "envelope_step.py", 5)
    legacy.context_hash = ""
    F.persist_findings([legacy])

    same_line = _detect(other / "src" / "envelope_step.py", 5)
    assert F.persist_findings([same_line]) == []

    moved = other / "src" / "envelope_step.py"
    moved.write_text("import os\n" + SOURCE)
    shifted = _detect(moved, 6)
    assert F.persist_findings([shifted]) == [shifted]


def test_other_pattern_or_resolved_original_is_new(worktrees):
    main, other = worktrees
    first = _detect(main / "src" / "envelope_step.py", 5)
    F.persist_findings([first])

    other_pattern = _detect(other / "src" / "envelope_step.py", 5, pattern="P011")
    assert F.persist_findings([other_pattern]) == [other_pattern]

    assert F.update_finding_status(first.fingerprint, "confirmed", emit_resolution_event=False) == 0
    after_resolution = _detect(other / "src" / "envelope_step.py", 5)
    assert F.persist_findings([after_resolution]) == [after_resolution]


def test_dup_survives_removal_of_the_original_worktree(worktrees):
    """repo_relpath is captured at persist time, not re-derived from disk."""
    main, other = worktrees
    first = _detect(other / "src" / "envelope_step.py", 5)
    F.persist_findings([first])
    assert _rows()[0]["repo_relpath"] == "src/envelope_step.py"
    _git(main, "worktree", "remove", "--force", str(other))

    again = _detect(main / "src" / "envelope_step.py", 5)
    assert F.persist_findings([again]) == []


def test_redetection_after_dedup_ttl_adds_no_row(worktrees):
    main, other = worktrees
    F.persist_findings([_detect(main / "src" / "envelope_step.py", 5)])
    F.persist_findings([_detect(other / "src" / "envelope_step.py", 5)])
    F.DEDUP_FILE.write_text("{}")  # TTL lapsed for both
    F.persist_findings([_detect(main / "src" / "envelope_step.py", 5)])
    F.persist_findings([_detect(other / "src" / "envelope_step.py", 5)])
    assert len(_rows()) == 2
    assert len(_unresolved()) == 1


# ---------------------------------------------------------------------------
# No outcomes, no events, no precision effect
# ---------------------------------------------------------------------------


def test_high_severity_dup_is_not_mirrored_or_resolved(worktrees, no_outcomes):
    main, other = worktrees
    F.persist_findings([_detect(main / "src" / "envelope_step.py", 5, severity="high")])
    assert no_outcomes == ["post_finding"]  # the canonical finding, once
    F.persist_findings([_detect(other / "src" / "envelope_step.py", 5, severity="high")])
    assert no_outcomes == ["post_finding"]


def test_auto_dup_is_not_a_dismissal_in_checkin_confidence(worktrees):
    main, other = worktrees
    F.persist_findings([_detect(main / "src" / "envelope_step.py", 5)])
    F.persist_findings([_detect(other / "src" / "envelope_step.py", 5)])
    summary, _complexity, confidence = A._build_checkin_summary()
    assert summary == "Watcher: 1 unresolved (1 medium)"
    assert confidence == A.compute_checkin_confidence(0, 0)


def test_auto_dup_carries_no_precision_signal(worktrees):
    main, other = worktrees
    F.persist_findings([_detect(main / "src" / "envelope_step.py", 5)])
    F.persist_findings([_detect(other / "src" / "envelope_step.py", 5)])
    now = datetime.now(timezone.utc)
    assert precision_by_pattern_and_class(_rows(), now=now) == {}
    summary = _watcher_summary_from_rows(_rows(), now=now)
    assert summary["by_status"] == {"open": 1, "dismissed": 1}
    [p006] = summary["patterns"]
    assert (p006["surfaced"], p006["dismissed"], p006["other"]) == (1, 0, 0)
    assert sum(day["dismissed"] for day in summary["timeline"]) == 0
    assert sum(day["detected"] for day in summary["timeline"]) <= 1


# ---------------------------------------------------------------------------
# Scoped delivery still reaches the duplicate's worktree
# ---------------------------------------------------------------------------


def test_each_worktree_still_sees_the_finding_at_its_own_path(worktrees, capsys):
    main, other = worktrees
    first = _detect(main / "src" / "envelope_step.py", 5)
    copy = _detect(other / "src" / "envelope_step.py", 5)
    F.persist_findings([first])
    F.persist_findings([copy])

    F.print_unresolved(scope_root=other)
    out = capsys.readouterr().out
    assert str(other / "src" / "envelope_step.py") in out
    assert copy.fingerprint[:8] in out
    assert first.fingerprint[:8] not in out

    A.surface_pending(audience="codex:other", scope_root=other, check_in=False)
    out = capsys.readouterr().out
    assert str(other / "src" / "envelope_step.py") in out
    stored = {r["fingerprint"]: r for r in _rows()}
    assert "codex:other" in stored[copy.fingerprint]["surface_receipts"]
    assert stored[copy.fingerprint]["status"] == "dismissed"  # still folded
    # Delivered once per audience, like any other finding.
    A.surface_pending(audience="codex:other", scope_root=other, check_in=False)
    assert str(other / "src" / "envelope_step.py") not in capsys.readouterr().out

    A.surface_pending(audience="codex:main", scope_root=main, check_in=False)
    out = capsys.readouterr().out
    assert str(main / "src" / "envelope_step.py") in out
    assert first.fingerprint[:8] in out


# ---------------------------------------------------------------------------
# The fold only lasts while the canonical finding is open
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status, reason",
    [("confirmed", None), ("aged_out", None), ("dismissed", "stale"), ("dismissed", "unclear")],
)
def test_closing_the_canonical_releases_the_copy(worktrees, capsys, status, reason):
    main, other = worktrees
    first = _detect(main / "src" / "envelope_step.py", 5)
    copy = _detect(other / "src" / "envelope_step.py", 5)
    F.persist_findings([first])
    F.persist_findings([copy])

    assert F.update_finding_status(
        first.fingerprint, status, reason=reason, emit_resolution_event=False
    ) == 0
    capsys.readouterr()

    [reopened] = _unresolved()
    assert reopened["fingerprint"] == copy.fingerprint
    assert reopened["released_from_duplicate_of"] == first.fingerprint
    assert "duplicate_of" not in reopened and "resolved_by" not in reopened
    F.print_unresolved(scope_root=other)
    assert copy.fingerprint[:8] in capsys.readouterr().out


@pytest.mark.parametrize("reason", ["fp", "wont_fix", "out_of_scope", None])
def test_a_verdict_on_the_code_settles_the_copy(worktrees, reason):
    # None: `--dismiss <fp>` with no --reason is the documented false-positive path.
    main, other = worktrees
    first = _detect(main / "src" / "envelope_step.py", 5)
    copy = _detect(other / "src" / "envelope_step.py", 5)
    F.persist_findings([first])
    F.persist_findings([copy])
    F.update_finding_status(first.fingerprint, "dismissed", reason=reason, emit_resolution_event=False)
    assert _unresolved() == []
    assert F.is_auto_duplicate({r["fingerprint"]: r for r in _rows()}[copy.fingerprint])


def test_release_re_folds_the_remaining_copies(worktrees, tmp_path):
    main, other = worktrees
    third_wt = tmp_path / "wt-third"
    _git(main, "worktree", "add", "-q", "-b", "third", str(third_wt))
    first = _detect(main / "src" / "envelope_step.py", 5)
    second = _detect(other / "src" / "envelope_step.py", 5)
    third = _detect(third_wt / "src" / "envelope_step.py", 5)
    for f in (first, second, third):
        F.persist_findings([f])
    F.update_finding_status(first.fingerprint, "confirmed", emit_resolution_event=False)
    rows = {r["fingerprint"]: r for r in _rows()}
    assert [r["fingerprint"] for r in _unresolved()] == [second.fingerprint]
    assert rows[third.fingerprint]["duplicate_of"] == second.fingerprint


def test_redetecting_a_shifted_copy_after_a_fix_claim_reopens_it(worktrees):
    """Confirming the canonical settles its same-file copy only until the
    code is seen again: a re-detection reopens that copy, in place."""
    main, _ = worktrees
    path = main / "src" / "envelope_step.py"
    first = _detect(path, 5)
    F.persist_findings([first])
    path.write_text("import os\n" * 5 + SOURCE)
    shifted = _detect(path, 10)
    F.persist_findings([shifted])
    F.update_finding_status(first.fingerprint, "confirmed", emit_resolution_event=False)
    assert _unresolved() == []

    F.DEDUP_FILE.write_text("{}")
    again = _detect(path, 10)
    assert [f.fingerprint for f in F.persist_findings([again])] == [shifted.fingerprint]
    [reopened] = _unresolved()
    assert reopened["fingerprint"] == shifted.fingerprint
    assert reopened["released_from_duplicate_of"] == first.fingerprint
    assert [r["fingerprint"] for r in _rows()].count(shifted.fingerprint) == 1


def test_adjudicating_a_copy_leaves_the_canonical_open(worktrees):
    main, other = worktrees
    first = _detect(main / "src" / "envelope_step.py", 5)
    copy = _detect(other / "src" / "envelope_step.py", 5)
    F.persist_findings([first])
    F.persist_findings([copy])
    assert F.update_finding_status(copy.fingerprint, "confirmed", emit_resolution_event=False) == 0
    rows = {r["fingerprint"]: r for r in _rows()}
    assert rows[first.fingerprint]["status"] == "open"
    assert rows[copy.fingerprint]["status"] == "confirmed"
    assert rows[copy.fingerprint]["was_duplicate_of"] == first.fingerprint
    assert not F.is_auto_duplicate(rows[copy.fingerprint])


def test_redetection_after_ttl_stays_addressable(worktrees):
    main, other = worktrees
    first = _detect(main / "src" / "envelope_step.py", 5)
    copy = _detect(other / "src" / "envelope_step.py", 5)
    F.persist_findings([first])
    F.persist_findings([copy])
    F.update_finding_status(first.fingerprint, "dismissed", reason="fp", emit_resolution_event=False)
    F.DEDUP_FILE.write_text("{}")
    assert F.persist_findings([_detect(other / "src" / "envelope_step.py", 5)]) == []
    assert [r["fingerprint"] for r in _rows()].count(copy.fingerprint) == 1
    assert F.update_finding_status(copy.fingerprint, "dismissed", reason="fp", emit_resolution_event=False) == 0


def test_same_relative_path_in_another_repo_is_not_a_dup(worktrees, tmp_path):
    main, _ = worktrees
    repo2 = tmp_path / "unrelated"
    repo2.mkdir()
    _git(repo2, "init", "-q", "-b", "main")
    (repo2 / "src").mkdir()
    (repo2 / "src" / "envelope_step.py").write_text(SOURCE)
    first = _detect(main / "src" / "envelope_step.py", 5)
    stranger = _detect(repo2 / "src" / "envelope_step.py", 5)
    F.persist_findings([first])
    assert F.persist_findings([stranger]) == [stranger]
    assert len(_unresolved()) == 2


# ---------------------------------------------------------------------------
# Per-worktree consumers treat a live copy as that worktree's record
# ---------------------------------------------------------------------------


def _fold(worktrees):
    main, other = worktrees
    first = _detect(main / "src" / "envelope_step.py", 5)
    copy = _detect(other / "src" / "envelope_step.py", 5)
    F.persist_findings([first])
    F.persist_findings([copy])
    return first, copy


def _age_all(days_ago: str = "2026-01-01T00:00:00Z") -> None:
    rows = _rows()
    for r in rows:
        r["detected_at"] = days_ago
    F._write_findings_atomic(rows)


def test_compaction_keeps_a_copy_while_its_original_is_open(worktrees, capsys):
    first, copy = _fold(worktrees)
    _age_all()
    F.compact_findings(7)
    assert {r["fingerprint"] for r in _rows()} == {first.fingerprint, copy.fingerprint}

    F.update_finding_status(first.fingerprint, "dismissed", reason="fp", emit_resolution_event=False)
    _age_all()
    F.compact_findings(7)
    capsys.readouterr()
    assert _rows() == []


def test_ship_trailer_cites_the_worktrees_own_copy(worktrees):
    _first, copy = _fold(worktrees)
    helper = Path(__file__).resolve().parents[1] / "scripts" / "dev" / "_ship_watcher_fingerprints.py"
    proc = subprocess.run(
        ["python3", str(helper), str(F.FINDINGS_FILE)],
        input=copy.file,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == copy.fingerprint


def test_commit_naming_a_copy_resolves_only_that_copy(worktrees, monkeypatch):
    first, copy = _fold(worktrees)
    monkeypatch.setattr(A, "_post_resolution_event", lambda *a, **k: None)

    class _Log:
        returncode = 0
        stderr = ""
        stdout = f"{'ab' * 20}\x00fix: handle it\x00Watcher-Findings: {copy.fingerprint}\x1e"

    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: _Log())
    assert A.scan_commits(since="14 days ago", repo_path=worktrees[1]) == 1
    rows = {r["fingerprint"]: r for r in _rows()}
    assert rows[copy.fingerprint]["status"] == "confirmed"
    assert rows[first.fingerprint]["status"] == "open"

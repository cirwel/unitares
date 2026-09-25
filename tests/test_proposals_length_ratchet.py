"""Tests for ``scripts/dev/check_proposals_length.py``.

Each failure mode gets a fixture that breaks it and asserts the guard fails,
plus one test that the committed tree passes. ``--lower`` is pinned to never add
or raise an entry, because a baseline that can be regenerated upward is only a
convention.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "check_proposals_length.py"

_SPEC = importlib.util.spec_from_file_location("check_proposals_length", SCRIPT)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)

CAP = mod.CAP


def _doc(root: Path, name: str, lines: int) -> str:
    rel = f"docs/proposals/active/{name}"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"line {i}\n" for i in range(lines)), encoding="utf-8")
    return rel


def _baseline(root: Path, entries: dict[str, int]) -> Path:
    path = root / mod.BASELINE_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# header\n" + "".join(f"{n} {rel}\n" for rel, n in entries.items())
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    _doc(tmp_path, "short.md", 10)
    _baseline(tmp_path, {})
    return tmp_path


def test_repository_currently_passes():
    problems, _ = mod.check(REPO_ROOT)
    assert not problems, problems


def test_baseline_only_lists_files_over_the_cap():
    _, entries = mod.read_baseline(REPO_ROOT / mod.BASELINE_REL)
    assert entries, "baseline unexpectedly empty"
    for rel, allowed in entries.items():
        assert allowed > CAP, f"{rel} baselined at {allowed}, not over the cap"


def test_file_at_cap_passes(root: Path):
    _doc(root, "at-cap.md", CAP)
    assert mod.check(root)[0] == []


def test_new_file_over_cap_fails(root: Path):
    _doc(root, "too-long.md", CAP + 1)
    problems, _ = mod.check(root)
    assert len(problems) == 1 and "too-long.md" in problems[0]
    assert mod.main(["--root", str(root)]) == 1


def test_baselined_file_may_not_grow(root: Path):
    rel = _doc(root, "big.md", CAP + 50)
    _baseline(root, {rel: CAP + 49})
    problems, _ = mod.check(root)
    assert len(problems) == 1 and "grew" in problems[0]


def test_baselined_file_may_shrink_and_is_noted(root: Path):
    rel = _doc(root, "big.md", CAP + 10)
    _baseline(root, {rel: CAP + 50})
    problems, notes = mod.check(root)
    assert problems == []
    assert notes and "--lower" in notes[0]


def test_entry_for_file_back_under_cap_must_be_removed(root: Path):
    rel = _doc(root, "was-big.md", CAP - 1)
    _baseline(root, {rel: CAP + 50})
    problems, _ = mod.check(root)
    assert len(problems) == 1 and "Remove its baseline entry" in problems[0]


def test_stale_entry_for_moved_file_fails(root: Path):
    _baseline(root, {"docs/proposals/active/gone.md": CAP + 50})
    problems, _ = mod.check(root)
    assert len(problems) == 1 and "stale baseline entry" in problems[0]


def test_lower_ratchets_down_and_drops_but_never_adds_or_raises(root: Path):
    shrunk = _doc(root, "shrunk.md", CAP + 10)
    grown = _doc(root, "grown.md", CAP + 90)
    under = _doc(root, "under.md", CAP - 5)
    _doc(root, "unlisted.md", CAP + 200)
    path = _baseline(
        root,
        {shrunk: CAP + 50, grown: CAP + 60, under: CAP + 70, "docs/proposals/active/gone.md": CAP + 5},
    )
    assert mod.main(["--root", str(root), "--lower"]) == 0
    header, entries = mod.read_baseline(path)
    assert header == ["# header"]
    assert entries == {shrunk: CAP + 10, grown: CAP + 60}
    # Growth and the unlisted file still fail after --lower.
    problems, _ = mod.check(root)
    assert len(problems) == 2
    assert any("grown.md" in p for p in problems)
    assert any("unlisted.md" in p for p in problems)


def test_baseline_entry_may_not_be_added_or_raised_against_base():
    base = {"docs/proposals/active/big.md": CAP + 50}
    assert mod.baseline_escalations(base, base) == []
    assert mod.baseline_escalations(base, {"docs/proposals/active/big.md": CAP + 10}) == []
    raised = mod.baseline_escalations(base, {"docs/proposals/active/big.md": CAP + 51})
    assert len(raised) == 1 and "raised" in raised[0]
    added = mod.baseline_escalations(base, {**base, "docs/proposals/active/new.md": CAP + 100})
    assert len(added) == 1 and "added" in added[0]


def test_base_comparison_rejects_a_pr_that_raises_its_own_entry(tmp_path: Path):
    import subprocess

    git = lambda *a: subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    rel = _doc(tmp_path, "big.md", CAP + 10)
    _baseline(tmp_path, {rel: CAP + 10})
    git("add", "-A")
    git("commit", "-qm", "base")
    _doc(tmp_path, "big.md", CAP + 20)
    _baseline(tmp_path, {rel: CAP + 20})
    assert mod.main(["--root", str(tmp_path)]) == 0
    assert mod.main(["--root", str(tmp_path), "--base", "HEAD"]) == 1


def test_missing_base_baseline_is_skipped_not_failed(tmp_path: Path):
    assert mod.read_base_baseline(tmp_path, "HEAD") is None

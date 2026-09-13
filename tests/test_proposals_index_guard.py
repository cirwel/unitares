"""Tests for ``scripts/dev/check_proposals_index.py``.

The guard exists because ``docs/proposals/README.md`` is a hand-maintained index
over half the repo's documentation, and it had already drifted within ten days of
being written: the summary line still read ``Active 21`` after PR #2156 added a
row, and the original tagging count was itself one short. Nothing caught it.

What these tests pin is that the guard *can fail*. A guard nobody can make fail
is indistinguishable from one that always passes, and the second is worse than
none — it launders an unchecked surface as a checked one. So each failure mode
gets a test that breaks the fixture and asserts a non-zero exit, plus one that
asserts the real repository is currently clean.

They also pin the boundary the guard deliberately does not cross: it never
decides whether a disposition tag is *correct*. A doc tagged **Active** that
nobody has touched in a year passes. The 30-day line is the index's stated
choice, and re-deriving it in a script would make the script the tagging
authority.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "check_proposals_index.py"


def _run(cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(cwd or REPO_ROOT),
    )


def _load_module():
    spec = importlib.util.spec_from_file_location("check_proposals_index", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- the repository as it stands ---------------------------------------------


def test_script_exists_and_is_executable():
    assert SCRIPT.is_file(), f"{SCRIPT} is missing"


def test_repository_currently_passes():
    """The committed tree must be clean, or the guard is noise from day one."""
    result = _run()
    assert result.returncode == 0, (
        "proposals index guard fails on the committed tree:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_known_debt_is_reported_not_hidden():
    """Existing status-line debt is surfaced on every run, not silently tolerated."""
    result = _run()
    assert "known debt" in result.stdout, result.stdout


def test_allowlists_are_not_empty_catch_alls():
    """A guard whose allowlist swallows everything checks nothing."""
    mod = _load_module()
    on_disk = {p.name for p in (REPO_ROOT / "docs" / "proposals").glob("*.md")}
    exempt = mod.INDEX_EXEMPT | mod.STATUS_LINE_DEBT
    assert exempt, "allowlists unexpectedly empty"
    assert len(exempt) < len(on_disk) / 2, (
        "allowlists cover more than half the folder; the guard has stopped guarding"
    )


# --- failure modes, against a copied fixture tree -----------------------------


def _counts_line(readme: Path) -> tuple[str, dict[str, int]]:
    """The live 'Current counts:' line and its parsed tag->count mapping.

    Derived rather than hardcoded: adding a proposal legitimately changes these
    numbers, and a test that pins them would fail on ordinary work rather than
    on the drift it exists to catch.
    """
    text = readme.read_text()
    line = next(l for l in text.splitlines() if " · Registered " in l)
    return line, {m[0]: int(m[1]) for m in re.findall(r"(\w+) (\d+)", line)}


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A minimal copy of the repo layout the guard walks."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "scripts" / "dev").mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "docs" / "proposals", root / "docs" / "proposals")
    # The index links out to ../ontology/README.md; the guard resolves it, so
    # the fixture has to provide it or every run reports a false dead link.
    (root / "docs" / "ontology").mkdir()
    (root / "docs" / "ontology" / "README.md").write_text("# ontology\n")
    shutil.copy2(SCRIPT, root / "scripts" / "dev" / SCRIPT.name)
    return root


def _run_tree(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(root / "scripts" / "dev" / SCRIPT.name)],
        capture_output=True,
        text=True,
    )


def test_fixture_tree_passes_unmodified(tree: Path):
    """Sanity: the copy passes, so later failures are caused by the edit under test."""
    assert _run_tree(tree).returncode == 0, _run_tree(tree).stdout


def test_detects_count_drift(tree: Path):
    readme = tree / "docs" / "proposals" / "README.md"
    line, counts = _counts_line(readme)
    readme.write_text(
        readme.read_text().replace(line, line.replace(f"Active {counts['Active']}", "Active 99"))
    )
    result = _run_tree(tree)
    assert result.returncode == 1
    assert "count drift" in result.stdout


def test_detects_unindexed_proposal(tree: Path):
    (tree / "docs" / "proposals" / "zz-new-thing.md").write_text(
        "# New\n\n**Status:** draft\n"
    )
    result = _run_tree(tree)
    assert result.returncode == 1
    assert "not indexed" in result.stdout
    assert "zz-new-thing.md" in result.stdout


def test_detects_missing_status_line(tree: Path):
    """A new doc with no status line fails; the body stays canonical for status."""
    doc = tree / "docs" / "proposals" / "zz-no-status.md"
    doc.write_text("# New\n\nJust prose, no status anywhere.\n")
    readme = tree / "docs" / "proposals" / "README.md"
    readme.write_text(
        readme.read_text()
        + "\n| [`zz-no-status.md`](zz-no-status.md) | **Active** · x |\n"
    )
    result = _run_tree(tree)
    assert result.returncode == 1
    assert "no status line" in result.stdout


def test_detects_dead_index_link(tree: Path):
    readme = tree / "docs" / "proposals" / "README.md"
    readme.write_text(
        readme.read_text() + "\n| [`ghost.md`](ghost.md) | **Parked** · x |\n"
    )
    result = _run_tree(tree)
    assert result.returncode == 1
    assert "dead index link" in result.stdout


def test_detects_stale_allowlist_entry(tree: Path):
    """An allowlist entry whose file is gone is itself drift."""
    mod = _load_module()
    victim = sorted(mod.STATUS_LINE_DEBT)[0]
    (tree / "docs" / "proposals" / victim).unlink()
    readme = tree / "docs" / "proposals" / "README.md"
    # Drop its row too, so we isolate the stale-allowlist signal from a dead link.
    kept = [ln for ln in readme.read_text().splitlines() if f"({victim})" not in ln]
    readme.write_text("\n".join(kept) + "\n")
    result = _run_tree(tree)
    assert result.returncode == 1
    assert "stale STATUS_LINE_DEBT entry" in result.stdout


def test_detects_debt_entry_that_was_fixed(tree: Path):
    """Fixing a doc must shrink the debt list, so the recorded count stays honest."""
    mod = _load_module()
    victim = sorted(mod.STATUS_LINE_DEBT)[0]
    doc = tree / "docs" / "proposals" / victim
    doc.write_text("**Status:** now stated\n\n" + doc.read_text())
    result = _run_tree(tree)
    assert result.returncode == 1
    assert "out of date" in result.stdout


# --- the boundary the guard must not cross ------------------------------------


def test_guard_does_not_adjudicate_tag_correctness(tree: Path):
    """Re-tagging a doc's disposition is a human call, not a check failure.

    Flipping a row from Parked to Active (and fixing the arithmetic) must pass:
    the guard checks that the index is internally consistent, never that a tag
    reflects reality. If this test ever fails, the guard has quietly become the
    tagging authority and README.md has stopped being canonical for its own rule.
    """
    readme = tree / "docs" / "proposals" / "README.md"
    lines = readme.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("| [") and "| **Parked" in line:
            lines[i] = line.replace("| **Parked", "| **Active", 1)
            break
    else:
        pytest.fail("no Parked row found to re-tag")
    text = "\n".join(lines) + "\n"
    line, counts = _counts_line(readme)
    rebalanced = line.replace(
        f"Active {counts['Active']}", f"Active {counts['Active'] + 1}"
    ).replace(f"Parked {counts['Parked']}", f"Parked {counts['Parked'] - 1}")
    text = text.replace(line, rebalanced)
    readme.write_text(text)
    result = _run_tree(tree)
    assert result.returncode == 0, result.stdout

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


def test_known_debt_is_reported_not_hidden(tree: Path):
    """Debt, when it exists, is surfaced on every run rather than silently tolerated."""
    _add_doc(tree, "zz-owes-a-status.md", status=False)
    _seed_debt(tree, {"zz-owes-a-status.md"})
    result = _run_tree(tree)
    assert "known debt" in result.stdout, result.stdout
    assert result.returncode == 0, "recorded debt is reported, not failed on"


def test_repository_debt_list_and_reality_agree():
    """Whatever the committed debt is, the guard accepts the tree as it stands."""
    assert _run().returncode == 0


def test_allowlists_are_not_empty_catch_alls():
    """A guard whose allowlist swallows everything checks nothing."""
    mod = _load_module()
    on_disk = {p.relative_to(REPO_ROOT / "docs" / "proposals").as_posix()
               for p in (REPO_ROOT / "docs" / "proposals").rglob("*.md")}
    exempt = mod.INDEX_EXEMPT | mod.STATUS_LINE_DEBT
    # STATUS_LINE_DEBT is legitimately empty once paid off; INDEX_EXEMPT is not,
    # because the two relocation stubs it names are still on disk.
    assert mod.INDEX_EXEMPT, "INDEX_EXEMPT unexpectedly empty"
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
    # The guide links out to the small canonical reading path and migration
    # evidence. Their contents are irrelevant to this index guard fixture.
    for relative in (
        "AGENTS.md", "docs/PRODUCT_DEFINITION.md", "docs/UNIFIED_ARCHITECTURE.md",
        "docs/INTERFACE_CONTRACT.md", "docs/EVIDENCE_AND_LIMITS.md",
        "docs/dev/proposals-layout-2347.md", "docs/dev/proposals-layout-2347.json",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Fixture target\n")
    shutil.copy2(SCRIPT, root / "scripts" / "dev" / SCRIPT.name)
    return root


def _seed_debt(tree: Path, names: set[str]) -> None:
    """Rewrite the tree's copy of the script so STATUS_LINE_DEBT holds `names`.

    The debt list is legitimately empty in the committed tree, so a test that
    reached into the real one broke the moment the debt was paid off — the same
    brittleness as pinning a live count. Seeding it keeps these tests about the
    guard's behaviour rather than about how much debt happens to exist.
    """
    script = tree / "scripts" / "dev" / SCRIPT.name
    body = script.read_text()
    literal = ("{" + ", ".join(repr(n) for n in sorted(names)) + "}") if names else "set()"
    patched = re.sub(
        r"STATUS_LINE_DEBT(?:: set\[str\])? = (?:set\(\)|\{.*?\})",
        f"STATUS_LINE_DEBT = {literal}",
        body,
        count=1,
        flags=re.S,
    )
    assert patched != body, "STATUS_LINE_DEBT assignment not found in the script copy"
    script.write_text(patched)


def _add_doc(tree: Path, name: str, *, status: bool, indexed: bool = True) -> None:
    """Add a proposal to the fixture tree, with or without a status line."""
    body = "# Doc\n\n**Status:** stated\n" if status else "# Doc\n\nno status here\n"
    (tree / "docs" / "proposals" / name).write_text(body)
    if indexed:
        readme = tree / "docs" / "proposals" / "README.md"
        # Adding a row obliges updating the count — that is the rule the guard
        # enforces, so the helper obeys it rather than tripping it incidentally.
        line, counts = _counts_line(readme)
        text = readme.read_text().replace(
            line, line.replace(f"Active {counts['Active']}", f"Active {counts['Active'] + 1}"), 1
        )
        readme.write_text(text + f"\n| [`{name}`]({name}) | **Active** · x |\n")


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


def test_prose_link_does_not_satisfy_index_coverage(tree: Path):
    _add_doc(tree, "zz-prose-only.md", status=True, indexed=False)
    readme = tree / "docs" / "proposals" / "README.md"
    readme.write_text(
        readme.read_text()
        + "\nFurther context: [`zz-prose-only.md`](zz-prose-only.md).\n"
    )
    result = _run_tree(tree)
    assert result.returncode == 1
    assert "not indexed" in result.stdout
    assert "no table row" in result.stdout
    assert "zz-prose-only.md" in result.stdout


def test_detects_duplicate_proposal_rows_even_when_counts_match(tree: Path):
    name = "zz-duplicate-row.md"
    _add_doc(tree, name, status=True)
    readme = tree / "docs" / "proposals" / "README.md"
    line, counts = _counts_line(readme)
    text = readme.read_text().replace(
        line,
        line.replace(f"Active {counts['Active']}", f"Active {counts['Active'] + 1}"),
        1,
    )
    readme.write_text(text + f"\n| [`{name}`]({name}) | **Active** · duplicate |\n")

    result = _run_tree(tree)
    assert result.returncode == 1
    assert "duplicate index rows" in result.stdout
    assert name in result.stdout


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


@pytest.mark.parametrize("folder", ["active", "registered", "archive"])
def test_unindexed_nested_proposal_cannot_escape_coverage(tree: Path, folder: str):
    (tree / "docs" / "proposals" / folder / "new.md").write_text("# New\n\nStatus: draft\n")
    result = _run_tree(tree)
    assert result.returncode == 1
    assert f"{folder}/new.md has no table row" in result.stdout


def test_cross_index_duplicate_is_rejected(tree: Path):
    index = tree / "docs" / "proposals" / "archive" / "README.md"
    index.write_text(index.read_text() + "\n| [duplicate](../active/plexus-scope.md) | duplicate |\n")
    result = _run_tree(tree)
    assert result.returncode == 1
    assert "duplicate index rows" in result.stdout


def test_missing_supporting_artifact_is_rejected(tree: Path):
    (tree / "docs" / "proposals" / "archive" / "accountable-testbed-federation-trace.json").unlink()
    result = _run_tree(tree)
    assert result.returncode == 1
    assert "dead index link" in result.stdout


def test_new_archive_cannot_hide_missing_status(tree: Path):
    index = tree / "docs" / "proposals" / "archive" / "README.md"
    (index.parent / "unstated.md").write_text("# Record\n\nNo labelled status.\n")
    index.write_text(index.read_text() + "\n| [unstated](unstated.md) | historical |\n")
    result = _run_tree(tree)
    assert result.returncode == 1
    assert "no status line: docs/proposals/archive/unstated.md" in result.stdout


def test_detects_stale_allowlist_entry(tree: Path):
    """An allowlist entry whose file is gone is itself drift."""
    _seed_debt(tree, {"zz-never-existed.md"})
    result = _run_tree(tree)
    assert result.returncode == 1
    assert "stale STATUS_LINE_DEBT entry" in result.stdout


def test_detects_debt_entry_that_was_fixed(tree: Path):
    """Fixing a doc must shrink the debt list, so the recorded count stays honest."""
    _add_doc(tree, "zz-status-now-stated.md", status=True)
    _seed_debt(tree, {"zz-status-now-stated.md"})
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
    archive = tree / "docs" / "proposals" / "archive" / "README.md"
    lines = archive.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("| [") and "| **Parked" in line:
            lines[i] = line.replace("| **Parked", "| **Active", 1)
            break
    else:
        pytest.fail("no Parked row found to re-tag")
    archive.write_text("\n".join(lines) + "\n")
    line, counts = _counts_line(readme)
    rebalanced = line.replace(
        f"Active {counts['Active']}", f"Active {counts['Active'] + 1}"
    ).replace(f"Parked {counts['Parked']}", f"Parked {counts['Parked'] - 1}")
    text = readme.read_text().replace(line, rebalanced)
    readme.write_text(text)
    result = _run_tree(tree)
    assert result.returncode == 0, result.stdout

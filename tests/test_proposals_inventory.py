"""The migration inventory must identify its snapshot and disclose ambiguity."""

import hashlib
import subprocess
from pathlib import Path

import pytest

from scripts.dev.proposals_inventory import inventory, status_block


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def write(root, path, content):
    dest = root / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(content)


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Inventory test")
    git(tmp_path, "config", "user.email", "inventory@example.invalid")
    write(tmp_path, "docs/proposals/README.md", "# Proposals\n")
    write(tmp_path, "docs/proposals/active/README.md",
          "| [current](current.md) | **Built** · current contract |\n")
    write(tmp_path, "docs/proposals/active/current.md", "# Current\n\nStatus: implemented\n")
    write(tmp_path, "src/use.py", "# docs/proposals/active/current.md\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "Initial contract")
    return tmp_path


def test_inventory_reads_the_named_commit_not_dirty_or_untracked_files(repo):
    revision = git(repo, "rev-parse", "HEAD")
    original = (repo / "docs/proposals/active/current.md").read_bytes()
    write(repo, "docs/proposals/active/current.md", "# Dirty body\n")
    write(repo, "docs/proposals/untracked.md", "Status: draft\n")
    result = inventory(repo, revision)
    rows = {r["path"]: r for r in result["documents"]}
    row = rows["docs/proposals/active/current.md"]
    assert result["source_commit"] == revision
    assert "docs/proposals/untracked.md" not in rows
    assert row["sha256"] == hashlib.sha256(original).hexdigest()
    assert row["declared_status"] == "Status: implemented"
    assert row["owning_index"] == "docs/proposals/active/README.md"
    assert row["disposition"] == "Built"
    assert "source" in row["referenced_by"]


def test_duplicate_basenames_are_conservative_candidates_not_false_certainty(repo):
    write(repo, "docs/proposals/archive/current.md", "Status: historical\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Add historical name collision")
    rows = {r["path"]: r for r in inventory(repo)["documents"]}
    for path in ("docs/proposals/active/current.md", "docs/proposals/archive/current.md"):
        assert any(r["path"] == "src/use.py" for r in rows[path]["inbound_reference_candidates"])


@pytest.mark.parametrize("directory", ["active", "registered", "archive", "archive/older"])
def test_qualified_child_index_references_are_inventoried(repo, directory):
    index = f"docs/proposals/{directory}/README.md"
    write(repo, index, "# Reading path\n")
    write(repo, "docs/guide.md", f"See `{index}`.\n")
    write(repo, "docs/unrelated.md", "See README.md.\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Add qualified child-index reference")

    rows = {r["path"]: r for r in inventory(repo)["documents"]}
    refs = rows[index]["inbound_reference_candidates"]
    assert {r["path"] for r in refs} == {"docs/guide.md"}


def test_content_date_ignores_whitespace_only_path_change(repo):
    initial = git(repo, "rev-parse", "HEAD")
    write(repo, "docs/proposals/active/current.md", "# Current\n\n\nStatus:   implemented\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Whitespace only")
    row = next(r for r in inventory(repo)["documents"] if r["path"].endswith("current.md"))
    assert row["last_path_change"]["commit"] != initial
    assert row["last_meaningful_update"]["commit"] == initial


def test_binary_evidence_is_inventoried_without_decoding(repo):
    (repo / "docs/proposals/trace.bin").write_bytes(b"\x00\xff")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "Add evidence")
    row = next(r for r in inventory(repo)["documents"] if r["path"].endswith("trace.bin"))
    assert row["sha256"] == hashlib.sha256(b"\x00\xff").hexdigest()
    assert row["declared_status"] is None


def test_status_preserves_continuation_without_treating_prose_as_status():
    assert status_block("# Draft\n\n**Created:** today · **Status:** proposed;\nnot enabled.\n\nBody") == (
        "**Created:** today · **Status:** proposed;\nnot enabled."
    )
    assert status_block("# Note\n\nInference status: unknown\n") is None

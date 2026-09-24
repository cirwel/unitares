"""docs/changelog.d/ fragments and the release-cut assembler.

The assembler is run as the release author runs it, as a subprocess against a
throwaway checkout (`--root`), so the tests see exactly what it writes and
deletes on disk.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSEMBLE = REPO_ROOT / "scripts/dev/changelog_assemble.py"

CHANGELOG = """# Changelog

Intro.

---

## [Unreleased]

### Added
- **old-added:** already on master (#1).

### Changed
- **old-changed:** already on master (#2).

### Fixed
- **old-fixed:** already on master (#3).

### Removed
- **old-removed:** already on master (#4).

### Documentation
- **old-docs:** already on master (#5).

## [1.0.0] - 2026-01-01

### Fixed

- **released:** never touched (#0).
"""

README = "# Changelog fragments\n\n- this bullet is documentation, not an entry\n"


def _checkout(tmp_path: Path, fragments: dict[str, str], changelog: str = CHANGELOG) -> Path:
    (tmp_path / "docs" / "changelog.d").mkdir(parents=True)
    (tmp_path / "docs" / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    (tmp_path / "docs" / "changelog.d" / "README.md").write_text(README, encoding="utf-8")
    for name, body in fragments.items():
        (tmp_path / "docs" / "changelog.d" / name).write_text(body, encoding="utf-8")
    return tmp_path


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ASSEMBLE), "--root", str(root), *args],
        capture_output=True, text=True,
    )


def _changelog(root: Path) -> str:
    return (root / "docs" / "CHANGELOG.md").read_text(encoding="utf-8")


def _unreleased(text: str) -> str:
    start = text.index("## [Unreleased]")
    return text[start:text.index("## [1.0.0]")]


def _remaining(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "docs" / "changelog.d").iterdir())


# --- validation ------------------------------------------------------------


def test_check_accepts_well_formed_fragments(tmp_path: Path):
    root = _checkout(tmp_path, {
        "added-new-thing.md": "- **new:** a thing (#10).\n",
        "fixed-2410-race.md": "- **race:** fixed (#11).\n  continued line\n",
    })
    result = _run(root, "--check")
    assert result.returncode == 0, result.stderr
    assert "2 fragment(s), all valid" in result.stdout


@pytest.mark.parametrize("name, body, complaint", [
    ("misc-thing.md", "- **x:** y\n", "unknown section 'misc'"),
    ("Added-thing.md", "- **x:** y\n", "name must be"),
    ("added_thing.md", "- **x:** y\n", "name must be"),
    ("added-.md", "- **x:** y\n", "name must be"),
    ("added-thing.txt", "- **x:** y\n", "name must be"),
    ("added-thing.md", "\n\n", "empty fragment"),
    ("added-thing.md", "**x:** not a bullet\n", "markdown bullet"),
    ("added-thing.md", "- **x:** y\n\n### Fixed\n- **z:** w\n", "may not contain a heading"),
])
def test_check_rejects_a_malformed_fragment(tmp_path: Path, name: str, body: str, complaint: str):
    root = _checkout(tmp_path, {name: body})
    result = _run(root, "--check")
    assert result.returncode == 1
    assert complaint in result.stderr


@pytest.mark.parametrize("nested", ["added/new-thing.md", "added/README.md"])
def test_a_fragment_in_a_subdirectory_is_rejected_not_ignored(tmp_path: Path, nested: str):
    """A shallow scan would report zero fragments, and the entry would never ship."""
    root = _checkout(tmp_path, {})
    target = root / "docs" / "changelog.d" / nested
    target.parent.mkdir()
    target.write_text("- **nested:** entry (#10).\n", encoding="utf-8")
    result = _run(root, "--check")
    assert result.returncode == 1
    assert "not in a subdirectory" in result.stderr
    assert _run(root).returncode == 1
    assert target.exists()


def test_an_invalid_fragment_blocks_assembly_and_changes_nothing(tmp_path: Path):
    root = _checkout(tmp_path, {
        "added-good.md": "- **good:** fine (#10).\n",
        "other-bad.md": "- **bad:** wrong section (#11).\n",
    })
    result = _run(root)
    assert result.returncode == 1
    assert _changelog(root) == CHANGELOG
    assert _remaining(root) == ["README.md", "added-good.md", "other-bad.md"]


def test_the_readme_is_not_a_fragment(tmp_path: Path):
    root = _checkout(tmp_path, {})
    assert _run(root, "--check").returncode == 0
    result = _run(root)
    assert result.returncode == 0
    assert "no fragments" in result.stdout
    assert _changelog(root) == CHANGELOG
    assert _remaining(root) == ["README.md"]


# --- assembly --------------------------------------------------------------


def test_fragments_join_the_top_of_their_existing_subsections(tmp_path: Path):
    root = _checkout(tmp_path, {
        "added-zeta.md": "- **zeta:** z (#12).\n",
        "added-alpha.md": "- **alpha:** a (#11).\n",
        "fixed-bug.md": "- **bug:** squashed (#13).\n",
    })
    assert _run(root).returncode == 0
    section = _unreleased(_changelog(root))
    assert ("### Added\n- **alpha:** a (#11).\n- **zeta:** z (#12).\n"
            "- **old-added:**") in section
    assert "### Fixed\n- **bug:** squashed (#13).\n- **old-fixed:**" in section
    # Released entries are not touched.
    assert _changelog(root).endswith("## [1.0.0] - 2026-01-01\n\n### Fixed\n\n"
                                     "- **released:** never touched (#0).\n")


def test_an_absent_subsection_is_created_in_section_order(tmp_path: Path):
    root = _checkout(tmp_path, {
        "security-hole.md": "- **hole:** closed (#20).\n",
        "breaking-api.md": "- **api:** renamed (#21).\n",
        "validation-run.md": "- **run:** exercised (#22).\n",
        "deprecated-old.md": "- **old:** on its way out (#23).\n",
    })
    assert _run(root).returncode == 0
    section = _unreleased(_changelog(root))
    headings = [line for line in section.splitlines() if line.startswith("### ")]
    assert headings == [
        "### Breaking", "### Added", "### Changed", "### Deprecated", "### Fixed",
        "### Removed", "### Security", "### Documentation", "### Validation",
    ]
    assert "### Security\n- **hole:** closed (#20).\n\n### Documentation" in section
    # The last subsection keeps the section's separation from the release below.
    assert section.endswith("### Validation\n- **run:** exercised (#22).\n\n")


def test_assembly_is_deterministic_whatever_the_creation_order(tmp_path: Path):
    fragments = {
        "changed-b.md": "- **b:** two (#2).\n",
        "changed-a.md": "- **a:** one (#1).\n",
        "tests-c.md": "- **c:** three (#3).\n",
    }
    first = _checkout(tmp_path / "one", fragments)
    second = _checkout(tmp_path / "two", dict(reversed(list(fragments.items()))))
    assert _run(first).returncode == 0
    assert _run(second).returncode == 0
    assert _changelog(first) == _changelog(second)
    assert "### Changed\n- **a:** one (#1).\n- **b:** two (#2).\n- **old-changed:**" \
        in _changelog(first)


def test_assembly_deletes_the_fragments_and_leaves_the_readme(tmp_path: Path):
    root = _checkout(tmp_path, {
        "added-one.md": "- **one:** (#1).\n",
        "fixed-two.md": "- **two:** (#2).\n",
    })
    result = _run(root)
    assert result.returncode == 0
    assert "folded 2 fragment(s)" in result.stdout
    assert _remaining(root) == ["README.md"]
    assert (root / "docs" / "changelog.d" / "README.md").read_text(encoding="utf-8") == README
    assert "documentation, not an entry" not in _changelog(root)


def test_a_multi_line_fragment_is_folded_verbatim(tmp_path: Path):
    body = "- **one:** first line\n  continuation (#1).\n- **two:** second bullet (#2).\n"
    root = _checkout(tmp_path, {"removed-things.md": body})
    assert _run(root).returncode == 0
    assert f"### Removed\n{body}- **old-removed:**" in _changelog(root)


def test_preview_prints_the_fold_and_changes_nothing(tmp_path: Path):
    root = _checkout(tmp_path, {"added-one.md": "- **one:** (#1).\n"})
    result = _run(root, "--preview")
    assert result.returncode == 0
    assert "### Added\n<!-- docs/changelog.d/added-one.md -->\n- **one:** (#1).\n" in result.stdout
    assert _changelog(root) == CHANGELOG
    assert _remaining(root) == ["README.md", "added-one.md"]


def test_no_unreleased_section_fails_and_keeps_the_fragments(tmp_path: Path):
    root = _checkout(tmp_path, {"added-one.md": "- **one:** (#1).\n"},
                     changelog="# Changelog\n\n## [1.0.0] - 2026-01-01\n")
    result = _run(root)
    assert result.returncode == 1
    assert "no `## [Unreleased]` section" in result.stderr
    assert _remaining(root) == ["README.md", "added-one.md"]


def test_an_empty_unreleased_section_gains_its_subsections(tmp_path: Path):
    changelog = "# Changelog\n\n## [Unreleased]\n\n---\n\n## [1.0.0] - 2026-01-01\n"
    root = _checkout(tmp_path, {"fixed-one.md": "- **one:** (#1).\n",
                                "added-two.md": "- **two:** (#2).\n"},
                     changelog=changelog)
    assert _run(root).returncode == 0
    assert _changelog(root) == (
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- **two:** (#2).\n\n"
        "### Fixed\n- **one:** (#1).\n\n---\n\n## [1.0.0] - 2026-01-01\n"
    )


def test_the_repository_fragments_are_valid():
    """The live directory must pass the check the Release Seams job runs."""
    result = subprocess.run([sys.executable, str(ASSEMBLE), "--check"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_output_order_does_not_depend_on_the_order_fragments_are_given():
    """Directory order is filesystem-dependent; the fold must not inherit it."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("changelog_assemble", ASSEMBLE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    fragments = [
        module.Fragment(Path(f"added-{name}.md"), "Added", f"- **{name}:** (#1).")
        for name in ("alpha", "beta", "gamma")
    ]
    forward = module.assemble_text(CHANGELOG, fragments)
    backward = module.assemble_text(CHANGELOG, list(reversed(fragments)))
    assert forward == backward
    assert "### Added\n- **alpha:** (#1).\n- **beta:** (#1).\n- **gamma:** (#1).\n" in forward

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))

from compat_component_rows import (  # noqa: E402
    PLUGIN_ROW_PREFIX,
    SDK_ROW_PREFIX,
    main,
    newest_sdk_version,
    recut_plugin_version,
    regenerate,
)

REPO = Path(__file__).resolve().parents[1]
COMPAT = (
    "| Artifact | Current version | Role |\n"
    "|---|---:|---|\n"
    "| Published server/container | `v9.1.0` | Verified release. |\n"
    "| `unitares-governance` plugin | `v0.1.0` | Carries the bundle for server `v9.0.0`. |\n"
    "| `unitares-sdk` | `0.4.0` | Old prose. Install it with `pip install unitares-sdk==0.4.0`. |\n"
)
SITE = (
    "[server v9.1.0](x) · [Python SDK 0.4.0](https://pypi.org/project/unitares-sdk/0.4.0/)\n"
    "python -m pip install unitares-sdk==0.4.0\n"
)
CHANGELOG = (
    "## [Unreleased]\n\n"
    "## [9.1.0] - 2026-01-02\n### Added\n- a thing\n<!-- plugin-bundle-recut: v0.2.0 -->\n\n"
    "## [9.0.0] - 2026-01-01\n<!-- plugin-bundle-recut: v0.1.0 -->\n"
)


def _tree(tmp_path: Path, changelog: str = CHANGELOG) -> Path:
    (tmp_path / "docs" / "public-site").mkdir(parents=True)
    (tmp_path / "docs" / "COMPATIBILITY.md").write_text(COMPAT)
    (tmp_path / "docs" / "public-site" / "index.md").write_text(SITE)
    (tmp_path / "docs" / "CHANGELOG.md").write_text(changelog)
    return tmp_path


def _run(root: Path, *, tags=("sdk-v0.4.0", "sdk-v0.10.0", "sdk-v0.9.1"), exists=lambda url: True):
    return regenerate(root, "9.1.0", "v9.1.0", sdk_tags=lambda tag: list(tags), exists=exists)


def test_sdk_is_the_newest_tag_by_version_not_by_string():
    assert newest_sdk_version(["sdk-v0.9.1", "sdk-v0.10.0", "sdk-v0.4.0", "other"]) == "0.10.0"
    with pytest.raises(ValueError):
        newest_sdk_version(["v3.5.0"])


def test_recut_is_read_from_the_release_entry_only():
    assert recut_plugin_version(CHANGELOG, "9.1.0") == "v0.2.0"
    assert recut_plugin_version(CHANGELOG, "9.0.0") == "v0.1.0"
    assert recut_plugin_version(CHANGELOG.replace("<!-- plugin-bundle-recut: v0.2.0 -->", ""), "9.1.0") is None
    with pytest.raises(ValueError):
        recut_plugin_version(CHANGELOG, "9.2.0")
    two = CHANGELOG.replace("- a thing", "- a thing\n<!-- plugin-bundle-recut: v0.3.0 -->")
    with pytest.raises(ValueError):
        recut_plugin_version(two, "9.1.0")


def test_rows_and_every_sdk_pin_move_together(tmp_path: Path):
    root = _tree(tmp_path)
    _run(root)
    compat = (root / "docs" / "COMPATIBILITY.md").read_text()
    site = (root / "docs" / "public-site" / "index.md").read_text()
    sdk_row = next(line for line in compat.split("\n") if line.startswith(SDK_ROW_PREFIX))
    plugin_row = next(line for line in compat.split("\n") if line.startswith(PLUGIN_ROW_PREFIX))
    assert "`0.10.0`" in sdk_row and "unitares-sdk==0.10.0" in sdk_row and "Old prose" not in sdk_row
    assert "`v0.2.0`" in plugin_row and "server `v9.1.0`" in plugin_row
    assert "releases/tag/v0.2.0" in plugin_row
    assert "0.4.0" not in compat and "0.4.0" not in site
    assert site.count("0.10.0") == 3
    # The server row is compat_published_row.py's, and stays as it was.
    assert "| Published server/container | `v9.1.0` | Verified release. |" in compat


def test_no_recut_leaves_the_plugin_row(tmp_path: Path):
    root = _tree(tmp_path, CHANGELOG.replace("<!-- plugin-bundle-recut: v0.2.0 -->", ""))
    done = _run(root)
    assert "| `unitares-governance` plugin | `v0.1.0` | Carries the bundle for server `v9.0.0`. |" in (
        root / "docs" / "COMPATIBILITY.md"
    ).read_text()
    assert any("plugin row unchanged" in line for line in done)


@pytest.mark.parametrize("missing", ["pypi.org", "unitares-governance-plugin"])
def test_refuses_what_is_not_published(tmp_path: Path, missing: str):
    root = _tree(tmp_path)
    with pytest.raises(ValueError):
        _run(root, exists=lambda url: missing not in url)


def test_refuses_a_tag_that_does_not_name_the_version(tmp_path: Path):
    with pytest.raises(ValueError):
        regenerate(_tree(tmp_path), "9.1.0", "v9.0.0", sdk_tags=lambda t: ["sdk-v0.4.0"], exists=lambda u: True)


def test_cli_fails_closed(tmp_path: Path):
    assert main(["--version", "9.1.0", "--release-tag", "v9.0.0", "--root", str(_tree(tmp_path))]) == 1


def test_promote_release_pin_job_runs_it():
    workflow = (REPO / ".github" / "workflows" / "promote-release.yml").read_text()
    assert re.search(r"compat_component_rows\.py --version \"\$VERSION\" --release-tag \"\$RELEASE_TAG\"", workflow)


def test_compatibility_policy_has_an_entry_for_this_version():
    # The policy bullet is prose and is not generated; a release PR that bumps
    # VERSION must add one, or the release ships without a compatibility note.
    version = (REPO / "VERSION").read_text().strip()
    text = (REPO / "docs" / "COMPATIBILITY.md").read_text()
    policy = text.split("## Compatibility policy", 1)[1]
    assert re.search(rf"^- v{re.escape(version)} ", policy, re.M), (
        f"docs/COMPATIBILITY.md has no '- v{version} ' bullet under Compatibility policy"
    )

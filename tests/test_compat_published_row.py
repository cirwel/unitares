from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ops"))

from compat_published_row import ROW_PREFIX, main, render_row, rewrite  # noqa: E402

RUN = "https://github.com/cirwel/unitares/actions/runs/123"
REL = "https://github.com/cirwel/unitares/releases/tag/v9.1.0"
TABLE = (
    "| Artifact | Current version | Role |\n"
    "|---|---:|---|\n"
    "| UNITARES server | `v9.1.0` | Source version string. |\n"
    "| Published server/container | `v9.0.0` | Verified maintenance release. It "
    "preserves the v8 runtime APIs, verified in [run 1](https://x/actions/runs/1). |\n"
    "| `unitares-sdk` | `0.4.0` | Published Python client. |\n"
)


def test_row_states_only_what_the_promotion_established():
    row = render_row("9.1.0", RUN, REL)
    assert row.startswith(f"{ROW_PREFIX} `v9.1.0` |")
    assert f"[Promote Release run 123]({RUN})" in row
    assert f"[release notes]({REL})" in row
    # Nothing carried over from a previous release's prose.
    assert "maintenance" not in row and "preserv" not in row


def test_rewrite_replaces_the_whole_row_and_nothing_else():
    out = rewrite(TABLE, render_row("9.1.0", RUN, REL))
    assert "Verified maintenance release" not in out
    assert "runs/1)" not in out
    before, after = TABLE.split("\n"), out.split("\n")
    changed = [i for i, (a, b) in enumerate(zip(before, after)) if a != b]
    assert len(before) == len(after) and len(changed) == 1


def test_version_manager_still_reads_the_regenerated_row():
    from version_manager import PUBLISHED_VERSION_REFERENCES

    pattern = dict(PUBLISHED_VERSION_REFERENCES)["docs/COMPATIBILITY.md"][0][0]
    assert re.findall(pattern, render_row("9.1.0", RUN, REL)) == ["9.1.0"]


@pytest.mark.parametrize(
    "version,run,rel",
    [
        ("v9.1.0", RUN, REL),
        ("9.1.0", "https://github.com/cirwel/unitares/pull/5", REL),
        ("9.1.0", RUN, "https://github.com/cirwel/unitares/releases/tag/v9.0.0"),
    ],
)
def test_refuses_inputs_that_do_not_name_the_release(version, run, rel):
    with pytest.raises(ValueError):
        render_row(version, run, rel)


@pytest.mark.parametrize("table", [TABLE.replace(ROW_PREFIX, "| Published |"), TABLE + TABLE])
def test_refuses_a_missing_or_duplicated_row(table):
    with pytest.raises(ValueError):
        rewrite(table, render_row("9.1.0", RUN, REL))


def test_cli_writes_the_file_and_fails_closed(tmp_path: Path):
    doc = tmp_path / "COMPATIBILITY.md"
    doc.write_text(TABLE)
    args = ["--version", "9.1.0", "--run-url", RUN, "--release-url", REL, "--path", str(doc)]
    assert main(args) == 0
    assert "Promote Release run 123" in doc.read_text()
    doc.write_text("no table here\n")
    assert main(args) == 1
    assert doc.read_text() == "no table here\n"


def test_repository_compatibility_has_exactly_one_row():
    text = (Path(__file__).resolve().parents[1] / "docs" / "COMPATIBILITY.md").read_text()
    assert sum(line.startswith(ROW_PREFIX) for line in text.split("\n")) == 1

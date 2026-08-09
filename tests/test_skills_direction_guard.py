"""The skills sync may only proceed when canonical is POSITIVELY newer.

`sync-plugin-skills.sh` overwrites the plugin mirror from canonical. When the
two differ, anything other than a demonstrably-older mirror is a potential
revert of merged work, and reverting leaves no signal anywhere — the next run
just reports "in sync".

This guard has already shipped one off-by-one (`>` where it needed `>=`, which
let the equal-date case through) and one fail-open (requiring BOTH dates before
it would refuse, which silently reverted an undated mirror). Both are pinned
below. The asymmetry that decides every ambiguous case: a false refusal costs
one forward-port command, a false pass costs merged work.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts" / "dev"))

from skills_direction_guard import (  # noqa: E402
    EXIT_BLOCKED,
    EXIT_OK,
    last_verified,
    regressions,
)

GUARD = REPO / "scripts" / "dev" / "skills_direction_guard.py"


def _skill(root: Path, name: str, *, date: str | None, body: str) -> None:
    root.joinpath(name).mkdir(parents=True, exist_ok=True)
    front = f'---\nname: {name}\n'
    if date is not None:
        front += f'last_verified: "{date}"\n'
    front += "---\n"
    root.joinpath(name, "SKILL.md").write_text(front + body, encoding="utf-8")


@pytest.fixture()
def pair(tmp_path: Path) -> tuple[Path, Path]:
    src, dst = tmp_path / "canonical", tmp_path / "mirror"
    src.mkdir()
    dst.mkdir()
    return src, dst


def test_identical_content_never_blocks(pair):
    src, dst = pair
    _skill(src, "a", date="2026-07-28", body="same")
    _skill(dst, "a", date="2026-07-28", body="same")
    assert regressions(src, dst) == []


def test_canonical_strictly_newer_is_the_passing_case(pair):
    """The ONLY combination that may proceed: both dated, mirror older."""
    src, dst = pair
    _skill(src, "a", date="2026-08-05", body="new canonical work")
    _skill(dst, "a", date="2026-07-28", body="old mirror")
    assert regressions(src, dst) == []


def test_mirror_newer_blocks(pair):
    src, dst = pair
    _skill(src, "a", date="2026-07-28", body="canonical")
    _skill(dst, "a", date="2026-08-02", body="newer mirror work")
    (msg,) = regressions(src, dst)
    assert "newer than" in msg


def test_equal_dates_with_differing_content_blocks(pair):
    """Regression pin: `>` let this through and it is the common hazard.

    Plugin #112 merged the mirror's content ahead of canonical while both sides
    still read 2026-07-28, because the content PR and the freshness PR were
    split. An equal-date test that waves this past reverts the merged content.
    """
    src, dst = pair
    _skill(src, "a", date="2026-07-28", body="canonical")
    _skill(dst, "a", date="2026-07-28", body="DIFFERENT merged content")
    (msg,) = regressions(src, dst)
    assert "same date as" in msg


def test_mirror_without_last_verified_blocks(pair):
    """Regression pin: the fail-open case.

    Requiring both dates meant an undated mirror carrying newer work was
    silently reverted — the exact silent-data-loss class the guard exists to
    prevent, and the opposite of the stated cost asymmetry.
    """
    src, dst = pair
    _skill(src, "a", date="2026-07-28", body="canonical")
    _skill(dst, "a", date=None, body="newer mirror work, nobody bumped the date")
    (msg,) = regressions(src, dst)
    assert "cannot show canonical is newer" in msg


def test_canonical_without_last_verified_blocks(pair):
    src, dst = pair
    _skill(src, "a", date=None, body="canonical")
    _skill(dst, "a", date="2026-08-02", body="mirror")
    (msg,) = regressions(src, dst)
    assert "canonical declares none" in msg


def test_neither_side_dated_blocks(pair):
    src, dst = pair
    _skill(src, "a", date=None, body="canonical")
    _skill(dst, "a", date=None, body="mirror differs")
    (msg,) = regressions(src, dst)
    assert "neither side declares" in msg


def test_mirror_only_skill_is_not_this_guards_call(pair):
    """rsync --delete drops plugin-only skills by design; the guard stays out."""
    src, dst = pair
    _skill(dst, "plugin-only", date="2026-08-02", body="only in the mirror")
    assert regressions(src, dst) == []


def test_only_the_drifted_skill_is_reported(pair):
    src, dst = pair
    _skill(src, "clean", date="2026-07-28", body="same")
    _skill(dst, "clean", date="2026-07-28", body="same")
    _skill(src, "drifted", date="2026-07-28", body="canonical")
    _skill(dst, "drifted", date="2026-08-02", body="mirror")
    reported = regressions(src, dst)
    assert len(reported) == 1
    assert reported[0].startswith("drifted:")


def test_last_verified_reads_unquoted_and_quoted(tmp_path):
    quoted = tmp_path / "q.md"
    quoted.write_text('last_verified: "2026-08-02"\n', encoding="utf-8")
    bare = tmp_path / "b.md"
    bare.write_text("last_verified: 2026-08-02\n", encoding="utf-8")
    assert last_verified(quoted) == "2026-08-02"
    assert last_verified(bare) == "2026-08-02"


def test_missing_file_reads_as_undated(tmp_path):
    assert last_verified(tmp_path / "nope.md") is None


@pytest.mark.parametrize(
    "mirror_date,expected_exit",
    [("2026-07-01", EXIT_OK), ("2026-08-09", EXIT_BLOCKED)],
)
def test_cli_exit_codes(pair, mirror_date, expected_exit):
    """The shell script branches on these exit codes; pin them."""
    src, dst = pair
    _skill(src, "a", date="2026-08-05", body="canonical")
    _skill(dst, "a", date=mirror_date, body="mirror")
    proc = subprocess.run(
        [sys.executable, str(GUARD), str(src), str(dst)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == expected_exit
    if expected_exit == EXIT_BLOCKED:
        assert "a:" in proc.stdout


def test_cli_rejects_wrong_arity():
    proc = subprocess.run(
        [sys.executable, str(GUARD), "only-one"], capture_output=True, text=True
    )
    assert proc.returncode == 2
    assert "usage:" in proc.stderr

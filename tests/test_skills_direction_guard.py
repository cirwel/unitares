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
    is_past_canonical_state,
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


# --- the equal-date tie-break, resolved by canonical's git history -----------
#
# A mirror produced by a sync inherits canonical's `last_verified` verbatim, so
# a canonical edit later the same day leaves both sides on one date with
# different content. By date that is indistinguishable from #112 above. By
# history it is not, and these pin both directions of that.


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def canonical_repo(tmp_path: Path) -> Path:
    """A canonical checkout that is a real git repo, plus its mirror dir."""
    repo = tmp_path / "unitares"
    (repo / "skills").mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    return repo


def test_equal_dates_pass_when_mirror_is_a_past_canonical_state(
    canonical_repo, tmp_path
):
    """Today's real case: the mirror is last week's canonical, same date.

    Canonical committed one state, the mirror was synced from it, then canonical
    moved on the same day. The mirror's bytes are still in canonical's history,
    so nothing is reverted by syncing forward.
    """
    src = canonical_repo / "skills"
    dst = tmp_path / "mirror"
    dst.mkdir()

    _skill(src, "a", date="2026-09-08", body="state the mirror was synced from")
    _git(canonical_repo, "add", "-A")
    _git(canonical_repo, "commit", "-qm", "canonical state one")

    # The mirror is a byte copy of that committed state.
    _skill(dst, "a", date="2026-09-08", body="state the mirror was synced from")

    # Canonical then moves on, same day, same declared date.
    _skill(src, "a", date="2026-09-08", body="canonical moved on")
    _git(canonical_repo, "add", "-A")
    _git(canonical_repo, "commit", "-qm", "canonical state two")

    assert regressions(src, dst) == []


def test_equal_dates_still_block_when_mirror_content_is_not_in_history(
    canonical_repo, tmp_path
):
    """The #112 case must survive the tie-break.

    Mirror-side work that canonical never had appears nowhere in canonical's
    history, so the appeal to history finds nothing and the guard still refuses.
    """
    src = canonical_repo / "skills"
    dst = tmp_path / "mirror"
    dst.mkdir()

    _skill(src, "a", date="2026-07-28", body="canonical")
    _git(canonical_repo, "add", "-A")
    _git(canonical_repo, "commit", "-qm", "canonical")

    _skill(dst, "a", date="2026-07-28", body="DIFFERENT merged content")

    (msg,) = regressions(src, dst)
    assert "same date as" in msg
    assert "not a past state" in msg


def test_equal_dates_block_when_canonical_is_not_a_git_checkout(pair):
    """No history to appeal to means no proof, and no proof means refuse."""
    src, dst = pair
    # Pin the precondition. Under a --basetemp inside a working tree this
    # directory would BE a checkout, and the test would silently start
    # exercising the history-lookup-miss branch instead of the one it names.
    probe = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        pytest.skip("tmp_path sits inside a git checkout; precondition not met")

    _skill(src, "a", date="2026-09-08", body="canonical")
    _skill(dst, "a", date="2026-09-08", body="mirror")
    (msg,) = regressions(src, dst)
    assert "not a past state" in msg


def test_mirror_newer_date_still_blocks_even_when_history_would_allow(
    canonical_repo, tmp_path
):
    """The tie-break is scoped to EQUAL dates and must not widen.

    A mirror declaring a later date is claiming a verification canonical has
    not made, and git history does not overrule that claim.

    The mirror's bytes are committed to canonical FIRST, so the history test
    would say yes if it were consulted. Only the date scoping refuses. Without
    that committed state this test passes vacuously — it did, in review.
    """
    src = canonical_repo / "skills"
    dst = tmp_path / "mirror"
    dst.mkdir()

    # Byte-identical to the mirror, date included, and committed.
    _skill(src, "a", date="2026-09-09", body="state also held by the mirror")
    _git(canonical_repo, "add", "-A")
    _git(canonical_repo, "commit", "-qm", "one")
    _skill(dst, "a", date="2026-09-09", body="state also held by the mirror")

    # Canonical moves on, to an OLDER declared date.
    _skill(src, "a", date="2026-09-08", body="canonical moved on")
    _git(canonical_repo, "add", "-A")
    _git(canonical_repo, "commit", "-qm", "two")

    # Precondition: history alone would allow this.
    assert is_past_canonical_state(
        src / "a" / "SKILL.md", dst / "a" / "SKILL.md"
    )

    (msg,) = regressions(src, dst)
    assert "newer than" in msg


def test_revert_in_canonical_does_not_unlock_the_sync(canonical_repo, tmp_path):
    """Presence in history is not enough; the ordering is what matters.

    Canonical goes A -> B -> back to A while the mirror sits at B. B IS in
    canonical's history, so a presence test waves the sync past and deletes it.
    This is reachable through the guard's own remedy: it tells operators to
    forward-port mirror-side work into canonical, and a later rollback would
    then turn the guard against the very content it first protected.
    """
    src = canonical_repo / "skills"
    dst = tmp_path / "mirror"
    dst.mkdir()

    _skill(src, "a", date="2026-09-08", body="state A")
    _git(canonical_repo, "add", "-A")
    _git(canonical_repo, "commit", "-qm", "A")

    _skill(src, "a", date="2026-09-08", body="state B")
    _git(canonical_repo, "add", "-A")
    _git(canonical_repo, "commit", "-qm", "B")

    # The mirror holds B, which canonical then reverts away from.
    _skill(dst, "a", date="2026-09-08", body="state B")
    _skill(src, "a", date="2026-09-08", body="state A")
    _git(canonical_repo, "add", "-A")
    _git(canonical_repo, "commit", "-qm", "revert to A")

    (msg,) = regressions(src, dst)
    assert "not a past state" in msg


def test_relative_paths_do_not_hash_a_decoy_inside_canonical(
    canonical_repo, tmp_path, monkeypatch
):
    """The CLI takes arbitrary dirs, so paths must not resolve against the repo.

    `git -C repo hash-object <relative>` resolves against the REPO root. With a
    same-named file sitting there, that hashes the decoy instead of the mirror
    and answers the ordering question about the wrong file.
    """
    src = canonical_repo / "skills"
    dst = tmp_path / "mirror"
    dst.mkdir()

    _skill(src, "a", date="2026-09-08", body="state the mirror was synced from")
    _git(canonical_repo, "add", "-A")
    _git(canonical_repo, "commit", "-qm", "one")
    _skill(dst, "a", date="2026-09-08", body="state the mirror was synced from")
    _skill(src, "a", date="2026-09-08", body="canonical moved on")
    _git(canonical_repo, "add", "-A")
    _git(canonical_repo, "commit", "-qm", "two")

    # A decoy at the same relative path the mirror would be named by.
    decoy = canonical_repo / "mirror" / "a"
    decoy.mkdir(parents=True)
    decoy.joinpath("SKILL.md").write_text("decoy", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    assert regressions(Path("unitares/skills"), Path("mirror")) == []

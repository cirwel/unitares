"""The skill freshness checker compares cited sources to recorded content digests.

File mtime, the previous signal, is rewritten by every checkout, clone, rsync
and worktree, so CI could only run the calendar-age check and a skill whose
cited sources had moved on stayed green until its age window ran out. Commit
dates do not work either: the repository squash-merges, which stamps a change
with its merge time rather than the day it was verified against. These tests
run the checker the way CI does, as a subprocess against a throwaway layout.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "scripts/client/_check_freshness.py"


def _day(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class Layout:
    """<projects>/unitares holds both the skill and its cited source."""

    def __init__(self, projects: Path):
        self.projects = projects
        self.repo = projects / "unitares"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "skills" / "demo").mkdir(parents=True)

    @property
    def skill_file(self) -> Path:
        return self.repo / "skills" / "demo" / "SKILL.md"

    def skill(self, last_verified: str, *, digest: str | None, freshness_days: int = 14,
              source: str = "unitares/src/thing.py") -> None:
        digests = f'source_digests:\n  {source}: "{digest}"\n' if digest else ""
        self.skill_file.write_text(textwrap.dedent(f'''\
            ---
            name: demo
            description: >
              Two-line folded description that a stamp
              must leave exactly as it found it.
            last_verified: "{last_verified}"
            freshness_days: {freshness_days}
            source_files:
              # a comment inside the list survives too
              - {source}
            ''') + digests + "---\n# Demo\n")

    def source(self, content: str) -> None:
        (self.repo / "src" / "thing.py").write_text(content)

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CHECKER), str(self.repo), str(self.projects), *args],
            capture_output=True, text=True,
            env={**os.environ, "SKILL_FRESHNESS_FLOOR_DAYS": "30", **env},
        )


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path)


def test_a_source_matching_its_digest_is_fresh_whatever_its_mtime(layout: Layout):
    """The file was written seconds ago; its content is what was verified."""
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(1), digest=_digest("x = 1\n"))
    result = layout.run()
    assert result.returncode == 0, result.stdout
    assert "FRESH" in result.stdout


def test_a_source_whose_content_changed_is_stale_and_named(layout: Layout):
    layout.source("x = 2\n")
    layout.skill(last_verified=_day(1), digest=_digest("x = 1\n"))
    result = layout.run()
    assert result.returncode == 1
    assert "STALE" in result.stdout
    assert "unitares/src/thing.py changed since" in result.stdout
    assert "--stamp demo" in result.stdout


def test_a_cited_source_without_a_digest_is_stale(layout: Layout):
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(1), digest=None)
    result = layout.run()
    assert result.returncode == 1
    assert "has no recorded digest" in result.stdout


def test_an_absent_source_is_reported_not_guessed(layout: Layout):
    """A source in another repository is skipped and counted, as in the plugin mirror."""
    layout.skill(last_verified=_day(1), digest=None, source="elsewhere/src/bot.py")
    result = layout.run()
    assert result.returncode == 0, result.stdout
    assert "1 cited source(s) absent from this checkout" in result.stdout


def _attestations(layout: "Layout") -> list:
    adir = layout.repo / "skills" / ".attestations" / "demo"
    return sorted(adir.glob("*.json")) if adir.is_dir() else []


def test_stamp_writes_a_new_attestation_and_never_touches_skill_md(layout: Layout):
    layout.source("x = 2\n")
    layout.skill(last_verified=_day(10), digest=_digest("x = 1\n"))
    before = layout.skill_file.read_bytes()

    result = layout.run("--stamp", "demo")
    assert result.returncode == 0, result.stdout
    assert layout.skill_file.read_bytes() == before

    [path] = _attestations(layout)
    record = json.loads(path.read_text())
    assert record["schema"] == "unitares.skill_attestation.v1"
    assert record["skill"] == "demo"
    assert record["verified_date"] == _day(0)
    assert record["source_digests"] == {"unitares/src/thing.py": _digest("x = 2\n")}
    assert layout.run().returncode == 0


def test_two_stamps_write_two_distinct_files(layout: Layout):
    # Distinct names are what keep concurrent pull requests from conflicting.
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(1), digest=None)
    layout.run("--stamp", "demo")
    layout.run("--stamp", "demo")
    paths = _attestations(layout)
    assert len(paths) == 2 and paths[0].name != paths[1].name


def test_same_second_stamps_sort_in_the_order_they_were_made(layout: Layout):
    # Names lead with a microsecond UTC timestamp, so the lexically last file
    # is the newest even for stamps inside one second; the random suffix must
    # never decide which record wins.
    import re
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(1), digest=None)
    for content in ("x = 1\n", "x = 2\n", "x = 3\n"):
        layout.source(content)
        layout.run("--stamp", "demo")
    paths = _attestations(layout)
    assert all(re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{8}\.json", p.name) for p in paths)
    newest = json.loads(paths[-1].read_text())
    assert newest["source_digests"] == {"unitares/src/thing.py": _digest("x = 3\n")}
    assert layout.run().returncode == 0


def test_a_new_stamp_records_changed_content(layout: Layout):
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(1), digest=None)
    layout.run("--stamp", "demo")
    layout.source("x = 2\n")
    assert layout.run().returncode == 1          # no record of this content
    layout.run("--stamp", "demo")
    assert layout.run().returncode == 0          # now there is one


_CURRENT = object()


def _attest(layout: "Layout", stem: str, verified_date: str, digests: dict,
            skill_digest=_CURRENT) -> None:
    """Write an attestation by hand, as a merged branch would have left it.

    By default it certifies the skill text currently on disk; pass a digest
    for other text, or None for a record older than `skill_digest`."""
    if skill_digest is _CURRENT:
        skill_digest = hashlib.sha256(layout.skill_file.read_bytes()).hexdigest()[:16]
    adir = layout.repo / "skills" / ".attestations" / "demo"
    adir.mkdir(parents=True, exist_ok=True)
    record = {
        "schema": "unitares.skill_attestation.v1", "skill": "demo",
        "verified_at": f"{verified_date}T00:00:00Z", "verified_date": verified_date,
        "verifier": "test", "source_digests": digests,
    }
    if skill_digest is not None:
        record["skill_digest"] = skill_digest
    (adir / f"{stem}.json").write_text(json.dumps(record))


def test_a_newer_attestation_with_an_old_digest_does_not_mask_a_matching_one(layout: Layout):
    # The 2026-09-24 unitares-governance case. Master verified the skill against
    # the changed source (x = 2). A branch cut before that change stamped the
    # skill later, recording the OLD digest (x = 1) for a source it never
    # touched, and merged after. Its attestation sorts newest; the older one
    # still records exactly the current content, so the skill is fresh.
    layout.source("x = 2\n")
    layout.skill(last_verified=_day(20), digest=None)
    src = "unitares/src/thing.py"
    _attest(layout, "20260101T000000000000Z-aaaaaaaa", _day(2), {src: _digest("x = 2\n")})
    _attest(layout, "20260102T000000000000Z-bbbbbbbb", _day(1), {src: _digest("x = 1\n")})
    result = layout.run()
    assert result.returncode == 0, result.stdout
    assert "FRESH" in result.stdout
    # The age still comes from the newest verified date on record.
    assert "verified 1 days ago" in result.stdout


def test_an_older_attestation_for_different_skill_text_does_not_vouch(layout: Layout):
    # Skill v1 was stamped against source X, v2 against Y, and the source then
    # reverted to X. v2 was never reviewed against X, so X is not fresh for it.
    src = "unitares/src/thing.py"
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(20), digest=None)
    v1 = hashlib.sha256(layout.skill_file.read_bytes()).hexdigest()[:16]
    _attest(layout, "20260101T000000000000Z-aaaaaaaa", _day(3), {src: _digest("x = 1\n")},
            skill_digest=v1)
    layout.skill_file.write_text(layout.skill_file.read_text() + "v2 prose\n")
    _attest(layout, "20260102T000000000000Z-bbbbbbbb", _day(2), {src: _digest("x = 2\n")})
    result = layout.run()
    assert result.returncode == 1, result.stdout
    assert "no attestation records its current content" in result.stdout


def test_an_older_attestation_without_a_skill_digest_vouches_only_as_newest(layout: Layout):
    # Records written before `skill_digest` existed cannot say which skill text
    # they certified, so they keep the old newest-only behaviour.
    src = "unitares/src/thing.py"
    layout.source("x = 2\n")
    layout.skill(last_verified=_day(20), digest=None)
    _attest(layout, "20260101T000000000000Z-aaaaaaaa", _day(2), {src: _digest("x = 2\n")},
            skill_digest=None)
    assert layout.run().returncode == 0                  # newest: still the record
    _attest(layout, "20260102T000000000000Z-bbbbbbbb", _day(1), {src: _digest("x = 1\n")},
            skill_digest=None)
    assert layout.run().returncode == 1                  # older and unscoped: no vouching


def test_a_newest_attestation_for_other_skill_text_does_not_vouch(layout: Layout):
    # Concurrent branches: one stamped v1 (its file sorts newest), the other
    # edited the skill to v2 and stamped that. v2 is the text on disk, so only
    # the v2 record vouches; v1's source digest does not.
    src = "unitares/src/thing.py"
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(20), digest=None)
    v1 = hashlib.sha256(layout.skill_file.read_bytes()).hexdigest()[:16]
    layout.skill_file.write_text(layout.skill_file.read_text() + "v2 prose\n")
    _attest(layout, "20260101T000000000000Z-aaaaaaaa", _day(2), {src: _digest("x = 2\n")})
    _attest(layout, "20260102T000000000000Z-bbbbbbbb", _day(1), {src: _digest("x = 1\n")},
            skill_digest=v1)
    result = layout.run()
    assert result.returncode == 1, result.stdout
    layout.source("x = 2\n")
    assert layout.run().returncode == 0


def test_an_uncertified_skill_edit_falls_back_to_the_newest_record(layout: Layout):
    # Editing SKILL.md without re-stamping keeps the pre-existing behaviour:
    # the newest attestation's digests still vouch.
    src = "unitares/src/thing.py"
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(20), digest=None)
    _attest(layout, "20260101T000000000000Z-aaaaaaaa", _day(1), {src: _digest("x = 1\n")})
    layout.skill_file.write_text(layout.skill_file.read_text() + "unstamped edit\n")
    assert layout.run().returncode == 0


def test_a_newer_stamp_of_other_skill_text_does_not_reset_aging(layout: Layout):
    # The current text was last certified 45 days ago. A stale branch then
    # stamped DIFFERENT skill text yesterday. Nobody re-verified the text on
    # disk, so it is still 45 days old and AGING.
    src = "unitares/src/thing.py"
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(60), digest=None, freshness_days=14)
    _attest(layout, "20260101T000000000000Z-aaaaaaaa", _day(45), {src: _digest("x = 1\n")})
    _attest(layout, "20260102T000000000000Z-bbbbbbbb", _day(1), {src: _digest("x = 1\n")},
            skill_digest="0123456789abcdef")
    result = layout.run()
    assert result.returncode == 1, result.stdout
    assert "AGING" in result.stdout
    assert "verified 45 days ago" in result.stdout


def test_stamp_records_the_skill_text_it_certified(layout: Layout):
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(1), digest=None)
    assert layout.run("--stamp", "demo").returncode == 0
    [path] = _attestations(layout)
    record = json.loads(path.read_text())
    assert record["skill_digest"] == hashlib.sha256(layout.skill_file.read_bytes()).hexdigest()[:16]


def test_a_source_whose_digest_is_in_no_attestation_is_stale(layout: Layout):
    layout.source("x = 3\n")
    layout.skill(last_verified=_day(20), digest=_digest("x = 0\n"))
    src = "unitares/src/thing.py"
    _attest(layout, "20260101T000000000000Z-aaaaaaaa", _day(2), {src: _digest("x = 1\n")})
    _attest(layout, "20260102T000000000000Z-bbbbbbbb", _day(1), {src: _digest("x = 2\n")})
    result = layout.run()
    assert result.returncode == 1
    assert "STALE" in result.stdout
    assert f"{src} changed since {_day(1)}: no attestation records its current content" in result.stdout


def test_the_legacy_frontmatter_digest_still_counts(layout: Layout):
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(3), digest=_digest("x = 1\n"))
    _attest(layout, "20260102T000000000000Z-bbbbbbbb", _day(1),
            {"unitares/src/thing.py": _digest("x = 9\n")})
    result = layout.run()
    assert result.returncode == 0, result.stdout


def test_aging_uses_the_newest_date_even_if_its_file_sorts_first(layout: Layout):
    # File-name order and date order normally agree; the date must not depend on it.
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(60), digest=None, freshness_days=14)
    src = "unitares/src/thing.py"
    _attest(layout, "20260101T000000000000Z-aaaaaaaa", _day(1), {src: _digest("x = 1\n")})
    _attest(layout, "20260102T000000000000Z-bbbbbbbb", _day(50), {src: _digest("x = 1\n")})
    result = layout.run()
    assert result.returncode == 0, result.stdout
    assert "verified 1 days ago" in result.stdout


def test_a_stamp_carries_an_absent_sources_most_recent_digest(layout: Layout):
    # A source this checkout cannot see keeps the digest recorded where it was
    # visible, taken from the newest attestation that records it.
    layout.skill(last_verified=_day(1), digest=None, source="elsewhere/src/bot.py")
    _attest(layout, "20260101T000000000000Z-aaaaaaaa", _day(3), {"elsewhere/src/bot.py": "old"})
    _attest(layout, "20260102T000000000000Z-bbbbbbbb", _day(2), {"elsewhere/src/bot.py": "new"})
    assert layout.run("--stamp", "demo").returncode == 0
    newest = json.loads(_attestations(layout)[-1].read_text())
    assert newest["source_digests"] == {"elsewhere/src/bot.py": "new"}


def test_a_stamp_does_not_carry_a_digest_certified_for_other_skill_text(layout: Layout):
    # The skill text was edited since the only record that saw the absent
    # source. Carrying that digest would have the new stamp certify the edited
    # prose against content nobody reviewed it against, so it stays unrecorded.
    layout.skill(last_verified=_day(1), digest=None, source="elsewhere/src/bot.py")
    _attest(layout, "20260101T000000000000Z-aaaaaaaa", _day(3), {"elsewhere/src/bot.py": "old"},
            skill_digest="0123456789abcdef")
    _attest(layout, "20260102T000000000000Z-bbbbbbbb", _day(2), {"elsewhere/src/bot.py": "legacy"},
            skill_digest=None)
    result = layout.run("--stamp", "demo")
    assert result.returncode == 0, result.stdout
    assert "1 absent source(s) left unrecorded" in result.stdout
    newest = json.loads(_attestations(layout)[-1].read_text())
    assert newest["source_digests"] == {}


def test_a_recent_attestation_keeps_an_old_frontmatter_date_fresh(layout: Layout):
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(45), digest=None, freshness_days=14)
    assert layout.run().returncode == 1          # stale: no digest, and aging
    layout.run("--stamp", "demo")
    result = layout.run()
    assert result.returncode == 0, result.stdout
    assert "verified 0 days ago" in result.stdout


def test_migrate_moves_frontmatter_digests_into_an_attestation(layout: Layout):
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(3), digest=_digest("x = 1\n"))
    assert layout.run("--migrate").returncode == 0
    text = layout.skill_file.read_text()
    assert "source_digests" not in text
    assert "must leave exactly as it found it." in text      # rest untouched
    [path] = _attestations(layout)
    record = json.loads(path.read_text())
    assert record["verified_date"] == _day(3)
    assert record["source_digests"] == {"unitares/src/thing.py": _digest("x = 1\n")}
    # The digest is of the migrated (stripped) text, the text now on disk.
    assert record["skill_digest"] == hashlib.sha256(layout.skill_file.read_bytes()).hexdigest()[:16]
    assert layout.run().returncode == 0


def test_prune_keeps_only_the_newest(layout: Layout):
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(1), digest=None)
    for _ in range(3):
        layout.run("--stamp", "demo")
    newest = _attestations(layout)[-1]
    assert layout.run("--prune", "1").returncode == 0
    assert _attestations(layout) == [newest]


def test_this_repo_sources_resolve_in_a_checkout_not_named_unitares(tmp_path: Path):
    # A worktree is rarely named "unitares"; before 2026-09-24 every cited
    # unitares/ path was "absent" there and the check passed vacuously.
    projects = tmp_path
    repo = projects / "unitares-some-worktree"
    (repo / "src").mkdir(parents=True)
    (repo / "skills" / "demo").mkdir(parents=True)
    (repo / "src" / "thing.py").write_text("x = 2\n")
    (repo / "skills" / "demo" / "SKILL.md").write_text(textwrap.dedent(f'''\
        ---
        name: demo
        last_verified: "{_day(1)}"
        freshness_days: 14
        source_files:
          - unitares/src/thing.py
        source_digests:
          unitares/src/thing.py: "{_digest("x = 1\n")}"
        ---
        # Demo
        '''))
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(repo), str(projects)],
        capture_output=True, text=True,
        env={**os.environ, "SKILL_FRESHNESS_FLOOR_DAYS": "30"},
    )
    assert result.returncode == 1
    assert "STALE" in result.stdout and "absent" not in result.stdout


def test_age_only_override_skips_the_source_check(layout: Layout):
    layout.source("x = 2\n")
    layout.skill(last_verified=_day(10), digest=_digest("x = 1\n"))
    result = layout.run(SKILL_FRESHNESS_AGE_ONLY="1")
    assert result.returncode == 0, result.stdout
    assert "FRESH" in result.stdout


def test_calendar_age_still_fails_past_the_window(layout: Layout):
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(45), digest=_digest("x = 1\n"), freshness_days=14)
    result = layout.run()
    assert result.returncode == 1
    assert "AGING" in result.stdout

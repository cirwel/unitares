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


def test_the_newest_attestation_is_the_record(layout: Layout):
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(1), digest=None)
    layout.run("--stamp", "demo")
    layout.source("x = 2\n")
    assert layout.run().returncode == 1          # drift against the attestation
    layout.run("--stamp", "demo")
    assert layout.run().returncode == 0          # the newer record wins


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

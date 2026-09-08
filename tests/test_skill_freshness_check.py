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


def test_stamp_records_today_and_the_current_digest_and_touches_nothing_else(layout: Layout):
    layout.source("x = 2\n")
    layout.skill(last_verified=_day(10), digest=_digest("x = 1\n"))
    before = layout.skill_file.read_text()

    result = layout.run("--stamp", "demo")
    assert result.returncode == 0, result.stdout + result.stderr
    after = layout.skill_file.read_text()

    assert f'last_verified: "{_day(0)}"' in after
    assert f'  unitares/src/thing.py: "{_digest("x = 2\n")}"' in after
    # Only the date line and the digest block moved.
    untouched = [l for l in before.splitlines()
                 if not l.startswith("last_verified:") and "unitares/src/thing.py: " not in l]
    assert [l for l in after.splitlines()
            if not l.startswith("last_verified:") and "unitares/src/thing.py: " not in l] == untouched
    assert "# a comment inside the list survives too" in after
    assert after.index("source_files:") < after.index("source_digests:") < after.index("\n---\n# Demo")

    assert layout.run().returncode == 0


def test_stamp_adds_the_digest_block_when_none_existed(layout: Layout):
    layout.source("x = 1\n")
    layout.skill(last_verified=_day(10), digest=None)
    assert layout.run("--stamp", "demo").returncode == 0
    assert 'source_digests:\n  unitares/src/thing.py: "' in layout.skill_file.read_text()
    assert layout.run().returncode == 0


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

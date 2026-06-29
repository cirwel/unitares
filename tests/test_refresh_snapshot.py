"""Unit tests for scripts/dev/refresh_snapshot.py.

The DB fetch is brittle in CI (needs a live governance DB), so these exercise the
pure, clock-free pieces: humanization, block rendering, in-place row replacement,
and drift detection — driven by a fixed Snapshot so no clock or DB is touched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "dev" / "refresh_snapshot.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("refresh_snapshot", SCRIPT)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    sys.modules["refresh_snapshot"] = m  # dataclass needs the module registered
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def snap(mod):
    return mod.Snapshot(
        events_total=3_748_915,
        events_7d=713_540,
        agents_total=3_777,
        distinct_21d=510,
        distinct_7d=369,
        kg_discoveries=1_054,
    )


def test_humanize(mod):
    assert mod.humanize(3_748_915) == "3.7M"
    assert mod.humanize(713_540) == "714K"
    assert mod.humanize(999) == "999"
    assert mod.humanize(1_000) == "1K"


def test_floor_thousands(mod):
    assert mod.floor_thousands(3_748_915) == "3,748,000"
    assert mod.floor_thousands(999) == "0"


def test_headline(mod, snap):
    line = mod.headline(snap, "June 16, 2026")
    assert line.startswith("Frozen public snapshot from June 16, 2026")
    assert "**3.7M+ governance events processed · ≈714K in the last 7 days**." in line


def test_render_block_includes_db_and_static_rows(mod, snap):
    block = mod.render_block(snap, "June 16, 2026")
    assert "| Governance events processed | 3,748,000+ (≈714K in the last 7 days) |" in block
    assert "| Knowledge graph discoveries | 1,054 |" in block
    # Static (non-DB) rows survive into the printed block.
    assert "| V operating range | Active agents often within [-0.1, 0.1] |" in block
    assert "| Tests | 8,500+ collected" in block


def test_apply_to_readme_updates_db_rows_and_leaves_static(mod, snap):
    original = (
        "Frozen public snapshot from May 6, 2026 (single-operator deployment — "
        "the author's own traffic, not external adoption). Headline: "
        "**351K+ governance events processed · ≈94K in the last 7 days**.\n\n"
        "| Metric | Value |\n"
        "|--------|-------|\n"
        "| Agents onboarded | 3,660 total process-instances — old text |\n"
        "| Distinct event-emitting identities (last 21 days) | 1,144 total; old |\n"
        "| Unique agents active (last 7 days) | 135 distinct event emitters |\n"
        "| Governance events processed | 351,000+ (≈94K in the last 7 days) |\n"
        "| Knowledge graph discoveries | 860 |\n"
        "| V operating range | Active agents often within [-0.1, 0.1] |\n"
        "| Tests | 8,500+ collected · smoke/pre-push subset plus 75% min coverage gate |\n"
    )
    updated = mod.apply_to_readme(original, snap, "June 16, 2026")
    assert "**3.7M+ governance events processed · ≈714K in the last 7 days**." in updated
    assert "| Governance events processed | 3,748,000+ (≈714K in the last 7 days) |" in updated
    assert "| Knowledge graph discoveries | 1,054 |" in updated
    # Non-DB rows are untouched.
    assert "| V operating range | Active agents often within [-0.1, 0.1] |" in updated
    assert "| Tests | 8,500+ collected · smoke/pre-push subset plus 75% min coverage gate |" in updated
    # Old numbers are gone.
    assert "351,000+" not in updated
    assert "| Knowledge graph discoveries | 860 |" not in updated


def test_apply_to_readme_aborts_when_anchor_missing(mod, snap):
    with pytest.raises(SystemExit):
        mod.apply_to_readme("no snapshot here", snap, "June 16, 2026")


def test_check_readme_reports_drift_and_clean(mod, snap):
    current_rows = "\n".join(f"| {k} | {v} |" for k, v in mod.db_rows(snap))
    assert mod.check_readme(current_rows, snap) == []

    stale = "| Knowledge graph discoveries | 860 |"
    drift = mod.check_readme(stale, snap)
    assert any("Knowledge graph discoveries" in d for d in drift)

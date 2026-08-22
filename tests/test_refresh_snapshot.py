"""Unit tests for scripts/dev/refresh_snapshot.py.

The DB fetch is brittle in CI (needs a live governance DB), so these exercise the
pure, clock-free pieces: block rendering, in-place row replacement, and drift
detection — driven by a fixed Snapshot so no clock or DB is touched.

Row formats are contract-tested against docs/PRODUCTION_SNAPSHOT.md's wording:
--check compares rendered rows byte-for-byte against that doc, so a renderer
that drifts from the doc's format reads as permanent staleness (the 2026-08-22
audit found exactly that drift).
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
        state_rows=54_321,
        non_auto_resumes=21,
        self_recoveries=15,
        kg_discoveries=1_054,
    )


def test_headline(mod, snap):
    line = mod.headline(snap, "2026-08-11 14:02:51 MDT")
    assert line.startswith("Frozen at **2026-08-11 14:02:51 MDT** from a single-operator deployment")
    assert "**3,748,915 audit and\ntelemetry events recorded · 713,540 in the prior 7 days**." in line


def test_db_rows_match_snapshot_doc_format(mod, snap):
    rows = dict(mod.db_rows(snap))
    # Formats mirror docs/PRODUCTION_SNAPSHOT.md rows exactly.
    assert rows["Distinct event-emitting identities (prior 21 days)"] == (
        "510; mostly ephemeral local CLI sessions, not external adopters"
    )
    assert rows["Distinct event-emitting identities (prior 7 days)"] == "369"
    assert rows["Audit/telemetry events recorded"] == (
        "3,748,915 total; 713,540 in the prior 7 days"
    )
    assert rows["Stored EISV state rows"] == (
        "54,321 observations; not independent agents or trials"
    )
    assert rows["Canonical non-automatic lifecycle resumes"].startswith(
        "21, including 15 recorded self-recoveries (reason begins `Self-recovery`)"
    )
    assert rows["Knowledge graph discoveries"] == "1,054"


def test_render_block_includes_db_and_static_rows(mod, snap):
    block = mod.render_block(snap, "June 16, 2026")
    assert "| Audit/telemetry events recorded | 3,748,915 total; 713,540 in the prior 7 days |" in block
    assert "| Knowledge graph discoveries | 1,054 |" in block
    # Static (non-DB) rows survive into the printed block.
    assert "| V operating range | Active agents often within [-0.1, 0.1] |" in block
    assert "| Tests | 13,400+ collected" in block


def test_row_formats_present_in_frozen_snapshot_doc(mod):
    """Every metric name db_rows() emits must anchor a row in the live doc, so
    --check can find each row (values will differ; names must not)."""
    doc = (REPO_ROOT / "docs" / "PRODUCTION_SNAPSHOT.md").read_text(encoding="utf-8")
    dummy = mod.Snapshot(1, 1, 1, 1, 1, 1, 1, 1, 1)
    for metric, _value in mod.db_rows(dummy):
        assert mod._row_re(metric).search(doc), f"no row anchor for {metric!r} in PRODUCTION_SNAPSHOT.md"


def test_apply_to_readme_updates_db_rows_and_leaves_static(mod, snap):
    original = (
        "Frozen at **2026-05-06 09:00:00 MDT** from a single-operator deployment: the\n"
        "author's own traffic, not external adoption. Headline: **351,204 audit and\n"
        "telemetry events recorded · 94,110 in the prior 7 days**.\n\n"
        "| Metric | Value |\n"
        "|--------|-------|\n"
        "| Agents onboarded | 3,660 total process-instances — old text |\n"
        "| Distinct event-emitting identities (prior 21 days) | 1,144 total; old |\n"
        "| Distinct event-emitting identities (prior 7 days) | 135 |\n"
        "| Audit/telemetry events recorded | 351,204 total; 94,110 in the prior 7 days |\n"
        "| Stored EISV state rows | 11,111 observations; not independent agents or trials |\n"
        "| Canonical non-automatic lifecycle resumes | 9, including 5 recorded self-recoveries (reason begins `Self-recovery`); requiring a `type` field excludes legacy dual-written rows |\n"
        "| Knowledge graph discoveries | 860 |\n"
        "| V operating range | Active agents often within [-0.1, 0.1] |\n"
        "| Tests | 13,400+ collected · smoke/pre-push subset plus 75% min coverage gate |\n"
    )
    updated = mod.apply_to_readme(original, snap, "June 16, 2026")
    assert "**3,748,915 audit and\ntelemetry events recorded · 713,540 in the prior 7 days**." in updated
    assert "| Audit/telemetry events recorded | 3,748,915 total; 713,540 in the prior 7 days |" in updated
    assert "| Stored EISV state rows | 54,321 observations; not independent agents or trials |" in updated
    assert "| Knowledge graph discoveries | 1,054 |" in updated
    # Non-DB rows are untouched.
    assert "| V operating range | Active agents often within [-0.1, 0.1] |" in updated
    assert (
        "| Tests | 13,400+ collected · smoke/pre-push subset plus 75% min coverage gate |"
        in updated
    )
    # Old numbers are gone.
    assert "351,204" not in updated
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

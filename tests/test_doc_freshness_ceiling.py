"""Freshness-ceiling checks in scripts/diagnostics/check_doc_drift.py.

A doc carrying a "Last Updated" / "Last reviewed" stamp claims its content
was verified on that date; past MAX_STAMP_AGE_DAYS the claim is stale and
the check fails. Exemptions: the resolved/ archive tier and dated-filename
point-in-time records, both preserved-as-written by policy.
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "check_doc_drift",
    Path(__file__).resolve().parents[1] / "scripts" / "diagnostics" / "check_doc_drift.py",
)
check_doc_drift = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check_doc_drift)

TODAY = date(2026, 8, 16)


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_parse_stamp_date_iso_and_prose():
    assert check_doc_drift.parse_stamp_date("2026-02-06 (notes)") == date(2026, 2, 6)
    assert check_doc_drift.parse_stamp_date("May 6, 2026") == date(2026, 5, 6)
    assert check_doc_drift.parse_stamp_date("soon") is None


def test_fresh_stamp_passes_and_stale_stamp_fails(tmp_path):
    _write(tmp_path, "docs/fresh.md", "# F\n\n**Last Updated:** 2026-08-01\n")
    _write(tmp_path, "docs/stale.md", "# S\n\n**Last Updated:** 2026-02-06\n")
    failures = check_doc_drift.freshness_failures(tmp_path, TODAY)
    assert len(failures) == 1
    assert failures[0].startswith("docs/stale.md:")


def test_last_reviewed_variant_and_roadmap_are_checked(tmp_path):
    _write(tmp_path, "ROADMAP.md", "# R\n\n**Last reviewed:** 2026-01-01\n")
    failures = check_doc_drift.freshness_failures(tmp_path, TODAY)
    assert failures and failures[0].startswith("ROADMAP.md:")


def test_burndown_tolerates_stale_and_flags_cleared_entries(tmp_path, monkeypatch):
    _write(tmp_path, "docs/stale.md", "# S\n\n**Last Updated:** 2026-02-06\n")
    _write(tmp_path, "docs/fixed.md", "# F\n\n**Last Updated:** 2026-08-01\n")
    monkeypatch.setattr(
        check_doc_drift,
        "STALE_STAMP_BURNDOWN",
        {"docs/stale.md": "#1702", "docs/fixed.md": "#1702"},
    )
    failures = check_doc_drift.freshness_failures(tmp_path, TODAY)
    assert len(failures) == 1
    assert "remove its STALE_STAMP_BURNDOWN entry" in failures[0]
    assert failures[0].startswith("docs/fixed.md:")


def test_exemptions_and_unstamped_docs_are_skipped(tmp_path):
    _write(
        tmp_path,
        "docs/proposals/resolved/old.md",
        "# archived\n\n**Last Updated:** 2025-01-01\n",
    )
    _write(
        tmp_path,
        "docs/operations/triage-2026-01-05.md",
        "# record\n\n**Last Updated:** 2026-01-05\n",
    )
    _write(tmp_path, "docs/no_stamp.md", "# no stamp here\n")
    assert check_doc_drift.freshness_failures(tmp_path, TODAY) == []

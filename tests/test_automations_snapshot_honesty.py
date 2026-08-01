"""An external snapshot cannot report a run it never observed.

`next_run` on a `collect_external` item is a PREDICTION the snapshot made.
Once that moment passes without a manifest refresh, the snapshot says nothing
about whether the run happened -- so "past due" asserts an unobserved failure.
A weekly routine with a six-week-old snapshot gets flagged every 30 minutes
forever, which is how a registry trains people to ignore it.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def census_mod():
    module_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "ops" / "unitares-automations"
    )
    loader = importlib.machinery.SourceFileLoader("unitares_automations", str(module_path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    sys.modules["unitares_automations"] = module
    loader.exec_module(module)
    return module


def _item(census_mod, *, next_run: str | None, notes: list[str], status: str = "enabled"):
    return census_mod.Automation(
        id="i", name="routine", source="claude-ai", kind="automation",
        scheduler="claude-ai-routines", runner="claude", status=status,
        cadence="0 15 * * 1", next_run=next_run, notes=list(notes),
    )


def test_snapshot_taken_before_predicted_run_cannot_confirm(census_mod):
    item = _item(
        census_mod,
        next_run="2026-06-22T15:01:46Z",
        notes=["gate:external", "snapshot_at=2026-06-21"],
    )
    assert census_mod.snapshot_cannot_confirm_run(item) is True


def test_snapshot_refreshed_after_predicted_run_is_authoritative(census_mod):
    """A snapshot taken AFTER the predicted run did observe the outcome."""
    item = _item(
        census_mod,
        next_run="2026-06-22T15:01:46Z",
        notes=["gate:external", "snapshot_at=2026-06-25"],
    )
    assert census_mod.snapshot_cannot_confirm_run(item) is False


def test_local_item_without_snapshot_is_unaffected(census_mod):
    """launchd/cron items are observed directly -- past-due stays past-due."""
    past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    item = _item(census_mod, next_run=past, notes=[])
    assert census_mod.snapshot_cannot_confirm_run(item) is False
    assert census_mod.next_run_past_due(item, datetime.now(timezone.utc)) is True


def test_unparseable_snapshot_note_does_not_suppress(census_mod):
    """A malformed note must not silently disable the past-due signal."""
    past = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    item = _item(census_mod, next_run=past, notes=["snapshot_at=not-a-date"])
    assert census_mod.snapshot_cannot_confirm_run(item) is False


def test_snapshot_at_parses_iso_and_bare_date(census_mod):
    item = _item(census_mod, next_run=None, notes=["snapshot_at=2026-06-21"])
    got = census_mod.snapshot_at(item)
    assert got is not None and got.year == 2026 and got.month == 6 and got.day == 21


def test_snapshot_at_absent_returns_none(census_mod):
    assert census_mod.snapshot_at(_item(census_mod, next_run=None, notes=[])) is None

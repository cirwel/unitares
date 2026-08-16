"""Unit tests for _compute_staleness_warning.

The warning must key on the LAST write, not the first store: an entry whose
content was updated recently is current regardless of when it was created, and
the "written against vX" claim is only honest while the store-time version
still describes the content (i.e. it was never updated). Regression context:
before 2026-08-16 both checks keyed on store-time facts, so actively-maintained
long-lived entries (the memory-mirror corpus) warned on every search result.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.mcp_handlers.knowledge.handlers import _compute_staleness_warning

CURRENT_VERSION = "2.18.0"


def _iso(days_ago: float, naive: bool = False) -> str:
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    if naive:
        ts = ts.replace(tzinfo=None)
    return ts.isoformat()


def _discovery(days_old: float, updated_days_ago=None, version="2.14.0", **extra):
    fields = {
        "timestamp": _iso(days_old),
        "updated_at": _iso(updated_days_ago) if updated_days_ago is not None else None,
        "provenance": {"system_version": version} if version else None,
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def test_old_never_updated_warns_on_age_and_version():
    warning = _compute_staleness_warning(_discovery(days_old=90), CURRENT_VERSION)
    assert warning is not None
    assert "90 days old and still open" in warning
    assert "Written against v2.14.0" in warning


def test_recent_update_suppresses_both_warnings():
    # The observed over-fire: stored months ago at an old version, content
    # updated minutes ago — must not warn at all.
    warning = _compute_staleness_warning(
        _discovery(days_old=90, updated_days_ago=0.01), CURRENT_VERSION
    )
    assert warning is None


def test_stale_update_warns_on_update_age_without_version_claim():
    warning = _compute_staleness_warning(
        _discovery(days_old=120, updated_days_ago=70), CURRENT_VERSION
    )
    assert warning is not None
    assert "last updated 70 days ago" in warning
    assert "Written against" not in warning


def test_fresh_entry_behind_version_warns_on_version_only():
    warning = _compute_staleness_warning(_discovery(days_old=1), CURRENT_VERSION)
    assert warning is not None
    assert "Written against v2.14.0" in warning
    assert "days old" not in warning


def test_updated_at_not_newer_than_created_keeps_store_semantics():
    # updated_at == created (some backends echo the store time) is not an update.
    created = _iso(90)
    disc = SimpleNamespace(
        timestamp=created, updated_at=created, provenance={"system_version": "2.14.0"}
    )
    warning = _compute_staleness_warning(disc, CURRENT_VERSION)
    assert warning is not None
    assert "90 days old and still open" in warning
    assert "Written against v2.14.0" in warning


def test_fresh_entry_current_version_no_warning():
    warning = _compute_staleness_warning(
        _discovery(days_old=1, version=CURRENT_VERSION), CURRENT_VERSION
    )
    assert warning is None


def test_naive_timestamps_treated_as_utc():
    disc = SimpleNamespace(
        timestamp=_iso(90, naive=True),
        updated_at=_iso(0.01, naive=True),
        provenance={"system_version": "2.14.0"},
    )
    assert _compute_staleness_warning(disc, CURRENT_VERSION) is None


def test_unparseable_updated_at_falls_back_to_store_facts():
    disc = _discovery(days_old=90, updated_at="not-a-timestamp")
    warning = _compute_staleness_warning(disc, CURRENT_VERSION)
    assert warning is not None
    assert "90 days old and still open" in warning
    assert "Written against v2.14.0" in warning


def test_backend_object_without_updated_at_attr():
    disc = SimpleNamespace(
        timestamp=_iso(90), provenance={"system_version": "2.14.0"}
    )
    warning = _compute_staleness_warning(disc, CURRENT_VERSION)
    assert warning is not None
    assert "90 days old and still open" in warning

"""Unit tests for _compute_staleness_warning.

The warning must key on the LAST write, not the first store: an entry whose
content was updated recently is current regardless of when it was created.
Regression context: before 2026-08-16 the checks keyed on store-time facts, so
actively-maintained long-lived entries (the memory-mirror corpus) warned on
every search result.

There is no version clause (operator decision D1, 2026-09-26): "written against
vX (current: vY), 2+ minor releases behind" compared two VERSION-file values,
so it counted release cuts rather than code change. The wording follows the
entry's type (decision D2): for durable entries "still open" is the resting
status, not a neglected task.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.mcp_handlers.knowledge.handlers import (
    _compute_staleness,
    _compute_staleness_warning,
)

VERIFY_TAIL = "verify before acting on"


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


def test_old_never_updated_warns_on_age_only():
    warning = _compute_staleness_warning(_discovery(days_old=90))
    assert warning is not None
    assert "90 days old and still open" in warning
    assert VERIFY_TAIL in warning
    assert "Written against" not in warning


def test_recent_update_suppresses_warning():
    # The observed over-fire: stored months ago, content updated minutes ago —
    # must not warn at all.
    warning = _compute_staleness_warning(_discovery(days_old=90, updated_days_ago=0.01))
    assert warning is None


def test_stale_update_warns_on_update_age():
    warning = _compute_staleness_warning(_discovery(days_old=120, updated_days_ago=70))
    assert warning is not None
    assert "last updated 70 days ago" in warning
    assert "Written against" not in warning


def test_fresh_entry_behind_version_does_not_warn():
    # Inverted by D1: a 1-day-old entry stored under an older release used to
    # warn on the version clause alone. Release cuts are not a staleness signal.
    assert _compute_staleness_warning(_discovery(days_old=1, version="2.14.0")) is None
    assert _compute_staleness_warning(_discovery(days_old=1, version=None)) is None


def test_warning_starts_after_sixty_days():
    assert _compute_staleness_warning(_discovery(days_old=60.5)) is None
    assert _compute_staleness(_discovery(days_old=61.5))[0] == 61


def test_updated_at_not_newer_than_created_keeps_store_semantics():
    # updated_at == created (some backends echo the store time) is not an update.
    created = _iso(90)
    disc = SimpleNamespace(
        timestamp=created, updated_at=created, provenance={"system_version": "2.14.0"}
    )
    warning = _compute_staleness_warning(disc)
    assert warning is not None
    assert "90 days old and still open" in warning


def test_naive_timestamps_treated_as_utc():
    disc = SimpleNamespace(
        timestamp=_iso(90, naive=True),
        updated_at=_iso(0.01, naive=True),
        provenance={"system_version": "2.14.0"},
    )
    assert _compute_staleness_warning(disc) is None


def test_unparseable_updated_at_falls_back_to_store_facts():
    disc = _discovery(days_old=90, updated_at="not-a-timestamp")
    warning = _compute_staleness_warning(disc)
    assert warning is not None
    assert "90 days old and still open" in warning


def test_backend_object_without_updated_at_attr():
    disc = SimpleNamespace(
        timestamp=_iso(90), provenance={"system_version": "2.14.0"}
    )
    warning = _compute_staleness_warning(disc)
    assert warning is not None
    assert "90 days old and still open" in warning


def test_structured_age_is_the_same_last_write_fact():
    """`age_days` is what search rows and the lean digest carry: the same
    last-write basis as the sentence, and only when the sentence exists."""
    age_days, warning = _compute_staleness(_discovery(days_old=120, updated_days_ago=70))
    assert age_days == 70
    assert warning == _compute_staleness_warning(
        _discovery(days_old=120, updated_days_ago=70)
    )
    assert _compute_staleness(_discovery(days_old=10)) is None
    assert _compute_staleness(SimpleNamespace(timestamp="garbage")) is None


@pytest.mark.parametrize(
    "extra",
    [
        {"type": "pattern"},
        {"type": "learning"},
        {"type": "architectural_decision"},
        {"type": "insight"},
        {"type": "note", "tags": ["permanent"]},
        {"type": "observation", "tags": ["foundational"]},
    ],
)
def test_durable_entries_get_neutral_wording_with_verify_tail(extra):
    """D2: open is the resting status for durable entries (the lifecycle's
    permanent types and tags, plus insight), so the age is stated neutrally —
    but still with a verify tail, because durable rules carry volatile
    details."""
    warning = _compute_staleness_warning(_discovery(days_old=90, **extra))
    assert warning is not None
    assert warning.startswith("Last written 90 days ago")
    assert "still open" not in warning
    assert VERIFY_TAIL in warning


@pytest.mark.parametrize(
    "extra",
    [
        {"type": "bug_found"},
        {"type": "question"},
        {"type": "note"},
        {"type": "note", "tags": ["ephemeral"]},
        {},  # a backend object with neither type nor tags
    ],
)
def test_open_means_unresolved_types_keep_still_open_wording(extra):
    warning = _compute_staleness_warning(_discovery(days_old=90, **extra))
    assert warning is not None
    assert "90 days old and still open" in warning
    assert VERIFY_TAIL in warning


def test_permanent_tag_on_an_open_bug_reads_as_durable():
    """Known edge of D2, recorded rather than fixed: the retention classifier
    is partly tag-driven, so an open bug_found tagged `architecture` gets the
    neutral wording even though it is an unresolved bug."""
    warning = _compute_staleness_warning(
        _discovery(days_old=90, type="bug_found", tags=["architecture"])
    )
    assert warning.startswith("Last written 90 days ago")


def test_malformed_tags_do_not_break_the_warning():
    warning = _compute_staleness_warning(
        _discovery(days_old=90, type="note", tags=[{"legacy": "dict"}, "permanent"])
    )
    assert warning.startswith("Last written 90 days ago")

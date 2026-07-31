"""Tests for centralized real-time date utilities."""

from __future__ import annotations

from datetime import datetime, timezone

from scripts.utils.date_utils import (
    get_current_date,
    now_timestamp,
    now_utc,
    today_compact,
    today_full,
    today_short,
)


def test_now_utc_is_current_and_timezone_aware() -> None:
    """UTC utility must return a live aware timestamp."""

    before = datetime.now(timezone.utc)
    observed = now_utc()
    after = datetime.now(timezone.utc)

    assert observed.tzinfo is not None
    assert observed.utcoffset() is not None
    assert before <= observed <= after


def test_documented_date_formats_are_consistent() -> None:
    """Public helpers must emit their documented shapes for the same current day."""

    assert today_short() == get_current_date("%Y-%m-%d")
    assert today_compact() == get_current_date("%Y%m%d")
    assert today_full() == get_current_date("%B %d, %Y").replace(" 0", " ")
    assert datetime.strptime(now_timestamp(), "%Y-%m-%d %H:%M:%S")

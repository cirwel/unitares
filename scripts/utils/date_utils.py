"""Centralized real-time date and timestamp utilities."""

from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Return the current timezone-aware UTC datetime."""

    return datetime.now(timezone.utc)


def get_current_date(date_format: str = "%B %d, %Y") -> str:
    """Return the current local date/time formatted with ``strftime`` syntax."""

    return datetime.now().astimezone().strftime(date_format)


def today_full() -> str:
    """Return the current local date as ``Month D, YYYY``."""

    return get_current_date("%B %d, %Y").replace(" 0", " ")


def today_short() -> str:
    """Return the current local date as ``YYYY-MM-DD``."""

    return get_current_date("%Y-%m-%d")


def today_compact() -> str:
    """Return the current local date as ``YYYYMMDD``."""

    return get_current_date("%Y%m%d")


def now_timestamp() -> str:
    """Return the current local timestamp as ``YYYY-MM-DD HH:MM:SS``."""

    return get_current_date("%Y-%m-%d %H:%M:%S")

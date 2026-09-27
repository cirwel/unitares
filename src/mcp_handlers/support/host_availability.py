"""Provider-side availability for the subscription-CLI host adapters.

``host_adapter_available`` sees only the local side: flag, CLI, bearer. A
provider account at its usage limit, or logged out, still has a working CLI,
so every call to it fails the same way until the limit resets. Outages rotate
between providers (Claude one week, Codex the next), so a manual switch that
someone must remember to flip back is the wrong tool.

This module learns availability from failures the adapter already sees:

* ``classify`` reads a failed call's provider error and says whether it is a
  quota/rate limit or an auth failure, and when the provider says to retry.
  An error it does not recognise is NOT classified, so a wording change can
  only cost a wasted attempt, never take a working host out of rotation.
* ``record_unavailable`` starts a cooldown: until the provider's own reset
  time when it states one (Codex: "try again at Sep 29th, 2026 9:10 PM"),
  otherwise an exponential backoff. Both are capped, so a limit lifted early
  (credits bought, a login fixed) is noticed within hours.
* ``cooldown`` is what availability probing consults; ``clear`` runs on the
  next success.

State is per process: a restart forgets it and costs one failed attempt,
which the consult failover then routes around. Nothing polls a provider.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

#: Backoff when the provider states no reset time: 30 min, doubling, capped.
BACKOFF_BASE_S = 30 * 60
BACKOFF_CAP_S = 6 * 3600
#: A stated reset time is honoured, but never beyond this: re-probing once
#: costs one failed call, while trusting a far date can hide a fixed account.
STATED_RESET_CAP_S = 12 * 3600

_QUOTA_MARKERS = (
    "usage limit", "weekly limit", "rate limit", "rate_limit", "ratelimit",
    "quota", "resource_exhausted", "resource exhausted", "too many requests",
    "credit balance is too low", "purchase more credits", "limit reached",
)
_AUTH_MARKERS = (
    "not logged in", "please run /login", "please sign in",
    "you are not logged into", "authentication failed", "unauthorized",
    "invalid api key", "account has been suspended", "account suspended",
)

_MONTHS = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
# Codex: "try again at Sep 29th, 2026 9:10 PM"
_CODEX_RESET = re.compile(
    rf"try again at\s+(?P<mon>{_MONTHS})[a-z]*\.?\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?,?"
    r"\s+(?P<year>\d{4})\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>am|pm)",
    re.IGNORECASE,
)
# Claude CLI: "... usage limit reached|1790000000" (epoch seconds)
_EPOCH_RESET = re.compile(r"limit reached\|(?P<epoch>\d{9,11})\b", re.IGNORECASE)
# Claude CLI: "... resets 3pm" / "resets 3:30am"
_CLOCK_RESET = re.compile(
    r"resets?\s+(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)\b",
    re.IGNORECASE,
)

_lock = threading.Lock()
# host_id -> {"reason", "retry_after" (epoch s), "failures", "detail"}
_state: dict[str, dict[str, Any]] = {}


def _stated_reset(text: str, now: float) -> Optional[float]:
    """The provider's own retry time, as epoch seconds, if the text states one."""
    m = _CODEX_RESET.search(text)
    if m:
        try:
            stamp = datetime.strptime(
                f"{m['mon'][:3].title()} {int(m['day'])} {m['year']} "
                f"{int(m['hour'])}:{m['minute']} {m['ampm'].upper()}",
                "%b %d %Y %I:%M %p",
            )
            return stamp.timestamp()  # the CLI reports the machine's local time
        except ValueError:
            return None
    m = _EPOCH_RESET.search(text)
    if m:
        return float(m["epoch"])
    m = _CLOCK_RESET.search(text)
    if m:
        hour = int(m["hour"]) % 12 + (12 if m["ampm"].lower() == "pm" else 0)
        base = datetime.fromtimestamp(now)
        stamp = base.replace(hour=hour, minute=int(m["minute"] or 0), second=0, microsecond=0)
        if stamp.timestamp() <= now:
            stamp += timedelta(days=1)
        return stamp.timestamp()
    return None


def classify(text: str, *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """{"reason": "quota"|"auth", "stated_reset": epoch|None}, or None.

    None means "not recognisably a provider-availability failure": the caller
    must treat it as an ordinary failure and take no host out of rotation.
    """
    if not text:
        return None
    low = text.lower()
    if any(marker in low for marker in _QUOTA_MARKERS):
        reason = "quota"
    elif any(marker in low for marker in _AUTH_MARKERS):
        reason = "auth"
    else:
        return None
    now = time.time() if now is None else now
    return {"reason": reason, "stated_reset": _stated_reset(text, now)}


def record_unavailable(
    host_id: str, classified: dict[str, Any], *, detail: str = "",
    now: Optional[float] = None,
) -> dict[str, Any]:
    """Start or extend a cooldown for ``host_id``; returns the public view."""
    now = time.time() if now is None else now
    with _lock:
        failures = _state.get(host_id, {}).get("failures", 0) + 1
        stated = classified.get("stated_reset")
        if isinstance(stated, (int, float)) and stated > now:
            retry_after = min(stated, now + STATED_RESET_CAP_S)
            source = "provider"
        else:
            retry_after = now + min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (failures - 1))
            source = "backoff"
        _state[host_id] = {
            "reason": classified.get("reason"),
            "retry_after": retry_after,
            "retry_after_source": source,
            "failures": failures,
            "detail": detail[:200],
        }
        return _public(host_id, _state[host_id])


def cooldown(host_id: str, *, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """The active cooldown for ``host_id``, or None once it has lapsed.

    A lapsed cooldown keeps its failure count, so a host that fails again
    right after re-probing backs off longer; ``clear`` resets it."""
    now = time.time() if now is None else now
    with _lock:
        entry = _state.get(host_id)
        if not entry or entry["retry_after"] <= now:
            return None
        return _public(host_id, entry)


def clear(host_id: str) -> None:
    with _lock:
        _state.pop(host_id, None)


def _public(host_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "host_id": host_id,
        "reason": entry["reason"],
        "retry_after": datetime.fromtimestamp(entry["retry_after"]).astimezone().isoformat(
            timespec="seconds"),
        "retry_after_source": entry["retry_after_source"],
        "consecutive_failures": entry["failures"],
        "detail": entry["detail"],
    }


def _reset_for_tests() -> None:
    with _lock:
        _state.clear()

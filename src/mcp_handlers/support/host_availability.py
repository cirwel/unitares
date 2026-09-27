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

Reads stay in process: ``host_adapter_available`` is sync and on the hot
path, so ``cooldown`` never touches Redis. Each cooldown is also written
through to Redis (``unitares:host_cooldown:<host_id>``, TTL = time left in
the window) by the ``*_async`` variants the async call sites use, and
``load_from_redis`` refills the cache at startup, so a gov restart no longer
forgets that a provider is at its limit and spends a failed call finding out
again. Redis is a copy, never a dependency: every await is bounded, and
Redis down or slow means the in-process behaviour, never an exception.
Nothing polls a provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

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

#: Redis copy of each live cooldown; the key expires with the window.
REDIS_KEY_PREFIX = "unitares:host_cooldown:"
#: Bound on every Redis await (CLAUDE.md "Substrate Tax"). A cooldown copy is
#: worth far less than a stalled inference call.
REDIS_TIMEOUT_S = 1.0

_lock = threading.Lock()
# host_id -> {"reason", "retry_after" (epoch s), "retry_after_source",
#             "failures", "detail", "recorded_at" (epoch s)}
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
        previous = _state.get(host_id, {})
        # One backoff step per cooldown window: calls that were already in
        # flight when the outage began fail too, and must not each double it.
        still_cooling = previous.get("retry_after", 0) > now
        failures = previous.get("failures", 0) + (0 if still_cooling else 1)
        failures = max(failures, 1)
        stated = classified.get("stated_reset")
        if isinstance(stated, (int, float)) and stated > now:
            retry_after = min(stated, now + STATED_RESET_CAP_S)
            source = "provider"
        else:
            retry_after = now + min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2 ** (failures - 1))
            source = "backoff"
        # Inside a running window a guess changes nothing: it must neither
        # shorten the cooldown nor overwrite a reset the provider stated. Only
        # the provider's own statement moves it (it knows; we back off blind),
        # so a stated reset is taken even when it is earlier.
        recorded_at = now
        if still_cooling and source == "backoff":
            retry_after, source = previous["retry_after"], previous["retry_after_source"]
            recorded_at = previous.get("recorded_at", now)
        _state[host_id] = {
            "reason": classified.get("reason"),
            "retry_after": retry_after,
            "retry_after_source": source,
            "failures": failures,
            "detail": detail[:200],
            "recorded_at": recorded_at,
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


# --- Redis copy ------------------------------------------------------------


async def _get_redis() -> Any:
    """The shared Redis client, or None. Tests replace this seam."""
    from src.cache.redis_client import get_redis

    return await get_redis()


async def _bounded(op, what: str) -> Any:
    """Run ``op(redis)`` under REDIS_TIMEOUT_S; None on no Redis or any error."""

    async def _run() -> Any:
        redis = await _get_redis()
        if redis is None:
            return None
        return await op(redis)

    try:
        return await asyncio.wait_for(_run(), timeout=REDIS_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.warning("[HOST_COOLDOWN] Redis %s timed out after %ss; in-process only",
                       what, REDIS_TIMEOUT_S)
    except Exception as exc:  # fail soft: the in-process state is authoritative here
        logger.debug("[HOST_COOLDOWN] Redis %s failed: %s", what, exc)
    return None


def _decode(raw: Any) -> Optional[dict[str, Any]]:
    """A stored entry, or None when it is missing or not one of ours."""
    if raw is None:
        return None
    try:
        entry = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        entry["retry_after"] = float(entry["retry_after"])
        entry["recorded_at"] = float(entry.get("recorded_at") or 0.0)
        entry["failures"] = max(int(entry.get("failures") or 1), 1)
        if entry.get("retry_after_source") not in ("provider", "backoff"):
            return None
        entry["reason"] = entry.get("reason")
        entry["detail"] = str(entry.get("detail") or "")[:200]
        return entry
    except (TypeError, ValueError, KeyError, AttributeError):
        return None


def _adopt(host_id: str, incoming: Optional[dict[str, Any]], now: float) -> bool:
    """Merge a stored entry into the cache under the window rules.

    The same rules ``record_unavailable`` applies: a guess never shortens or
    overrides a live window, a provider-stated reset beats a guess, and of
    two stated resets the later statement wins. The 12h cap holds even for a
    copy this process did not write. Returns whether the cache changed."""
    if incoming is None or incoming["retry_after"] <= now:
        return False
    incoming = {**incoming, "retry_after": min(incoming["retry_after"], now + STATED_RESET_CAP_S)}
    with _lock:
        current = _state.get(host_id)
        if current is not None and current["retry_after"] > now:
            cur_src, inc_src = current["retry_after_source"], incoming["retry_after_source"]
            if cur_src == "provider" and inc_src == "backoff":
                take = False
            elif cur_src == "backoff" and inc_src == "provider":
                take = True
            elif cur_src == "provider":
                take = incoming["recorded_at"] > current.get("recorded_at", 0.0)
            else:
                take = incoming["retry_after"] > current["retry_after"]
            if not take:
                if incoming["failures"] > current["failures"]:
                    current["failures"] = incoming["failures"]
                    return True
                return False
            incoming["failures"] = max(incoming["failures"], current["failures"])
        elif current is not None:  # lapsed: keep the longer failure history
            incoming["failures"] = max(incoming["failures"], current["failures"])
        _state[host_id] = incoming
        return True


async def _pull(host_id: str, now: float) -> None:
    raw = await _bounded(lambda r: r.get(REDIS_KEY_PREFIX + host_id), f"read {host_id}")
    _adopt(host_id, _decode(raw), now)


async def _push(host_id: str, now: float) -> None:
    with _lock:
        entry = dict(_state.get(host_id) or {})
    ttl = math.ceil(entry.get("retry_after", 0) - now)
    if ttl <= 0:
        return
    payload = json.dumps(entry, sort_keys=True)
    await _bounded(
        lambda r: r.set(REDIS_KEY_PREFIX + host_id, payload, ex=ttl), f"write {host_id}",
    )


async def record_unavailable_async(
    host_id: str, classified: dict[str, Any], *, detail: str = "",
    now: Optional[float] = None,
) -> dict[str, Any]:
    """``record_unavailable`` with the Redis copy: sync the cache from Redis
    first (another process or a previous run may hold a live window), apply
    the window rules, then write the result through."""
    now = time.time() if now is None else now
    await _pull(host_id, now)
    view = record_unavailable(host_id, classified, detail=detail, now=now)
    await _push(host_id, now)
    return view


async def clear_async(host_id: str) -> None:
    """``clear`` with the Redis copy removed too, so a restart after a
    recovered provider does not resurrect its old window."""
    clear(host_id)
    await _bounded(lambda r: r.delete(REDIS_KEY_PREFIX + host_id), f"delete {host_id}")


async def load_from_redis(*, now: Optional[float] = None) -> int:
    """Refill the cache from Redis at startup; returns how many live
    cooldowns were adopted. Redis down means 0 and in-process behaviour."""
    now = time.time() if now is None else now

    async def _read_all(redis: Any) -> list[tuple[str, Any]]:
        keys = [key async for key in redis.scan_iter(match=REDIS_KEY_PREFIX + "*")]
        if not keys:
            return []
        return list(zip(keys, await redis.mget(keys)))

    rows = await _bounded(_read_all, "startup load") or []
    adopted = 0
    for key, raw in rows:
        key = key.decode() if isinstance(key, (bytes, bytearray)) else str(key)
        if _adopt(key[len(REDIS_KEY_PREFIX):], _decode(raw), now):
            adopted += 1
    if adopted:
        logger.info("[HOST_COOLDOWN] Restored %d provider cooldown(s) from Redis", adopted)
    return adopted


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

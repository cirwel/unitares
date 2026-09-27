"""Fleet risk and verdict-pressure trend, computed from core.agent_state.

The dashboard's Risk section used to chart Chronicler's daily scrape of
``governance.risk.mean.7d`` / ``governance.guide.7d`` / ``governance.pause.7d``.
Chronicler is a reference resident, so an install without it had an empty Risk
tab. This module computes the same three trailing-7-day series straight from
the table Chronicler's scrapers read (``agents/chronicler/scrapers.py``), with
the same filters, so any install can draw them.

The live table is not an archive: retention thins rows older than about ten
weeks. The window is therefore capped at ``MAX_WINDOW_DAYS`` so that every
point's trailing week lies inside retained history. Chronicler's scrape remains
the long-horizon record for a deployment that runs it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

TRAILING_DAYS = 7
MAX_WINDOW_DAYS = 60
MIN_WINDOW_DAYS = 7

# One row per UTC day: the sums the trailing windows are built from. Filters
# match the Chronicler scrapers: non-synthetic check-ins; `guide` by name; a
# "pause" is any recorded action other than approve/guide, so a new hard-stop
# action folds in rather than being dropped.
_DAILY_SQL = """
SELECT (recorded_at AT TIME ZONE 'UTC')::date AS day,
       sum(risk_score)   AS risk_sum,
       count(risk_score) AS risk_n,
       count(*) FILTER (WHERE state_json->>'action' = 'guide') AS guide,
       count(*) FILTER (WHERE state_json->>'action' IS NOT NULL
                          AND state_json->>'action' NOT IN ('approve', 'guide')) AS pause
FROM core.agent_state
WHERE recorded_at >= $1 AND synthetic = false
GROUP BY 1
ORDER BY 1
"""


def clamp_window(days: Any) -> int:
    try:
        d = int(days)
    except (TypeError, ValueError):
        d = MAX_WINDOW_DAYS
    return max(MIN_WINDOW_DAYS, min(d, MAX_WINDOW_DAYS))


def rolling_series(
    daily: Iterable[Mapping[str, Any]], *, window_days: int, today: date
) -> dict[str, list[dict[str, Any]]]:
    """Turn per-day sums into trailing-7-day series, one point per day.

    ``daily`` rows carry ``day`` (a date), ``risk_sum``, ``risk_n``, ``guide``
    and ``pause``. The last point ends on ``today`` and, like a Chronicler
    scrape taken now, covers a partial final day. A day whose trailing week
    holds no risk readings gets no risk point rather than a zero.
    """
    by_day = {row["day"]: row for row in daily}
    risk: list[dict[str, Any]] = []
    guide: list[dict[str, Any]] = []
    pause: list[dict[str, Any]] = []
    for offset in range(window_days - 1, -1, -1):
        end = today - timedelta(days=offset)
        r_sum = 0.0
        r_n = g = p = 0
        for back in range(TRAILING_DAYS):
            row = by_day.get(end - timedelta(days=back))
            if not row:
                continue
            r_sum += float(row["risk_sum"] or 0.0)
            r_n += int(row["risk_n"] or 0)
            g += int(row["guide"] or 0)
            p += int(row["pause"] or 0)
        ts = f"{end.isoformat()}T00:00:00Z"
        if r_n:
            risk.append({"ts": ts, "value": round(r_sum / r_n, 4)})
        guide.append({"ts": ts, "value": g})
        pause.append({"ts": ts, "value": p})
    return {"risk": risk, "guide": guide, "pause": pause}


async def query_governance_trend(conn, *, window_days: int, now: datetime | None = None) -> dict[str, Any]:
    """Read the daily sums for the window plus its first trailing week."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(timezone.utc).date()
    first = today - timedelta(days=window_days - 1 + TRAILING_DAYS - 1)
    since = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    rows = await conn.fetch(_DAILY_SQL, since)
    series = rolling_series([dict(r) for r in rows], window_days=window_days, today=today)
    return {
        "success": True,
        "schema": "governance.trend.v1",
        "window_days": window_days,
        "trailing_days": TRAILING_DAYS,
        "source": "core.agent_state",
        "note": (
            "Trailing-7-day aggregates of non-synthetic check-ins. Pause counts "
            "are verdicts produced, not interventions delivered. The window is "
            f"capped at {MAX_WINDOW_DAYS} days because retention thins older rows."
        ),
        **series,
    }

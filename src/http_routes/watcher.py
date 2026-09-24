"""Watcher findings summary endpoints.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from starlette.responses import JSONResponse


from src.logging_utils import get_logger

from src.http_routes import access

logger = get_logger(__name__)


def _watcher_findings_path() -> Path:
    """Resolve Watcher's findings.jsonl via the shared, checkout-independent
    state dir.

    Must match where the Watcher agent writes — the reader-side contract
    (shared dir, legacy fallback, migration) lives in
    ``src.watcher_state_reader``, kept import-independent from the
    ``agents.watcher`` writer and pinned to it by a parity test.
    """
    from src.watcher_state_reader import watcher_findings_path

    return watcher_findings_path()


_WATCHER_DAILY_WINDOW_DAYS = 30


def _watcher_summary_from_rows(rows, now=None, window_days=_WATCHER_DAILY_WINDOW_DAYS):
    """Aggregate watcher findings.jsonl rows into dashboard-ready shape.

    Pure function so test coverage doesn't need to stand up the full HTTP app —
    feed it a list of parsed-dict rows, get back the counts + daily buckets.
    """
    from collections import Counter, defaultdict
    from datetime import datetime, timedelta, timezone

    by_status = Counter()
    by_severity = Counter()   # open-only (surfaced + open) — the actionable queue
    by_pattern = defaultdict(
        lambda: {"surfaced": 0, "confirmed": 0, "dismissed": 0, "dismissed_fp": 0, "other": 0}
    )
    daily = defaultdict(int)  # yyyy-mm-dd → count of detected_at in that day
    resolutions_daily = defaultdict(lambda: {"confirmed": 0, "dismissed": 0})

    if now is None:
        now = datetime.now(timezone.utc)
    window_start = (now - timedelta(days=window_days - 1)).date()

    def _parse_date(value):
        if not value:
            return None
        try:
            # Tolerate trailing Z and no tz
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            return datetime.fromisoformat(value)
        except Exception:
            return None

    # Status names used by Watcher (findings.py:VALID_FINDING_STATUSES):
    # the closed-resolved status is "confirmed", not "resolved" — earlier
    # versions of this aggregator looked for "resolved" and silently dropped
    # every confirmed finding into "other", which made the dashboard claim
    # zero confirms regardless of reality.
    for row in rows:
        status = str(row.get("status", "surfaced"))
        pattern = str(row.get("pattern") or "?")
        severity = str(row.get("severity") or "?")
        by_status[status] += 1

        bucket = by_pattern[pattern]
        if status in ("confirmed", "dismissed"):
            bucket[status] += 1
            # A dismissal with reason "fp" is a *confirmed false positive* the
            # verifier/operator caught — the detector fired on a known-benign
            # shape. Tracking it separately lets the panel tell "FP filters
            # working" apart from "rule produces no signal" (see findings.py
            # DISMISSAL_REASONS / PRECISION_REASONS_TRUE_NEGATIVE).
            if status == "dismissed" and str(row.get("resolution_reason") or "") == "fp":
                bucket["dismissed_fp"] += 1
        elif status in ("surfaced", "open"):
            bucket["surfaced"] += 1
            by_severity[severity] += 1
        else:
            bucket["other"] += 1

        detected = _parse_date(row.get("detected_at"))
        if detected and detected.date() >= window_start:
            daily[detected.date().isoformat()] += 1

        # Resolution timestamps written by update_finding_status:
        # confirmed_at / dismissed_at (ISO 8601, UTC).
        for key, kind in (("confirmed_at", "confirmed"), ("dismissed_at", "dismissed")):
            ts = _parse_date(row.get(key))
            if ts and ts.date() >= window_start:
                resolutions_daily[ts.date().isoformat()][kind] += 1

    # Pattern table — include confirm/dismiss ratio for noise detection
    patterns_out = []
    for pat, b in by_pattern.items():
        total_closed = b["confirmed"] + b["dismissed"]
        dismiss_ratio = (b["dismissed"] / total_closed) if total_closed else None
        patterns_out.append({
            "pattern": pat,
            "surfaced": b["surfaced"],
            "confirmed": b["confirmed"],
            "dismissed": b["dismissed"],
            "dismissed_fp": b["dismissed_fp"],
            "other": b["other"],
            "dismiss_ratio": dismiss_ratio,
        })
    patterns_out.sort(
        key=lambda p: (-p["surfaced"], -(p["confirmed"] + p["dismissed"]), p["pattern"])
    )

    # Daily series spans the full window so the chart renders zeros instead of gaps
    timeline = []
    for i in range(window_days):
        day = (window_start + timedelta(days=i)).isoformat()
        timeline.append({
            "day": day,
            "detected": daily.get(day, 0),
            "confirmed": resolutions_daily[day]["confirmed"],
            "dismissed": resolutions_daily[day]["dismissed"],
        })

    return {
        "total": sum(by_status.values()),
        "by_status": dict(by_status),
        "by_severity_open": dict(by_severity),
        "patterns": patterns_out,
        "timeline": timeline,
        "window_days": window_days,
        "generated_at": now.isoformat(),
    }


async def http_watcher_summary(request):
    """GET /v1/watcher/summary — aggregate Watcher findings for the dashboard panel.

    Reads data/watcher/findings.jsonl in-process (watcher's append-only audit
    log) and returns counts + a daily time series. Data is gitignored, so
    absence = empty summary (not an error)."""
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    rows = []
    path = _watcher_findings_path()
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        # Skip malformed lines silently — findings.jsonl is
                        # append-only and a partial write shouldn't 500 the panel.
                        continue
    except OSError as e:
        return JSONResponse({"success": False, "error": f"findings read failed: {e}"}, status_code=500)

    summary = _watcher_summary_from_rows(rows)
    summary["success"] = True
    summary["findings_path"] = str(path)
    return JSONResponse(summary)

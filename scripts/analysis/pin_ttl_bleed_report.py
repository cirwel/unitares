#!/usr/bin/env python3
"""Pin-TTL masking hypothesis report — queries audit.events, prints markdown.

Tests whether the 30-minute sliding onboard pin TTL (_PIN_TTL=1800 in
src/mcp_handlers/identity/session.py) silently lets clients fall through to
continuity-token-only resumes between minutes 30-60 of a session.

Instrumentation was added in PR #203 (identity_resolution_observed events).

Usage:
    python3 scripts/analysis/pin_ttl_bleed_report.py
    python3 scripts/analysis/pin_ttl_bleed_report.py --days 14
    python3 scripts/analysis/pin_ttl_bleed_report.py --output report.md

Env:
    GOVERNANCE_DATABASE_URL  (default: postgresql://postgres:postgres@localhost:5432/governance)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

ANALYSIS_SQL = Path(__file__).with_name("pin_ttl_bleed_analysis.sql")

MASKING_CANDIDATE_THRESHOLD = 10  # min continuity_token wins with present+match to call supported
MIN_TOTAL_FOR_VERDICT = 50        # below this → inconclusive regardless


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

async def run_queries(db_url: str, start_ts: datetime, end_ts: datetime) -> dict[str, Any]:
    try:
        import asyncpg
    except ImportError:
        print("error: asyncpg not installed — pip install asyncpg", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(db_url)
    try:
        results: dict[str, Any] = {}

        # Q1: total count
        row = await conn.fetchrow(
            """
            SELECT count(*)::int AS total_events, min(ts) AS window_start, max(ts) AS window_end
            FROM audit.events
            WHERE event_type = 'identity_resolution_observed'
              AND ts >= $1::timestamptz AND ts < $2::timestamptz
            """,
            start_ts, end_ts,
        )
        results["q1"] = dict(row) if row else {"total_events": 0}

        # Q2: source distribution
        rows = await conn.fetch(
            """
            SELECT coalesce(payload->>'resolution_source', '(null)') AS resolution_source,
                   count(*)::int AS n
            FROM audit.events
            WHERE event_type = 'identity_resolution_observed'
              AND ts >= $1::timestamptz AND ts < $2::timestamptz
            GROUP BY 1 ORDER BY 2 DESC
            """,
            start_ts, end_ts,
        )
        results["q2"] = [dict(r) for r in rows]

        # Q3: shadow-pin breakdown by source (non-pin winners)
        rows = await conn.fetch(
            """
            SELECT
                coalesce(payload->>'resolution_source', '(null)') AS resolution_source,
                count(*)::int AS n,
                count(*) FILTER (WHERE (payload->>'pin_entry_present')::boolean IS TRUE)::int AS pin_present,
                count(*) FILTER (
                    WHERE (payload->>'pin_entry_present')::boolean IS TRUE
                      AND (payload->>'pin_fingerprint_match')::boolean IS TRUE)::int AS present_and_match,
                count(*) FILTER (
                    WHERE (payload->>'pin_entry_present')::boolean IS TRUE
                      AND (payload->>'pin_fingerprint_match')::boolean IS FALSE)::int AS present_but_mismatch,
                count(*) FILTER (WHERE (payload->>'pin_entry_present')::boolean IS FALSE)::int AS pin_absent,
                count(*) FILTER (WHERE payload->>'pin_entry_present' IS NULL)::int AS shadow_not_run
            FROM audit.events
            WHERE event_type = 'identity_resolution_observed'
              AND ts >= $1::timestamptz AND ts < $2::timestamptz
              AND coalesce(payload->>'resolution_source', '') != 'pinned_onboard_session'
            GROUP BY 1 ORDER BY 2 DESC
            """,
            start_ts, end_ts,
        )
        results["q3"] = [dict(r) for r in rows]

        # Q4: pin-age-bucketed match rate for continuity_token wins where shadow ran
        rows = await conn.fetch(
            """
            SELECT
                CASE
                    WHEN (payload->>'pin_entry_age_seconds')::int < 450   THEN '0–449s'
                    WHEN (payload->>'pin_entry_age_seconds')::int < 900   THEN '450–899s'
                    WHEN (payload->>'pin_entry_age_seconds')::int < 1500  THEN '900–1499s'
                    WHEN (payload->>'pin_entry_age_seconds')::int <= 1800 THEN '1500–1800s'
                    ELSE                                                        '>1800s (stale?)'
                END AS age_bucket,
                count(*)::int AS n,
                count(*) FILTER (WHERE (payload->>'pin_fingerprint_match')::boolean IS TRUE)::int AS pin_match,
                count(*) FILTER (WHERE (payload->>'pin_fingerprint_match')::boolean IS FALSE)::int AS pin_mismatch,
                round(
                    100.0 * count(*) FILTER (WHERE (payload->>'pin_fingerprint_match')::boolean IS TRUE)
                    / nullif(count(*), 0), 1
                ) AS match_pct
            FROM audit.events
            WHERE event_type = 'identity_resolution_observed'
              AND ts >= $1::timestamptz AND ts < $2::timestamptz
              AND payload->>'resolution_source' = 'continuity_token'
              AND (payload->>'pin_entry_present')::boolean IS TRUE
            GROUP BY 1
            ORDER BY
                CASE
                    WHEN (payload->>'pin_entry_age_seconds')::int < 450   THEN 1
                    WHEN (payload->>'pin_entry_age_seconds')::int < 900   THEN 2
                    WHEN (payload->>'pin_entry_age_seconds')::int < 1500  THEN 3
                    WHEN (payload->>'pin_entry_age_seconds')::int <= 1800 THEN 4
                    ELSE 5
                END
            """,
            start_ts, end_ts,
        )
        results["q4"] = [dict(r) for r in rows]

        # Q5: token-age × pin-state cross-tab for continuity_token wins
        rows = await conn.fetch(
            """
            SELECT
                CASE
                    WHEN (payload->>'token_age_seconds')::int <  1800 THEN 'token<30m'
                    WHEN (payload->>'token_age_seconds')::int <  3600 THEN 'token_30-60m'
                    WHEN (payload->>'token_age_seconds')::int <  7200 THEN 'token_1h-2h'
                    WHEN (payload->>'token_age_seconds')::int < 86400 THEN 'token_2h-24h'
                    ELSE                                                    'token>24h'
                END AS token_age_bucket,
                coalesce(payload->>'pin_entry_present', 'null')    AS pin_present,
                coalesce(payload->>'pin_fingerprint_match', 'null') AS pin_match,
                count(*)::int AS n
            FROM audit.events
            WHERE event_type = 'identity_resolution_observed'
              AND ts >= $1::timestamptz AND ts < $2::timestamptz
              AND payload->>'resolution_source' = 'continuity_token'
              AND payload->>'token_age_seconds' IS NOT NULL
            GROUP BY 1, 2, 3 ORDER BY 1, 2, 3
            """,
            start_ts, end_ts,
        )
        results["q5"] = [dict(r) for r in rows]

        return results
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def _pct(n: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{100 * n / total:.1f}%"


def build_report(results: dict[str, Any], start_ts: datetime, end_ts: datetime, days: int) -> str:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    q1 = results["q1"]
    total = q1.get("total_events", 0)

    lines: list[str] = []
    a = lines.append

    a("# Pin-TTL Masking Hypothesis — Audit Report")
    a("")
    a(f"**Window:** {start_ts.strftime('%Y-%m-%d %H:%M UTC')} → {end_ts.strftime('%Y-%m-%d %H:%M UTC')}  ")
    a(f"**Generated:** {now_str}  ")
    a(f"**Instrumentation:** PR #203 `identity_resolution_observed` events  ")
    a(f"**Hypothesis:** 30-min sliding pin TTL masks continuity-token resumes in minutes 30–60 of a session")
    a("")
    a("---")
    a("")

    # ── Section 1: Total ─────────────────────────────────────────────────────
    a("## 1. Total Events")
    a("")
    a("| Metric | Value |")
    a("|--------|-------|")
    a(f"| `identity_resolution_observed` events in window | **{total}** |")
    if total == 0:
        a(f"| Window start (actual) | — |")
        a(f"| Window end (actual) | — |")
    else:
        ws = q1.get("window_start")
        we = q1.get("window_end")
        a(f"| Window start (actual) | {ws} |")
        a(f"| Window end (actual) | {we} |")
    a("")
    if total < MIN_TOTAL_FOR_VERDICT:
        a(f"> **Sample too small** — only {total} events (threshold: {MIN_TOTAL_FOR_VERDICT}).")
        a("> Verdict below is inconclusive regardless of observed ratios.")
    a("")
    a("---")
    a("")

    # ── Section 2: Source distribution ───────────────────────────────────────
    a("## 2. Resolution Source Distribution")
    a("")
    a("| resolution_source | n | % of total |")
    a("|---|---|---|")
    for r in results["q2"]:
        a(f"| `{r['resolution_source']}` | {r['n']} | {_pct(r['n'], total)} |")
    a("")
    a("---")
    a("")

    # ── Section 3: Shadow-pin breakdown ──────────────────────────────────────
    a("## 3. Shadow-Pin Observation (non-`pinned_onboard_session` wins)")
    a("")
    a("Masking candidates are rows where `pin_entry_present=true` **and** `pin_fingerprint_match=true` — "
      "the pin would have resolved the same agent but token ordering shadowed it.")
    a("")
    a("| source | n | pin_present | present+match | present+mismatch | pin_absent | shadow_not_run |")
    a("|---|---|---|---|---|---|---|")
    for r in results["q3"]:
        n = r["n"]
        a(f"| `{r['resolution_source']}` | {n} "
          f"| {r['pin_present']} ({_pct(r['pin_present'], n)}) "
          f"| **{r['present_and_match']}** ({_pct(r['present_and_match'], n)}) "
          f"| {r['present_but_mismatch']} ({_pct(r['present_but_mismatch'], n)}) "
          f"| {r['pin_absent']} ({_pct(r['pin_absent'], n)}) "
          f"| {r['shadow_not_run']} ({_pct(r['shadow_not_run'], n)}) |")
    a("")
    a("---")
    a("")

    # ── Section 4: Pin-age-bucketed match rate ────────────────────────────────
    a("## 4. Pin-Age-Bucketed Match Rate (continuity_token wins, shadow ran)")
    a("")
    a("Buckets span quarter-TTL intervals (TTL=1800s, bucket width=450s). "
      "A high match rate in the 0–1499s band — where the pin is still viable — is the bleed signal.")
    a("")
    if results["q4"]:
        a("| age_bucket | n | pin_match | pin_mismatch | match_rate |")
        a("|---|---|---|---|---|")
        for r in results["q4"]:
            a(f"| {r['age_bucket']} | {r['n']} | {r['pin_match']} | {r['pin_mismatch']} | {r['match_pct']}% |")
    else:
        a("_No continuity_token wins with shadow pin data found in this window._")
    a("")
    a("---")
    a("")

    # ── Section 5: Token-age × pin-state cross-tab ───────────────────────────
    a("## 5. Token-Age × Pin-State Cross-Tab (continuity_token wins)")
    a("")
    a("If bleed is active, sessions with a fresh token (token<30m) should skew toward "
      "`pin_present=true, pin_match=true` because the pin is still alive and would resolve the same agent.")
    a("")
    if results["q5"]:
        a("| token_age_bucket | pin_present | pin_match | n |")
        a("|---|---|---|---|")
        for r in results["q5"]:
            a(f"| {r['token_age_bucket']} | {r['pin_present']} | {r['pin_match']} | {r['n']} |")
    else:
        a("_No continuity_token wins with token_age data found in this window._")
    a("")
    a("---")
    a("")

    # ── Verdict ───────────────────────────────────────────────────────────────
    a("## Verdict")
    a("")

    # Compute masking candidate count from Q3
    masking_candidates = 0
    ct_rows = [r for r in results["q3"] if r["resolution_source"] == "continuity_token"]
    ct_present_and_match = ct_rows[0]["present_and_match"] if ct_rows else 0
    ct_n = ct_rows[0]["n"] if ct_rows else 0

    if total < MIN_TOTAL_FOR_VERDICT:
        verdict = "INCONCLUSIVE — sample too small"
        verdict_detail = (
            f"Only {total} `identity_resolution_observed` events in the 7-day window "
            f"(threshold for a non-inconclusive verdict: ≥{MIN_TOTAL_FOR_VERDICT}). "
            "Cannot distinguish masking from noise at this sample size."
        )
    elif ct_present_and_match >= MASKING_CANDIDATE_THRESHOLD:
        verdict = "SUPPORTED"
        verdict_detail = (
            f"{ct_present_and_match} of {ct_n} continuity_token wins had "
            f"`pin_entry_present=true` AND `pin_fingerprint_match=true` "
            f"({_pct(ct_present_and_match, ct_n)} of token wins, "
            f"threshold: ≥{MASKING_CANDIDATE_THRESHOLD}). "
            "The pin would have resolved the same agent in these cases, confirming that "
            "resolution ordering — not pin expiry — is causing the fall-through."
        )
    elif ct_n > 0 and ct_present_and_match == 0:
        # Check if pin_absent dominates (clean expiry explanation)
        ct_absent = ct_rows[0]["pin_absent"] if ct_rows else 0
        if ct_absent > ct_n * 0.7:
            verdict = "NOT SUPPORTED (pin expiry explains the data)"
            verdict_detail = (
                f"Of {ct_n} continuity_token wins, {ct_absent} ({_pct(ct_absent, ct_n)}) "
                "had `pin_entry_present=false` — the pin had already expired before the token won. "
                "Zero masking candidates observed. Resolution ordering is not the proximate cause."
            )
        else:
            verdict = "NOT SUPPORTED"
            verdict_detail = (
                f"Zero masking candidates (pin_present+match) among {ct_n} continuity_token wins "
                f"(threshold: ≥{MASKING_CANDIDATE_THRESHOLD}). "
                "The data does not support the masking hypothesis."
            )
    else:
        verdict = "INCONCLUSIVE — insufficient continuity_token data"
        verdict_detail = (
            f"Only {ct_n} continuity_token wins observed in the window. "
            f"Masking candidates: {ct_present_and_match}. "
            f"Need ≥{MASKING_CANDIDATE_THRESHOLD} candidates or a clear zero to call the verdict."
        )

    a(f"**{verdict}**")
    a("")
    a(verdict_detail)
    a("")
    a(f"Thresholds: ≥{MASKING_CANDIDATE_THRESHOLD} `present+match` candidates among token wins = "
      f"hypothesis supported; ≥{MIN_TOTAL_FOR_VERDICT} total events = non-inconclusive window.")
    a("")
    a("### Interpretation Key")
    a("")
    a("| Observation | Meaning |")
    a("|---|---|")
    a("| `pin_entry_present=false` on token wins | Pin expired before resolution — clean explanation, no masking |")
    a("| `pin_entry_present=true, pin_match=false` on token wins | Fingerprint changed (IP/UA shift) — pin pointed stale identity |")
    a("| `pin_entry_present=true, pin_match=true` on token wins | **Masking** — pin viable but token won due to ordering |")
    a("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--days", type=int, default=7, help="Lookback window in days (default: 7)")
    p.add_argument("--db", default=DEFAULT_DB_URL, help="PostgreSQL connection URL")
    p.add_argument(
        "--output",
        help="Write report to this file (default: print to stdout). "
             "Suffix .md recommended.",
    )
    p.add_argument(
        "--json",
        dest="json_output",
        help="Also dump raw query results as JSON to this file.",
    )
    return p.parse_args()


async def main() -> None:
    args = parse_args()

    end_ts = datetime.now(timezone.utc)
    start_ts = end_ts - timedelta(days=args.days)

    print(f"Connecting to {args.db} …", file=sys.stderr)
    print(f"Window: {start_ts.isoformat()} → {end_ts.isoformat()}", file=sys.stderr)

    results = await run_queries(args.db, start_ts, end_ts)

    if args.json_output:
        Path(args.json_output).write_text(json.dumps(results, default=str, indent=2))
        print(f"Raw results written to {args.json_output}", file=sys.stderr)

    report = build_report(results, start_ts, end_ts, args.days)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    asyncio.run(main())

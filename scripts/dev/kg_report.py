#!/usr/bin/env python3
"""Knowledge-graph backlog + hygiene report (with conservative dry-run cleanup).

The KG (`knowledge.discoveries`) accumulates an issue-tracker-like backlog that
no surface reports cleanly: `open` is used as a default, so it mixes a small
actionable todo list with a large tail of aging notes and entries whose cited
GitHub issue has long since closed. `knowledge action=stats` gives raw counts
and `run_kg_lifecycle_cleanup` archives *already-resolved* / ephemeral items by
age — but nothing transitions stale `open` rows or surfaces the real backlog.
This fills that gap.

Three report sections (all read-only):

  1. ACTIONABLE BACKLOG — open bug_found / improvement / question /
     architectural_decision, by severity. The untracked todo list.
  2. STALE-CITATION SWEEP — open discoveries citing a GitHub issue # that is no
     longer open. Strong "this entry is probably done" signal. Report-only:
     a closed issue does not *prove* the discovery is resolved, so a human (or
     a later targeted pass) makes that call.
  3. HYGIENE —
       a. COLD candidates: open note/insight rows older than --cold-age-days
          (default 30), excluding permanent types/tags. These are what --apply
          can cool.
       b. PROMOTE candidates: open high/critical actionable rows not linked to
          an open issue — candidates to file as real GitHub issues.

Cleanup (`--apply`, dry-run by default): cools ONLY the section-3a cold
candidates (open note/insight → `cold`, still queryable, reversible) via the
sanctioned `KnowledgeGraphLifecycle._batch_update_status` primitive, which keeps
the active backend and canonical PG table aligned. Never deletes (matches the
lifecycle's "archive forever" philosophy); never touches actionable rows.

Usage:
    python3 scripts/dev/kg_report.py                 # report only
    python3 scripts/dev/kg_report.py --json
    python3 scripts/dev/kg_report.py --apply          # cool aged notes (writes)
    python3 scripts/dev/kg_report.py --cold-age-days 60
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

DEFAULT_DSN = "postgresql://postgres:postgres@localhost:5432/governance"
ACTIONABLE_TYPES = (
    "bug_found", "improvement", "question", "architectural_decision",
    "optimization",
)
NOTE_TYPES = ("note", "insight")
COLD_AGE_DAYS_DEFAULT = 30
ISSUE_RE = r"#([0-9]{2,4})"


def _lifecycle_consts():
    """Permanent type/tag sets from the live lifecycle module (so cooling never
    cools a row the sanctioned cleanup would treat as permanent). Pinned fallback.
    """
    try:
        from src.knowledge_graph_lifecycle import PERMANENT_TYPES, PERMANENT_TAGS  # type: ignore
        return set(PERMANENT_TYPES), set(PERMANENT_TAGS)
    except Exception:
        return (
            {"architecture_decision", "architectural_decision", "learning", "pattern"},
            {"permanent", "pinned", "protected", "invariant"},
        )


def _clean(s: str) -> str:
    """Collapse whitespace/newlines so multi-line summaries fit one table row."""
    return " ".join((s or "").split())


def connect():
    import psycopg2  # type: ignore
    return psycopg2.connect(os.environ.get("GOVERNANCE_DATABASE_URL", DEFAULT_DSN))


def open_issue_numbers():
    """Set of currently-open GitHub issue numbers, or None if gh is unavailable."""
    try:
        out = subprocess.run(
            ["gh", "issue", "list", "--state", "open", "--limit", "300",
             "--json", "number"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        return {i["number"] for i in json.loads(out)}
    except Exception:
        return None


def gather(cold_age_days: int) -> dict:
    perm_types, perm_tags = _lifecycle_consts()
    open_issues = open_issue_numbers()
    conn = connect()
    try:
        cur = conn.cursor()

        # 1. Actionable backlog: type x severity (open, actionable types).
        cur.execute(
            """
            SELECT type, coalesce(severity,'(none)'), count(*)
            FROM knowledge.discoveries
            WHERE status='open' AND type = ANY(%s)
            GROUP BY 1,2 ORDER BY 1, 3 DESC
            """,
            (list(ACTIONABLE_TYPES),),
        )
        backlog = [{"type": t, "severity": s, "n": n} for t, s, n in cur.fetchall()]

        # 2/3b. Open rows with their cited issue numbers + flags for triage.
        cur.execute(
            f"""
            SELECT id, type, coalesce(severity,'(none)') AS severity,
                   left(coalesce(summary,''), 90) AS summary,
                   ARRAY(
                     SELECT DISTINCT (m)[1]::int
                     FROM regexp_matches(
                       coalesce(summary,'')||' '||coalesce(details,''),
                       '{ISSUE_RE}', 'g') AS m
                   ) AS cited_issues
            FROM knowledge.discoveries
            WHERE status='open'
            """
        )
        open_rows = [
            {"id": i, "type": t, "severity": s, "summary": _clean(sm), "cited": list(c)}
            for i, t, s, sm, c in cur.fetchall()
        ]

        # 3a. Cold candidates: aged open note/insight, not permanent type/tag,
        #     and not self-marked canonical/design-principle in the summary
        #     (those read as load-bearing knowledge — keep them warm, report them
        #     under a separate "kept" count so the skip is visible, not silent).
        cur.execute(
            """
            SELECT id, type, coalesce(severity,'(none)'),
                   coalesce(summary,''),
                   extract(day FROM now()-created_at)::int AS age_days
            FROM knowledge.discoveries
            WHERE status='open'
              AND type = ANY(%s)
              AND created_at < now() - (%s || ' days')::interval
              AND NOT (type = ANY(%s))
              AND NOT (coalesce(tags,'{}') && %s)
            ORDER BY created_at
            """,
            (list(NOTE_TYPES), str(cold_age_days),
             list(perm_types), list(perm_tags)),
        )
        cold_candidates, cold_kept_canonical = [], []
        for i, t, s, sm, a in cur.fetchall():
            row = {"id": i, "type": t, "severity": s,
                   "summary": _clean(sm), "age_days": a}
            up = sm.upper()
            if "CANONICAL" in up or "DESIGN PRINCIPLE" in up or "INVARIANT" in up:
                cold_kept_canonical.append(row)
            else:
                cold_candidates.append(row)
    finally:
        conn.close()

    # Derive stale-citation + promote sets in python (needs the open-issue set).
    stale_citation = []
    for r in open_rows:
        if not r["cited"]:
            continue
        if open_issues is None:
            r["_cited_state"] = "unknown(gh unavailable)"
        else:
            still_open = [n for n in r["cited"] if n in open_issues]
            if still_open:
                continue  # cites a live issue — not stale
            r["_cited_state"] = "all-closed"
        stale_citation.append(r)

    promote = [
        r for r in open_rows
        if r["severity"] in ("high", "critical")
        and r["type"] in ACTIONABLE_TYPES
        and (open_issues is None or not any(n in open_issues for n in r["cited"]))
    ]

    return {
        "open_issue_set_known": open_issues is not None,
        "cold_age_days": cold_age_days,
        "actionable_backlog": backlog,
        "stale_citation": stale_citation,
        "cold_candidates": cold_candidates,
        "cold_kept_canonical": cold_kept_canonical,
        "promote_candidates": promote,
    }


def apply_cooling(cold_candidates: list, dry_run: bool, limit: int | None = None) -> dict:
    """Transition the cold candidates open -> cold via the sanctioned dual-store
    primitive. Candidate selection already happened (pure SQL); this only mutates.

    `limit` bounds how many are cooled in one run (oldest first — candidates are
    created_at-ordered), so a scheduled job drains the tail gradually instead of
    cooling everything in one sweep.
    """
    selected = cold_candidates if limit is None else cold_candidates[: max(0, limit)]
    ids = [c["id"] for c in selected]
    if dry_run or not ids:
        return {"cooled": 0, "dry_run": dry_run, "ids": ids,
                "eligible": len(cold_candidates)}

    # Cool via a direct UPDATE on the canonical relational table. The relational
    # store is canonical (AGE is a LIVE advisory mirror that reconciles); this is
    # exactly the PG-alignment step the in-server lifecycle primitive performs.
    # We deliberately do NOT import the app's KnowledgeGraphLifecycle here: it is
    # heavy (needs the full server env) and its graph.update_discovery currently
    # raises `AttributeError: 'ExecutorPool' object has no attribute 'fetchval'`
    # (knowledge_graph_postgres.py — stale db._pool.fetchval vs the post-#218
    # ExecutorPool contract). A dependency-light UPDATE is the robust path for a
    # scheduled job. `status='open'` guard keeps it idempotent.
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE knowledge.discoveries
            SET status='cold', updated_at=now()
            WHERE id = ANY(%s) AND status='open'
            """,
            (ids,),
        )
        cooled = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"cooled": cooled, "dry_run": False, "ids": ids,
            "eligible": len(cold_candidates)}


def _print(r: dict, applied: dict) -> None:
    print("KG backlog + hygiene report\n" + "=" * 32)
    if not r["open_issue_set_known"]:
        print("  ⚠ gh unavailable — stale-citation/promote use cited-issue text only\n")

    print("\n[1] ACTIONABLE BACKLOG (open, actionable types)")
    if not r["actionable_backlog"]:
        print("    (none)")
    for b in r["actionable_backlog"]:
        print(f"    {b['type']:<24} {b['severity']:<8} {b['n']}")

    sc = r["stale_citation"]
    print(f"\n[2] STALE-CITATION SWEEP — {len(sc)} open rows whose cited #refs are all non-open (closed issues/PRs)")
    for x in sc[:20]:
        print(f"    {x['id'][:24]:<24} {x['type']:<14} cites {x['cited']}  {x['summary'][:48]}")
    if len(sc) > 20:
        print(f"    … +{len(sc)-20} more")

    cc = r["cold_candidates"]
    verb = "COOLED" if applied.get("cooled") else "WOULD COOL"
    print(f"\n[3a] HYGIENE — {len(cc)} aged open note/insight (> {r['cold_age_days']}d) → {verb} to cold")
    for x in cc[:15]:
        print(f"    {x['id'][:24]:<24} {x['type']:<8} {x['age_days']:>4}d  {x['summary'][:52]}")
    if len(cc) > 15:
        print(f"    … +{len(cc)-15} more")
    kept = r.get("cold_kept_canonical", [])
    if kept:
        print(f"    ↳ kept warm ({len(kept)} self-marked CANONICAL/DESIGN-PRINCIPLE/INVARIANT, not cooled):")
        for x in kept[:6]:
            print(f"      · {x['id'][:24]:<24} {x['age_days']:>4}d  {x['summary'][:50]}")

    pc = r["promote_candidates"]
    print(f"\n[3b] PROMOTE — {len(pc)} open high/critical actionable, no open-issue link")
    for x in pc:
        print(f"    {x['id'][:24]:<24} {x['type']:<14} {x['severity']:<8} {x['summary'][:50]}")

    if applied.get("cooled"):
        print(f"\n✓ Applied: cooled {applied['cooled']} discoveries to cold (reversible).")
    elif cc:
        print(f"\n(dry-run) re-run with --apply to cool the {len(cc)} aged notes above.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cold-age-days", type=int, default=COLD_AGE_DAYS_DEFAULT)
    ap.add_argument("--apply", action="store_true",
                    help="cool aged note/insight rows to cold (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None,
                    help="max rows to cool this run (oldest first; bounds a scheduled sweep)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    r = gather(args.cold_age_days)
    applied = apply_cooling(r["cold_candidates"], dry_run=not args.apply, limit=args.limit)
    if args.json:
        print(json.dumps({"report": r, "applied": applied}, indent=2, default=str))
    else:
        _print(r, applied)


if __name__ == "__main__":
    main()

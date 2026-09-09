#!/usr/bin/env python3
"""List dialectic reviews that ended with a standing objection nobody answered.

A reviewer that refuses a thesis is the dialectic working. Measured over 30
days, the reviewer returned ``agrees=false`` on 61 of 95 real syntheses, and
every session that ended ``failed`` had a reviewer antithesis on the record --
none failed for want of a reviewer. What fails is the lifecycle around it: the
requesting agent is a short-lived process, so by the time the reviewer objects
and proposes conditions it has exited, the self-clear guard correctly refuses
anyone answering in its place, and the session ages out.

The conditions survive that. Measured 2026-09-09: 55 of 56 sessions that ended
``failed`` in 60 days carry reviewer conditions, averaging 4.3 each -- roughly
240 specific, actionable conditions. And nothing reads them. There is no HTTP
route, no dashboard panel, no digest, and no operator queue keyed on any of
this; they are written to Postgres and never seen.

So this does not try to make sessions resolve. Chasing that means either
keeping agents alive for hours or weakening the guard that stops an agent
clearing its own objection, and both are worse than the problem. It makes the
OUTPUT land: one list, newest first, that an operator surface can print when
there is something to say and stay silent when there is not.

Read-only. Prints text by default, ``--json`` for machine use.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

DEFAULT_DSN = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

# ⛔Canary/probe traffic is partitioned on the agent LABEL, never on
# `trigger_source`. Measured 2026-09-09: trigger_source is 'manual' for all 51
# canary sessions AND for 127 non-canary ones, so it has no discriminating
# power at all. The label prefix is the only reliable partition.
#
# ⛔The join to core.agents is a LEFT JOIN with COALESCE, not an inner join.
# 12 sessions reference a paused_agent_id with no row in core.agents; an inner
# join would silently drop real unresolved work while looking like a filter.
#
# "Unresolved" is `status <> 'resolved'`, NOT `status = 'active'`. There are no
# active sessions at all -- every session reaches a terminal status -- so
# keying on 'active' returns an empty list forever and reads as "all clear".
QUERY = """
WITH unresolved AS (
    SELECT s.session_id, s.topic, s.reason, s.reviewer_agent_id,
           s.awaiting_facilitation, s.status, s.phase, s.created_at, s.updated_at
    FROM core.dialectic_sessions s
    LEFT JOIN core.agents pa ON pa.id = s.paused_agent_id
    WHERE s.status <> 'resolved'
      AND COALESCE(pa.label, '') NOT LIKE 'canary_dialectic%%'
      AND s.created_at >= now() - interval '1 day' * %(window_days)s
)
SELECT u.session_id,
       COALESCE(NULLIF(u.topic, ''), u.reason, '') AS topic,
       COALESCE(ra.label, u.reviewer_agent_id, '') AS reviewer,
       u.awaiting_facilitation,
       u.status,
       u.phase,
       u.created_at,
       u.updated_at,
       COALESCE(r.reasoning, '') AS reviewer_reasoning,
       CASE
         WHEN jsonb_typeof(r.proposed_conditions) = 'array'
           THEN ARRAY(SELECT jsonb_array_elements_text(r.proposed_conditions))
         ELSE ARRAY[]::text[]
       END AS conditions
FROM unresolved u
LEFT JOIN core.agents ra ON ra.id = u.reviewer_agent_id
JOIN LATERAL (
    SELECT dm.agrees, dm.proposed_conditions, dm.reasoning
    FROM core.dialectic_messages dm
    WHERE dm.session_id = u.session_id
      AND dm.message_type = 'synthesis'
      AND dm.agent_id = u.reviewer_agent_id
    ORDER BY dm.timestamp DESC, dm.message_id DESC
    LIMIT 1
) r ON r.agrees IS FALSE
ORDER BY u.created_at DESC
"""


def fetch(dsn: str, window_days: int) -> List[Dict[str, Any]]:
    import psycopg2
    import psycopg2.extras

    with psycopg2.connect(dsn) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(QUERY, {"window_days": window_days})
            return [dict(r) for r in cur.fetchall()]


def render(rows: List[Dict[str, Any]], max_conditions: int) -> str:
    """One block per review. Conditions are the point, so they are never elided
    silently -- a truncated list says how many it dropped."""
    if not rows:
        return ""

    out = [f"{len(rows)} dialectic review(s) ended with a standing objection nobody answered.", ""]
    for r in rows:
        topic = (r["topic"] or "(no topic)").strip().replace("\n", " ")
        if len(topic) > 140:
            topic = topic[:137] + "..."
        age_days = (r["updated_at"] - r["created_at"]).days if r["updated_at"] else 0
        out.append(f"  {r['session_id']}  [{r['status']}, {age_days}d]  reviewer={r['reviewer']}")
        out.append(f"    {topic}")
        conds = r["conditions"] or []
        for c in conds[:max_conditions]:
            c = " ".join(str(c).split())
            out.append(f"      - {c if len(c) <= 160 else c[:157] + '...'}")
        if len(conds) > max_conditions:
            out.append(f"      ... and {len(conds) - max_conditions} more condition(s)")
        if not conds:
            out.append("      (reviewer objected but proposed no conditions)")
        out.append("")
    out.append("Reopen one with: dialectic(action='reassign', session_id=..., new_reviewer_id=...)")
    return "\n".join(out)


def render_comment(row: Dict[str, Any]) -> str:
    """One review as a markdown block an operator can paste onto a PR.

    ⛔RENDER ONLY. There is deliberately no --apply and no posting path.
    Two measured reasons, not squeamishness:

    1. The operator's standing rule is that automated finders are REPORT-ONLY,
       and it names public commenting explicitly. This content is LLM-composed
       reviewer prose, which puts it squarely in the finder's class.
    2. PR comments are the one GitHub write surface nothing in this fleet
       lints. The local pre-PR guard matches only `pr create|edit` and the CI
       scope job reads only the PR body, so a comment can carry deliberation
       register, operator paths and agent UUIDs into a public repo unchecked.

    Pasting costs the operator one keystroke and keeps a human in the path
    where no guard exists. Only 15 of 63 unresolved reviews name a PR at all,
    so an automated poster would be high-risk machinery for a quarter of cases.
    """
    conds = row.get("conditions") or []
    lines = [
        "## Dialectic review: standing conditions",
        "",
        f"Review session `{row['session_id']}` ended unresolved with a standing",
        f"objection from `{row['reviewer']}`. The conditions below were never",
        "answered, and are reproduced here so the merge decision can see them.",
        "",
    ]
    if conds:
        lines += [f"{i}. {' '.join(str(c).split())}" for i, c in enumerate(conds, 1)]
    else:
        lines.append("_The reviewer objected but proposed no conditions._")
    reasoning = " ".join(str(row.get("reviewer_reasoning") or "").split())
    if reasoning:
        lines += ["", "<details><summary>Reviewer reasoning</summary>", "", reasoning, "", "</details>"]
    lines += ["", "This is a record of an unresolved review, not an approval or a block."]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dsn", default=DEFAULT_DSN)
    ap.add_argument("--window-days", type=int, default=90,
                    help="How far back to look (default 90)")
    ap.add_argument("--max-conditions", type=int, default=4,
                    help="Conditions shown per review before truncating (default 4)")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    ap.add_argument("--session-id",
                    help="Render just this session as a paste-ready PR comment. "
                         "Renders only -- this tool never posts anywhere.")
    args = ap.parse_args()

    try:
        rows = fetch(args.dsn, args.window_days)
    except Exception as exc:  # noqa: BLE001 -- a reporting tool reports, never raises
        print(f"dialectic_unresolved: query failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 2

    if args.session_id:
        match = [r for r in rows if r["session_id"] == args.session_id]
        if not match:
            print(f"dialectic_unresolved: no unresolved review {args.session_id!r} "
                  f"in the last {args.window_days} days", file=sys.stderr)
            return 1
        print(render_comment(match[0]))
        return 0

    if args.json:
        print(json.dumps(
            {"count": len(rows), "window_days": args.window_days, "reviews": rows},
            default=str, indent=2,
        ))
        return 0

    text = render(rows, args.max_conditions)
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

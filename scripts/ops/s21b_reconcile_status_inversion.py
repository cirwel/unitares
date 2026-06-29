#!/usr/bin/env python3
"""Reconcile core.identities.status against core.agents.status.

S21-b §4. Verifier observed (review pass-2, 2026-04-27):
  - 67 rows where core.identities.status='active' AND core.agents.status='archived'
    (auth-relevant: with S21-b §2 status-gate, identity slips through as active
    while the dict / agents view says archived)
  - 88 rows the other direction (identity archived, agent active)
  - 16 deleted/archived

The two sides of an inversion encode different operator decisions:
  - identity='archived' AND agent='active' (88) — usually an old identity
    archive that was never propagated to agents; un-archiving via "trust
    agents" would re-open intentional archives
  - identity='active' AND agent='archived' (67) — Vigil/operator archived
    the agent but identity wasn't updated; archiving identity matches operator
    intent

So this script does not pick a default direction. Run with --apply requires
an explicit --only flag.

Usage:
    python3 scripts/ops/s21b_reconcile_status_inversion.py
        # dry run, prints breakdown + sample

    python3 scripts/ops/s21b_reconcile_status_inversion.py --apply --only active-to-archived
        # safest direction: identity='active' & agent='archived' → archive identity (67 rows)

    python3 scripts/ops/s21b_reconcile_status_inversion.py --apply --only archived-to-active
        # identity='archived' & agent='active' → activate identity (88 rows). USE WITH CARE.

The 16 deleted/archived rows cannot be reconciled here — core.agents.status
CHECK constraint forbids 'deleted'. Handle separately if needed.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))


INVERSION_QUERY = """
SELECT i.agent_id, i.status AS identity_status, a.status AS agent_status,
       i.created_at, a.updated_at AS agent_updated_at
FROM core.identities i
JOIN core.agents a ON a.id = i.agent_id
WHERE i.status IS DISTINCT FROM a.status
ORDER BY a.updated_at DESC NULLS LAST, i.created_at DESC
"""

# Review pass-2 (code-reviewer #3): the INNER JOIN above silently excludes
# identities with no matching core.agents row. Those are the H12 ghost
# identities (PATH 3 minted via upsert_identity but no create_agent call) —
# the most operationally interesting class. Surface them informationally.
ORPHAN_IDENTITY_QUERY = """
SELECT i.agent_id, i.status, i.created_at, i.parent_agent_id, i.spawn_reason
FROM core.identities i
LEFT JOIN core.agents a ON a.id = i.agent_id
WHERE a.id IS NULL
ORDER BY i.created_at DESC
"""


def _filter(rows, only: str | None):
    if not only:
        return rows
    if only == "active-to-archived":
        return [r for r in rows if r["identity_status"] == "active" and r["agent_status"] == "archived"]
    if only == "archived-to-active":
        return [r for r in rows if r["identity_status"] == "archived" and r["agent_status"] == "active"]
    raise ValueError(f"unknown --only filter: {only!r}")


async def _run(apply: bool, only: str | None) -> int:
    from src.db import get_db, init_db

    await init_db()
    db = get_db()

    async with db.acquire() as conn:
        rows = [dict(r) for r in await conn.fetch(INVERSION_QUERY)]
        orphans = [dict(r) for r in await conn.fetch(ORPHAN_IDENTITY_QUERY)]

    if orphans and not apply:
        # Informational only — the apply path does not touch orphan rows.
        # H12 archival policy is item-4 scope; this surface is for visibility.
        active_orphans = sum(1 for o in orphans if o["status"] == "active")
        print(f"[orphan identities] {len(orphans)} core.identities rows have no core.agents peer "
              f"(of which {active_orphans} are status='active' — would still pass auth gate). "
              f"Out of scope for this script's --apply; see S21-b item 4.")

    rows = _filter(rows, only)

    by_pair: dict[tuple[str, str], int] = {}
    for r in rows:
        key = (r["identity_status"], r["agent_status"])
        by_pair[key] = by_pair.get(key, 0) + 1

    print(f"Found {len(rows)} status-inverted rows.")
    for (i_s, a_s), count in sorted(by_pair.items(), key=lambda kv: -kv[1]):
        print(f"  identities='{i_s}' / agents='{a_s}': {count}")

    if not rows:
        return 0

    if not apply:
        print()
        print("Dry run — re-run with --apply to write.")
        print("Sample (first 10):")
        for r in rows[:10]:
            uuid_short = (r["agent_id"] or "")[:12]
            print(f"  {uuid_short}  i={r['identity_status']}  a={r['agent_status']}  agent_updated_at={r['agent_updated_at']}")
        return 0

    written = 0
    async with db.acquire() as conn:
        for r in rows:
            agent_uuid = r["agent_id"]
            new_status = r["agent_status"]
            await conn.execute(
                """
                UPDATE core.identities
                SET status = $2, updated_at = now()
                WHERE agent_id = $1
                """,
                agent_uuid, new_status,
            )
            written += 1

    print(f"Updated {written} core.identities rows to match core.agents.status.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    parser.add_argument(
        "--only",
        choices=["active-to-archived", "archived-to-active"],
        default=None,
        help="Restrict to one inversion direction (required with --apply)",
    )
    args = parser.parse_args()
    if args.apply and not args.only:
        parser.error(
            "--apply requires --only {active-to-archived|archived-to-active} "
            "— see module docstring for which direction matches operator intent"
        )
    return asyncio.run(_run(apply=args.apply, only=args.only))


if __name__ == "__main__":
    raise SystemExit(main())

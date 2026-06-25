#!/usr/bin/env python3
"""Stage 0 — reproducible inventory of the exogenous anchor budget.

Tiers ``audit.outcome_events`` by ``verification_source`` using the canonical
mapping in ``src.grounding.outcome_anchors`` (Invariant 4: self-referential and
unknown-provenance rows are EXCLUDED), and reports the externally-anchored label
budget that bounds B's falsifiability gate.

This is the command form of the roadmap's Appendix-A recon — run it to see how
the budget has grown, and to catch regressions (e.g. a new verification_source
that is silently EXCLUDED, or the test_failed wiring gap closing).

Usage:
    PYTHONPATH=. python3 scripts/analysis/outcome_anchor_inventory.py
    PYTHONPATH=. python3 scripts/analysis/outcome_anchor_inventory.py --db-url ...
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    import psycopg2
except ImportError:
    print("ERROR: psycopg2 not installed. Run: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)

from src.grounding.outcome_anchors import AnchorTier, tier_for_source  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-url", default="postgresql://postgres:postgres@localhost:5432/governance")
    args = ap.parse_args()

    conn = psycopg2.connect(args.db_url)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COALESCE(verification_source, '(null)'),
                   count(*),
                   sum(CASE WHEN is_bad THEN 1 ELSE 0 END)
            FROM audit.outcome_events
            GROUP BY 1 ORDER BY 2 DESC
            """
        )
        rows = cur.fetchall()

    print(f"{'verification_source':<32}{'tier':<20}{'count':>9}{'bad':>8}")
    print("-" * 69)
    by_tier = {t: [0, 0] for t in AnchorTier}
    for source, count, bad in rows:
        bad = int(bad or 0)
        src = None if source == "(null)" else source
        tier = tier_for_source(src)
        by_tier[tier][0] += count
        by_tier[tier][1] += bad
        print(f"{source:<32}{tier.value:<20}{count:>9}{bad:>8}")

    print("\nBy tier:")
    for t in AnchorTier:
        c, b = by_tier[t]
        print(f"  {t.value:<20} count={c:>8}  bad={b:>6}")

    trusted_c, trusted_bad = by_tier[AnchorTier.TRUSTED_EXTERNAL]
    soft_c, soft_bad = by_tier[AnchorTier.SOFT_SELF_ATTESTED]
    excl_c, _ = by_tier[AnchorTier.EXCLUDED]
    total = trusted_c + soft_c + excl_c
    print(f"\nExogenous anchor budget (TRUSTED_EXTERNAL): {trusted_c} events, {trusted_bad} bad")
    print(f"  (+soft self-attested if opted in: +{soft_c} events, +{soft_bad} bad)")
    if total:
        print(f"Self-referential / unknown EXCLUDED: {excl_c}/{total} "
              f"({100*excl_c/total:.0f}%) — Invariant 4 in effect")

    # Gap check: is the most objective bad anchor (a failing test) flowing?
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM audit.outcome_events
            WHERE outcome_type = 'test_failed' AND verification_source = 'external_signal'
            """
        )
        test_failed_external = cur.fetchone()[0]
    flag = "  ⚠ WIRING GAP — CI/test failures not reaching outcome_events" if test_failed_external < 10 else ""
    print(f"\nexternally-verified test_failed events: {test_failed_external}{flag}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

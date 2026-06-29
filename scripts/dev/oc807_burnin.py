#!/usr/bin/env python3
"""#807 tier-honesty over-claim burn-in — size the prefix-echo over-claim subset.

Canonical query for issue #807 ("Tokenless prefix-echo over-claims
strong/caller_asserted across an unverifiable process boundary"). PR #1223
added `proof_origin` + `csid_transport_injected` to `identity_resolution_observed`
so the over-claim is *countable* without changing any tier or write gate. This
script is that count, repeatable — re-run it after the strict-window accrues
~1 week of data to make the (a)/(c) tier-policy call decision-ready.

The structural fact it surfaces (verified 2026-06-29): **tier is keyed purely on
`resolution_source`** (`_STRONG_IDENTITY_SOURCES`), while `proof_origin` is
computed separately and does NOT feed tier. So the over-claim splits into two
separable halves, and live traffic diverges them:

  - caller_proven half (proof_origin == "caller_asserted"): transport-injection
    already down-rates the live majority to "server_inferred" — effectively
    honest for real drivers.
  - tier:strong half: still universally granted to the prefix-echo source,
    regardless of proof_origin.

The part that breaks the live write path under strict is the *tier* change
(source-keyed) — independent of proof_origin. This script reports both halves
so the cost split is explicit.

Strict flip: 2026-06-28 10:51Z. #1223 merge (telemetry live): 2026-06-29 01:17:46Z.
Rows before the merge have null proof_origin (the field did not exist yet), so
the default window starts at the merge.

Usage:
    python3 scripts/dev/oc807_burnin.py [--since 2026-06-29T01:17:46Z] [--json]
"""
from __future__ import annotations

import argparse
import json
import os

# Telemetry went live (proof_origin recorded) at the #1223 merge; older rows are null.
DEFAULT_SINCE = "2026-06-29T01:17:46Z"


def connect():
    import psycopg2  # type: ignore

    dsn = os.environ.get(
        "GOVERNANCE_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/governance",
    )
    return psycopg2.connect(dsn)


def _tier_sets() -> tuple[set, set]:
    """Authoritative strong/medium source sets, imported from the live module so
    the tier annotation can never drift from the real classifier (the exact drift
    class #807 is about). Falls back to a pinned copy if the import path is absent.
    """
    try:
        from src.services.identity_payloads import (  # type: ignore
            _STRONG_IDENTITY_SOURCES,
            _MEDIUM_IDENTITY_SOURCES,
        )
        return set(_STRONG_IDENTITY_SOURCES), set(_MEDIUM_IDENTITY_SOURCES)
    except Exception:
        strong = {
            "continuity_token", "client_session_id", "explicit_client_session_id",
            "explicit_client_session_id_scoped", "mcp_session_id", "x_session_id",
            "oauth_client_id", "agent_uuid_direct", "agent_uuid_direct_fastpath",
        }
        medium = {
            "x_client_id", "pinned_onboard_session", "context_mcp_session_id",
            "context_session_key",
        }
        return strong, medium


def _tier_of(source, strong, medium) -> str:
    if source in strong:
        return "strong"
    if source in medium:
        return "medium"
    return "weak"


def burn_in(since: str) -> dict:
    strong, medium = _tier_sets()
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT payload->>'resolution_source'                 AS source,
                   payload->>'proof_origin'                      AS proof_origin,
                   payload->>'csid_transport_injected'           AS transport_injected,
                   count(*)                                      AS n
            FROM audit.events
            WHERE event_type = 'identity_resolution_observed'
              AND ts > %s
            GROUP BY 1, 2, 3
            ORDER BY n DESC
            """,
            (since,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    breakdown = []
    total = 0
    caller_asserted = 0
    strong_resolves = 0
    strong_not_caller_proven = 0  # the over-claim: strong tier without caller proof
    for source, proof_origin, transport_injected, n in rows:
        tier = _tier_of(source, strong, medium)
        total += n
        if proof_origin == "caller_asserted":
            caller_asserted += n
        if tier == "strong":
            strong_resolves += n
            if proof_origin != "caller_asserted":
                strong_not_caller_proven += n
        breakdown.append({
            "resolution_source": source,
            "proof_origin": proof_origin,
            "transport_injected": transport_injected,
            "tier": tier,
            "n": n,
        })

    return {
        "since": since,
        "total_resolves": total,
        "caller_asserted": caller_asserted,
        "strong_tier_resolves": strong_resolves,
        "over_claim_strong_without_caller_proof": strong_not_caller_proven,
        "breakdown": breakdown,
    }


def _print_human(r: dict) -> None:
    print(f"#807 over-claim burn-in — resolves since {r['since']}\n")
    w = max((len(str(b["resolution_source"])) for b in r["breakdown"]), default=18)
    print(f"  {'resolution_source':<{w}}  {'proof_origin':<16}  {'inj':<5}  {'tier':<6}  n")
    print(f"  {'-'*w}  {'-'*16}  {'-'*5}  {'-'*6}  ---")
    for b in r["breakdown"]:
        print(
            f"  {str(b['resolution_source']):<{w}}  "
            f"{str(b['proof_origin']):<16}  {str(b['transport_injected']):<5}  "
            f"{b['tier']:<6}  {b['n']}"
        )
    print()
    print(f"  total resolves ................... {r['total_resolves']}")
    print(f"  caller_asserted (caller-proven) .. {r['caller_asserted']}")
    print(f"  strong-tier resolves ............. {r['strong_tier_resolves']}")
    print(f"  OVER-CLAIM (strong, not proven) .. {r['over_claim_strong_without_caller_proof']}")
    print()
    print("  Read: 'OVER-CLAIM' is the strong-tier label granted without caller proof")
    print("  (the tier ⊥ proof_origin half). 'caller_asserted' near zero means the")
    print("  caller_proven half is already honest in live traffic — see issue #807.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default=DEFAULT_SINCE,
                    help=f"ISO ts lower bound (default: #1223 merge {DEFAULT_SINCE})")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args()

    result = burn_in(args.since)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)


if __name__ == "__main__":
    main()

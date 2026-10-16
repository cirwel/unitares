#!/usr/bin/env python3
"""Inventory durable dialectic runtime effects that are not confirmed applied.

This command is intentionally read-only.  It exposes the state needed for an
operator to choose the existing ``dialectic(action='reconcile_effect')`` path;
it never claims, fences, replays, or acknowledges an effect itself.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

CONNECT_TIMEOUT_S = 5
STATEMENT_TIMEOUT_MS = 5000
ACTIONABLE_STATES = ("pending", "applying", "unknown_partial")

DEFAULT_DSN = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)

QUERY = """
SELECT e.session_id,
       e.state,
       e.attempt_count,
       e.owner_token,
       e.last_error,
       e.effect_required,
       e.pause_token,
       e.claimed_at,
       e.uncertain_at,
       e.created_at,
       e.updated_at,
       EXTRACT(EPOCH FROM (clock_timestamp() - e.updated_at))::bigint
           AS state_age_seconds,
       s.status AS session_status,
       s.phase AS session_phase,
       s.awaiting_facilitation,
       a.status AS agent_status,
       i.status AS identity_status,
       i.metadata->>'paused_at' AS current_pause_token
FROM coordination.dialectic_resolution_effects e
JOIN core.dialectic_sessions s ON s.session_id = e.session_id
LEFT JOIN core.agents a ON a.id = e.paused_agent_id
LEFT JOIN core.identities i ON i.agent_id = e.paused_agent_id
WHERE e.state = ANY(%(states)s::text[])
ORDER BY e.updated_at ASC, e.session_id ASC
"""


def _run_read_only(
    dsn: str, sql: str, params: Dict[str, Any]
) -> List[Dict[str, Any]]:
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(dsn, connect_timeout=CONNECT_TIMEOUT_S)
    try:
        # The server enforces the promise in this command's name.  A future
        # accidental UPDATE fails instead of turning an inventory into repair.
        conn.set_session(readonly=True)
        with conn:
            with conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(
                    "SET LOCAL statement_timeout = %s",
                    (STATEMENT_TIMEOUT_MS,),
                )
                cur.execute(sql, params)
                return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def fetch(dsn: str, states: Sequence[str]) -> List[Dict[str, Any]]:
    return _run_read_only(dsn, QUERY, {"states": list(states)})


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def json_safe(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _iso(value) for key, value in row.items()}


def pause_generation_state(row: Dict[str, Any]) -> str:
    """Describe live pause evidence without deciding whether work applied."""
    if not row.get("effect_required"):
        return "not_required"
    agent_status = row.get("agent_status")
    identity_status = row.get("identity_status")
    if agent_status is None or identity_status is None:
        return "identity_missing"
    if agent_status != identity_status:
        return "status_mismatch"
    if agent_status != "paused":
        return "not_paused"
    if row.get("current_pause_token") == row.get("pause_token"):
        return "matches_effect"
    return "pause_token_mismatch"


def _age(seconds: Any) -> str:
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return "unknown"
    if total >= 86_400:
        return f"{total // 86_400}d"
    if total >= 3_600:
        return f"{total // 3_600}h"
    if total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


def render(rows: Sequence[Dict[str, Any]]) -> str:
    if not rows:
        return "No non-applied dialectic resolution effects."

    lines = [f"{len(rows)} non-applied dialectic resolution effect(s):"]
    for row in rows:
        owner = row.get("owner_token") or "(none)"
        lines.extend([
            "",
            (
                f"  {row.get('session_id')}  [{row.get('state')}, "
                f"age={_age(row.get('state_age_seconds'))}, "
                f"attempts={int(row.get('attempt_count') or 0)}]"
            ),
            f"    owner_token={owner}",
            (
                "    session="
                f"{row.get('session_status')}/{row.get('session_phase')} "
                f"awaiting_facilitation={bool(row.get('awaiting_facilitation'))}"
            ),
            (
                "    paused_agent="
                f"agent:{row.get('agent_status') or 'missing'} "
                f"identity:{row.get('identity_status') or 'missing'} "
                f"generation:{pause_generation_state(row)}"
            ),
        ])
        if row.get("last_error"):
            error = " ".join(str(row["last_error"]).split())
            lines.append(
                f"    last_error={error if len(error) <= 240 else error[:237] + '...'}"
            )
    lines.extend([
        "",
        "Inspect each row with dialectic(action='reconcile_effect', session_id=...).",
        "Do not replay applying or unknown_partial effects; follow the operator runbook.",
    ])
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only inventory of pending, applying, and unknown-partial "
            "dialectic resolution effects."
        )
    )
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument(
        "--state",
        action="append",
        choices=ACTIONABLE_STATES,
        dest="states",
        help="Limit to one state; repeat to select several.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args(list(argv) if argv is not None else None)
    states = tuple(dict.fromkeys(args.states or ACTIONABLE_STATES))

    try:
        rows = fetch(args.dsn, states)
    except Exception as exc:  # noqa: BLE001 - operator CLI reports bounded DB failures
        print(
            f"dialectic_resolution_effects: inventory failed: {exc}",
            file=sys.stderr,
        )
        return 2

    if args.json:
        print(json.dumps([json_safe(row) for row in rows], indent=2, sort_keys=True))
    else:
        print(render(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

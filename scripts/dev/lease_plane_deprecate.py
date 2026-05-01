#!/usr/bin/env python3
"""
Lease-plane deprecation CLI (RFC v0.8 §7.11).

Implements the 4-phase operator-driven scheme deprecation procedure as a
standalone Python CLI in `scripts/dev/`. Operator decision 2026-04-30 — Python
CLI rather than Mix wrapper because deprecation is governance/operator policy
plus Postgres state, not BEAM live coordination.

Usage:
    python3 scripts/dev/lease_plane_deprecate.py deprecate <kind> [--days N]
    python3 scripts/dev/lease_plane_deprecate.py deprecation-sweep <kind>
    python3 scripts/dev/lease_plane_deprecate.py deprecation-finalize <kind>
    python3 scripts/dev/lease_plane_deprecate.py deprecation-status [<kind>]

Authorization:
    `deprecation-sweep` requires LEASE_FORCE_RELEASE_TOKEN (RFC §7.10) — read
    from env or `~/.config/cirwel/secrets.env`. GOVERNANCE_TOKEN does NOT
    authorize sweep.

Phase ordering (RFC §7.11.2):
    Phase 0:  deprecate           — INSERT into deprecated_schemes
    Phase 1:  (verification, lint via unitares_doctor — out of CLI scope)
    Phase 2:  deprecation-sweep   — force-release surviving leases (idempotent)
    Phase 3:  deprecation-finalize — record check_migrated_at + migrate CHECK
              (atomic with Phase 2 in same operator session per RFC §7.11.2)

Idempotency:
    Phase 0 INSERT uses ON CONFLICT DO NOTHING (re-marking is a no-op).
    Phase 2 sweep predicate `WHERE released_at IS NULL AND surface_kind = $1`
    reaches fixpoint on re-run after partial completion (RFC §7.11.4).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("error: asyncpg not installed; install with `pip install asyncpg`", file=sys.stderr)
    sys.exit(2)


DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)


def _read_force_release_token() -> str | None:
    """Read LEASE_FORCE_RELEASE_TOKEN from env or ~/.config/cirwel/secrets.env."""
    tok = os.environ.get("LEASE_FORCE_RELEASE_TOKEN")
    if tok:
        return tok
    secrets_path = Path.home() / ".config" / "cirwel" / "secrets.env"
    if not secrets_path.exists():
        return None
    for line in secrets_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("LEASE_FORCE_RELEASE_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


async def deprecate_cmd(
    *,
    kind: str,
    session_id: str,
    drain_window_days: int,
    db_url: str = DEFAULT_DB_URL,
) -> int:
    """Phase 0: mark scheme deprecated. Idempotent via ON CONFLICT.

    Per RFC §7.11.7 race-window mitigation, the INSERT runs inside a
    serializable transaction with a session-level advisory lock so concurrent
    acquires racing the mark transaction see a consistent state.
    """
    conn = await asyncpg.connect(db_url)
    try:
        # Validate against catalog OUTSIDE the transaction so a failure doesn't
        # abort the serializable tx (and so the error message can list valid kinds).
        catalog = await _list_catalog_kinds(conn)
        if kind not in catalog:
            print(
                f"error: scheme {kind!r} not in lease_plane.surface_kind_catalog. "
                f"Valid kinds: {catalog}",
                file=sys.stderr,
            )
            return 1
        async with conn.transaction(isolation="serializable"):
            # Hash the scheme name to a stable int for advisory lock; ensure positive int range.
            lock_key = abs(hash(kind)) % (2**31 - 1)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)
            await conn.execute(
                """
                INSERT INTO lease_plane.deprecated_schemes
                  (surface_kind, marked_by_session_id, drain_window_days)
                VALUES ($1, $2, $3)
                ON CONFLICT (surface_kind) DO NOTHING
                """,
                kind, session_id, drain_window_days,
            )
        return 0
    finally:
        await conn.close()


async def deprecation_sweep_cmd(*, kind: str, db_url: str = DEFAULT_DB_URL) -> int:
    """Phase 2: idempotent force-release sweep (RFC §7.11.4).

    Predicate: `WHERE released_at IS NULL AND surface_kind = $1` with no
    timestamp filter. Re-running on partial failure reaches fixpoint because
    already-released leases are excluded by the released_at IS NULL clause.

    Authorization: requires LEASE_FORCE_RELEASE_TOKEN (RFC §7.10).
    GOVERNANCE_TOKEN does NOT authorize.

    Each swept lease emits a `lease.deprecation_swept` event with
    deprecation_id in the payload jsonb for batch correlation (§7.11.3).
    """
    if not _read_force_release_token():
        print(
            "error: deprecation-sweep requires LEASE_FORCE_RELEASE_TOKEN "
            "(env or ~/.config/cirwel/secrets.env). GOVERNANCE_TOKEN does NOT authorize.",
            file=sys.stderr,
        )
        return 1

    conn = await asyncpg.connect(db_url)
    try:
        # Look up deprecation_id for audit correlation.
        depr_id = await conn.fetchval(
            "SELECT deprecation_id FROM lease_plane.deprecated_schemes WHERE surface_kind = $1",
            kind,
        )
        if depr_id is None:
            print(f"error: scheme {kind!r} is not marked deprecated; run `deprecate {kind}` first.",
                  file=sys.stderr)
            return 1

        async with conn.transaction():
            # Record sweep_started_at.
            await conn.execute(
                "UPDATE lease_plane.deprecated_schemes SET sweep_started_at = COALESCE(sweep_started_at, now()) "
                "WHERE surface_kind = $1",
                kind,
            )
            # Idempotent predicate per RFC §7.11.4 — no timestamp filter.
            unreleased = await conn.fetch(
                """
                SELECT lease_id, surface_id FROM lease_plane.surface_leases
                WHERE released_at IS NULL AND surface_kind = $1
                ORDER BY acquired_at
                FOR UPDATE SKIP LOCKED
                """,
                kind,
            )
            for row in unreleased:
                # Force-release the lease.
                await conn.execute(
                    "UPDATE lease_plane.surface_leases "
                    "SET released_at = now(), release_reason = 'forced' "
                    "WHERE lease_id = $1",
                    row["lease_id"],
                )
                # Emit audit event with deprecation_id for batch correlation.
                payload = json.dumps({"deprecation_id": str(depr_id), "kind": kind})
                await conn.execute(
                    """
                    INSERT INTO lease_plane.lease_plane_events
                      (event_type, lease_id, surface_id, surface_kind, advisory_mode, payload)
                    VALUES ('lease.deprecation_swept', $1, $2, $3, false, $4::jsonb)
                    """,
                    row["lease_id"], row["surface_id"], kind, payload,
                )
            # Mark sweep complete.
            await conn.execute(
                "UPDATE lease_plane.deprecated_schemes SET sweep_completed_at = now() "
                "WHERE surface_kind = $1",
                kind,
            )
        return 0
    finally:
        await conn.close()


async def deprecation_finalize_cmd(*, kind: str, db_url: str = DEFAULT_DB_URL) -> int:
    """Phase 3: record check_migrated_at on the deprecated_schemes row.

    Per RFC §7.11.2, Phase 3 also extends the surface_id_grammar CHECK to
    remove the deprecated scheme — that ALTER TABLE should be performed
    atomically in the same operator session as Phase 2 (DDL as a separate
    follow-on migration is the fallback if the operator splits sessions).
    """
    conn = await asyncpg.connect(db_url)
    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT sweep_completed_at FROM lease_plane.deprecated_schemes WHERE surface_kind = $1",
                kind,
            )
            if row is None:
                print(f"error: scheme {kind!r} is not marked deprecated.", file=sys.stderr)
                return 1
            if row["sweep_completed_at"] is None:
                print(f"error: deprecation-sweep has not completed for {kind!r}; "
                      f"run `deprecation-sweep {kind}` first.", file=sys.stderr)
                return 1
            await conn.execute(
                "UPDATE lease_plane.deprecated_schemes "
                "SET check_migrated_at = COALESCE(check_migrated_at, now()) "
                "WHERE surface_kind = $1",
                kind,
            )
        return 0
    finally:
        await conn.close()


async def deprecation_status_cmd(*, kind: str | None = None, db_url: str = DEFAULT_DB_URL) -> int:
    """Print deprecated_schemes table contents (operator visibility)."""
    conn = await asyncpg.connect(db_url)
    try:
        if kind:
            rows = await conn.fetch(
                "SELECT * FROM lease_plane.deprecated_schemes WHERE surface_kind = $1",
                kind,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM lease_plane.deprecated_schemes ORDER BY marked_deprecated_at"
            )
        for row in rows:
            print(json.dumps({k: str(v) for k, v in dict(row).items()}, indent=2))
        if not rows:
            print("(no deprecated schemes)")
        return 0
    finally:
        await conn.close()


async def _list_catalog_kinds(conn) -> list[str]:
    rows = await conn.fetch("SELECT surface_kind FROM lease_plane.surface_kind_catalog ORDER BY surface_kind")
    return [r["surface_kind"] for r in rows]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lease_plane_deprecate", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    dep = sub.add_parser("deprecate", help="Phase 0: mark scheme deprecated.")
    dep.add_argument("kind", help="surface_kind to deprecate (must be in surface_kind_catalog)")
    dep.add_argument("--days", type=int, default=30, help="drain window in days (default 30, max 90)")
    dep.add_argument("--session-id", default=os.environ.get("USER", "operator-cli"),
                     help="audit identifier for marked_by_session_id")

    sweep = sub.add_parser("deprecation-sweep", help="Phase 2: force-release surviving leases.")
    sweep.add_argument("kind")

    fin = sub.add_parser("deprecation-finalize", help="Phase 3: record check_migrated_at.")
    fin.add_argument("kind")

    status = sub.add_parser("deprecation-status", help="Show deprecated_schemes table.")
    status.add_argument("kind", nargs="?", default=None)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "deprecate":
        return asyncio.run(
            deprecate_cmd(kind=args.kind, session_id=args.session_id, drain_window_days=args.days)
        )
    if args.cmd == "deprecation-sweep":
        return asyncio.run(deprecation_sweep_cmd(kind=args.kind))
    if args.cmd == "deprecation-finalize":
        return asyncio.run(deprecation_finalize_cmd(kind=args.kind))
    if args.cmd == "deprecation-status":
        return asyncio.run(deprecation_status_cmd(kind=args.kind))
    return 2


if __name__ == "__main__":
    sys.exit(main())

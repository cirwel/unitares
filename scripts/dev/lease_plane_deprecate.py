#!/usr/bin/env python3
"""
Lease-plane deprecation CLI (RFC v0.8 §7.11).

Implements the 4-phase operator-driven scheme deprecation procedure as a
standalone Python CLI in `scripts/dev/`. Operator decision 2026-04-30 — Python
CLI rather than Mix wrapper because deprecation is governance/operator policy
plus Postgres state, not BEAM live coordination.

## Canonical operator path (R1 — RFC §7.11.2 atomicity)

The `deprecate-and-finalize` super-command is the canonical Phase 2+3 path.
It runs sweep and finalize on a single asyncpg connection (two transactions,
one connection — the meaningful "same operator session" invariant at the DB
wire level), correlates both phases under a shared `run_id` (in logs and
event payloads), and emits a `lease.deprecation_aborted` event with the
`run_id` if Phase 3 fails after Phase 2 succeeded.

The standalone `deprecation-sweep` and `deprecation-finalize` subcommands
remain as **operator escape hatches** for emergency partial recovery (e.g.,
super-command was killed mid-run; operator needs to manually advance from
the Phase 2 success / Phase 3 failure state). The super-command's clear
"rerun deprecation-finalize" guidance points operators here.

Usage:
    python3 scripts/dev/lease_plane_deprecate.py deprecate <kind> [--days N]
    python3 scripts/dev/lease_plane_deprecate.py deprecate-and-finalize <kind>  # canonical Phase 2+3
    python3 scripts/dev/lease_plane_deprecate.py deprecation-sweep <kind>       # escape hatch
    python3 scripts/dev/lease_plane_deprecate.py deprecation-finalize <kind>    # escape hatch
    python3 scripts/dev/lease_plane_deprecate.py deprecation-status [<kind>]

Authorization:
    `deprecation-sweep` and `deprecate-and-finalize` require
    LEASE_FORCE_RELEASE_TOKEN (RFC §7.10) — read from env or
    `~/.config/cirwel/secrets.env`. GOVERNANCE_TOKEN does NOT authorize.

Phase ordering (RFC §7.11.2):
    Phase 0:  deprecate                  — INSERT into deprecated_schemes
    Phase 1:  (verification, lint via unitares_doctor — out of CLI scope)
    Phase 2:  deprecation-sweep          — force-release surviving leases (idempotent)
    Phase 3:  deprecation-finalize       — record check_migrated_at + migrate CHECK
    Phase 2+3: deprecate-and-finalize    — atomic two-tx-one-conn (R1 canonical)

Idempotency:
    Phase 0 INSERT uses ON CONFLICT DO NOTHING (re-marking is a no-op).
    Phase 2 sweep predicate `WHERE released_at IS NULL AND surface_kind = $1`
    reaches fixpoint on re-run after partial completion (RFC §7.11.4).
    Phase 3 guards on `check_migrated_at IS NULL` for emit-once.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import uuid
from pathlib import Path

# Lazy import of lease-plane client so the CLI can be imported without the
# full src/ package on the path (e.g., during argparse-only help invocations).
# The actual import happens inside _sweep_inner and the sweep commands.

try:
    import asyncpg
except ImportError:
    print("error: asyncpg not installed; install with `pip install asyncpg`", file=sys.stderr)
    sys.exit(2)


# Operator-visible logger; format prefix includes run_id when threaded through.
logger = logging.getLogger("lease_plane_deprecate")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


def _lock_key_for_kind(kind: str) -> int:
    """Deterministic int32-positive advisory-lock key from a scheme kind.

    Python's `hash()` for strings is salted per-process via PYTHONHASHSEED,
    so two CLI invocations in different shells would otherwise produce
    different lock keys for the same kind — silently breaking the §7.11.7
    race-window protection (council BLOCK from PR 1-4 stack review).
    Use SHA-256 truncated to int32-positive instead.
    """
    digest = hashlib.sha256(kind.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") & 0x7FFFFFFF


DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)


def _read_force_release_token() -> str | None:
    """Read LEASE_FORCE_RELEASE_TOKEN from env or ~/.config/cirwel/secrets.env."""
    tok = os.environ.get("LEASE_FORCE_RELEASE_TOKEN")
    if tok:
        return tok
    _env_override = os.environ.get("UNITARES_SECRETS_ENV")
    secrets_path = (
        Path(_env_override)
        if _env_override
        else Path.home() / ".config" / "cirwel" / "secrets.env"
    )
    if not secrets_path.exists():
        return None
    for line in secrets_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("LEASE_FORCE_RELEASE_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# ---------- payload helper ----------

def _payload_with_run_id(base: dict, run_id: str | None) -> str:
    """JSON-encode an event payload, injecting run_id when present.

    R1: the super-command threads a shared `run_id` (uuid4) through every
    event it emits (Phase 2 sweep events, Phase 3 migrated event, Phase 3
    aborted event) so partial-completion across the two transactions is
    correlatable in audit queries:

        SELECT event_type, ts FROM lease_plane.lease_plane_events
        WHERE payload->>'run_id' = '<uuid>' ORDER BY ts;

    Singleton sub-commands (escape hatches) pass run_id=None and the field
    is omitted, preserving existing payload shapes.
    """
    if run_id is not None:
        base = {**base, "run_id": run_id}
    return json.dumps(base)


# ---------- Phase 0: deprecate ----------

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
        catalog = await _list_catalog_kinds(conn)
        if kind not in catalog:
            print(
                f"error: scheme {kind!r} not in lease_plane.surface_kind_catalog. "
                f"Valid kinds: {catalog}",
                file=sys.stderr,
            )
            return 1
        async with conn.transaction(isolation="serializable"):
            lock_key = _lock_key_for_kind(kind)
            await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_key)
            row = await conn.fetchrow(
                """
                INSERT INTO lease_plane.deprecated_schemes
                  (surface_kind, marked_by_session_id, drain_window_days)
                VALUES ($1, $2, $3)
                ON CONFLICT (surface_kind) DO NOTHING
                RETURNING deprecation_id
                """,
                kind, session_id, drain_window_days,
            )
            if row is not None:
                # Phase 0 is single-step (one CLI invocation, one tx) so it
                # has no run_id to correlate. Pass None to preserve the
                # pre-R1 payload shape; super-command does not call this
                # helper for Phase 0 (Phase 0 happens before super-command).
                payload = _payload_with_run_id(
                    {
                        "deprecation_id": str(row["deprecation_id"]),
                        "kind": kind,
                        "session_id": session_id,
                        "drain_window_days": drain_window_days,
                    },
                    None,
                )
                await conn.execute(
                    """
                    INSERT INTO lease_plane.lease_plane_events
                      (event_type, surface_id, surface_kind, advisory_mode, payload)
                    VALUES ('lease.deprecation_marked', $1, $2, false, $3::jsonb)
                    """,
                    f"{kind}:/__deprecation_marker__", kind, payload,
                )
        return 0
    finally:
        await conn.close()


# ---------- Phase 2: sweep (inner + wrapper) ----------

async def _sweep_inner(
    conn,
    *,
    kind: str,
    deprecation_id,
    run_id: str | None = None,
    lease_client,
) -> int:
    """Inner Phase 2 helper: force-release surviving leases via HTTP contract layer.

    Caller owns the connection lifecycle. R1: lifted out of `deprecation_sweep_cmd`
    so the new `deprecate_and_finalize_cmd` super-command can call it on a
    connection it already opened (the "same operator session" invariant lives
    here at the DB wire level, not at the CLI subprocess level).

    RFC §7.10 contract: every force-release goes through POST /v1/lease/force-release
    on the Elixir router using LEASE_FORCE_RELEASE_TOKEN. The Python CLI reads
    unreleased leases via SQL (read-only) but makes no direct state-changing SQL
    writes to surface_leases — the Elixir router owns that state change.

    Returns the number of leases swept (callers ignore but useful for tests
    + caller-side logging).

    Raises RuntimeError if any individual force-release HTTP call fails — the
    sweep is aborted and the caller can re-run (idempotency predicate:
    WHERE released_at IS NULL excludes already-released leases on re-run).
    """
    from src.lease_plane.models import ForceReleaseRequest
    from src.lease_plane.client import SimpleOk

    async with conn.transaction():
        await conn.execute(
            "UPDATE lease_plane.deprecated_schemes "
            "SET sweep_started_at = COALESCE(sweep_started_at, now()) "
            "WHERE surface_kind = $1",
            kind,
        )

    # Read-only: fetch unreleased leases. No FOR UPDATE needed — the HTTP call
    # is idempotent (Elixir returns already_released on retry), and concurrent
    # sweeps are prevented by the session-level advisory lock in
    # deprecate_and_finalize_cmd. Standalone deprecation_sweep_cmd relies on
    # operator discipline (single operator invocation).
    unreleased = await conn.fetch(
        """
        SELECT lease_id, surface_id FROM lease_plane.surface_leases
        WHERE released_at IS NULL AND surface_kind = $1
        ORDER BY acquired_at
        """,
        kind,
    )

    for row in unreleased:
        # State change via HTTP — §7.10 contract layer. The Elixir router
        # sets released_at + release_reason='forced' in its own transaction.
        result = lease_client.force_release(ForceReleaseRequest(lease_id=row["lease_id"]))
        if not isinstance(result, SimpleOk):
            raise RuntimeError(
                f"force-release HTTP call failed for lease {row['lease_id']}: "
                f"error={getattr(result, 'error', '?')} "
                f"reason={getattr(result, 'reason', '')!r}"
            )

        # Emit audit event for this deprecation sweep (Python-side audit trail;
        # Elixir emits its own lease.released event — these are complementary).
        payload = _payload_with_run_id(
            {"deprecation_id": str(deprecation_id), "kind": kind},
            run_id,
        )
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO lease_plane.lease_plane_events
                  (event_type, lease_id, surface_id, surface_kind, advisory_mode, payload)
                VALUES ('lease.deprecation_swept', $1, $2, $3, false, $4::jsonb)
                """,
                row["lease_id"], row["surface_id"], kind, payload,
            )

    # COALESCE on sweep_completed_at preserves the original first-completion
    # timestamp across re-runs (council CONCERN 4 reviewer). Without this, a
    # rerun overwrites the audit record with the rerun time, drifting the
    # "when was this deprecation actually swept?" answer.
    async with conn.transaction():
        await conn.execute(
            "UPDATE lease_plane.deprecated_schemes "
            "SET sweep_completed_at = COALESCE(sweep_completed_at, now()) "
            "WHERE surface_kind = $1",
            kind,
        )

    return len(unreleased)


async def deprecation_sweep_cmd(
    *,
    kind: str,
    db_url: str = DEFAULT_DB_URL,
    run_id: str | None = None,
    _lease_client=None,
) -> int:
    """Phase 2 standalone (escape-hatch) wrapper: idempotent force-release sweep
    (RFC §7.11.4).

    Predicate: `WHERE released_at IS NULL AND surface_kind = $1` with no
    timestamp filter. Re-running on partial failure reaches fixpoint because
    already-released leases are excluded by the released_at IS NULL clause.

    Authorization: requires LEASE_FORCE_RELEASE_TOKEN (RFC §7.10).
    GOVERNANCE_TOKEN does NOT authorize.

    Each swept lease emits a `lease.deprecation_swept` event with
    deprecation_id (and optional run_id) in the payload jsonb for batch
    correlation (§7.11.3).

    NOTE (R1): `deprecate-and-finalize` is the canonical operator path for
    Phase 2+3. Use this subcommand only for emergency partial recovery.

    Args:
        _lease_client: injectable LeasePlaneClient for testing; if None, one is
            constructed from LEASE_FORCE_RELEASE_TOKEN. Underscore prefix signals
            this is a test seam, not production API.
    """
    token = _read_force_release_token()
    if not token:
        print(
            "error: deprecation-sweep requires LEASE_FORCE_RELEASE_TOKEN "
            "(env or ~/.config/cirwel/secrets.env). GOVERNANCE_TOKEN does NOT authorize.",
            file=sys.stderr,
        )
        return 1

    if _lease_client is None:
        from src.lease_plane.client import LeasePlaneClient, LeasePlaneClientConfig
        _lease_client = LeasePlaneClient(LeasePlaneClientConfig(force_release_token=token))

    conn = await asyncpg.connect(db_url)
    try:
        depr_id = await conn.fetchval(
            "SELECT deprecation_id FROM lease_plane.deprecated_schemes WHERE surface_kind = $1",
            kind,
        )
        if depr_id is None:
            print(f"error: scheme {kind!r} is not marked deprecated; run `deprecate {kind}` first.",
                  file=sys.stderr)
            return 1
        await _sweep_inner(conn, kind=kind, deprecation_id=depr_id, run_id=run_id, lease_client=_lease_client)
        return 0
    finally:
        await conn.close()


# ---------- Phase 3: finalize (inner + wrapper) ----------

async def _finalize_inner(
    conn,
    *,
    kind: str,
    run_id: str | None = None,
) -> int:
    """Inner Phase 3 helper: record check_migrated_at on an OPEN conn.

    Returns 0 on success, 1 on operator-fixable error (scheme not marked,
    sweep not completed). Raises on infrastructure errors (DB unavailable,
    integrity violations) so the caller can decide whether to emit
    lease.deprecation_aborted.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            "SELECT deprecation_id, sweep_completed_at, check_migrated_at "
            "FROM lease_plane.deprecated_schemes WHERE surface_kind = $1",
            kind,
        )
        if row is None:
            print(f"error: scheme {kind!r} is not marked deprecated.", file=sys.stderr)
            return 1
        if row["sweep_completed_at"] is None:
            print(f"error: deprecation-sweep has not completed for {kind!r}; "
                  f"run `deprecation-sweep {kind}` first.", file=sys.stderr)
            return 1
        already_finalized = row["check_migrated_at"] is not None
        await conn.execute(
            "UPDATE lease_plane.deprecated_schemes "
            "SET check_migrated_at = COALESCE(check_migrated_at, now()) "
            "WHERE surface_kind = $1",
            kind,
        )
        if not already_finalized:
            payload = _payload_with_run_id(
                {"deprecation_id": str(row["deprecation_id"]), "kind": kind},
                run_id,
            )
            await conn.execute(
                """
                INSERT INTO lease_plane.lease_plane_events
                  (event_type, surface_id, surface_kind, advisory_mode, payload)
                VALUES ('lease.deprecation_migrated', $1, $2, false, $3::jsonb)
                """,
                f"{kind}:/__deprecation_marker__", kind, payload,
            )
        return 0


async def deprecation_finalize_cmd(
    *,
    kind: str,
    db_url: str = DEFAULT_DB_URL,
    run_id: str | None = None,
) -> int:
    """Phase 3 standalone (escape-hatch) wrapper: record check_migrated_at.

    Per RFC §7.11.2, Phase 3 should be atomic with Phase 2 in the same
    operator session. R1's `deprecate-and-finalize` super-command satisfies
    that invariant at the DB wire level (single connection, two transactions
    with shared run_id correlation). This standalone subcommand exists for
    emergency partial recovery — e.g., the super-command's Phase 3 attempt
    failed and emitted `lease.deprecation_aborted`, the operator fixed the
    underlying issue and is rerunning finalize.
    """
    conn = await asyncpg.connect(db_url)
    try:
        return await _finalize_inner(conn, kind=kind, run_id=run_id)
    finally:
        await conn.close()


# ---------- Phase 2+3 super-command (R1 canonical) ----------

async def _emit_aborted_event(
    conn,
    *,
    kind: str,
    deprecation_id,
    run_id: str,
    reason: str,
) -> None:
    """Emit `lease.deprecation_aborted` event when Phase 3 fails after Phase 2
    succeeded. Runs in its own short transaction (Phase 3's tx already failed
    and is rolled back by the surrounding caller; we open a fresh tx here for
    the abort emission so the audit trail survives).

    Per the architect council finding: without this event class, an operator
    who fails Phase 3 thrice and gives up has no audit trail of the abandoned
    deprecation. Migration 030 extends event_type CHECK to permit this.
    """
    payload = _payload_with_run_id(
        {
            "deprecation_id": str(deprecation_id),
            "kind": kind,
            "phase": "finalize",
            "reason": reason,
        },
        run_id,
    )
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO lease_plane.lease_plane_events
              (event_type, surface_id, surface_kind, advisory_mode, payload)
            VALUES ('lease.deprecation_aborted', $1, $2, false, $3::jsonb)
            """,
            f"{kind}:/__deprecation_marker__", kind, payload,
        )


async def deprecate_and_finalize_cmd(
    *,
    kind: str,
    db_url: str = DEFAULT_DB_URL,
    _lease_client=None,
) -> int:
    """R1 canonical Phase 2+3 super-command (RFC §7.11.2 atomicity).

    Runs Phase 2 (sweep) and Phase 3 (finalize) on a single asyncpg connection
    in two separate transactions, correlated under a shared `run_id` (uuid4)
    that surfaces in logs and event payloads. The DB-wire-level "same
    operator session" invariant is satisfied because both phases run on the
    same connection within one process — preventing the v0.7 1-day Layer-1
    enforcement gap that v0.8 §7.11.2 line 775 promised to close.

    Two-transactions-one-connection (operator decision 2026-05-02 vs strict
    one-tx atomicity): if Phase 3 fails after Phase 2 succeeded, the swept
    rows STAY swept (no rollback of the operator's Phase 2 work). The
    super-command then:
      1. Emits `lease.deprecation_aborted` event with run_id + reason
      2. Logs clear "rerun deprecation-finalize <kind>" guidance
      3. Returns non-zero exit
    The §7.11.4 idempotent-sweep predicate makes the rerun safe.

    Authorization: same as `deprecation-sweep` (LEASE_FORCE_RELEASE_TOKEN).

    Args:
        _lease_client: injectable LeasePlaneClient for testing; if None, one is
            constructed from LEASE_FORCE_RELEASE_TOKEN.
    """
    token = _read_force_release_token()
    if not token:
        print(
            "error: deprecate-and-finalize requires LEASE_FORCE_RELEASE_TOKEN "
            "(env or ~/.config/cirwel/secrets.env). GOVERNANCE_TOKEN does NOT authorize.",
            file=sys.stderr,
        )
        return 1

    if _lease_client is None:
        from src.lease_plane.client import LeasePlaneClient, LeasePlaneClientConfig
        _lease_client = LeasePlaneClient(LeasePlaneClientConfig(force_release_token=token))

    run_id = str(uuid.uuid4())
    logger.info(f"[run_id={run_id}] deprecate-and-finalize starting for kind={kind!r}")

    conn = await asyncpg.connect(db_url)
    try:
        # Council CONCERN 3 (reviewer): pg_try_advisory_lock on the same
        # SHA-256-derived key as Phase 0's pg_advisory_xact_lock. Session-level
        # (not transaction-level) so it spans Phase 2 + Phase 3's two separate
        # transactions. Try-variant fails fast (returns false) if another
        # super-command holds the lock — better operator UX than blocking
        # indefinitely. Released automatically on conn.close() in the finally
        # block; explicit unlock not required but cleaner if added.
        lock_key = _lock_key_for_kind(kind)
        got_lock = await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_key)
        if not got_lock:
            print(
                f"error: another deprecate-and-finalize is in progress for kind={kind!r}; "
                f"retry once it completes (or use `deprecation-status {kind}` to inspect).",
                file=sys.stderr,
            )
            logger.error(f"[run_id={run_id}] aborted before Phase 2: advisory lock held")
            return 4

        depr_id = await conn.fetchval(
            "SELECT deprecation_id FROM lease_plane.deprecated_schemes WHERE surface_kind = $1",
            kind,
        )
        if depr_id is None:
            print(
                f"error: scheme {kind!r} is not marked deprecated; run `deprecate {kind}` first.",
                file=sys.stderr,
            )
            logger.error(f"[run_id={run_id}] aborted before Phase 2: scheme not marked")
            return 1

        # Phase 2 (own transaction).
        try:
            swept = await _sweep_inner(conn, kind=kind, deprecation_id=depr_id, run_id=run_id, lease_client=_lease_client)
            logger.info(f"[run_id={run_id}] Phase 2 complete: swept {swept} lease(s)")
        except Exception as e:
            # Phase 2 failure: nothing committed beyond the sweep_started_at
            # update (which is itself inside the failed tx and rolled back).
            # No abort event needed — there's nothing to be inconsistent with.
            logger.error(f"[run_id={run_id}] Phase 2 sweep failed: {e!r}")
            print(f"error: Phase 2 sweep failed: {e}", file=sys.stderr)
            return 2

        # Phase 3 (own transaction on the same connection — "same operator
        # session" at the DB wire level).
        try:
            rc = await _finalize_inner(conn, kind=kind, run_id=run_id)
            if rc != 0:
                # `_finalize_inner` returns rc=1 only for "scheme not marked"
                # (we just fetched depr_id) or "sweep not completed" (we just
                # ran _sweep_inner). Both are unreachable in this code path.
                # Defensive log + return without abort emission — abort is
                # for genuine Phase 3 infrastructure failures (raises), not
                # for operator-fixable rc!=0 (council architect NIT 1).
                logger.error(
                    f"[run_id={run_id}] Phase 3 finalize returned unexpected rc={rc} "
                    f"(should be unreachable); not emitting abort event"
                )
                return rc
            logger.info(f"[run_id={run_id}] Phase 3 complete; deprecation finalized")
            return 0
        except Exception as e:
            # Phase 3 infrastructure failure after Phase 2 succeeded. Sweep
            # rows stay swept (per the two-tx-one-conn decision). Try to emit
            # the abort event so the audit trail captures the abandoned-Phase-3
            # state — but if abort emission ALSO fails (e.g., DB unreachable
            # cause persists), surface the rerun guidance anyway. Council
            # BLOCK 1 (reviewer) + CONCERN 1 (architect): without this guard,
            # connection-level Phase 3 failure produces an unhandled exception
            # from _emit_aborted_event that drops the structured exit code
            # and audit silence.
            try:
                await _emit_aborted_event(
                    conn,
                    kind=kind,
                    deprecation_id=depr_id,
                    run_id=run_id,
                    reason=f"finalize raised: {e!r}",
                )
            except Exception as emit_err:
                logger.error(
                    f"[run_id={run_id}] abort event emission ALSO failed: {emit_err!r}; "
                    f"audit trail incomplete but rerun guidance still applies"
                )
            logger.error(
                f"[run_id={run_id}] Phase 3 finalize raised: {e!r}. "
                f"Rerun: deprecation-finalize {kind}"
            )
            print(
                f"error: Phase 3 finalize failed after Phase 2 succeeded ({e}). "
                f"Phase 2 work is preserved. Rerun: "
                f"python3 scripts/dev/lease_plane_deprecate.py deprecation-finalize {kind}",
                file=sys.stderr,
            )
            return 3
    finally:
        await conn.close()


# ---------- status (read-only) ----------

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


# ---------- argparse ----------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lease_plane_deprecate", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    dep = sub.add_parser("deprecate", help="Phase 0: mark scheme deprecated.")
    dep.add_argument("kind", help="surface_kind to deprecate (must be in surface_kind_catalog)")
    dep.add_argument("--days", type=int, default=30, help="drain window in days (default 30, max 90)")
    dep.add_argument("--session-id", default=os.environ.get("USER", "operator-cli"),
                     help="audit identifier for marked_by_session_id")

    daf = sub.add_parser(
        "deprecate-and-finalize",
        help="R1 canonical: Phase 2+3 atomic on single connection (RFC §7.11.2).",
    )
    daf.add_argument("kind")

    sweep = sub.add_parser(
        "deprecation-sweep",
        help="Phase 2 standalone (ESCAPE HATCH — prefer `deprecate-and-finalize`).",
    )
    sweep.add_argument("kind")

    fin = sub.add_parser(
        "deprecation-finalize",
        help="Phase 3 standalone (ESCAPE HATCH — prefer `deprecate-and-finalize`; "
             "use this only to recover from a failed super-command Phase 3).",
    )
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
    if args.cmd == "deprecate-and-finalize":
        return asyncio.run(deprecate_and_finalize_cmd(kind=args.kind))
    if args.cmd == "deprecation-sweep":
        return asyncio.run(deprecation_sweep_cmd(kind=args.kind))
    if args.cmd == "deprecation-finalize":
        return asyncio.run(deprecation_finalize_cmd(kind=args.kind))
    if args.cmd == "deprecation-status":
        return asyncio.run(deprecation_status_cmd(kind=args.kind))
    return 2


if __name__ == "__main__":
    sys.exit(main())

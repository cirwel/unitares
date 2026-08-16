#!/usr/bin/env python3
"""Wave 3 §8.2 shadow-divergence checker (hourly via launchd).

Runs scripts/ops/wave-3-shadow-divergence-check.sql against the governance DB
and emits one `coordination_failure.beam_python_boundary.shadow_divergence`
event per divergent row, payload built by
`governance_core.coordination_events_helpers.make_shadow_divergence_payload`
(contract: {table_name, agent_id, kind, divergent_columns}).

Shadow-window state is TRI-STATE, not a bare `count == 0` skip
----------------------------------------------------------------
With empty shadow tables every canonical row reports `shadow_missing`, which
would flood the failure channel with non-signal. So an empty shadow table
suppresses emission. But "empty" has two very different causes, and the
original bare `count == 0` gate rendered them identically:

  never_started  shadow table empty and never seen non-empty -> inert, exit 0.
                 Correct today: the Wave 3 BEAM shadow writer does not exist.
  active         shadow table non-empty -> compare and emit.
  went_dark      shadow table empty AFTER having been active -> ALARM, exit 2.
                 A shadow window that stops producing is a broken writer, and
                 it previously read as "inert" — i.e. as nothing to do.

`went_dark` is the case this file exists to catch. A checker whose silence
means both "nothing wrong" and "cannot see" is not a checker; see
`scripts/dev/garden.py --self-test` in the memory tooling for the same move.

The transition marker is a small JSON file under ~/.unitares (checkout- and
deploy-independent by design, same reasoning as the Watcher floor state). If
it is missing, the checker degrades to `never_started` rather than guessing.

--self-test
-----------
Proves the comparator can actually SEE. Plants one synthetic row per
divergence kind inside a transaction, runs the REAL comparator SQL against it,
asserts every kind is detected, then ROLLS BACK -- no committed writes. Exits
1 naming any kind that came back undetected. Run this before believing a
zero from a normal run.

Exit codes: 0 = ran (divergences, if any, were emitted -- events are the
signal); 1 = runner error (DB unreachable, SQL failure, emit failure) or
self-test failure; 2 = shadow window went dark (was active, now empty).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import asyncpg  # noqa: E402

from governance_core.coordination_events_helpers import (  # noqa: E402
    make_shadow_divergence_payload,
)
from src.coordination_events import (  # noqa: E402
    COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_SHADOW_DIVERGENCE,
    emit_event,
)

SQL_FILE = Path(__file__).with_name("wave-3-shadow-divergence-check.sql")
DSN = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)
WINDOW_STATE_FILE = Path(
    os.environ.get(
        "WAVE3_SHADOW_WINDOW_STATE",
        str(Path.home() / ".unitares" / "wave3_shadow_window_state.json"),
    )
)

# Maps the comparator's boolean *_diff columns to divergent-column names.
_NON_DIFF_COLUMNS = {"table_name", "agent_id", "canonical_missing", "shadow_missing"}

_SHADOW_TABLES = {
    "identities": "core.identities_shadow",
    "agents": "core.agents_shadow",
}


def _row_kind(row: asyncpg.Record) -> str:
    if row["canonical_missing"]:
        return "canonical_missing"
    if row["shadow_missing"]:
        return "shadow_missing"
    return "column_mismatch"


def _divergent_columns(row: asyncpg.Record) -> list[str]:
    return sorted(
        key.removesuffix("_diff")
        for key, value in dict(row).items()
        if key not in _NON_DIFF_COLUMNS and value is True
    )


def _load_window_state() -> dict:
    try:
        return json.loads(WINDOW_STATE_FILE.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        # A corrupt marker must not silently downgrade to "never_started" --
        # that is the failure mode this whole file is about.
        print(
            f"[shadow-divergence] window-state file unreadable ({exc!r}); "
            f"treating as never_started but this is a blind spot",
            file=sys.stderr,
        )
        return {}


def _save_window_state(state: dict) -> None:
    try:
        WINDOW_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        WINDOW_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))
    except OSError as exc:
        print(
            f"[shadow-divergence] could not persist window state: {exc!r}",
            file=sys.stderr,
        )


def _window_status(table: str, count: int, state: dict) -> str:
    if count > 0:
        return "active"
    return "went_dark" if state.get(table, {}).get("ever_active") else "never_started"


def _load_statements() -> list[str] | None:
    """Strip full-line comments FIRST (a comment may contain a quoted
    semicolon -- it did, once), then split on top-level ';' (the comparator
    file guarantees no procedural bodies and no semicolons in string
    literals). Chunks without a SELECT (e.g. trailing whitespace) drop."""
    sql_text = "\n".join(
        line
        for line in SQL_FILE.read_text().splitlines()
        if not line.lstrip().startswith("--")
    )
    statements = [s.strip() for s in sql_text.split(";") if s.strip()]
    statements = [s for s in statements if "SELECT" in s.upper()]
    if len(statements) != 2:
        print(
            f"[shadow-divergence] expected 2 comparator statements in "
            f"{SQL_FILE.name}, found {len(statements)} — refusing to run",
            file=sys.stderr,
        )
        return None
    return statements


async def self_test() -> int:
    """Plant one row per divergence kind, run the real comparator, assert all
    three are detected, roll back. Nothing is committed."""
    statements = _load_statements()
    if statements is None:
        return 1

    conn = await asyncpg.connect(DSN)
    try:
        tx = conn.transaction()
        await tx.start()
        try:
            # shadow_missing: a canonical identity with no shadow counterpart.
            # Any canonical row satisfies this while the shadow table is empty;
            # plant one explicitly so the test does not depend on live data.
            probe = "wave3-selftest-shadow-missing"
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash) VALUES ($1, $2)",
                probe,
                "selftest",
            )

            # canonical_missing: a shadow row with no canonical counterpart.
            await conn.execute(
                "INSERT INTO core.identities_shadow (agent_id, api_key_hash) "
                "VALUES ($1, $2)",
                "wave3-selftest-canonical-missing",
                "selftest",
            )

            # column_mismatch: same agent_id both sides, one column differing.
            mism = "wave3-selftest-column-mismatch"
            await conn.execute(
                "INSERT INTO core.identities (agent_id, api_key_hash, spawn_reason) "
                "VALUES ($1, $2, $3)",
                mism,
                "selftest",
                "explicit",
            )
            await conn.execute(
                "INSERT INTO core.identities_shadow "
                "(agent_id, api_key_hash, spawn_reason) VALUES ($1, $2, $3)",
                mism,
                "selftest",
                "subagent",  # <- the planted divergence
            )

            rows = []
            for stmt in statements:
                rows.extend(await conn.fetch(stmt))

            by_kind: dict[str, list[asyncpg.Record]] = {}
            for row in rows:
                by_kind.setdefault(_row_kind(row), []).append(row)

            print("wave3 shadow-divergence --self-test "
                  "(each comparator branch is shown a planted defect)\n")
            failures = []
            for kind in ("shadow_missing", "canonical_missing", "column_mismatch"):
                hits = by_kind.get(kind, [])
                if hits:
                    print(f"  PASS   {kind:<20} detected {len(hits)} row(s)")
                else:
                    print(f"  BLIND  {kind:<20} planted a defect, comparator saw NOTHING")
                    failures.append(kind)

            # The mismatch row must also name the column, or the payload's
            # divergent_columns contract is silently empty.
            mismatch_rows = [
                r for r in by_kind.get("column_mismatch", [])
                if r["agent_id"] == mism
            ]
            if mismatch_rows:
                cols = _divergent_columns(mismatch_rows[0])
                if "spawn_reason" in cols:
                    print(f"  PASS   divergent_columns    named {cols}")
                else:
                    print(f"  BLIND  divergent_columns    got {cols}, expected spawn_reason")
                    failures.append("divergent_columns")
            elif "column_mismatch" not in failures:
                print("  BLIND  divergent_columns    planted mismatch row not returned")
                failures.append("divergent_columns")

            if failures:
                print(f"\nFAILED: {len(failures)} blind detector(s): {', '.join(failures)}")
                return 1
            print("\nAll comparator branches fired on planted defects. An empty\n"
                  "result from a normal run is now evidence of no divergence\n"
                  "rather than evidence of a broken comparator.")
            return 0
        finally:
            await tx.rollback()  # nothing above is committed
    finally:
        await conn.close()


async def main() -> int:
    statements = _load_statements()
    if statements is None:
        return 1

    state = _load_window_state()
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            shadow_counts = {
                name: await conn.fetchval(f"SELECT count(*) FROM {table}")
                for name, table in _SHADOW_TABLES.items()
            }
            rows: list[asyncpg.Record] = []
            for stmt in statements:
                rows.extend(await conn.fetch(stmt))

        statuses = {
            name: _window_status(name, count, state)
            for name, count in shadow_counts.items()
        }
        now = datetime.now(timezone.utc).isoformat()
        for name, status in statuses.items():
            if status == "active":
                entry = state.setdefault(name, {})
                entry["ever_active"] = True
                entry["last_nonzero_at"] = now
                entry["last_count"] = shadow_counts[name]
        _save_window_state(state)

        emitted = 0
        skipped_inert = 0
        for row in rows:
            table_name = row["table_name"]
            if statuses.get(table_name) != "active":
                # Window not producing for this table; see the module docstring.
                skipped_inert += 1
                continue
            try:
                payload = make_shadow_divergence_payload(
                    table_name=table_name,
                    agent_id=str(row["agent_id"]),
                    kind=_row_kind(row),
                    divergent_columns=_divergent_columns(row),
                )
                await emit_event(
                    pool,
                    service="governance_mcp",
                    event_type=COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_SHADOW_DIVERGENCE,
                    payload=payload,
                    agent_id=None,  # row agent_id may predate UUID discipline; carried in payload
                )
            except Exception as exc:  # noqa: BLE001 — clean exit-1 contract
                # Already-emitted events stay committed (each emit is its own
                # INSERT); fail the run loudly but with the documented exit
                # code rather than a traceback.
                print(
                    f"[shadow-divergence] emit failed after {emitted} events "
                    f"({table_name}/{row['agent_id']}): {exc!r}",
                    file=sys.stderr,
                )
                return 1
            emitted += 1

        print(
            f"[shadow-divergence] rows={len(rows)} emitted={emitted} "
            f"inert_skipped={skipped_inert} shadow_counts={shadow_counts} "
            f"window={statuses}"
        )

        dark = sorted(n for n, s in statuses.items() if s == "went_dark")
        if dark:
            print(
                f"[shadow-divergence] ALARM: shadow window went dark for "
                f"{', '.join(dark)} — the table was populated before and is "
                f"empty now, so the shadow writer has stopped. This is NOT "
                f"the inert/never-started state.",
                file=sys.stderr,
            )
            return 2
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(asyncio.run(self_test()))
    sys.exit(asyncio.run(main()))

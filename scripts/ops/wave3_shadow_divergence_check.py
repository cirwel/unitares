#!/usr/bin/env python3
"""Wave 3 §8.2 shadow-divergence checker (hourly via launchd).

Runs scripts/ops/wave-3-shadow-divergence-check.sql against the governance DB
and emits one `coordination_failure.beam_python_boundary.shadow_divergence`
event per divergent row, payload built by
`governance_core.coordination_events_helpers.make_shadow_divergence_payload`
(contract: {table_name, agent_id, kind, divergent_columns}).

Inert-by-construction until the Wave 3 BEAM shadow writer exists: with empty
shadow tables, every canonical row reports `shadow_missing`, which would flood
the failure channel with non-signal. So rows are only emitted once the shadow
table is non-empty (the shadow window has actually started); until then the
checker logs the would-be count and exits 0. This gate is documented here and
in the PR body — remove it is NOT the right fix if you want earlier signal;
start the shadow writer instead.

Exit codes: 0 = ran (divergences, if any, were emitted — events are the
signal); 1 = runner error (DB unreachable, SQL failure, emit failure).
"""

from __future__ import annotations

import asyncio
import os
import sys
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

# Maps the comparator's boolean *_diff columns to divergent-column names.
_NON_DIFF_COLUMNS = {"table_name", "agent_id", "canonical_missing", "shadow_missing"}


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


async def main() -> int:
    # Strip full-line comments FIRST (a comment may contain a quoted
    # semicolon — it did, once), then split on top-level ';' (the comparator
    # file guarantees no procedural bodies and no semicolons in string
    # literals). Chunks without a SELECT (e.g. trailing whitespace) drop.
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
        return 1

    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            shadow_counts = {
                "identities": await conn.fetchval(
                    "SELECT count(*) FROM core.identities_shadow"
                ),
                "agents": await conn.fetchval(
                    "SELECT count(*) FROM core.agents_shadow"
                ),
            }
            rows: list[asyncpg.Record] = []
            for stmt in statements:
                rows.extend(await conn.fetch(stmt))

        emitted = 0
        skipped_inert = 0
        for row in rows:
            table_name = row["table_name"]
            if shadow_counts.get(table_name, 0) == 0:
                # Shadow window not started for this table — see module docstring.
                skipped_inert += 1
                continue
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
            emitted += 1

        print(
            f"[shadow-divergence] rows={len(rows)} emitted={emitted} "
            f"inert_skipped={skipped_inert} shadow_counts={shadow_counts}"
        )
        return 0
    finally:
        await pool.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

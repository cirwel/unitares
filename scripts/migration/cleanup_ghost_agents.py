#!/usr/bin/env python3
"""
Clean up ghost agents — archived agents with no governance state.

Identifies agents with status='archived' and no rows in core.agent_state,
then hard-deletes them (sessions, identities, agents) in cascade.

Agents WITH state data are preserved for forensic value.

Dry-run by default. Pass --execute to apply.

Usage:
    python scripts/cleanup_ghost_agents.py          # dry-run
    python scripts/cleanup_ghost_agents.py --execute # apply
"""
import asyncio
import sys

BATCH_SIZE = 100


async def main(execute: bool = False):
    from src.db.postgres_backend import PostgresBackend

    db = PostgresBackend()
    await db.init()

    mode = "EXECUTE" if execute else "DRY-RUN"
    print(f"\n{'='*60}")
    print(f"  Ghost Agent Cleanup — {mode}")
    print(f"{'='*60}")

    async with db.acquire() as conn:
        # Count totals
        total = await conn.fetchval("SELECT COUNT(*) FROM core.agents")
        archived = await conn.fetchval(
            "SELECT COUNT(*) FROM core.agents WHERE status = 'archived'"
        )
        active = await conn.fetchval(
            "SELECT COUNT(*) FROM core.agents WHERE status = 'active'"
        )
        print(f"\n  Total agents:    {total}")
        print(f"  Active:          {active}")
        print(f"  Archived:        {archived}")

        # Find ghosts: archived agents with zero state rows
        ghosts = await conn.fetch("""
            SELECT a.id, a.label
            FROM core.agents a
            LEFT JOIN core.identities i ON i.agent_id = a.id
            LEFT JOIN core.agent_state s ON s.identity_id = i.identity_id
            WHERE a.status = 'archived'
            GROUP BY a.id, a.label
            HAVING COUNT(s.state_id) = 0
        """)
        print(f"  Ghosts (archived, zero state): {len(ghosts)}")

        # Archived agents WITH state (preserved)
        with_state = archived - len(ghosts)
        print(f"  Archived with state (kept):    {with_state}")

        if not ghosts:
            print("\n  No ghosts to clean up.")
            return

        if not execute:
            # Show sample
            print(f"\n  Sample ghosts (first 10):")
            for g in ghosts[:10]:
                label = g["label"] or "(no label)"
                print(f"    {g['id'][:12]}...  label={label}")
            print(f"\n  [DRY-RUN] No changes applied. Pass --execute to apply.")
            return

        # Delete in batches
        ghost_ids = [str(g["id"]) for g in ghosts]
        deleted_sessions = 0
        deleted_identities = 0
        deleted_agents = 0

        for i in range(0, len(ghost_ids), BATCH_SIZE):
            batch = ghost_ids[i:i + BATCH_SIZE]

            async with conn.transaction():
                # Delete sessions for these identities
                result = await conn.execute("""
                    DELETE FROM core.sessions
                    WHERE identity_id IN (
                        SELECT identity_id FROM core.identities
                        WHERE agent_id = ANY($1::text[])
                    )
                """, batch)
                deleted_sessions += int(result.split()[-1]) if result else 0

                # Delete identities
                result = await conn.execute(
                    "DELETE FROM core.identities WHERE agent_id = ANY($1::text[])",
                    batch,
                )
                deleted_identities += int(result.split()[-1]) if result else 0

                # Delete agents
                result = await conn.execute(
                    "DELETE FROM core.agents WHERE id = ANY($1::text[])",
                    batch,
                )
                deleted_agents += int(result.split()[-1]) if result else 0

            print(f"  [EXEC] Batch {i // BATCH_SIZE + 1}: "
                  f"deleted {len(batch)} agents")

        # Final counts
        remaining = await conn.fetchval("SELECT COUNT(*) FROM core.agents")
        remaining_archived = await conn.fetchval(
            "SELECT COUNT(*) FROM core.agents WHERE status = 'archived'"
        )

        print(f"\n  [RESULTS]")
        print(f"  Deleted sessions:   {deleted_sessions}")
        print(f"  Deleted identities: {deleted_identities}")
        print(f"  Deleted agents:     {deleted_agents}")
        print(f"  Remaining total:    {remaining}")
        print(f"  Remaining archived: {remaining_archived} (all have state data)")
        print(f"\n  Cleanup complete.")


if __name__ == "__main__":
    execute = "--execute" in sys.argv
    asyncio.run(main(execute=execute))

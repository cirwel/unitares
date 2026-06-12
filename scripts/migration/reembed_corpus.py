#!/usr/bin/env python3
"""
Re-embed the KG corpus into the active embedding model's table.

Reads every row in knowledge.discoveries, embeds summary + details with the
model selected by UNITARES_EMBEDDING_MODEL, and upserts into that model's
parallel pgvector table (see src/embeddings.py KNOWN_MODELS).

Usage:
    UNITARES_EMBEDDING_MODEL=bge-m3 python scripts/migration/reembed_corpus.py
    UNITARES_EMBEDDING_MODEL=bge-m3 python scripts/migration/reembed_corpus.py --limit 50 --dry-run

Idempotent: re-running upserts existing rows.

Phase 2 of docs/plans/2026-04-20-kg-retrieval-rebuild.md.
"""

import argparse
import asyncio
import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.db import get_db
from src.embeddings import (
    KNOWN_MODELS,
    DEFAULT_MODEL_KEY,
    EmbeddingsService,
)


MAX_DETAILS_CHARS = 500  # Keep embedding input bounded


def build_text(summary: str, details: str | None) -> str:
    if details:
        return f"{summary}\n{details[:MAX_DETAILS_CHARS]}"
    return summary


async def ensure_table_exists(db, table_qualified: str) -> None:
    """Raise a helpful error if the target table is missing.

    We don't auto-create here — the schema file is the source of truth. This is
    a precondition check, not a migration step.
    """
    schema, _, table = table_qualified.partition(".")
    async with db.acquire() as conn:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = $1 AND table_name = $2
            )
            """,
            schema, table,
        )
    if not exists:
        raise SystemExit(
            f"Target table {table_qualified} does not exist. "
            f"Apply the schema first: psql -d governance -f db/postgres/embeddings_bge_m3_schema.sql"
        )


async def fetch_all_ids(db) -> list[tuple[str, str, str]]:
    async with db.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, summary, COALESCE(details, '') AS details FROM knowledge.discoveries ORDER BY created_at DESC"
        )
    return [(r["id"], r["summary"], r["details"]) for r in rows]


async def upsert_batch(
    db,
    table_qualified: str,
    model_name: str,
    batch: list[tuple[str, list[float]]],
) -> int:
    if not batch:
        return 0
    rows_ok = 0
    async with db.acquire() as conn:
        for discovery_id, embedding in batch:
            embedding_str = "[" + ",".join(str(x) for x in embedding) + "]"
            await conn.execute(
                f"""
                INSERT INTO {table_qualified} (discovery_id, embedding, model_name)
                VALUES ($1, $2::vector, $3)
                ON CONFLICT (discovery_id) DO UPDATE SET
                    embedding = EXCLUDED.embedding,
                    model_name = EXCLUDED.model_name,
                    updated_at = now()
                """,
                discovery_id, embedding_str, model_name,
            )
            rows_ok += 1
    return rows_ok


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL_KEY,
                        help=f"Embedding model key (known: {list(KNOWN_MODELS)})")
    parser.add_argument("--limit", type=int, default=None, help="Max discoveries to re-embed")
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--only-missing", action="store_true",
                        help="Skip discoveries already present in the target table")
    args = parser.parse_args()

    if args.model not in KNOWN_MODELS:
        raise SystemExit(f"Unknown model {args.model!r}. Known: {list(KNOWN_MODELS)}")

    # Force the embeddings service to use the chosen model regardless of env.
    svc = EmbeddingsService(model_key=args.model)
    table = svc.table_name
    print(f"Re-embedding corpus into {table} using {svc.model_name} ({svc.dim}d)")
    if args.dry_run:
        print("DRY RUN — no database writes will be performed.")

    db = get_db()
    await ensure_table_exists(db, table)

    discoveries = await fetch_all_ids(db)
    if args.limit:
        discoveries = discoveries[: args.limit]
    print(f"Found {len(discoveries)} discoveries")

    if args.only_missing:
        async with db.acquire() as conn:
            rows = await conn.fetch(f"SELECT discovery_id FROM {table}")
        already = {r["discovery_id"] for r in rows}
        before = len(discoveries)
        discoveries = [d for d in discoveries if d[0] not in already]
        print(f"{before - len(discoveries)} already embedded; re-embedding {len(discoveries)}")

    if not discoveries:
        print("Nothing to do.")
        return

    t0 = time.perf_counter()
    total = 0
    bs = args.batch_size
    for i in range(0, len(discoveries), bs):
        chunk = discoveries[i : i + bs]
        texts = [build_text(s, d) for _, s, d in chunk]
        embeddings = await svc.embed_batch(texts, batch_size=bs)

        if args.dry_run:
            total += len(chunk)
            print(f"  [dry-run] would upsert {len(chunk)} (processed {total}/{len(discoveries)})")
            continue

        pairs = [(chunk[j][0], embeddings[j]) for j in range(len(chunk))]
        n = await upsert_batch(db, table, svc.model_name, pairs)
        total += n
        elapsed = time.perf_counter() - t0
        print(f"  upserted {n} ({total}/{len(discoveries)} · {elapsed:.1f}s)")

    elapsed = time.perf_counter() - t0
    print(f"\nDone. {total} embeddings written in {elapsed:.1f}s.")


if __name__ == "__main__":
    asyncio.run(main())

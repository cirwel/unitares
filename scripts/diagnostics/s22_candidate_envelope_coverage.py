#!/usr/bin/env python3
"""Report S22 write-context field coverage across persisted provenance rows.

Read-only. Replaces ad-hoc SQL when deciding whether a candidate envelope
field has enough dogfood evidence to promote out of "candidate" status.

Reads from:
  - core.agent_state.state_json.provenance_context
  - knowledge.discoveries.provenance.s22_context

Output groups fields by promotion status per Hermes's 2026-05-08 audit
(````):
  - promoted-core: must be present
  - fork-discriminator: R6 v2 fork fields (newly persisted 2026-05-08)
  - optional: present when meaningful, not required
  - candidate: deferred until targeted dogfood earns them

Usage:
    python3 scripts/diagnostics/s22_candidate_envelope_coverage.py
    python3 scripts/diagnostics/s22_candidate_envelope_coverage.py --comparison-key r6-h1-2026-05-08
    python3 scripts/diagnostics/s22_candidate_envelope_coverage.py --since 2026-05-08T00:00:00Z
    python3 scripts/diagnostics/s22_candidate_envelope_coverage.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Optional


sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

from src.db import close_db, get_db


PROMOTED_CORE = (
    "schema",
    "context_source",
    "harness_type",
    "transport",
    "model_provider",
    "model",
    "tool_surface",
    "comparison_key",
    "task_label",
    "task_outcome",
    "governance_mode",
)
FORK_DISCRIMINATOR = (
    "episode_fork_kind",
    "identity_lineage_fork",
)
OPTIONAL = (
    "memory_context",
    "verification_source",
    "thread_id",
    "session_resolution_source",
    "parent_agent_id",
    "spawn_reason",
)
CANDIDATE = (
    "affordance_state",
    "harness_id",
    "episode_id",
    "invocation_id",
    "process_instance_id",
    "identity_assurance",
    "locus",
    "label_at_write",
    "agent_uuid",
    "client_session_id",
)


def _has_field(context: Mapping[str, Any], field: str) -> bool:
    """A field is considered present if it has a non-empty, non-null value."""
    value = context.get(field)
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _coverage_block(
    contexts: list[Mapping[str, Any]],
    fields: Iterable[str],
) -> list[dict[str, Any]]:
    total = len(contexts)
    rows: list[dict[str, Any]] = []
    for field in fields:
        populated = sum(1 for c in contexts if _has_field(c, field))
        rows.append(
            {
                "field": field,
                "populated": populated,
                "total": total,
                "ratio": f"{populated}/{total}" if total else "0/0",
            }
        )
    return rows


async def _fetch_agent_state_contexts(
    pool: Any,
    comparison_key: Optional[str],
    since: Optional[str],
) -> list[dict[str, Any]]:
    where, params = _build_context_where(
        json_expr="state_json->'provenance_context'",
        timestamp_column="recorded_at",
        comparison_key=comparison_key,
        since=since,
    )
    sql = f"SELECT state_json->'provenance_context' AS pc FROM core.agent_state {where}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    out: list[dict[str, Any]] = []
    for row in rows:
        raw = row["pc"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                continue
        if isinstance(raw, dict):
            out.append(raw)
    return out


async def _fetch_kg_contexts(
    pool: Any,
    comparison_key: Optional[str],
    since: Optional[str],
) -> list[dict[str, Any]]:
    where, params = _build_context_where(
        json_expr="provenance->'s22_context'",
        timestamp_column="created_at",
        comparison_key=comparison_key,
        since=since,
    )
    sql = f"SELECT provenance->'s22_context' AS pc FROM knowledge.discoveries {where}"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    out: list[dict[str, Any]] = []
    for row in rows:
        raw = row["pc"]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                continue
        if isinstance(raw, dict):
            out.append(raw)
    return out


def _print_text(payload: dict[str, Any]) -> None:
    print(f"comparison_key: {payload.get('comparison_key') or '<all>'}")
    print(f"since: {payload.get('since') or '<all>'}")
    for source_label, source_key in (
        ("agent_state", "agent_state"),
        ("knowledge graph", "knowledge_graph"),
    ):
        block = payload[source_key]
        print(f"\n=== {source_label} (rows: {block['total']}) ===")
        if block["total"] == 0:
            print("  (no rows)")
            continue
        for group_label, group_key in (
            ("promoted-core", "promoted_core"),
            ("fork-discriminator", "fork_discriminator"),
            ("optional", "optional"),
            ("candidate", "candidate"),
        ):
            print(f"  {group_label}:")
            for row in block[group_key]:
                print(f"    {row['field']:<30} {row['ratio']}")


def _build_context_where(
    *,
    json_expr: str,
    timestamp_column: str,
    comparison_key: Optional[str],
    since: Optional[str],
) -> tuple[str, list[Any]]:
    clauses = [f"{json_expr} IS NOT NULL"]
    params: list[Any] = []
    if comparison_key:
        params.append(comparison_key)
        clauses.append(f"{json_expr}->>'comparison_key' = ${len(params)}")
    if since:
        params.append(since)
        clauses.append(f"{timestamp_column} >= ${len(params)}::timestamptz")
    return "WHERE " + " AND ".join(clauses), params


def _parse_since(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise argparse.ArgumentTypeError("--since must not be empty")
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--since must be an ISO timestamp, e.g. 2026-05-08T00:00:00Z"
        ) from exc


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--comparison-key",
        help="Restrict the diagnostic to one comparison_key.",
    )
    parser.add_argument(
        "--since",
        type=_parse_since,
        help=(
            "Restrict rows to writes at or after this timestamptz. "
            "Uses core.agent_state.recorded_at and knowledge.discoveries.created_at."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit full assessment payload as JSON.",
    )
    args = parser.parse_args()

    db = get_db()
    pool = getattr(db, "pool", None) or db
    try:
        agent_state = await _fetch_agent_state_contexts(
            pool,
            args.comparison_key,
            args.since,
        )
        kg = await _fetch_kg_contexts(pool, args.comparison_key, args.since)
    finally:
        await close_db()

    payload: dict[str, Any] = {
        "comparison_key": args.comparison_key,
        "since": args.since.isoformat() if args.since else None,
        "agent_state": {
            "total": len(agent_state),
            "promoted_core": _coverage_block(agent_state, PROMOTED_CORE),
            "fork_discriminator": _coverage_block(agent_state, FORK_DISCRIMINATOR),
            "optional": _coverage_block(agent_state, OPTIONAL),
            "candidate": _coverage_block(agent_state, CANDIDATE),
        },
        "knowledge_graph": {
            "total": len(kg),
            "promoted_core": _coverage_block(kg, PROMOTED_CORE),
            "fork_discriminator": _coverage_block(kg, FORK_DISCRIMINATOR),
            "optional": _coverage_block(kg, OPTIONAL),
            "candidate": _coverage_block(kg, CANDIDATE),
        },
    }

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_text(payload)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

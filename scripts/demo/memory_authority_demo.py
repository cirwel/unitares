#!/usr/bin/env python3
"""Deterministic false-memory demonstration for authority-aware retrieval."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.knowledge_authority import (
    PROMOTION_SCHEMA,
    PROMOTION_TAG,
    assess_authority,
    rank_by_authority,
)
from src.knowledge_graph import DiscoveryNode


def main() -> None:
    imported = DiscoveryNode(
        id="memory-1",
        agent_id="memory-bridge",
        type="note",
        summary="Production uses SQLite.",
        tags=["memory-sync", "source-external-memory"],
    )
    evidence = DiscoveryNode(
        id="evidence-1",
        agent_id="reviewer",
        type="observation",
        summary="Deployment configuration and integration tests use PostgreSQL 17.",
    )
    governed = DiscoveryNode(
        id="claim-1",
        agent_id="reviewer",
        type="insight",
        summary="Production uses PostgreSQL 17; the imported SQLite memory is stale.",
        tags=[PROMOTION_TAG, "promoted-from-memory"],
        provenance={
            "source": "explicit_promotion",
            "knowledge_authority": {
                "schema": PROMOTION_SCHEMA,
                "source_id": imported.id,
                "evidence_ids": [evidence.id],
            },
        },
    )
    raw = [imported, evidence, governed]
    scores = {"memory-1": 0.82, "evidence-1": 0.55, "claim-1": 0.61}
    ranked, _ = rank_by_authority(raw, relevance_scores=scores)

    def render(rows: list[DiscoveryNode]) -> list[dict]:
        return [
            {
                "id": row.id,
                "summary": row.summary,
                "relevance": scores[row.id],
                "authority": assess_authority(row).to_dict(),
            }
            for row in rows
        ]

    print(json.dumps({
        "raw_relevance_order": render(raw),
        "authority_aware_order": render(ranked),
    }, indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Synthetic negative controls for UNITARES/EISV ablation checks.

These fixtures deliberately create known-safe bad outcomes in memory or local
JSONL only. They are red-team controls for the analysis path, not a write
adapter: do not import them into production outcome tables, KG, dialectic, or
CI gates.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.analysis.eisv_ablation_matrix import (  # noqa: E402
    AblationMatrixRow,
    build_matrix_row,
)
from scripts.analysis.eisv_skeptic_report import OutcomeRow  # noqa: E402
from scripts.analysis.outcome_inventory import (  # noqa: E402
    OutcomeInventory,
    OutcomeInventoryRow,
    build_inventory,
)

SYNTHETIC_FIXTURE_SOURCE = "synthetic_fixture"
EVENT_TYPE = "ablation_negative_controls"
DEFAULT_COUNT = 60


def _ensure_utc(dt: datetime | None) -> datetime:
    """Normalize optional datetimes to timezone-aware UTC."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an optional ISO timestamp, accepting a trailing Z."""
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _outcome_for_index(index: int) -> tuple[str, bool, float, float, str]:
    """Return outcome fields for a deterministic synthetic fixture row."""
    if index % 3 == 2:
        outcome_type = "test_failed" if (index // 3) % 2 == 0 else "tool_rejected"
        return outcome_type, True, 0.0, 0.92, "high-risk"
    return "test_passed", False, 1.0, 0.08, "safe"


def _detail(index: int) -> dict[str, Any]:
    """Return non-secret fixture metadata that explicitly forbids persistence."""
    return {
        "synthetic_negative_control": True,
        "do_not_persist": True,
        "fixture_id": f"negative-control-{index:03d}",
        "prediction_id": f"synthetic-prediction-{index:03d}",
        "prediction_binding": "synthetic_negative_control",
        "hard_exogenous": True,
        "eprocess_eligible": True,
        "red_team_fixture": "known_safe_bad_outcome",
    }


def build_negative_control_outcome_rows(
    *,
    generated_at: datetime | None = None,
    count: int = DEFAULT_COUNT,
) -> list[OutcomeRow]:
    """Build synthetic rows with a known bad class and predictive prior risk."""
    if count <= 0:
        raise ValueError("count must be positive")
    observed_at = _ensure_utc(generated_at)
    rows: list[OutcomeRow] = []
    previous_bad = False
    for index in range(count):
        outcome_type, is_bad, outcome_score, risk, verdict = _outcome_for_index(index)
        ts = observed_at + timedelta(minutes=index)
        row = OutcomeRow(
            ts=ts,
            agent_id=f"synthetic-negative-control/agent-{index % 4}",
            outcome_type=outcome_type,
            is_bad=is_bad,
            outcome_score=outcome_score,
            verification_source=SYNTHETIC_FIXTURE_SOURCE,
            reported_confidence=0.8 if not is_bad else 0.6,
            reported_complexity=0.2,
            detail=_detail(index),
            prior_state_age_seconds=30.0,
            prior_risk=risk,
            prior_phi=1.0 - risk,
            prior_verdict=verdict,
            prior_coherence=0.8 if not is_bad else 0.35,
            prior_e=0.7,
            prior_i=0.75 if not is_bad else 0.35,
            prior_s=0.12 if not is_bad else 0.86,
            prior_v=0.05 if not is_bad else 0.45,
            snapshot_verdict=None,
            snapshot_e=None,
            snapshot_i=None,
            snapshot_s=None,
            snapshot_v=None,
            snapshot_phi=None,
            snapshot_coherence=None,
            row_key=f"synthetic-negative-control-{index:03d}",
            previous_bad=previous_bad,
        )
        rows.append(row)
        previous_bad = is_bad
    return rows


def _inventory_rows(
    rows: Sequence[OutcomeRow],
    lead_minutes: Sequence[float],
) -> list[OutcomeInventoryRow]:
    """Convert skeptic-report rows into outcome-inventory rows."""
    leads = tuple(float(lead) for lead in lead_minutes)
    return [
        OutcomeInventoryRow(
            outcome_type=row.outcome_type,
            is_bad=row.is_bad,
            verification_source=row.verification_source,
            detail=row.detail,
            prior_state_by_lead={lead: True for lead in leads},
        )
        for row in rows
    ]


def build_negative_control_inventory(
    *,
    generated_at: datetime | None = None,
    count: int = DEFAULT_COUNT,
    lead_minutes: Sequence[float] = (0, 5, 30),
) -> OutcomeInventory:
    """Build an inventory report over synthetic negative-control rows."""
    rows = build_negative_control_outcome_rows(
        generated_at=generated_at,
        count=count,
    )
    return build_inventory(_inventory_rows(rows, lead_minutes), lead_minutes=lead_minutes)


def build_negative_control_matrix_rows(
    *,
    generated_at: datetime | None = None,
    count: int = DEFAULT_COUNT,
    scopes: Sequence[str] = ("strict", "task"),
    window_days: int = 90,
    lead_minutes: float = 5,
) -> list[AblationMatrixRow]:
    """Build ablation matrix rows labeled as synthetic negative controls."""
    rows = build_negative_control_outcome_rows(
        generated_at=generated_at,
        count=count,
    )
    matrix_rows: list[AblationMatrixRow] = []
    for scope in scopes:
        if scope not in {"strict", "task"}:
            raise ValueError("scope must be strict or task")
        matrix = build_matrix_row(
            rows,
            scope=scope,
            window_days=window_days,
            lead_minutes=lead_minutes,
            train_fraction=0.7,
            min_feature_rows=6,
        )
        matrix_rows.append(
            replace(
                matrix,
                conclusion=(
                    "SYNTHETIC NEGATIVE CONTROL: analysis path observed a "
                    f"known bad class; not validation. {matrix.conclusion}"
                ),
            )
        )
    return matrix_rows


def serialize_outcome_rows(rows: Sequence[OutcomeRow]) -> list[dict[str, Any]]:
    """Serialize synthetic rows for local JSONL fixture files."""
    serialized: list[dict[str, Any]] = []
    for row in rows:
        item = asdict(row)
        item["ts"] = row.ts.isoformat()
        serialized.append(item)
    return serialized


def _write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    """Write synthetic rows to a local JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def _strict_bad(rows: Sequence[OutcomeRow]) -> int:
    """Count synthetic strict bad rows."""
    return sum(
        1
        for row in rows
        if row.is_bad and row.outcome_type in {"test_failed", "tool_rejected"}
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated-at", default=None)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT)
    parser.add_argument("--output-jsonl", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Generate synthetic negative-control fixtures without real writes."""
    args = parse_args(argv)
    rows = build_negative_control_outcome_rows(
        generated_at=_parse_datetime(args.generated_at),
        count=args.count,
    )
    if args.output_jsonl is not None:
        _write_jsonl(args.output_jsonl, serialize_outcome_rows(rows))
        status = "fixtures_written"
    else:
        status = "fixtures_built"
    print(
        json.dumps(
            {
                "event_type": EVENT_TYPE,
                "mode": "synthetic_only",
                "status": status,
                "generated_rows": len(rows),
                "strict_bad": _strict_bad(rows),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

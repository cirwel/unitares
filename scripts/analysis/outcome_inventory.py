#!/usr/bin/env python3
"""Inventory outcome-event provenance and prior-state coverage.

This is a read-only diagnostic for UNITARES/EISV validation work. It does not
claim predictive lift; it answers the prerequisite question: what outcome data
exists, how objective is it, how many failures are present, and whether prior
agent state is available at prospective lead times.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:***@localhost:5432/governance",
)

STRICT_OUTCOMES = frozenset({"test_passed", "test_failed", "tool_rejected"})
STRICT_BAD_MIN_FOR_VALIDATION = 10
TASK_OUTCOMES = frozenset(
    {
        "test_passed",
        "test_failed",
        "tool_rejected",
        "task_completed",
        "task_failed",
    }
)


@dataclass(frozen=True)
class OutcomeInventoryRow:
    """One outcome row with enough metadata for inventory aggregation."""

    outcome_type: str
    is_bad: bool
    verification_source: str | None
    detail: Mapping[str, Any] = field(default_factory=dict)
    prior_state_by_lead: Mapping[float, bool] = field(default_factory=dict)


@dataclass
class InventoryBucket:
    """Aggregated outcome bucket for one evidence/provenance lane."""

    scope: str
    outcome_type: str
    verification_source: str
    hard_exogenous: bool
    eprocess_eligible: bool
    prediction_binding: str
    harness_lane: str = "substrate"
    n_total: int = 0
    n_bad: int = 0
    prediction_id_count: int = 0
    registry_prediction_bound_count: int = 0
    prior_state_counts: dict[float, int] = field(default_factory=dict)

    @property
    def bad_rate(self) -> float | None:
        """Return bad-outcome rate for this bucket, or None if empty."""
        if self.n_total == 0:
            return None
        return self.n_bad / self.n_total


@dataclass(frozen=True)
class OutcomeInventory:
    """Complete outcome inventory plus totals used in the printed report."""

    buckets: tuple[InventoryBucket, ...]
    lead_minutes: tuple[float, ...]
    total_outcomes: int
    total_bad: int
    strict_outcomes: int
    strict_bad: int
    hard_exogenous_count: int
    eprocess_eligible_count: int
    total_prediction_id_count: int
    registry_prediction_bound_count: int = 0
    eprocess_eligible_by_harness_lane: dict[str, int] = field(default_factory=dict)
    registry_prediction_bound_by_harness_lane: dict[str, int] = field(default_factory=dict)

    @property
    def total_bad_rate(self) -> float | None:
        """Return global bad-outcome rate, or None if there are no outcomes."""
        if self.total_outcomes == 0:
            return None
        return self.total_bad / self.total_outcomes


def _truthy(value: Any) -> bool:
    """Interpret JSON-ish booleans without treating arbitrary strings as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return False


def _normalized_detail(detail: Mapping[str, Any] | str | None) -> Mapping[str, Any]:
    """Return a dict-like detail mapping from asyncpg JSONB or test fixtures."""
    if detail is None:
        return {}
    if isinstance(detail, str):
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, Mapping) else {}
    return detail if isinstance(detail, Mapping) else {}


_CONTROLLED_FIXTURE_FLAGS = frozenset(
    {
        "synthetic_calibration_fixture",
        "synthetic_negative_control",
        "do_not_use_for_live_validation",
        "do_not_persist",
        "calibration_excluded",
    }
)
_CONTROLLED_FIXTURE_BINDINGS = frozenset({"synthetic_negative_control"})
_CONTROLLED_FIXTURE_TEST_NAMES = frozenset({"clean_control", "overconfidence_probe"})


def is_controlled_validation_fixture(detail: Mapping[str, Any] | str | None) -> bool:
    """Return whether an outcome detail belongs to a controlled validation fixture.

    These rows are useful for harness plumbing and local red-team checks, but they
    must not be counted as live fleet validation evidence. Keep this deliberately
    conservative: explicit fixture flags win, and the legacy one-shot calibration
    probe names cover rows written before the flags existed.
    """
    normalized = _normalized_detail(detail)
    if any(_truthy(normalized.get(flag)) for flag in _CONTROLLED_FIXTURE_FLAGS):
        return True
    binding = normalized.get("prediction_binding")
    if binding in _CONTROLLED_FIXTURE_BINDINGS:
        return True
    test_name = normalized.get("test_name")
    return bool(test_name in _CONTROLLED_FIXTURE_TEST_NAMES)


def _verification_source(row: OutcomeInventoryRow) -> str:
    """Prefer promoted verification_source column; fall back to detail JSON."""
    if row.verification_source:
        return row.verification_source
    detail = _normalized_detail(row.detail)
    source = detail.get("verification_source")
    return str(source) if source else "unknown"


def _scope_for_outcome(outcome_type: str) -> str:
    """Classify rows into strict, broader task, or other outcome scopes."""
    if outcome_type in STRICT_OUTCOMES:
        return "strict"
    if outcome_type in TASK_OUTCOMES:
        return "task"
    return "other"


def _grouped_outcome_type(outcome_type: str) -> str:
    """Group pass/fail pairs where the inventory question is failure coverage."""
    if outcome_type in {"test_passed", "test_failed"}:
        return "test_passed/test_failed"
    return outcome_type


def _prediction_binding(detail: Mapping[str, Any]) -> str:
    """Return prediction-binding label, using a stable 'none' value when absent."""
    binding = detail.get("prediction_binding")
    return str(binding) if binding else "none"


def _has_prediction_id(detail: Mapping[str, Any]) -> bool:
    """Return whether an outcome references an explicit prediction id."""
    prediction_id = detail.get("prediction_id")
    return prediction_id not in (None, "")


def _is_registry_prediction_bound(detail: Mapping[str, Any]) -> bool:
    """Return whether an outcome has a real prospective registry binding."""
    return _has_prediction_id(detail) and _prediction_binding(detail) == "registry"


def harness_lane_from_detail(detail: Mapping[str, Any] | str | None) -> str:
    """Return the analysis lane for a row's harness provenance.

    Rows without an explicit harness belong to the substrate lane. Runtime
    harnesses such as the BEAM dispatch harness are useful telemetry, but they
    must be visible as their own lane before any EISV predictive read.
    """
    normalized = _normalized_detail(detail)
    harness = normalized.get("harness") or normalized.get("harness_type")
    if not harness:
        return "substrate"
    return str(harness)


def build_inventory(
    rows: Sequence[OutcomeInventoryRow],
    *,
    lead_minutes: Sequence[float],
) -> OutcomeInventory:
    """Aggregate outcome rows by provenance, objectivity, and lead coverage."""
    leads = tuple(float(lead) for lead in lead_minutes)
    buckets: dict[tuple[str, str, str, bool, bool, str, str], InventoryBucket] = {}

    total_outcomes = 0
    total_bad = 0
    strict_outcomes = 0
    strict_bad = 0
    hard_exogenous_count = 0
    eprocess_eligible_count = 0
    eprocess_eligible_by_harness_lane: dict[str, int] = {}
    total_prediction_id_count = 0
    registry_prediction_bound_count = 0
    registry_prediction_bound_by_harness_lane: dict[str, int] = {}

    for row in rows:
        detail = _normalized_detail(row.detail)
        scope = _scope_for_outcome(row.outcome_type)
        hard_exogenous = _truthy(detail.get("hard_exogenous"))
        eprocess_eligible = _truthy(detail.get("eprocess_eligible"))
        prediction_binding = _prediction_binding(detail)
        harness_lane = harness_lane_from_detail(detail)
        key = (
            scope,
            _grouped_outcome_type(row.outcome_type),
            _verification_source(row),
            hard_exogenous,
            eprocess_eligible,
            prediction_binding,
            harness_lane,
        )
        bucket = buckets.get(key)
        if bucket is None:
            bucket = InventoryBucket(
                scope=key[0],
                outcome_type=key[1],
                verification_source=key[2],
                hard_exogenous=key[3],
                eprocess_eligible=key[4],
                prediction_binding=key[5],
                harness_lane=key[6],
                prior_state_counts={lead: 0 for lead in leads},
            )
            buckets[key] = bucket

        bucket.n_total += 1
        total_outcomes += 1
        if row.is_bad:
            bucket.n_bad += 1
            total_bad += 1
            if scope == "strict":
                strict_bad += 1
        if scope == "strict":
            strict_outcomes += 1
        if hard_exogenous:
            hard_exogenous_count += 1
        if eprocess_eligible:
            eprocess_eligible_count += 1
            eprocess_eligible_by_harness_lane[harness_lane] = (
                eprocess_eligible_by_harness_lane.get(harness_lane, 0) + 1
            )
        if _has_prediction_id(detail):
            bucket.prediction_id_count += 1
            total_prediction_id_count += 1
        if _is_registry_prediction_bound(detail):
            bucket.registry_prediction_bound_count += 1
            registry_prediction_bound_count += 1
            registry_prediction_bound_by_harness_lane[harness_lane] = (
                registry_prediction_bound_by_harness_lane.get(harness_lane, 0) + 1
            )
        for lead in leads:
            if bool(row.prior_state_by_lead.get(lead, False)):
                bucket.prior_state_counts[lead] += 1

    sorted_buckets = tuple(
        sorted(
            buckets.values(),
            key=lambda bucket: (
                bucket.scope,
                bucket.outcome_type,
                bucket.verification_source,
                bucket.hard_exogenous,
                bucket.eprocess_eligible,
                bucket.prediction_binding,
                bucket.harness_lane,
            ),
        )
    )
    return OutcomeInventory(
        buckets=sorted_buckets,
        lead_minutes=leads,
        total_outcomes=total_outcomes,
        total_bad=total_bad,
        strict_outcomes=strict_outcomes,
        strict_bad=strict_bad,
        hard_exogenous_count=hard_exogenous_count,
        eprocess_eligible_count=eprocess_eligible_count,
        total_prediction_id_count=total_prediction_id_count,
        registry_prediction_bound_count=registry_prediction_bound_count,
        eprocess_eligible_by_harness_lane=dict(
            sorted(eprocess_eligible_by_harness_lane.items())
        ),
        registry_prediction_bound_by_harness_lane=dict(
            sorted(registry_prediction_bound_by_harness_lane.items())
        ),
    )


def _format_rate(rate: float | None) -> str:
    """Format a bad-rate value for CLI output."""
    if rate is None:
        return "n/a"
    return f"{rate:.3f}"


def _format_lead(lead: float) -> str:
    """Format a lead-minute value for stable column names."""
    return str(int(lead)) if float(lead).is_integer() else str(lead).replace(".", "p")


def _harness_lane_summary_rows(
    inventory: OutcomeInventory,
    *,
    lead_minutes: Sequence[float],
) -> list[list[str]]:
    """Summarize outcome, task-scope, and prior-state coverage by harness lane."""
    leads = tuple(float(lead) for lead in lead_minutes)
    summaries: dict[str, dict[str, Any]] = {}
    for bucket in inventory.buckets:
        lane_summary = summaries.setdefault(
            bucket.harness_lane,
            {
                "outcomes": 0,
                "bad": 0,
                "strict_outcomes": 0,
                "strict_bad": 0,
                "task_scope_outcomes": 0,
                "task_scope_bad": 0,
                "eprocess_eligible": 0,
                "prediction_id": 0,
                "registry_prediction_bound": 0,
                "prior_state": {lead: 0 for lead in leads},
            },
        )
        lane_summary["outcomes"] += bucket.n_total
        lane_summary["bad"] += bucket.n_bad
        if bucket.scope == "strict":
            lane_summary["strict_outcomes"] += bucket.n_total
            lane_summary["strict_bad"] += bucket.n_bad
        if bucket.scope in {"strict", "task"}:
            lane_summary["task_scope_outcomes"] += bucket.n_total
            lane_summary["task_scope_bad"] += bucket.n_bad
        if bucket.eprocess_eligible:
            lane_summary["eprocess_eligible"] += bucket.n_total
        lane_summary["prediction_id"] += bucket.prediction_id_count
        lane_summary["registry_prediction_bound"] += bucket.registry_prediction_bound_count
        for lead in leads:
            lane_summary["prior_state"][lead] += bucket.prior_state_counts.get(lead, 0)

    rows: list[list[str]] = []
    for lane, summary in sorted(summaries.items()):
        outcomes = int(summary["outcomes"])
        prior_state = summary["prior_state"]
        rows.append(
            [
                lane,
                str(outcomes),
                str(summary["bad"]),
                str(summary["strict_outcomes"]),
                str(summary["strict_bad"]),
                str(summary["task_scope_outcomes"]),
                str(summary["task_scope_bad"]),
                str(summary["eprocess_eligible"]),
                str(summary["prediction_id"]),
                str(summary["registry_prediction_bound"]),
                *[f"{prior_state[lead]}/{outcomes}" for lead in leads],
            ]
        )
    return rows


def format_inventory_report(
    inventory: OutcomeInventory,
    *,
    window_days: int,
    lead_minutes: Sequence[float],
) -> str:
    """Render a compact terminal-friendly markdown report."""
    leads = tuple(float(lead) for lead in lead_minutes)
    lines = [
        "# Outcome Inventory",
        "",
        f"window_days: {window_days}",
        f"lead_minutes: {', '.join(_format_lead(lead) for lead in leads)}",
        f"total_outcomes: {inventory.total_outcomes}",
        f"total_bad: {inventory.total_bad}",
        f"total_bad_rate: {_format_rate(inventory.total_bad_rate)}",
        f"strict_outcomes: {inventory.strict_outcomes}",
        f"strict_bad: {inventory.strict_bad}",
        f"strict_bad_min_for_validation: {STRICT_BAD_MIN_FOR_VALIDATION}",
        f"strict_bad_gap_to_min: {max(0, STRICT_BAD_MIN_FOR_VALIDATION - inventory.strict_bad)}",
        f"hard_exogenous: {inventory.hard_exogenous_count}",
        f"eprocess_eligible: {inventory.eprocess_eligible_count}",
        *[
            f"eprocess_eligible_{lane}: {count}"
            for lane, count in sorted(
                inventory.eprocess_eligible_by_harness_lane.items()
            )
        ],
        f"prediction_id_present: {inventory.total_prediction_id_count}",
        f"registry_prediction_bound: {inventory.registry_prediction_bound_count}",
        *[
            f"registry_prediction_bound_{lane}: {count}"
            for lane, count in sorted(
                inventory.registry_prediction_bound_by_harness_lane.items()
            )
        ],
        "",
        "## Interpretation rule",
        "",
        "`bad` is an outcome-label class (`is_bad=true`), not a moral verdict or a prevented outcome. CI/test failure is task-negative evidence; strict governance-bad claims require external outcome evidence for contract, authority, or harm boundaries.",
        "EISV is proprioceptive telemetry and policy input, not a bad-verdict dispenser, outcome-truth source, or enforcement evidence.",
        "",
        "## Harness Lane Summary",
        "",
    ]

    summary_headers = [
        "Lane",
        "Outcomes",
        "Bad",
        "Strict outcomes",
        "Strict bad",
        "Task-scope outcomes",
        "Task-scope bad",
        "E-process eligible",
        "Prediction IDs",
        "Registry-bound predictions",
        *[f"Prior state {_format_lead(lead)}m" for lead in leads],
    ]
    lines.append("| " + " | ".join(summary_headers) + " |")
    lines.append("|" + "|".join("---" for _ in summary_headers) + "|")
    for row in _harness_lane_summary_rows(inventory, lead_minutes=leads):
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")

    lead_headers = [f"prior_state_{_format_lead(lead)}m" for lead in leads]
    headers = [
        "scope",
        "outcome_type",
        "verification_source",
        "harness_lane",
        "hard_exogenous",
        "eprocess_eligible",
        "prediction_binding",
        "n_total",
        "n_bad",
        "bad_rate",
        "prediction_id",
        *lead_headers,
    ]
    lines.append("\t".join(headers))
    for bucket in inventory.buckets:
        prior_columns = [
            f"{bucket.prior_state_counts.get(lead, 0)}/{bucket.n_total}"
            for lead in leads
        ]
        lines.append(
            "\t".join(
                [
                    bucket.scope,
                    bucket.outcome_type,
                    bucket.verification_source,
                    bucket.harness_lane,
                    str(bucket.hard_exogenous).lower(),
                    str(bucket.eprocess_eligible).lower(),
                    bucket.prediction_binding,
                    str(bucket.n_total),
                    str(bucket.n_bad),
                    _format_rate(bucket.bad_rate),
                    str(bucket.prediction_id_count),
                    *prior_columns,
                ]
            )
        )
    return "\n".join(lines)


def parse_leads(value: str) -> tuple[float, ...]:
    """Parse comma-separated lead-minute values."""
    leads = tuple(float(part.strip()) for part in value.split(",") if part.strip())
    if not leads:
        raise argparse.ArgumentTypeError("at least one lead minute is required")
    return leads


def _lead_alias(index: int) -> str:
    """Return the SQL alias used for a lead index."""
    return f"prior_state_lead_{index}"


def _build_fetch_query(lead_minutes: Sequence[float]) -> str:
    """Build the read-only outcome inventory query for the requested leads."""
    lead_selects = []
    for index, _lead in enumerate(lead_minutes):
        placeholder = index + 2
        lead_selects.append(f"""
                EXISTS (
                    SELECT 1
                    FROM core.identities ident
                    JOIN core.agent_state state
                      ON state.identity_id = ident.identity_id
                    WHERE ident.agent_id = o.agent_id
                      AND state.synthetic IS NOT TRUE
                      AND state.recorded_at <= o.ts - (${placeholder}::double precision * INTERVAL '1 minute')
                    LIMIT 1
                ) AS {_lead_alias(index)}
            """)
    return f"""
        SELECT
            o.outcome_type,
            o.is_bad,
            COALESCE(o.verification_source, o.detail->>'verification_source') AS verification_source,
            o.detail,
            {", ".join(lead_selects)}
        FROM audit.outcome_events o
        WHERE o.ts >= now() - ($1::int * INTERVAL '1 day')
        ORDER BY o.ts ASC
    """


def _row_from_record(
    record: Mapping[str, Any], lead_minutes: Sequence[float]
) -> OutcomeInventoryRow:
    """Convert an asyncpg record to an inventory row."""
    data = dict(record)
    prior_state_by_lead = {
        float(lead): bool(data[_lead_alias(index)])
        for index, lead in enumerate(lead_minutes)
    }
    return OutcomeInventoryRow(
        outcome_type=str(data["outcome_type"]),
        is_bad=bool(data["is_bad"]),
        verification_source=data.get("verification_source"),
        detail=_normalized_detail(data.get("detail")),
        prior_state_by_lead=prior_state_by_lead,
    )


async def fetch_rows(
    db_url: str,
    *,
    window_days: int,
    lead_minutes: Sequence[float],
) -> list[OutcomeInventoryRow]:
    """Fetch outcome inventory rows from PostgreSQL without mutating state."""
    try:
        asyncpg = importlib.import_module("asyncpg")
    except ImportError:
        print(
            "error: asyncpg not installed. Install with `pip install asyncpg`.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None

    leads = tuple(float(lead) for lead in lead_minutes)
    conn = await asyncpg.connect(db_url)
    try:
        records = await conn.fetch(_build_fetch_query(leads), window_days, *leads)
    finally:
        await conn.close()
    return [
        row
        for record in records
        if not is_controlled_validation_fixture(
            (row := _row_from_record(record, leads)).detail
        )
    ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window-days", type=int, default=365)
    parser.add_argument("--leads", type=parse_leads, default=(0.0, 5.0, 30.0))
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--output", help="Optional report output path")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    """Run the inventory query and print or write the report."""
    rows = await fetch_rows(
        args.db_url,
        window_days=args.window_days,
        lead_minutes=args.leads,
    )
    inventory = build_inventory(rows, lead_minutes=args.leads)
    report = format_inventory_report(
        inventory,
        window_days=args.window_days,
        lead_minutes=args.leads,
    )
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report + "\n", encoding="utf-8")
        print(f"Wrote {output_path}")
    else:
        print(report)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

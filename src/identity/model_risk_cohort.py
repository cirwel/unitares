"""Prospective, descriptive risk cohorts by exact model and harness.

Only ``s22.runtime_provenance.v1`` state rows can enter an exact-model cohort.
Legacy flat fields, identity display names, and inferred model families remain
visible as attrition categories and are never retroactively attributed.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from statistics import mean, median
from typing import Any, Optional

from src.db import get_db
from src.model_harness_provenance import (
    exact_model_attribution_status,
    normalize_persisted_runtime_provenance,
)


MODEL_RISK_COHORT_SQL = """
WITH measured AS (
    SELECT
        s.state_id,
        s.identity_id,
        i.agent_id AS agent_uuid,
        s.recorded_at,
        s.risk_score,
        s.state_json,
        row_number() OVER (
            PARTITION BY s.identity_id ORDER BY s.recorded_at, s.state_id
        ) AS measured_update_index,
        min(s.recorded_at) OVER (PARTITION BY s.identity_id) AS session_started_at
    FROM core.agent_state s
    JOIN core.identities i ON i.identity_id = s.identity_id
    WHERE COALESCE(s.synthetic, false) = false
)
SELECT
    state_id::TEXT AS state_id,
    agent_uuid,
    recorded_at,
    risk_score,
    state_json->'provenance_context'->'runtime_provenance'
        AS runtime_provenance,
    state_json#>>'{provenance_context,task_type}' AS task_type,
    state_json#>>'{eisv_telemetry,measurement,behavioral,updates}'
        AS behavioral_updates,
    state_json#>>'{eisv_telemetry,measurement,behavioral,warmup,is_baselined}'
        AS is_baselined,
    measured_update_index,
    EXTRACT(EPOCH FROM (recorded_at - session_started_at)) AS exposure_seconds
FROM measured
WHERE recorded_at >= $1
  AND recorded_at < $2
ORDER BY recorded_at, state_id
LIMIT $3
"""


@dataclass(frozen=True)
class ModelRiskObservation:
    state_id: str
    agent_uuid: Optional[str]
    recorded_at: Optional[str]
    risk_score: Optional[float]
    attribution_status: str
    model_identifier: Optional[str]
    model_provider: Optional[str]
    model_source: str
    harness_type: Optional[str]
    harness_version: Optional[str]
    readiness: str
    update_count: Optional[int]
    update_count_source: str
    update_bucket: str
    task_type: str
    exposure_seconds: Optional[float]
    exposure_bucket: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, Mapping) else {}
    return {}


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError):
        return default


def _optional_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _risk(value: Any) -> Optional[float]:
    parsed = _optional_float(value)
    if parsed is None or not 0.0 <= parsed <= 1.0:
        return None
    return parsed


def _optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return None


def _time_text(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value:
        return value
    return None


def update_count_bucket(value: Optional[int]) -> str:
    if value is None:
        return "unavailable"
    if value <= 2:
        return "0-2"
    if value <= 24:
        return "3-24"
    if value <= 99:
        return "25-99"
    return "100+"


def exposure_window_bucket(value: Optional[float]) -> str:
    if value is None:
        return "unavailable"
    if value < 15 * 60:
        return "<15m"
    if value < 60 * 60:
        return "15-60m"
    if value < 4 * 60 * 60:
        return "1-4h"
    return "4h+"


def normalize_model_risk_observation(raw: Mapping[str, Any]) -> ModelRiskObservation:
    """Normalize one query row without consulting identity labels."""
    runtime_raw = _mapping(_row_value(raw, "runtime_provenance"))
    runtime = normalize_persisted_runtime_provenance(runtime_raw)
    attribution_status = exact_model_attribution_status(runtime_raw)
    model = runtime["model"]
    harness = runtime["harness"]

    behavioral_updates = _optional_int(_row_value(raw, "behavioral_updates"))
    measured_index = _optional_int(_row_value(raw, "measured_update_index"))
    if behavioral_updates is not None:
        update_count = behavioral_updates
        update_source = "behavioral_telemetry"
    elif measured_index is not None:
        update_count = measured_index
        update_source = "measured_state_index"
    else:
        update_count = None
        update_source = "unavailable"

    baselined = _optional_bool(_row_value(raw, "is_baselined"))
    readiness = "warm" if baselined is True else "cold" if baselined is False else "unknown"
    exposure = _optional_float(_row_value(raw, "exposure_seconds"))
    if exposure is not None and exposure < 0:
        exposure = None
    task_type = str(_row_value(raw, "task_type") or "unavailable").strip()
    if not task_type:
        task_type = "unavailable"

    return ModelRiskObservation(
        state_id=str(_row_value(raw, "state_id") or "unknown"),
        agent_uuid=(
            str(_row_value(raw, "agent_uuid"))
            if _row_value(raw, "agent_uuid")
            else None
        ),
        recorded_at=_time_text(_row_value(raw, "recorded_at")),
        risk_score=_risk(_row_value(raw, "risk_score")),
        attribution_status=attribution_status,
        model_identifier=model["identifier"],
        model_provider=model["provider"],
        model_source=model["source"],
        harness_type=harness["type"],
        harness_version=harness["version"],
        readiness=readiness,
        update_count=update_count,
        update_count_source=update_source,
        update_bucket=update_count_bucket(update_count),
        task_type=task_type,
        exposure_seconds=exposure,
        exposure_bucket=exposure_window_bucket(exposure),
    )


async def collect_model_risk_observations(
    *,
    capture_start: datetime,
    capture_end: datetime,
    row_limit: int = 100_000,
    db: Optional[Any] = None,
) -> tuple[ModelRiskObservation, ...]:
    """Read measured state rows inside an explicit prospective window."""
    if capture_start.tzinfo is None or capture_end.tzinfo is None:
        raise ValueError("capture_start and capture_end must be timezone-aware")
    if capture_end <= capture_start:
        raise ValueError("capture_end must be after capture_start")
    if row_limit <= 0:
        raise ValueError("row_limit must be positive")

    backend = db or get_db()
    async with backend.acquire() as conn:
        rows = await conn.fetch(
            MODEL_RISK_COHORT_SQL,
            capture_start,
            capture_end,
            row_limit,
        )
    return tuple(normalize_model_risk_observation(row) for row in rows)


def _cohort_key(row: ModelRiskObservation) -> tuple[str, ...]:
    return (
        row.model_identifier or "unavailable",
        row.model_provider or "unavailable",
        row.harness_type or "unavailable",
        row.harness_version or "unavailable",
        row.readiness,
        row.update_bucket,
        row.task_type,
        row.exposure_bucket,
    )


def _cohort_summary(key: tuple[str, ...], rows: Sequence[ModelRiskObservation]) -> dict[str, Any]:
    risks = [row.risk_score for row in rows if row.risk_score is not None]
    return {
        "model": key[0],
        "model_provider": key[1],
        "harness_type": key[2],
        "harness_version": key[3],
        "readiness": key[4],
        "update_bucket": key[5],
        "task_type": key[6],
        "exposure_bucket": key[7],
        "n": len(risks),
        "mean_risk": round(mean(risks), 6) if risks else None,
        "median_risk": round(median(risks), 6) if risks else None,
        "min_risk": round(min(risks), 6) if risks else None,
        "max_risk": round(max(risks), 6) if risks else None,
        "risk_ge_0_5": sum(value >= 0.5 for value in risks),
        "risk_ge_0_7": sum(value >= 0.7 for value in risks),
        "distinct_sessions": len({row.agent_uuid for row in rows if row.agent_uuid}),
    }


def build_model_risk_cohort_report(
    observations: Sequence[ModelRiskObservation | Mapping[str, Any]],
    *,
    capture_start: str,
    capture_end: str,
    min_cell_size: int = 10,
    row_limit: Optional[int] = None,
) -> dict[str, Any]:
    """Build exact cohorts plus a like-for-like warm comparison readiness gate."""
    if min_cell_size <= 0:
        raise ValueError("min_cell_size must be positive")
    normalized = tuple(
        row
        if isinstance(row, ModelRiskObservation)
        else normalize_model_risk_observation(row)
        for row in observations
    )
    attrition = Counter(row.attribution_status for row in normalized)
    exact_rows = tuple(
        row
        for row in normalized
        if row.attribution_status == "eligible_exact" and row.risk_score is not None
    )

    grouped: dict[tuple[str, ...], list[ModelRiskObservation]] = defaultdict(list)
    for row in exact_rows:
        grouped[_cohort_key(row)].append(row)
    cohorts = [
        _cohort_summary(key, rows)
        for key, rows in sorted(grouped.items())
    ]

    warm_cells: dict[tuple[str, str, str], dict[tuple[str, ...], list[ModelRiskObservation]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in exact_rows:
        if row.readiness != "warm":
            continue
        cell = (row.update_bucket, row.task_type, row.exposure_bucket)
        model_harness = (
            row.model_identifier or "unavailable",
            row.model_provider or "unavailable",
            row.harness_type or "unavailable",
            row.harness_version or "unavailable",
        )
        warm_cells[cell][model_harness].append(row)

    matched_cells = []
    for cell, populations in sorted(warm_cells.items()):
        eligible = {
            key: rows
            for key, rows in populations.items()
            if len(rows) >= min_cell_size
        }
        if len(eligible) < 2:
            continue
        matched_cells.append(
            {
                "update_bucket": cell[0],
                "task_type": cell[1],
                "exposure_bucket": cell[2],
                "cohorts": [
                    _cohort_summary(
                        (*key, "warm", cell[0], cell[1], cell[2]), rows
                    )
                    for key, rows in sorted(eligible.items())
                ],
            }
        )

    row_limit_hit = row_limit is not None and len(normalized) >= row_limit
    reasons: list[str] = []
    if not exact_rows:
        reasons.append("no_exact_model_harness_rows_with_risk")
    if not matched_cells:
        reasons.append("no_like_for_like_warm_cell_meets_minimum_size")
    if row_limit_hit:
        reasons.append("row_limit_reached_window_may_be_truncated")
    comparison_ready = bool(matched_cells) and not row_limit_hit

    return {
        "schema": "s22.model_risk_cohort_report.v1",
        "scope": "prospective_descriptive_measurement",
        "capture_window": {
            "start": capture_start,
            "end": capture_end,
            "start_required": True,
            "historical_null_attribution": "forbidden",
        },
        "authority": {
            "causal_claim": False,
            "policy_change_allowed": False,
            "identity_authority": False,
            "verdict_authority": False,
        },
        "coverage": {
            "state_rows": len(normalized),
            "exact_attributed_rows": len(exact_rows),
            "risk_missing_after_exact_attribution": sum(
                row.attribution_status == "eligible_exact" and row.risk_score is None
                for row in normalized
            ),
            "harness_version_unavailable_exact_rows": sum(
                row.attribution_status == "eligible_exact"
                and row.harness_version is None
                for row in normalized
            ),
            "attribution_status": dict(sorted(attrition.items())),
            "row_limit": row_limit,
            "row_limit_hit": row_limit_hit,
        },
        "cohorts": cohorts,
        "like_for_like_warm_cells": matched_cells,
        "comparison_readiness": {
            "status": (
                "ready_for_descriptive_comparison"
                if comparison_ready
                else "not_ready"
            ),
            "min_cell_size": min_cell_size,
            "reasons": reasons,
            "next_step": (
                "Inspect matched warm cells; any policy proposal still requires "
                "a separately preregistered causal design."
                if comparison_ready
                else "Continue prospective capture without model-specific policy."
            ),
        },
    }

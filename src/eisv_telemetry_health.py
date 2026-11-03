"""Read-only fleet health report for the EISV telemetry envelope.

The report is deliberately observational.  It measures rollout coverage,
provenance completeness, same-row contract consistency, and descriptive
outcome calibration.  It does not feed the monitor, policy, or enforcement
paths and it does not reinterpret EISV as an outcome judgment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
import json
import math
from typing import Any

from src.eisv_telemetry import EISV_TELEMETRY_SCHEMA
from src.grounding.outcome_anchors import (
    DEFAULT_FIXTURE_RULE,
    is_scraped_only_exclusion,
    anchored_outcomes_predicate,
    is_structurally_controlled_fixture,
)


EISV_TELEMETRY_HEALTH_SCHEMA = "eisv.telemetry-health.v1"
CALIBRATION_LEAD_MINUTES = 5.0
MIN_CALIBRATION_BIN_CLUSTERS = 20
MIN_CALIBRATION_COHORT_CLUSTERS = 100

# These are intentionally shown as three distinct vocabularies.  The same
# numeric risk can truthfully occupy different named bands because each surface
# answers a different question.  Surfacing the split is safer than pretending
# one label is canonical without changing the runtime contract.
RISK_VOCABULARIES = (
    {
        "surface": "behavioral_verdict",
        "bands": (
            {"label": "safe", "minimum": 0.0, "maximum_exclusive": 0.35},
            {"label": "caution", "minimum": 0.35, "maximum_exclusive": 0.60},
            {"label": "high-risk", "minimum": 0.60, "maximum_exclusive": None},
        ),
    },
    {
        "surface": "experience_summary",
        "bands": (
            {"label": "low", "minimum": 0.0, "maximum_exclusive": 0.40},
            {"label": "elevated", "minimum": 0.40, "maximum_exclusive": 0.70},
            {"label": "high", "minimum": 0.70, "maximum_exclusive": None},
        ),
    },
    {
        "surface": "health_status",
        "bands": (
            {"label": "healthy", "minimum": 0.0, "maximum_exclusive": 0.45},
            {"label": "moderate", "minimum": 0.45, "maximum_exclusive": 0.70},
            {"label": "critical", "minimum": 0.70, "maximum_exclusive": None},
        ),
        "note": "Coherence and void gates can override the risk-only band.",
    },
)

_CALIBRATION_BINS = (
    ("0.0-0.2", 0.0, 0.2),
    ("0.2-0.4", 0.2, 0.4),
    ("0.4-0.6", 0.4, 0.6),
    ("0.6-0.8", 0.6, 0.8),
    ("0.8-1.0", 0.8, 1.0),
)


from src.db.mixins.state import EISV_TELEMETRY_STATE_HEALTH_SQL


_STRICT_ANCHOR_SQL = anchored_outcomes_predicate(table_alias="o")
EISV_TELEMETRY_CALIBRATION_SQL = f"""
SELECT
    o.outcome_id,
    o.ts,
    o.agent_id,
    o.is_bad,
    o.detail,
    ps.recorded_at AS prior_recorded_at,
    ps.risk_score::double precision AS prior_risk,
    ps.state_json #>> '{{eisv_telemetry,schema}}' AS prior_telemetry_schema,
    ps.state_json #>> '{{eisv_telemetry,measurement_id}}' AS prior_measurement_id
FROM audit.outcome_events o
LEFT JOIN LATERAL (
    SELECT s.recorded_at, s.risk_score, s.state_json
    FROM core.identities i
    JOIN core.agent_state s ON s.identity_id = i.identity_id
    WHERE i.agent_id = o.agent_id
      AND s.synthetic IS NOT TRUE
      AND s.recorded_at <= o.ts - ($2::double precision * INTERVAL '1 minute')
    ORDER BY s.recorded_at DESC
    LIMIT 1
) ps ON TRUE
WHERE o.ts >= now() - make_interval(days => $1)
  AND {_STRICT_ANCHOR_SQL}
ORDER BY o.ts
"""


def _get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, TypeError, IndexError):
        return default


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _cluster_key(row: Any) -> str | None:
    measurement_id = _get(row, "prior_measurement_id")
    if measurement_id:
        return f"measurement:{measurement_id}"
    recorded_at = _get(row, "prior_recorded_at")
    agent_id = _get(row, "agent_id")
    if recorded_at is None or not agent_id:
        return None
    timestamp = recorded_at.isoformat() if isinstance(recorded_at, datetime) else str(recorded_at)
    return f"state:{agent_id}:{timestamp}"


def _risk_bin_index(risk: float) -> int | None:
    if risk < 0.0 or risk > 1.0:
        return None
    if risk == 1.0:
        return len(_CALIBRATION_BINS) - 1
    return min(int(risk * len(_CALIBRATION_BINS)), len(_CALIBRATION_BINS) - 1)


def build_calibration_summary(
    rows: Sequence[Any],
    *,
    min_bin_clusters: int = MIN_CALIBRATION_BIN_CLUSTERS,
    min_cohort_clusters: int = MIN_CALIBRATION_COHORT_CLUSTERS,
    fixture_rule: str = DEFAULT_FIXTURE_RULE,
) -> dict[str, Any]:
    """Aggregate strict outcome-linked risk into clustered descriptive bins.

    ``fixture_rule`` selects how a server-stamped ``calibration_excluded`` is
    read (default ``corrected`` since 2026-09-02, a disclosed one-time step
    change in ``strict_outcomes`` and ``fixtures_excluded``); ``scraped_only_rows``
    counts rows whose only exclusion is a scraped confidence, which the
    registered rule drops and the corrected rule keeps.
    """
    fixtures_excluded = 0
    scraped_only_rows = 0
    strict_outcomes = 0
    with_prior_state = 0
    with_envelope = 0
    invalid_risk = 0
    bins: list[dict[str, Any]] = [
        {
            "band": label,
            "minimum": low,
            "maximum": high,
            "outcomes": 0,
            "bad_outcomes": 0,
            "clusters": {},
        }
        for label, low, high in _CALIBRATION_BINS
    ]

    for row in rows:
        detail = _get(row, "detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except json.JSONDecodeError:
                detail = {}
        if is_scraped_only_exclusion(detail):
            scraped_only_rows += 1
        if is_structurally_controlled_fixture(detail, rule=fixture_rule):
            fixtures_excluded += 1
            continue

        strict_outcomes += 1
        prior_recorded_at = _get(row, "prior_recorded_at")
        if prior_recorded_at is None:
            continue
        with_prior_state += 1
        if _get(row, "prior_telemetry_schema") != EISV_TELEMETRY_SCHEMA:
            continue
        with_envelope += 1

        risk = _as_float(_get(row, "prior_risk"))
        index = _risk_bin_index(risk) if risk is not None else None
        cluster = _cluster_key(row)
        if index is None or cluster is None:
            invalid_risk += 1
            continue
        bad = bool(_get(row, "is_bad"))
        item = bins[index]
        item["outcomes"] += 1
        item["bad_outcomes"] += int(bad)
        item["clusters"][cluster] = item["clusters"].get(cluster, False) or bad

    rendered_bins: list[dict[str, Any]] = []
    cohort_clusters: set[str] = set()
    cohort_bad_clusters: set[str] = set()
    for item in bins:
        clusters = item.pop("clusters")
        cluster_count = len(clusters)
        bad_clusters = sum(1 for bad in clusters.values() if bad)
        cohort_clusters.update(clusters)
        cohort_bad_clusters.update(cluster for cluster, bad in clusters.items() if bad)
        if cluster_count < min_bin_clusters:
            evidence_status = "sparse"
        elif bad_clusters in (0, cluster_count):
            evidence_status = "single_class"
        else:
            evidence_status = "descriptive"
        rendered_bins.append({
            **item,
            "clusters": cluster_count,
            "bad_clusters": bad_clusters,
            "bad_cluster_rate": bad_clusters / cluster_count if cluster_count else None,
            "evidence_status": evidence_status,
        })

    cluster_count = len(cohort_clusters)
    bad_cluster_count = len(cohort_bad_clusters)
    if with_envelope == 0:
        status = "awaiting_envelope"
    elif cluster_count < min_cohort_clusters:
        status = "inconclusive"
    elif bad_cluster_count in (0, cluster_count):
        status = "single_class"
    else:
        status = "descriptive"

    return {
        "status": status,
        "anchor_scope": "strict_external",
        "lead_minutes": CALIBRATION_LEAD_MINUTES,
        "strict_outcomes": strict_outcomes,
        "fixtures_excluded": fixtures_excluded,
        "fixture_rule": fixture_rule,
        "scraped_only_rows": scraped_only_rows,
        "with_prior_state": with_prior_state,
        "with_envelope": with_envelope,
        "envelope_coverage_rate": with_envelope / strict_outcomes if strict_outcomes else None,
        "clusters": cluster_count,
        "bad_clusters": bad_cluster_count,
        "invalid_risk_rows": invalid_risk,
        "minimum_bin_clusters": min_bin_clusters,
        "minimum_cohort_clusters": min_cohort_clusters,
        "bins": rendered_bins,
        "note": (
            "Strict external outcomes only. Rates are descriptive and clustered by the "
            "prior measurement; they do not establish predictive lift or causality."
        ),
    }


def _normalize_json(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise TypeError("telemetry health query did not return a JSON object")


def _as_utc(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return None


def _era_crossing(window_days: int, outcome_rows: Sequence[Any] = ()) -> dict[str, Any]:
    """Instrument changes the report's figures span (time-based, never raises).

    The state aggregates pool every row in the window, so a window that spans
    a maths change mixes instruments; the report says so instead of refusing.
    Calibration pairs each outcome in the window with its agent's latest state
    row before it, which can be older than the window, so it gets its own
    report from the earliest such row.
    """
    try:
        from src import eisv_eras

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=window_days)
        report = eisv_eras.crossing_report(start, end)
        report["note"] = eisv_eras.crossing_note(report)
        priors = [
            ts for ts in (_as_utc(_get(row, "prior_recorded_at")) for row in outcome_rows)
            if ts is not None
        ]
        calibration_start = min([start, *priors])
        calibration = eisv_eras.crossing_report(calibration_start, end)
        calibration["note"] = eisv_eras.crossing_note(calibration)
        report["calibration"] = calibration
        return report
    except Exception as exc:  # the registry is provenance; never fail the route on it
        return {"schema": "eisv.era_crossing.v1", "error": f"{type(exc).__name__}: {exc}"}


async def query_eisv_telemetry_health(
    conn: Any,
    *,
    window_days: int,
) -> dict[str, Any]:
    """Query durable state aggregates and strict outcome-linked calibration."""
    state_report = _normalize_json(
        await conn.fetchval(EISV_TELEMETRY_STATE_HEALTH_SQL, window_days)
    )
    outcome_rows = await conn.fetch(
        EISV_TELEMETRY_CALIBRATION_SQL,
        window_days,
        CALIBRATION_LEAD_MINUTES,
    )
    return {
        "success": True,
        "schema": EISV_TELEMETRY_HEALTH_SCHEMA,
        "window_days": window_days,
        **state_report,
        "risk_vocabularies": [
            {
                **vocabulary,
                "bands": [dict(band) for band in vocabulary["bands"]],
            }
            for vocabulary in RISK_VOCABULARIES
        ],
        "calibration": build_calibration_summary(outcome_rows),
        "eisv_instrument_eras": _era_crossing(window_days, outcome_rows),
        "semantics": {
            "measurement": "EISV is a proprioceptive estimate, not an outcome judgment.",
            "contract_checks": "Cross-field serialization invariants, not quality scores.",
            "maturity_gate": "Counterfactual shadow evaluation; no pause is suppressed.",
            "enforcement": "Intervention-conditioned delivery accounting, not a causal estimate.",
        },
    }

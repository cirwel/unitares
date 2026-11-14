"""
Fleet analytics, anomaly detection, outcome evidence, and audit event query
helpers for observability MCP handlers.

Extracted from src/mcp_handlers/observability/handlers.py. All public and
private symbols are re-exported on src.mcp_handlers.observability.handlers
and dynamically resolved so test monkeypatches continue to work.
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Sequence
import hashlib
import json
import sys

from mcp.types import TextContent

from src.governance_glossary import get_eisv_glossary as _default_get_eisv_glossary
from src.db.mixins.state import LATEST_MEASURED_STATE_FOR_SCOPE_JOIN
from src.logging_utils import get_logger
from src.mcp_handlers.shared import get_mcp_server
from src.mcp_handlers.utils import (
    error_response as _default_error_response,
    success_response as _default_success_response,
)

logger = get_logger("src.mcp_handlers.observability.handlers")
_default_mcp_server = get_mcp_server()


def _h():
    return sys.modules.get("src.mcp_handlers.observability.handlers")


def _h_attr(name: str, default: Any = None) -> Any:
    mod = _h()
    if mod is not None and hasattr(mod, name):
        return getattr(mod, name)
    return default


def anomaly_change_token(anomaly: Dict[str, Any]) -> str:
    """Emit-on-change dedup token for a persisting anomaly.

    Keyed on the data the anomaly was computed from — its type, agent, the
    full-precision context values, and the newest analyzed sample's timestamp —
    NOT on the human-readable description. This means the token advances exactly
    when a new state sample lands: a frozen/idle history yields an identical
    token every evaluation (suppressed), while a genuine recovery-then-respike
    produces a new timestamp and therefore a new token (emitted), even if the
    rounded values happen to match. Deliberately independent of the description
    f-string so message-format edits can never silently re-fire the idle fleet.
    """
    ctx = anomaly.get("context") or {}
    ctx_repr = "|".join(f"{k}={ctx[k]}" for k in sorted(ctx))
    basis = (
        f"{anomaly.get('type')}|{anomaly.get('agent_id')}|"
        f"{anomaly.get('timestamp')}|{ctx_repr}"
    )
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


# Ceiling on the default fleet scan. Not a bound on what a caller may see: it
# selects how many active agents one call analyzes, and every anomaly found in
# them is returned. A scan that hits it says so in the response's `scan` block,
# and agent_ids bypasses it entirely.
DEFAULT_ANOMALY_SCAN_CAP = 50


async def _handle_detect_anomalies(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Detect anomalies across agents."""
    import asyncio

    srv = _h_attr("mcp_server", _default_mcp_server)
    log = _h_attr("logger", logger)
    scan_cap = _h_attr("DEFAULT_ANOMALY_SCAN_CAP", DEFAULT_ANOMALY_SCAN_CAP)
    change_token_fn = _h_attr("anomaly_change_token", anomaly_change_token)
    display_name_fn = _h_attr("_agent_display_name")
    hydrated_fn = _h_attr("ensure_hydrated")
    glossary_fn = _h_attr("get_eisv_glossary", _default_get_eisv_glossary)
    ok_resp = _h_attr("success_response", _default_success_response)
    err_resp = _h_attr("error_response", _default_error_response)

    await srv.load_metadata_async()

    agent_ids = arguments.get("agent_ids")
    requested_agent_ids = agent_ids
    anomaly_types = arguments.get("anomaly_types", ["risk_spike", "coherence_drop"])
    min_severity = arguments.get("min_severity", "medium")

    severity_levels = {"low": 0, "medium": 1, "high": 2}
    min_severity_level = severity_levels.get(min_severity, 1)

    # Get agent list
    active_agent_ids = None
    if not agent_ids:
        active_agent_ids = [
            aid for aid, meta in srv.agent_metadata.items()
            if meta.status == "active"
        ]
        agent_ids = active_agent_ids[:scan_cap]

    all_anomalies = []
    skipped_no_activity: list[str] = []
    loop = asyncio.get_running_loop()

    async def process_agent(agent_id: str):
        """Process a single agent's anomalies."""
        meta = srv.agent_metadata.get(agent_id)
        if not meta or int(getattr(meta, "total_updates", 0) or 0) == 0:
            skipped_no_activity.append(agent_id)
            return

        monitor = await loop.run_in_executor(None, srv.get_or_create_monitor, agent_id)
        await hydrated_fn(monitor, agent_id)

        if monitor:
            from src.pattern_analysis import analyze_agent_patterns

            analysis = await loop.run_in_executor(
                None, analyze_agent_patterns, monitor, False
            )

            agent_anomalies = []
            agent_name = display_name_fn(agent_id)
            for anomaly in analysis.get("anomalies", []):
                if anomaly["type"] in anomaly_types:
                    anomaly_severity_level = severity_levels.get(anomaly.get("severity", "low"), 0)
                    if anomaly_severity_level >= min_severity_level:
                        anomaly["agent_id"] = agent_id
                        if agent_name:
                            anomaly["agent_name"] = agent_name
                        agent_anomalies.append(anomaly)
            return agent_anomalies
        return []

    try:
        batch_size = 10
        for i in range(0, len(agent_ids), batch_size):
            batch = agent_ids[i:i + batch_size]
            batch_results = await asyncio.gather(
                *[process_agent(aid) for aid in batch], return_exceptions=True
            )
            for result in batch_results:
                if isinstance(result, list):
                    all_anomalies.extend(result)
                elif isinstance(result, Exception):
                    log.warning(
                        f"Error processing agent in detect_anomalies: {result}",
                        exc_info=True,
                    )
    except Exception as e:
        log.error(f"Error in detect_anomalies: {e}", exc_info=True)
        return [err_resp(f"Error detecting anomalies: {str(e)}")]

    new_anomalies = []
    if all_anomalies:
        from src.event_detector import event_detector

        for a in all_anomalies:
            fp = hashlib.sha256(
                f"detect_anomalies|{a.get('type')}|{a.get('agent_id')}".encode()
            ).hexdigest()[:16]
            stored = event_detector.record_event({
                "fingerprint": fp,
                "change_token": change_token_fn(a),
                "type": a.get("type"),
                "severity": a.get("severity"),
                "agent_id": a.get("agent_id"),
                "agent_name": a.get("agent_name"),
                "message": (
                    f"{a.get('agent_name')}: {a.get('description')}"
                    if a.get("agent_name") and a.get("description")
                    else a.get("description", "")
                ),
                "description": a.get("description", ""),
                "source": "detect_anomalies",
            })
            if stored is not None:
                new_anomalies.append(a)

    if new_anomalies:
        from src.audit_log import AuditEntry, audit_logger

        ts = datetime.now().isoformat()
        for a in new_anomalies:
            audit_logger._write_entry(AuditEntry(
                timestamp=ts,
                agent_id=a.get("agent_id") or "system",
                event_type="anomaly_detected",
                confidence=1.0,
                details={
                    "type": a.get("type"),
                    "severity": a.get("severity"),
                    "description": a.get("description", ""),
                },
            ))

    all_anomalies.sort(
        key=lambda x: severity_levels.get(x.get("severity", "low"), 0),
        reverse=True,
    )

    by_severity = {"high": 0, "medium": 0, "low": 0}
    by_type = {}
    stale_count = 0
    for anomaly in all_anomalies:
        severity = anomaly.get("severity", "low")
        by_severity[severity] = by_severity.get(severity, 0) + 1
        anomaly_type = anomaly.get("type", "unknown")
        by_type[anomaly_type] = by_type.get(anomaly_type, 0) + 1
        if anomaly.get("stale") is True:
            stale_count += 1

    scan = {
        "selection": "caller_supplied" if requested_agent_ids else "active_agents",
        "agents_scanned": len(agent_ids),
        "agents_skipped_no_activity": len(skipped_no_activity),
        "filters": {
            "anomaly_types": list(anomaly_types),
            "min_severity": min_severity,
        },
    }
    if active_agent_ids is not None:
        unscanned = len(active_agent_ids) - len(agent_ids)
        scan["agents_active"] = len(active_agent_ids)
        scan["scan_cap"] = scan_cap
        scan["truncated"] = unscanned > 0
        if unscanned > 0:
            scan["note"] = (
                f"{unscanned} active agents were not analyzed: the default scan "
                f"stops at {scan_cap}. Pass agent_ids to cover "
                "them; the counts below describe the agents scanned, not the fleet."
            )

    return ok_resp({
        "anomalies": all_anomalies,
        "scan": scan,
        "eisv_labels": glossary_fn(),
        "anomaly_policy": {
            "schema": "observability.anomaly.authority.v2",
            "coherence_drop_required_role": "behavioral_update_consistency",
            "excluded_roles": {
                "ode_control_feedback": "directional_control_signal_not_health_evidence",
                "eis_structural_measurement": "thresholds_uncalibrated",
                "unknown": "missing_provenance_fails_closed",
            },
        },
        "summary": {
            "total_anomalies": len(all_anomalies),
            "fresh_anomalies": len(all_anomalies) - stale_count,
            "stale_anomalies": stale_count,
            "by_severity": by_severity,
            "by_type": by_type,
        },
    })


async def _handle_aggregate_metrics(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Get fleet-level health overview."""
    from src.db import get_db

    ok_resp = _h_attr("success_response", _default_success_response)
    fetch_total_updates = _h_attr("_fetch_scope_total_updates")

    agent_ids = arguments.get("agent_ids")
    include_health_breakdown = arguments.get("include_health_breakdown", True)

    db = get_db()
    async with db.acquire() as conn:
        epoch_row = await conn.fetchrow(
            "SELECT epoch, started_at FROM core.epochs ORDER BY epoch DESC LIMIT 1"
        )
        current_epoch = epoch_row["epoch"] if epoch_row else 1
        epoch_started_at = epoch_row["started_at"] if epoch_row else None

        if agent_ids:
            scope_clause = "WHERE a.id = ANY($1::text[])"
            scope_args: tuple = (list(agent_ids),)
        else:
            scope_clause = "WHERE a.status = 'active'"
            scope_args = ()

        state_sql = f"""
            WITH scope AS (
                SELECT a.id AS agent_id, a.status
                FROM core.agents a
                {scope_clause}
            )
            SELECT
              count(*)::int AS total_agents,
              count(ls.identity_id)::int AS agents_with_data,
              avg(ls.risk_score)::real AS mean_risk_score,
              avg(ls.coherence)::real AS mean_coherence,
              count(*) FILTER (
                  WHERE ls.regime IN ('nominal','STABLE','CONVERGENCE')
              )::int AS healthy,
              count(*) FILTER (
                  WHERE ls.regime IN ('warning','EXPLORATION','recovery','TRANSITION')
              )::int AS moderate,
              count(*) FILTER (
                  WHERE ls.regime IN ('critical','DIVERGENCE')
              )::int AS critical,
              count(*) FILTER (
                  WHERE ls.identity_id IS NULL OR ls.regime = 'unknown'
              )::int AS unknown_health,
              EXTRACT(EPOCH FROM (now() - min(ls.recorded_at)))::bigint
                  AS staleness_oldest_seconds,
              EXTRACT(EPOCH FROM (now() - max(ls.recorded_at)))::bigint
                  AS staleness_newest_seconds
            FROM scope
            {LATEST_MEASURED_STATE_FOR_SCOPE_JOIN}
        """
        state_row = await conn.fetchrow(state_sql, *scope_args)

        if agent_ids:
            paused_now = await conn.fetchval(
                "SELECT count(*)::int FROM core.agents "
                "WHERE status = 'paused' AND id = ANY($1::text[])",
                list(agent_ids),
            )
        else:
            paused_now = await conn.fetchval(
                "SELECT count(*)::int FROM core.agents WHERE status = 'paused'"
            )

        total_updates = await fetch_total_updates(conn, scope_clause, scope_args)

        if epoch_started_at is not None:
            from src.db.mixins.audit import count_lifecycle_pauses_since
            from src.db.mixins.tool_usage import (
                count_trajectory_validated_outcomes_for_epoch,
            )

            pauses_this_epoch = await count_lifecycle_pauses_since(
                conn, epoch_started_at
            )

            proceed_this_epoch = (
                await count_trajectory_validated_outcomes_for_epoch(
                    conn, current_epoch
                )
            )

            verdict_sql = """
                SELECT verdict, count(*)::int AS n
                FROM audit.r1_score_audit
                WHERE recorded_at >= $1 AND verdict IS NOT NULL
                GROUP BY verdict
            """
            verdict_rows = await conn.fetch(verdict_sql, epoch_started_at)
        else:
            pauses_this_epoch = 0
            proceed_this_epoch = 0
            verdict_rows = []

    R1_TO_LEGACY = {
        "plausible": "safe",
        "inconclusive": "caution",
        "unsupported": "high-risk",
    }
    verdict_distribution = {"safe": 0, "caution": 0, "high-risk": 0}
    for row in verdict_rows:
        legacy_key = R1_TO_LEGACY.get(row["verdict"], row["verdict"])
        verdict_distribution[legacy_key] = (
            verdict_distribution.get(legacy_key, 0) + int(row["n"])
        )
    verdict_distribution["total"] = sum(
        v for k, v in verdict_distribution.items() if k != "total"
    )

    decision_distribution = {
        "proceed": int(proceed_this_epoch or 0),
        "pause": int(pauses_this_epoch or 0),
        "total": int((proceed_this_epoch or 0) + (pauses_this_epoch or 0)),
    }

    aggregate_data = {
        "total_agents": int(state_row["total_agents"]),
        "agents_with_data": int(state_row["agents_with_data"]),
        "total_updates": int(total_updates or 0),
        "mean_risk_score": float(state_row["mean_risk_score"] or 0.0),
        "mean_risk": float(state_row["mean_risk_score"] or 0.0),  # DEPRECATED alias
        "mean_coherence": float(state_row["mean_coherence"] or 0.0),
        "mean_coherence_provenance": {
            "role": "producer_mixed_or_unknown",
            "health_evidence": False,
            "warning": (
                "Compatibility aggregate only; do not compare or threshold "
                "without per-row producer provenance"
            ),
        },
        "paused_now": int(paused_now or 0),
        "pauses_this_epoch": int(pauses_this_epoch or 0),
        "epoch": int(current_epoch),
        "as_of": datetime.now(timezone.utc).isoformat(),
        "staleness": {
            "oldest_state_seconds": int(state_row["staleness_oldest_seconds"] or 0),
            "newest_state_seconds": int(state_row["staleness_newest_seconds"] or 0),
        },
        "decision_distribution": decision_distribution,
        "verdict_distribution": verdict_distribution,
    }

    if include_health_breakdown:
        aggregate_data["health_breakdown"] = {
            "healthy": int(state_row["healthy"]),
            "moderate": int(state_row["moderate"]),
            "critical": int(state_row["critical"]),
            "unknown": int(state_row["unknown_health"]),
        }

    return ok_resp({
        "aggregate": aggregate_data,
    })


def _parse_window_arg(value: Any, default_hours: float = 24.0) -> "datetime":
    """Parse `since`/`until` window args into an aware datetime."""
    now = datetime.now(timezone.utc)
    if value is None:
        return now - timedelta(hours=default_hours)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    s = str(value).strip()
    if not s:
        return now - timedelta(hours=default_hours)

    if len(s) >= 2 and s[-1] in {"d", "h", "m", "s"} and s[:-1].lstrip("-").isdigit():
        n = int(s[:-1])
        if n < 0:
            raise ValueError(f"negative shorthand duration not allowed: {s!r}")
        unit = s[-1]
        delta = {
            "d": timedelta(days=n),
            "h": timedelta(hours=n),
            "m": timedelta(minutes=n),
            "s": timedelta(seconds=n),
        }[unit]
        return now - delta

    iso = s[:-1] + "+00:00" if s.endswith("Z") else s
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_OUTCOME_AGGREGATE_BATCH = 2000


async def _handle_outcome_evidence(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Audit outcome_events by corroboration grade and claim-field verification."""
    from src.db import get_db
    from src.outcome_corroboration import (
        assess_outcome_corroboration,
        ceiling_for_verification_source,
    )

    ok_resp = _h_attr("success_response", _default_success_response)
    err_resp = _h_attr("error_response", _default_error_response)
    parse_win = _h_attr("_parse_window_arg", _parse_window_arg)

    diagnostic = str(
        arguments.get("diagnostic") or arguments.get("mode") or "claim_only_task_completed"
    ).lower()
    valid_diagnostics = {
        "claim_only_task_completed",
        "agent_summary",
        "field_verification",
        "events",
    }
    if diagnostic not in valid_diagnostics:
        return err_resp(
            f"invalid diagnostic {diagnostic!r}",
            error_code="invalid_argument",
            recovery={"valid_diagnostics": sorted(valid_diagnostics)},
        )

    since_arg = arguments.get("since")
    window_defaulted = since_arg is None
    try:
        start_dt = parse_win(since_arg, default_hours=168.0)
        end_dt = (
            parse_win(arguments.get("until"), default_hours=0.0)
            if arguments.get("until") is not None
            else None
        )
    except (ValueError, KeyError) as e:
        return err_resp(
            f"invalid window arg: {e}",
            error_code="invalid_argument",
        )

    try:
        limit = int(arguments.get("limit", 500))
    except (TypeError, ValueError):
        limit = 500
    limit = max(1, min(limit, 5000))

    try:
        min_completions = int(arguments.get("min_completions", 3))
    except (TypeError, ValueError):
        min_completions = 3
    min_completions = max(1, min(min_completions, 1000))

    try:
        low_weight_threshold = float(arguments.get("low_weight_threshold", 0.50))
    except (TypeError, ValueError):
        low_weight_threshold = 0.50

    target_agent_id = arguments.get("target_agent_id")
    outcome_type = arguments.get("outcome_type")
    grade_filter = arguments.get("corroboration_grade")
    include_events_arg = arguments.get("include_events")
    include_events = (
        bool(include_events_arg)
        if include_events_arg is not None
        else diagnostic != "agent_summary"
    )
    include_detail = bool(arguments.get("include_detail", False))

    if diagnostic == "claim_only_task_completed":
        outcome_type = outcome_type or "task_completed"
        grade_filter = grade_filter or "claim_only"

    if end_dt is None:
        end_dt = datetime.now(timezone.utc)

    from src.db.mixins.tool_usage import (
        fetch_outcome_evidence_aggregate_batch,
        fetch_outcome_evidence_page,
    )

    db = get_db()
    async with db.acquire() as conn:
        rows = await fetch_outcome_evidence_page(
            conn,
            start_dt=start_dt,
            end_dt=end_dt,
            target_agent_id=target_agent_id,
            outcome_type=outcome_type,
            limit=limit,
        )

    def _row_detail(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    def _grade(row) -> tuple[Dict[str, Any], Dict[str, Any], Any]:
        detail = _row_detail(row["detail"])
        verification_source = row["verification_source"] or detail.get("verification_source")
        metadata = assess_outcome_corroboration(
            outcome_type=row["outcome_type"],
            detail=detail,
            verification_source=verification_source,
            ceiling=ceiling_for_verification_source(verification_source),
        ).as_metadata()
        return detail, metadata, verification_source

    by_grade: Dict[str, int] = {}
    agent_stats: Dict[str, Dict[str, Any]] = {}
    events: list[Dict[str, Any]] = []

    batch_size = _h_attr("_OUTCOME_AGGREGATE_BATCH", _OUTCOME_AGGREGATE_BATCH)
    window_total = 0
    cursor_ts = None
    cursor_id = None
    async with db.acquire() as conn:
        while True:
            batch = await fetch_outcome_evidence_aggregate_batch(
                conn,
                start_dt=start_dt,
                end_dt=end_dt,
                target_agent_id=target_agent_id,
                outcome_type=outcome_type,
                cursor_ts=cursor_ts,
                cursor_id=cursor_id,
                batch_size=batch_size,
            )
            if not batch:
                break
            for row in batch:
                _, metadata, _vs = _grade(row)
                grade = metadata["corroboration_grade"]
                by_grade[grade] = by_grade.get(grade, 0) + 1
                window_total += 1

                aid = row["agent_id"] or "<unknown>"
                stats = agent_stats.setdefault(aid, {
                    "agent_id": aid,
                    "total_events": 0,
                    "task_completed": 0,
                    "claim_only_task_completed": 0,
                    "total_evidence_weight": 0.0,
                    "by_grade": {},
                })
                stats["total_events"] += 1
                stats["total_evidence_weight"] += float(metadata["evidence_weight"])
                stats["by_grade"][grade] = stats["by_grade"].get(grade, 0) + 1
                if row["outcome_type"] == "task_completed":
                    stats["task_completed"] += 1
                    if grade == "claim_only":
                        stats["claim_only_task_completed"] += 1
            if len(batch) < batch_size:
                break
            cursor_ts = batch[-1]["ts"]
            cursor_id = batch[-1]["outcome_id"]

    for row in rows:
        detail, metadata, verification_source = _grade(row)
        grade = metadata["corroboration_grade"]

        include_row = True
        if grade_filter and grade != str(grade_filter):
            include_row = False
        if diagnostic == "field_verification" and not metadata["claimed_fields"]:
            include_row = False
        if include_row and include_events:
            event = {
                "outcome_id": row["outcome_id"],
                "ts": row["ts"].isoformat() if hasattr(row["ts"], "isoformat") else str(row["ts"]),
                "agent_id": row["agent_id"],
                "session_id": row["session_id"],
                "outcome_type": row["outcome_type"],
                "outcome_score": row["outcome_score"],
                "is_bad": row["is_bad"],
                "verification_source": verification_source,
                **metadata,
                "claim_fields": {
                    "claimed": metadata["claimed_fields"],
                    "verified": metadata["verified_fields"],
                    "unverified": metadata["unverified_fields"],
                },
            }
            if include_detail:
                event["detail"] = detail
            else:
                event["detail_summary"] = {
                    key: detail.get(key)
                    for key in ("source", "summary", "tool", "kind", "exit_code")
                    if key in detail
                }
            events.append(event)

    agents: list[Dict[str, Any]] = []
    for stats in agent_stats.values():
        total = max(1, int(stats["total_events"]))
        avg_weight = float(stats["total_evidence_weight"]) / total
        task_completed = int(stats["task_completed"])
        claim_only_completed = int(stats["claim_only_task_completed"])
        stats.pop("total_evidence_weight", None)
        stats["avg_evidence_weight"] = round(avg_weight, 4)
        stats["claim_only_completion_ratio"] = (
            round(claim_only_completed / task_completed, 4)
            if task_completed else 0.0
        )
        stats["low_corroboration"] = (
            task_completed >= min_completions
            and (
                avg_weight < low_weight_threshold
                or claim_only_completed / max(1, task_completed) >= 0.5
            )
        )
        agents.append(stats)

    agents.sort(
        key=lambda a: (
            not a["low_corroboration"],
            -a["task_completed"],
            a["avg_evidence_weight"],
            a["agent_id"],
        )
    )
    low_agents = [a for a in agents if a["low_corroboration"]]

    payload: Dict[str, Any] = {
        "diagnostic": diagnostic,
        "window": {
            "since": start_dt.isoformat(),
            "until": end_dt.isoformat() if end_dt else None,
            "defaulted": window_defaulted,
        },
        "filters": {
            "target_agent_id": target_agent_id,
            "outcome_type": outcome_type,
            "corroboration_grade": grade_filter,
            "min_completions": min_completions,
            "low_weight_threshold": low_weight_threshold,
        },
        "raw_row_count": window_total,
        "by_grade": by_grade,
        "agents": agents,
        "low_corroboration_agents": low_agents,
        "returned_event_count": len(events),
        "limit_reached": window_total > limit,
    }
    if include_events:
        payload["events"] = events

    return ok_resp(payload)


async def _handle_audit_events(arguments: Dict[str, Any]) -> Sequence[TextContent]:
    """Query the Postgres ``audit.events`` table by event_type + time window."""
    from src.audit_db import aggregate_audit_events_async, query_audit_events_async

    ok_resp = _h_attr("success_response", _default_success_response)
    err_resp = _h_attr("error_response", _default_error_response)
    parse_win = _h_attr("_parse_window_arg", _parse_window_arg)

    event_type = arguments.get("event_type")
    event_types_arg = arguments.get("event_types")
    if not event_type and not event_types_arg:
        return err_resp(
            "event_type or event_types is required",
            error_code="missing_argument",
            details={
                "next_step": (
                    "Retry observe(action='audit_events') with event_type=<name> "
                    "or event_types=[<name>, ...]."
                ),
                "safe_options": [
                    {
                        "action": "query_one_type",
                        "call": (
                            "observe(action='audit_events', "
                            "event_type='continuity_token_deprecated_accept', since='14d')"
                        ),
                    },
                    {
                        "action": "query_multiple_types",
                        "call": (
                            "observe(action='audit_events', "
                            "event_types=['governance_decision', "
                            "'coordination_failure.mcp_handler_timeout.tool_decorator'], "
                            "since='24h')"
                        ),
                    },
                    {
                        "action": "inspect_tool_schema",
                        "call": "describe_tool(tool_name='observe')",
                    },
                ],
            },
            recovery={
                "required_one_of": ["event_type", "event_types"],
                "examples": [
                    "observe(action='audit_events', event_type='continuity_token_deprecated_accept', since='14d')",
                    "observe(action='audit_events', event_types=['governance_decision'], since='24h')",
                ],
            },
        )

    since_arg = arguments.get("since")
    window_defaulted = since_arg is None
    try:
        start_dt = parse_win(since_arg, default_hours=168.0)
        end_dt = (
            parse_win(arguments.get("until"), default_hours=0.0)
            if arguments.get("until") is not None
            else None
        )
    except (ValueError, KeyError) as e:
        return err_resp(
            f"invalid window arg: {e}",
            error_code="invalid_argument",
        )

    try:
        limit = int(arguments.get("limit", 1000))
    except (TypeError, ValueError):
        limit = 1000
    limit = max(1, min(limit, 5000))

    agent_id = arguments.get("target_agent_id")
    include_events = bool(arguments.get("include_events", False))
    include_test_fixtures = bool(arguments.get("include_test_fixtures", True))

    effective_event_type = event_type if event_type and not event_types_arg else None
    effective_event_types = list(event_types_arg) if event_types_arg else None

    events = await query_audit_events_async(
        agent_id=agent_id,
        event_type=effective_event_type,
        event_types=effective_event_types,
        start_time=start_dt.isoformat(),
        end_time=end_dt.isoformat() if end_dt else None,
        limit=limit,
        order="asc",
    )

    def _is_test_fixture(aid: Any) -> bool:
        return isinstance(aid, str) and aid.lower().startswith("test_")

    visible_events = events if include_test_fixtures else [
        e for e in events if not _is_test_fixture(e.get("agent_id"))
    ]

    groups = await aggregate_audit_events_async(
        agent_id=agent_id,
        event_type=effective_event_type,
        event_types=effective_event_types,
        start_time=start_dt.isoformat(),
        end_time=end_dt.isoformat() if end_dt else None,
    )

    by_agent: Dict[str, int] = {}
    by_event_type: Dict[str, int] = {}
    first_dt = None
    last_dt = None
    window_total = 0
    test_fixture_count = 0
    for g in groups:
        count = g["count"]
        window_total += count
        aid = g["agent_id"] or "<unknown>"
        if _is_test_fixture(g["agent_id"]):
            test_fixture_count += count
            if not include_test_fixtures:
                continue
        by_agent[aid] = by_agent.get(aid, 0) + count
        et = g["event_type"] or "<unknown>"
        by_event_type[et] = by_event_type.get(et, 0) + count
        if g["first_ts"] and (first_dt is None or g["first_ts"] < first_dt):
            first_dt = g["first_ts"]
        if g["last_ts"] and (last_dt is None or g["last_ts"] > last_dt):
            last_dt = g["last_ts"]

    def _iso(dt):
        return dt.isoformat() if hasattr(dt, "isoformat") else dt

    first_ts = _iso(first_dt)
    last_ts = _iso(last_dt)

    visible_total = (
        window_total if include_test_fixtures else window_total - test_fixture_count
    )

    payload: Dict[str, Any] = {
        "event_type": effective_event_type,
        "event_types": effective_event_types,
        "window": {
            "since": start_dt.isoformat(),
            "until": end_dt.isoformat() if end_dt else None,
            "defaulted": window_defaulted,
        },
        "total_emits": visible_total,
        "test_fixture_emits": test_fixture_count,
        "raw_row_count": window_total,
        "by_agent_id": by_agent,
        "by_event_type": by_event_type,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "returned_event_count": len(visible_events),
        "limit_reached": window_total > limit,
        "include_test_fixtures": include_test_fixtures,
    }
    if include_events:
        payload["events"] = visible_events

    return ok_resp(payload)

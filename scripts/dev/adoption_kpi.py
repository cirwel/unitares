#!/usr/bin/env python3
"""Agent-adoption KPI snapshot over audit.tool_usage.

The adoption thesis (KG notes tagged agent-experience+adoption,
2026-06-12): governance calls today are hook-initiated; the conversion to
"value-sustained" use is measurable, not vibes. This script prints the four
numbers that baseline it:

  1. Check-in concentration — what share of process_agent_update calls come
     from the top-2 callers (residents). Baseline 2026-06-12: 76%.
  2. Voluntary KG retrieval — knowledge/search_knowledge_graph calls from
     NAMED agents, excluding operator credentials (the dashboard).
     Baseline: 3 per 14d, and both callers were burn-in probes — real
     agent-initiated retrieval was zero.
  3. Onboard engagement (three honest cuts). `converted` means the agent made
     itself visible through either ceremonial process_agent_update OR the BEAM
     harness's external outcome signal. `ceremonial_converted` preserves the old
     process_agent_update-only cut. Reports: `converted`; adopter-cohort
     `cohort_engaged` (any value action — check-in / KG / outcome); and
     `did_nothing` (onboarded, made no UUID-attributed call = true bounce).
  4. Ground-truth pipe health — outcome_event success rate.
     Baseline: 17% (290/416 identity_error); fixed client-side 2026-06-12.

Usage:
    python3 scripts/dev/adoption_kpi.py [--days 14] [--json]
"""
from __future__ import annotations

import argparse
import json
import os

import psycopg2  # type: ignore
import psycopg2.extras  # type: ignore


def connect():
    dsn = os.environ.get(
        "GOVERNANCE_DATABASE_URL",
        "postgresql://postgres:postgres@localhost:5432/governance",
    )
    return psycopg2.connect(dsn)


def _snapshot_queries() -> dict:
    return {
        "checkin_concentration": """
            WITH calls AS (
                SELECT agent_id, count(*) n
                FROM audit.tool_usage
                WHERE ts > now() - make_interval(days => %(days)s)
                  AND tool_name = 'process_agent_update' AND success
                GROUP BY 1
            )
            SELECT coalesce(sum(n), 0) AS total,
                   coalesce((SELECT sum(n) FROM (
                       SELECT n FROM calls ORDER BY n DESC LIMIT 2) top2), 0) AS top2
            FROM calls
        """,
        "voluntary_kg_retrieval": """
            -- Named agents only; operator credentials (the dashboard) are
            -- operator retrieval, not agent-initiated retrieval.
            SELECT count(*) AS named_searches,
                   count(DISTINCT u.agent_id) AS distinct_agents
            FROM audit.tool_usage u
            LEFT JOIN core.agents a ON a.id::text = u.agent_id
            WHERE u.ts > now() - make_interval(days => %(days)s)
              AND u.tool_name IN ('knowledge', 'search_knowledge_graph')
              AND u.agent_id IS NOT NULL
              AND coalesce(a.label, '') NOT LIKE 'operator\\_%%'
        """,
        # Onboard engagement. `converted` used to mean process_agent_update only,
        # which made BEAM-dispatch harness identities look like permanent
        # non-converters even though they emit externally verified outcome_event
        # rows under their governance UUID (#1055). Keep the ceremonial-only cut
        # as a separate continuity field, and make the headline conversion count
        # the real BEAM check-in surface too.
        "onboard_conversion": """
            WITH a AS (
                SELECT a.id,
                       (a.spawn_reason IS NULL OR a.spawn_reason NOT IN (
                           'dispatch_beam_harness','dispatch_beam_harness_wiring_test',
                           'operator_credential','dialectic_reviewer','burnin_probe',
                           'perf_profile_load','scheduled_kg_audit','orchestrated_thread_anchor',
                           'auto_onboard_no_session','compaction','rename_from_anon')
                       ) AS is_adopter
                FROM core.agents a
                WHERE a.created_at > now() - make_interval(days => %(days)s)
            ),
            f AS (
                SELECT a.id, a.is_adopter,
                    EXISTS (SELECT 1 FROM audit.tool_usage t WHERE t.agent_id = a.id::text
                            AND t.success AND t.tool_name = 'process_agent_update') AS ceremonial_checked_in,
                    EXISTS (SELECT 1 FROM audit.outcome_events oe WHERE oe.agent_id = a.id::text
                            AND oe.ts > now() - make_interval(days => %(days)s)
                            AND oe.verification_source = 'external_signal'
                            AND oe.detail->>'harness' = 'beam') AS beam_checked_in,
                    (
                        EXISTS (SELECT 1 FROM audit.tool_usage t WHERE t.agent_id = a.id::text AND t.success
                                AND t.tool_name IN ('process_agent_update','knowledge','search_knowledge_graph','outcome_event'))
                        OR EXISTS (SELECT 1 FROM audit.outcome_events oe WHERE oe.agent_id = a.id::text
                                   AND oe.ts > now() - make_interval(days => %(days)s))
                    ) AS engaged_value,
                    (
                        EXISTS (SELECT 1 FROM audit.tool_usage t WHERE t.agent_id = a.id::text)
                        OR EXISTS (SELECT 1 FROM audit.outcome_events oe WHERE oe.agent_id = a.id::text
                                   AND oe.ts > now() - make_interval(days => %(days)s))
                    ) AS did_anything
                FROM a
            )
            SELECT count(*) AS minted,
                   count(*) FILTER (WHERE ceremonial_checked_in OR beam_checked_in) AS converted,
                   count(*) FILTER (WHERE ceremonial_checked_in) AS ceremonial_converted,
                   count(*) FILTER (WHERE beam_checked_in) AS beam_converted,
                   count(*) FILTER (WHERE is_adopter) AS cohort_minted,
                   count(*) FILTER (WHERE is_adopter AND engaged_value) AS cohort_engaged,
                   count(*) FILTER (WHERE NOT did_anything) AS did_nothing
            FROM f
        """,
        "outcome_pipe_health": """
            SELECT count(*) AS total,
                   count(*) FILTER (WHERE success) AS ok,
                   count(*) FILTER (WHERE error_type = 'identity_error') AS identity_errors
            FROM audit.tool_usage
            WHERE ts > now() - make_interval(days => %(days)s)
              AND tool_name = 'outcome_event'
        """,
        "proactive_kg_surface": """
            -- Proactive KG surfacing (adoption v0): emitted inside
            -- mirror_signal.emit events as a kg_proactive_surface trigger.
            -- `surfaced` is true only when the agent's response_mode was mirror,
            -- so it actually saw the nudge — the rest are the shadow control.
            SELECT count(*) AS fired,
                   count(*) FILTER (WHERE (payload->>'surfaced')::boolean) AS surfaced,
                   count(DISTINCT agent_id) AS agents
            FROM audit.events
            WHERE ts > now() - make_interval(days => %(days)s)
              AND event_type = 'mirror_signal.emit'
              AND payload->'signals' @> '[{"signal_type": "kg_proactive_surface"}]'
        """,
    }


def snapshot(days: int) -> dict:
    queries = _snapshot_queries()
    out: dict = {"window_days": days}
    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            for key, sql in queries.items():
                cur.execute(sql, {"days": days})
                out[key] = dict(cur.fetchone())
    cc = out["checkin_concentration"]
    cc["top2_share_pct"] = round(100 * cc["top2"] / cc["total"], 1) if cc["total"] else None
    oc = out["onboard_conversion"]
    oc["conversion_pct"] = round(100 * oc["converted"] / oc["minted"], 1) if oc["minted"] else None
    oc["ceremonial_conversion_pct"] = (
        round(100 * oc["ceremonial_converted"] / oc["minted"], 1) if oc["minted"] else None
    )
    oc["beam_conversion_pct"] = (
        round(100 * oc["beam_converted"] / oc["minted"], 1) if oc["minted"] else None
    )
    oc["cohort_engaged_pct"] = round(100 * oc["cohort_engaged"] / oc["cohort_minted"], 1) if oc["cohort_minted"] else None
    oc["did_nothing_pct"] = round(100 * oc["did_nothing"] / oc["minted"], 1) if oc["minted"] else None
    op = out["outcome_pipe_health"]
    op["success_pct"] = round(100 * op["ok"] / op["total"], 1) if op["total"] else None

    # Recall-miss telemetry (#972): a zero-result / low-confidence search is a
    # no-value interaction — an adoption signal, so it belongs in this snapshot.
    # File-based, written by the live search path; summarize() reads it relative
    # to whatever checkout runs (the daily cron runs from the deploy worktree →
    # the live telemetry file). Fail-open: telemetry must never break the KPI.
    try:
        import sys as _sys
        from pathlib import Path as _Path
        _root = str(_Path(__file__).resolve().parent.parent.parent)
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        from src.recall_telemetry import summarize as _recall_summarize
        out["recall_misses"] = _recall_summarize()
    except Exception as exc:  # noqa: BLE001
        out["recall_misses"] = {"error": str(exc)}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    snap = snapshot(args.days)
    if args.json:
        print(json.dumps(snap, indent=2, default=str))
        return 0

    cc, kg = snap["checkin_concentration"], snap["voluntary_kg_retrieval"]
    oc, op = snap["onboard_conversion"], snap["outcome_pipe_health"]
    print(f"Adoption KPI snapshot — last {args.days}d")
    print(f"  check-ins: {cc['total']} total, top-2 callers {cc['top2_share_pct']}%")
    print(f"  voluntary KG retrieval (named agents): {kg['named_searches']} calls "
          f"by {kg['distinct_agents']} agents")
    print(f"  onboard→checkin (process + BEAM external): {oc['converted']}/{oc['minted']} "
          f"({oc['conversion_pct']}%)")
    print(f"    ceremonial-only: {oc['ceremonial_converted']}/{oc['minted']} "
          f"({oc['ceremonial_conversion_pct']}%); "
          f"BEAM external: {oc['beam_converted']}/{oc['minted']} "
          f"({oc['beam_conversion_pct']}%)")
    print(f"  adopter value-engagement: {oc['cohort_engaged']}/{oc['cohort_minted']} "
          f"({oc['cohort_engaged_pct']}%) — real-agent cohort, ANY value action (check-in/KG/outcome)")
    print(f"  true bounce (onboarded, did nothing): {oc['did_nothing']}/{oc['minted']} "
          f"({oc['did_nothing_pct']}%)")
    print(f"  outcome_event pipe: {op['success_pct']}% success "
          f"({op['identity_errors']} identity_errors of {op['total']})")
    pk = snap["proactive_kg_surface"]
    print(f"  proactive KG surface: {pk['surfaced']} seen / {pk['fired']} fired "
          f"by {pk['agents']} agents")
    rm = snap.get("recall_misses") or {}
    print(f"  recall misses (search no-value, #972): {rm.get('total', 0)} total "
          f"{rm.get('by_class', {})}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Dashboard tool — read-only overview of all agents' EISV state.

Bypasses session binding so visualizers and dashboards can see the full system.
"""

import asyncio
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Sequence
from mcp.types import TextContent
from ..types import ToolArgumentsDict
from ..utils import success_response, error_response
from ..decorators import mcp_tool
from src.mcp_handlers.shared import lazy_mcp_server as mcp_server
from src.db import get_db
from src.logging_utils import get_logger

logger = get_logger(__name__)

# Inner deadline for the one slow external call — the full-fleet state read.
# The @mcp_tool decorator's 15s timeout is a HARD backstop, not a fine-grained
# guard: without an inner deadline a hung or scheduler-amplified DB read (see
# CLAUDE.md "Substrate Tax" — a documented ~60x in-handler amplification) makes
# the whole dashboard hang the full 15s and then fail outright, even though the
# in-memory metadata and live ODE overlay needed for a useful overview are
# already on hand. This budget degrades to that in-memory view fast instead.
# Generous relative to the ≤500ms Redis guards because a full-fleet state query
# legitimately does more work; override via env for tighter/looser tuning.
_DASHBOARD_DB_BUDGET_S_DEFAULT = 5.0


def _dashboard_db_budget_s() -> float:
    """Inner DB-read budget in seconds (env-overridable, falls back on garbage)."""
    raw = os.environ.get("UNITARES_DASHBOARD_DB_BUDGET_S")
    if raw is None:
        return _DASHBOARD_DB_BUDGET_S_DEFAULT
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DASHBOARD_DB_BUDGET_S_DEFAULT
    return value if value > 0 else _DASHBOARD_DB_BUDGET_S_DEFAULT


@mcp_tool("dashboard", timeout=15.0, description="Read-only system overview: all agents with EISV state. No session binding required.")
async def handle_dashboard(arguments: ToolArgumentsDict) -> Sequence[TextContent]:
    """Return all active agents with their current EISV vectors."""
    try:
        db = get_db()

        # Inner fast-fail guard. On a hung/amplified read we degrade to the
        # in-memory overview rather than letting the decorator's 15s backstop
        # hang the caller and then return nothing useful. A DB *exception*
        # still falls through to the outer except (full error response) — only
        # a *slow* read degrades here.
        db_budget_s = _dashboard_db_budget_s()
        degraded = False
        try:
            states = await asyncio.wait_for(
                db.get_all_latest_agent_states(), timeout=db_budget_s
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Dashboard DB state read exceeded %.1fs budget — degrading to "
                "in-memory metadata/ODE overlay (DB-derived EISV omitted this call)",
                db_budget_s,
            )
            states = []
            degraded = True

        # Index states by agent_id — E now comes from s.energy (extracted in _row_to_agent_state)
        state_by_agent: Dict[str, Any] = {}
        for s in states:
            sj = s.state_json or {}
            risk = sj.get("risk_score", 0)
            verdict = sj.get("verdict", "proceed")
            state_by_agent[s.agent_id] = {
                "E": round(s.energy, 4) if s.energy is not None else None,
                "I": round(s.integrity, 4) if s.integrity is not None else None,
                "S": round(s.entropy, 4) if s.entropy is not None else None,
                "V": round(s.void, 4) if s.void is not None else None,
                "coherence": round(s.coherence, 4) if s.coherence is not None else None,
                "basin": s.regime,
                "risk": round(risk, 4) if risk is not None else 0,
                "verdict": verdict,
            }

        # Filter: recent_days=1 by default. Agents tagged ``pinned`` are
        # always included regardless of recency (see is_pinned below).
        recent_days = int(arguments.get("recent_days", 1))
        min_updates = int(arguments.get("min_updates", 1))
        limit = int(arguments.get("limit", 15))
        offset = int(arguments.get("offset", 0))
        basin_filter = arguments.get("basin_filter", None)
        risk_threshold = arguments.get("risk_threshold", None)
        if risk_threshold is not None:
            risk_threshold = float(risk_threshold)
        cutoff = datetime.now(timezone.utc) - timedelta(days=recent_days) if recent_days > 0 else None

        agents = []
        for agent_id, meta in list(mcp_server.agent_metadata.items()):
            if meta.status != "active":
                continue
            if meta.total_updates < min_updates:
                continue

            # Pinned agents always included regardless of recency
            is_pinned = "pinned" in (getattr(meta, "tags", None) or [])

            # Filter by recency using last_update from metadata
            if cutoff and not is_pinned and meta.last_update:
                try:
                    last_dt = datetime.fromisoformat(meta.last_update.replace("Z", "+00:00"))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    if last_dt < cutoff:
                        continue
                except Exception:
                    # Unparseable timestamp — exclude agent (fail closed)
                    logger.warning(f"Dashboard: unparseable last_update for {agent_id}: {meta.last_update!r}")
                    continue

            eisv = state_by_agent.get(agent_id, None)
            agent_entry: Dict[str, Any] = {
                "id": agent_id,
                "label": getattr(meta, "label", None),
                "updates": meta.total_updates,
                "pinned": is_pinned,
                "last_update": getattr(meta, "last_update", None),
            }
            if eisv:
                agent_entry["eisv"] = eisv

            # ODE diagnostic overlay from in-memory monitors (primary EISV
            # is now behavioral, stored in DB via process_update metrics)
            try:
                monitors = getattr(mcp_server, 'monitors', None)
                if isinstance(monitors, dict):
                    monitor = monitors.get(agent_id)
                    if monitor and hasattr(monitor, 'state'):
                        ode_state = monitor.state
                        ode_risk = float(ode_state.risk_history[-1]) if ode_state.risk_history else None
                        ode_entry = {
                            "E": round(float(ode_state.E), 4),
                            "I": round(float(ode_state.I), 4),
                            "S": round(float(ode_state.S), 4),
                            "V": round(float(ode_state.V), 4),
                        }
                        if ode_risk is not None:
                            ode_entry["risk"] = round(ode_risk, 4)
                        agent_entry["ode"] = ode_entry

                        # Use live ODE risk as primary when DB value is missing/zero
                        if ode_risk is not None and not agent_entry.get("eisv", {}).get("risk"):
                            agent_entry.setdefault("eisv", {})["risk"] = round(ode_risk, 4)

                    if monitor and hasattr(monitor, '_behavioral_state'):
                        beh = monitor._behavioral_state
                        if getattr(beh, 'confidence', 0) >= 0.3:
                            agent_entry["behavioral"] = {
                                "E": round(beh.E, 4),
                                "I": round(beh.I, 4),
                                "S": round(beh.S, 4),
                                "V": round(beh.V, 4),
                            }
                        beh_verdict = getattr(monitor, '_last_behavioral_verdict', None)
                        if beh_verdict:
                            agent_entry.setdefault("eisv", {})["behavioral_verdict"] = beh_verdict
            except Exception:
                pass  # Overlay is best-effort

            agents.append(agent_entry)

        # Sort: pinned agents first, then by update count
        agents.sort(key=lambda a: (0 if a.get("pinned") else 1, -(a.get("updates") or 0)))

        # Apply basin_filter and risk_threshold AFTER sorting
        if basin_filter is not None:
            agents = [a for a in agents if a.get("eisv", {}).get("basin") == basin_filter]
        if risk_threshold is not None:
            agents = [a for a in agents if (a.get("eisv", {}).get("risk") or 0) >= risk_threshold]

        # Apply offset + limit
        total = len(agents)
        agents = agents[offset:offset + limit]

        response: Dict[str, Any] = {
            "agents": agents,
            "total": total,
            "showing": len(agents),
            "offset": offset,
            "has_more": (offset + len(agents)) < total,
            "degraded": degraded,
        }
        if degraded:
            response["degraded_reason"] = (
                f"DB state read exceeded {db_budget_s:.0f}s inner budget; showing "
                "in-memory metadata and live ODE overlay only "
                "(DB-derived EISV omitted this call)"
            )
        return success_response(response)

    except Exception as e:
        logger.error(f"Dashboard error: {e}")
        return error_response(f"Dashboard failed: {e}")

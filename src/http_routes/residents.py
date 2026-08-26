"""Residents view: per-operator always-on agents, EISV enrichment, tag audit.

Split out of src/http_api.py (see that module for route registration).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Dict, Optional

from starlette.responses import JSONResponse


from src.logging_utils import get_logger
from src.broadcaster import broadcaster_instance

from src.http_routes import access
from src.grounding.onboard_classifier import RESIDENT_DEFAULT_TAGS

# Tags every active resident must carry. Single-sourced from the server-side
# RESIDENT_DEFAULT_TAGS (the same constant the creation-time stamp and the
# check-in reconcile use) so the audit endpoint and the writers cannot drift
# apart within this process. Keep in sync with the cross-process SDK copy
# agents/sdk/src/unitares_sdk/agent.py::RESIDENT_TAGS. The Steward regression
# of 2026-04-20 was caused by this set drifting across onboarding paths —
# the tag-audit endpoint exists so future drift is detectable in production.
#
# Hoisted to module scope 2026-08-26: resident label resolution now ranks on
# these too, and that runs well above the tag-audit endpoint in this file.
RESIDENT_REQUIRED_TAGS: frozenset[str] = frozenset(RESIDENT_DEFAULT_TAGS)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Residents endpoint — per-operator configurable "always-on agents" view
# ---------------------------------------------------------------------------


# Per-label silence thresholds in seconds — agents quiet for longer than this
# are flagged "silent" on the dashboard. DEPLOYMENT CONFIG, empty by default,
# same contract as UNITARES_RESIDENTS: a fresh install inherits no operator's
# residents and no operator's cron cadences.
#
# Format: ``UNITARES_RESIDENT_SILENCE_SECONDS="vigil=2400,sentinel=900"``
# (label=seconds, comma-separated, labels matched case-insensitively).
#
# This is a FALLBACK and should shrink to nothing. The generic path is a
# ``cadence.*`` tag on the agent, which drives the threshold with no label
# lookup at all; this map only covers agents not yet carrying one. Tag the
# agent instead of adding an entry here.
_SILENCE_ENV = "UNITARES_RESIDENT_SILENCE_SECONDS"


def _load_resident_silence_seconds() -> Dict[str, int]:
    """Parse ``label=seconds`` pairs from the environment. Bad pairs are skipped."""
    raw = os.getenv(_SILENCE_ENV, "").strip()
    if not raw:
        return {}
    out: Dict[str, int] = {}
    for pair in raw.split(","):
        label, _, value = pair.partition("=")
        label = label.strip().lower()
        try:
            seconds = int(value.strip())
        except ValueError:
            continue
        if label and seconds > 0:
            out[label] = seconds
    return out


_DEFAULT_RESIDENT_SILENCE_SECONDS: Dict[str, int] = _load_resident_silence_seconds()


def _resolve_resident_labels(mcp_server_obj) -> tuple[list[str], str]:
    """Figure out which agent labels to treat as residents.

    Precedence (operator choice wins):
    1. ``UNITARES_RESIDENT_AGENTS`` env var — comma-separated labels  → "env"
    2. Agent metadata with a ``resident`` attribute set to True       → "metadata"
    3. ``KNOWN_RESIDENT_LABELS`` ∩ labels present in agent_metadata   → "known-residents"
       (the roster declared in ``UNITARES_RESIDENTS`` is the source of truth;
       the dashboard reuses it rather than re-declaring it per-surface)
    4. Empty list                                                     → "none"

    Two env vars appear above and they are NOT the same knob:

    * ``UNITARES_RESIDENTS`` is the deployment roster — who the residents are,
      which calibration class each gets, and (being an ordered list) the order
      they are presented in. Most deployments set only this.
    * ``UNITARES_RESIDENT_AGENTS`` is a route-local override for *this*
      endpoint, for when the dashboard should show a different set than the
      calibration roster. Leave it unset unless you want them to diverge.

    See docs/operations/resident-roster.md.

    Returns ``(labels, source)`` so the caller can label the response without
    re-deriving the precedence state.
    """
    env_value = os.getenv("UNITARES_RESIDENT_AGENTS", "").strip()
    if env_value:
        labels = [lbl.strip() for lbl in env_value.split(",") if lbl.strip()]
        return labels, "env"

    flagged: list[str] = []
    for meta in getattr(mcp_server_obj, "agent_metadata", {}).values():
        if getattr(meta, "resident", False):
            label = getattr(meta, "label", None) or getattr(meta, "display_name", None)
            if label:
                flagged.append(label)
    if flagged:
        return flagged, "metadata"

    # Path 3: auto-detect from the canonical resident list, intersected with
    # the actual fleet so a fresh install doesn't advertise absent residents.
    from src.grounding.class_indicator import KNOWN_RESIDENT_LABELS
    present: set[str] = set()
    for meta in getattr(mcp_server_obj, "agent_metadata", {}).values():
        label = getattr(meta, "label", None) or getattr(meta, "display_name", None)
        if label and label in KNOWN_RESIDENT_LABELS:
            present.add(label)
    if present:
        # Order comes from the roster as the deployment DECLARED it in
        # UNITARES_RESIDENTS, so the dashboard doesn't jitter when dict
        # ordering shifts and no operator's names are baked in here.
        #
        # This used to filter through a hardcoded list of six labels. Any
        # deployment whose residents were named anything else resolved to an
        # EMPTY list reported with source "known-residents" — a roster that
        # silently vanished while the response still read like a success.
        from src.grounding.class_indicator import KNOWN_RESIDENT_ORDER

        ordered = [lbl for lbl in KNOWN_RESIDENT_ORDER if lbl in present]
        # Defensive: a label can only reach `present` by being in the roster,
        # so this should be empty. Append rather than drop if that ever stops
        # being true — losing a resident must not be the quiet outcome.
        ordered += sorted(present.difference(ordered))
        return ordered, "known-residents"

    return [], "none"


def _latest_eisv_for_agent(agent_id: str) -> Optional[dict]:
    """Find the most recent eisv_update event for a given agent_id in the broadcaster history."""
    for event in reversed(broadcaster_instance.event_history):
        if not isinstance(event, dict):
            continue
        if event.get("type") != "eisv_update":
            # Broadcaster puts eisv_updates in event_history too; non-eisv events are skipped.
            continue
        if event.get("agent_id") == agent_id:
            return event
    return None


async def _durable_latest_eisv_for_agent(
    agent_id: str, label: Optional[str] = None,
) -> Optional[dict]:
    """Most recent durable EISV check-in for an agent from core.agent_state.

    The residents panel reads live EISV from the broadcaster's in-memory ring
    (~6h, fleet-wide). A *daily* resident like Chronicler checks in once per
    24h, so its eisv_update has almost always rotated out of the ring by the
    time the panel loads — leaving its EISV blank even though it checked in
    fine. core.agent_state persists every check-in (the same source
    /v1/agents/{id}/history reads), so fall back to it.

    Returns a broadcaster-event-shaped dict so ``_extract_eisv_fields`` consumes
    it unchanged (E in state_json; I/S/V/coherence/risk_score in columns;
    verdict/action in state_json). Read-only; returns None on miss or error so
    the panel degrades to "no EISV" rather than erroring.
    """
    try:
        from src.db import get_db
        db = get_db()
        async with db.acquire() as conn:
            # Identity resolution mirrors http_agent_history: history is keyed by
            # the agent's UUID identity, but the resident row may carry the
            # structured id (mcp_DATE_<8hex>) whose suffix is that UUID's prefix.
            row = await conn.fetchrow(
                """
                WITH ids AS (
                    SELECT identity_id FROM core.identities WHERE agent_id = $1
                    UNION
                    SELECT identity_id FROM core.identities
                     WHERE substring($1 from '([0-9a-f]{8})$') IS NOT NULL
                       AND agent_id ~ '^[0-9a-f]{8}-'
                       AND agent_id LIKE substring($1 from '([0-9a-f]{8})$') || '%'
                )
                SELECT s.recorded_at,
                       (s.state_json->>'E')::real AS e,
                       s.integrity AS i, s.entropy AS s_entropy, s.volatility AS v,
                       s.coherence, s.risk_score,
                       s.state_json->>'verdict' AS verdict,
                       s.state_json->>'action'  AS action
                FROM core.agent_state s
                WHERE s.identity_id IN (SELECT identity_id FROM ids)
                  AND s.synthetic = false
                ORDER BY s.recorded_at DESC
                LIMIT 1
                """,
                agent_id,
            )
        if not row:
            return None
        recorded_at = row["recorded_at"]
        return {
            "type": "eisv_update",
            "agent_id": agent_id,
            "agent_name": label,
            "timestamp": recorded_at.isoformat() if recorded_at else None,
            "eisv": {"E": row["e"], "I": row["i"], "S": row["s_entropy"], "V": row["v"]},
            "coherence": row["coherence"],
            "metrics": {"risk_score": row["risk_score"], "verdict": row["verdict"]},
            "decision": {"action": row["action"]},
        }
    except Exception as exc:  # noqa: BLE001 — read-only panel fallback, degrade gracefully
        logger.debug("durable EISV fallback failed for %s: %s", agent_id, exc)
        return None


def _extract_eisv_fields(event: dict) -> dict:
    """Pull the data-shape we expose to the dashboard from a raw broadcaster event.

    The broadcaster stores eisv updates with nested ``eisv`` and ``metrics``
    dicts. Surface them flat so the JSON payload is convenient for the
    frontend without re-mapping.
    """
    eisv = event.get("eisv") or {}
    metrics = event.get("metrics") or {}
    decision = event.get("decision") or {}
    return {
        "E": eisv.get("E"),
        "I": eisv.get("I"),
        "S": eisv.get("S"),
        "V": eisv.get("V"),
        "coherence": event.get("coherence") if event.get("coherence") is not None else metrics.get("coherence"),
        "risk_score": metrics.get("risk_score") if metrics.get("risk_score") is not None else event.get("risk"),
        # Verdict can come from decision.action (governance dynamics) or
        # metrics.verdict (behavioral classifier — "safe", "caution", etc.).
        "verdict": decision.get("action") or metrics.get("verdict"),
        "agent_name": event.get("agent_name"),
        "timestamp": event.get("timestamp"),
    }


def _parse_resident_timestamp(value: object) -> Optional[datetime]:
    """Parse resident activity timestamps as timezone-aware datetimes."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _safe_resident_total_updates(meta: object) -> int:
    try:
        return int(getattr(meta, "total_updates", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _resident_label_claim(label: Optional[str], agent_id: object) -> Optional[str]:
    """The roster label this row is claiming, exact or renamed-on-collision.

    ``identity/persistence.py`` renames a fresh identity to ``{label}_{uuid8}``
    when another ACTIVE agent already holds the label it asked for. The rename
    lands on the newcomer regardless of which row is the real resident, so a
    ghost holding the clean name pushes the genuine resident to a suffixed
    label -- and every resident surface here resolves by exact label, so the
    genuine resident stops being a candidate at all while the ghost is the one
    presented and audited.

    Measured 2026-08-26 over the full identity table: 201 suffixed
    resident-prefixed labels, of which 199 are ghosts correctly taking the
    suffix -- that is the mechanism working. Only 2 were the inverse, a real
    resident renamed behind a ghost. It is rare, but it is silent and it
    persists: ``Watcher_7bf970d4`` carried ``persistent`` under a suffixed
    label from 2026-04-19 to 2026-06-14, invisible to this route and to the
    tag audit for nearly two months.

    Only the exact ``_{own uuid8}`` suffix counts. Stripping any trailing
    ``_xxxxxxxx`` would capture deliberately distinct names (a
    ``Sentinel_backup``-style label) and merge two real agents into one row.
    """
    if not label:
        return None
    prefix, sep, suffix = label.rpartition("_")
    if not sep or not prefix:
        return None
    if suffix and suffix == str(agent_id or "")[:8]:
        return prefix
    return None


def _resident_meta_preference_key(
    meta: object, *, exact_label: bool = True
) -> tuple[int, int, int, float, int]:
    """Sort key for rows competing for one roster label.

    Governance restarts can leave active 0-update resident forks beside the
    canonical row. Prefer an active row, then one that actually carries the
    resident tags, then an exact label over a renamed one, then a row with
    real updates, the freshest timestamp, and total update count.

    ``has_required_tags`` outranks ``exact_label`` deliberately. The tags are
    the only ground truth for "this is the resident" -- they are privileged,
    granted by the server at mint, and cannot be self-assigned -- whereas the
    label is cosmetic and is exactly what the collision rename took away. A
    ghost holding the clean name must never outrank the tagged resident.
    """
    status = getattr(meta, "status", None)
    total_updates = _safe_resident_total_updates(meta)
    last_dt = _parse_resident_timestamp(getattr(meta, "last_update", None))
    tags = set(getattr(meta, "tags", None) or [])
    return (
        1 if status == "active" else 0,
        1 if RESIDENT_REQUIRED_TAGS <= tags else 0,
        1 if exact_label else 0,
        1 if total_updates > 0 else 0,
        last_dt.timestamp() if last_dt else 0.0,
        total_updates,
    )


def _latest_eisv_for_label(label: str) -> Optional[dict]:
    """Find the most recent eisv_update event for a resident label."""
    for event in reversed(broadcaster_instance.event_history):
        if not isinstance(event, dict):
            continue
        if event.get("type") != "eisv_update":
            continue
        if event.get("agent_name") == label:
            return event
    return None


def _coherence_history_for_agent(agent_id: str, window_minutes: int = 60) -> list[dict]:
    """Collect coherence (plus risk, verdict) data points for a sparkline.

    Pulls from the broadcaster's 2000-entry event ring buffer — this covers
    roughly 6 hours of moderate activity. Each point has ts, coherence, risk.
    """
    cutoff = time.time() - window_minutes * 60
    points: list[dict] = []
    for event in broadcaster_instance.event_history:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "eisv_update":
            continue
        if event.get("agent_id") != agent_id:
            continue
        ts_str = event.get("timestamp")
        try:
            ts = datetime.fromisoformat(str(ts_str).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
        if ts < cutoff:
            continue
        flat = _extract_eisv_fields(event)
        if flat["coherence"] is None:
            continue
        points.append({
            "ts": ts,
            "coherence": float(flat["coherence"]),
            "risk": float(flat["risk_score"]) if flat["risk_score"] is not None else None,
            "verdict": flat["verdict"],
        })
    return points


async def _recent_writes_for_agent(agent_id: str, limit: int = 5) -> list[dict]:
    """Pull recent KG writes authored by this agent, newest first.

    Uses the shared graph query rather than re-reading the broadcaster history,
    so this survives broadcaster restarts and covers more than the last 6h.
    """
    try:
        from src.knowledge_graph import get_knowledge_graph
        graph = await get_knowledge_graph()
        discoveries = await graph.query(agent_id=agent_id, limit=limit)
        out = []
        for d in (discoveries or [])[:limit]:
            out.append({
                "id": getattr(d, "id", None),
                "type": getattr(d, "type", None) or "note",
                "severity": getattr(d, "severity", None) or "low",
                "summary": (getattr(d, "summary", None) or "")[:200],
                "tags": list(getattr(d, "tags", None) or []),
                "timestamp": getattr(d, "timestamp", None),
            })
        return out
    except Exception as exc:
        logger.debug("_recent_writes_for_agent(%s) failed: %s", agent_id, exc)
        return []


async def http_residents(request):
    """Per-resident fleet view for the dashboard.

    Response shape::

        {
            "success": true,
            "configured": ["Vigil", "Sentinel", ...],
            "residents": [
                {
                    "label": "Vigil",
                    "agent_id": "...",
                    "status": "healthy" | "silent" | "paused" | "unknown",
                    "silence_seconds": 142,
                    "silence_threshold_seconds": 2400,
                    "last_checkin_at": "2026-04-14T...",
                    "last_checkin_source": "broadcaster_eisv" | "agent_metadata",
                    "metadata_last_update": "2026-04-14T...",
                    "latest_eisv_at": "2026-04-14T...",
                    "eisv": {"E": ..., "I": ..., "S": ..., "V": ...},
                    "coherence": 0.48,
                    "risk_score": 0.12,
                    "verdict": "proceed",
                    "history": [{"ts": ..., "coherence": ..., "risk": ...}, ...],
                    "recent_writes": [{"summary": ..., "tags": ..., ...}, ...],
                    "total_updates": 467
                },
                ...
            ],
            "source": "env" | "metadata" | "known-residents" | "none"
        }
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    try:
        from src.mcp_handlers.shared import lazy_mcp_server
        mcp_server_obj = lazy_mcp_server

        labels, source = _resolve_resident_labels(mcp_server_obj)

        # Index agent_metadata by label for O(1) lookup. When the same label
        # appears multiple times (e.g. archived + active duplicates created
        # across server restarts), prefer the most-active live record so the
        # dashboard tracks the agent that's actually running.
        # A row competes for a roster label under its exact label OR under the
        # `{label}_{uuid8}` name the collision rename gave it. Without the
        # second case a resident that lost its name to a ghost is not a
        # candidate at all, and the ghost is what the dashboard shows.
        label_to_meta = {}
        label_exactness = {}
        for agent_id, meta in list(getattr(mcp_server_obj, "agent_metadata", {}).items()):
            label = getattr(meta, "label", None)
            if not label:
                continue
            claims = [(label, True)]
            renamed_from = _resident_label_claim(label, agent_id)
            if renamed_from:
                claims.append((renamed_from, False))
            for claim, exact in claims:
                key = _resident_meta_preference_key(meta, exact_label=exact)
                existing = label_to_meta.get(claim)
                if existing is None:
                    label_to_meta[claim] = (agent_id, meta)
                    label_exactness[claim] = key
                    continue
                if key > label_exactness[claim]:
                    label_to_meta[claim] = (agent_id, meta)
                    label_exactness[claim] = key

        residents: list[dict] = []
        now_ts = time.time()
        for label in labels:
            entry = label_to_meta.get(label)
            agent_id = entry[0] if entry else None
            meta = entry[1] if entry else None

            latest = _latest_eisv_for_agent(agent_id) if agent_id else None

            # If metadata points at a stale duplicate UUID but the broadcaster
            # has a newer EISV event for the same resident label, follow the
            # live event. This catches resident identity skew without waiting
            # for metadata hydration to converge.
            latest_by_label = _latest_eisv_for_label(label)
            if latest_by_label:
                label_dt = _parse_resident_timestamp(latest_by_label.get("timestamp"))
                current_event_dt = _parse_resident_timestamp(latest.get("timestamp")) if latest else None
                metadata_dt_for_selection = _parse_resident_timestamp(
                    getattr(meta, "last_update", None) if meta else None
                )
                if (
                    label_dt
                    and (current_event_dt is None or label_dt > current_event_dt)
                    and (metadata_dt_for_selection is None or label_dt >= metadata_dt_for_selection)
                ):
                    latest = latest_by_label
                    event_agent_id = latest_by_label.get("agent_id")
                    if event_agent_id and event_agent_id != agent_id:
                        agent_id = event_agent_id
                        meta = getattr(mcp_server_obj, "agent_metadata", {}).get(event_agent_id)

            # Durable EISV fallback. A daily resident (Chronicler) checks in once
            # per 24h, so its eisv_update has usually rotated out of the
            # broadcaster's ~6h in-memory ring by the time the panel loads — the
            # ring lookups above return None and its EISV shows blank. Read the
            # latest persisted check-in from core.agent_state instead. Labeled
            # distinctly ("agent_state") so the provenance stays honest.
            eisv_source = "broadcaster_eisv"
            if latest is None and agent_id:
                durable_latest = await _durable_latest_eisv_for_agent(agent_id, label)
                if durable_latest:
                    latest = durable_latest
                    eisv_source = "agent_state"

            history = _coherence_history_for_agent(agent_id) if agent_id else []
            recent_writes = await _recent_writes_for_agent(agent_id) if agent_id else []

            # Compute silence in seconds. The dashboard agent list uses
            # metadata.last_update while the resident strip also has access to
            # websocket/broadcaster EISV events. Treat both as activity signals
            # and choose the newest, otherwise the two dashboard rows can
            # disagree by several minutes after broadcaster gaps/restarts.
            metadata_last_update = getattr(meta, "last_update", None) if meta else None
            latest_eisv_at = latest.get("timestamp") if latest and latest.get("timestamp") else None
            metadata_dt = _parse_resident_timestamp(metadata_last_update)
            latest_dt = _parse_resident_timestamp(latest_eisv_at)
            last_checkin_str = None
            last_checkin_source = None
            if metadata_dt and latest_dt:
                if metadata_dt >= latest_dt:
                    last_checkin_str = metadata_last_update
                    last_checkin_source = "agent_metadata"
                else:
                    last_checkin_str = latest_eisv_at
                    last_checkin_source = eisv_source
            elif metadata_dt:
                last_checkin_str = metadata_last_update
                last_checkin_source = "agent_metadata"
            elif latest_dt:
                last_checkin_str = latest_eisv_at
                last_checkin_source = eisv_source

            silence_seconds: Optional[float] = None
            last_dt = _parse_resident_timestamp(last_checkin_str)
            if last_dt:
                silence_seconds = max(0.0, now_ts - last_dt.timestamp())

            # Prefer tag-driven cadence (generic, label-independent); fall
            # back to the deployment-declared per-label map for agents not yet
            # migrated to ``cadence.*`` tags. Both empty => 30 min.
            silence_threshold: int = 30 * 60
            meta_tags = getattr(meta, "tags", None) or []
            from src.background_tasks import cadence_from_tags
            tag_cadence = cadence_from_tags(meta_tags)
            if tag_cadence is not None:
                # Threshold = 2x expected cadence — tolerates one missed cycle.
                silence_threshold = tag_cadence * 2
            else:
                silence_threshold = _DEFAULT_RESIDENT_SILENCE_SECONDS.get(label.lower(), 30 * 60)

            # Status: paused > silent > healthy > unknown.
            status = "unknown"
            if meta and getattr(meta, "status", None) in ("paused", "archived"):
                status = getattr(meta, "status")
            elif silence_seconds is not None and silence_seconds > silence_threshold:
                status = "silent"
            elif latest is not None or silence_seconds is not None:
                status = "healthy"

            flat = _extract_eisv_fields(latest) if latest else None
            from src.resident_progress.registry import is_event_driven_label
            event_driven = is_event_driven_label(label)
            residents.append({
                "label": label,
                "agent_id": agent_id,
                "status": status,
                "event_driven": event_driven,
                "silence_seconds": round(silence_seconds, 1) if silence_seconds is not None else None,
                "silence_threshold_seconds": silence_threshold,
                "last_checkin_at": last_checkin_str,
                "last_checkin_source": last_checkin_source,
                "metadata_last_update": metadata_last_update,
                "latest_eisv_at": latest_eisv_at,
                "eisv": {
                    "E": flat["E"],
                    "I": flat["I"],
                    "S": flat["S"],
                    "V": flat["V"],
                } if flat else None,
                "coherence": flat["coherence"] if flat else None,
                "risk_score": flat["risk_score"] if flat else None,
                "verdict": flat["verdict"] if flat else None,
                "history": history,
                "recent_writes": recent_writes,
                "total_updates": getattr(meta, "total_updates", 0) if meta else 0,
            })

        return JSONResponse({
            "success": True,
            "configured": labels,
            "residents": residents,
            "source": source,
        })
    except Exception as exc:
        logger.error("http_residents error: %s", exc)
        return JSONResponse({
            "success": False,
            "error": str(exc),
            "residents": [],
        }, status_code=500)


# ---------------------------------------------------------------------------
# Resident tag-hygiene audit — Vigil consumes this to detect tag drift
# ---------------------------------------------------------------------------

async def http_resident_tag_audit(request):
    """Report which active residents are missing required tags.

    Response shape::

        {
            "success": true,
            "required_tags": ["persistent", "autonomous"],
            "checked": ["Vigil", "Sentinel", "Watcher", "Steward", "Chronicler", "Lumen"],
            "missing": {
                "Watcher": ["autonomous"],
                ...
            },
            "ok_count": 4
        }

    `missing` is empty when the fleet is healthy. Each entry is a sorted list
    of tags that the resident SHOULD carry but doesn't. Residents absent from
    the running fleet are absent from both ``checked`` and ``missing``.
    """
    http_api_token = os.getenv("UNITARES_HTTP_API_TOKEN")
    if not access._check_http_auth(request, http_api_token=http_api_token):
        return access._http_unauthorized()

    try:
        from src.mcp_handlers.shared import lazy_mcp_server
        from src.grounding.class_indicator import KNOWN_RESIDENT_LABELS

        mcp_server_obj = lazy_mcp_server
        checked: list[str] = []
        missing: dict[str, list[str]] = {}

        # Pick ONE row per roster label, by the same preference order
        # http_residents uses. First-encountered was wrong twice over: dict
        # order is arbitrary, and a resident renamed to `{label}_{uuid8}` by
        # the collision rename was skipped entirely (its label is not on the
        # roster) while the ghost holding the clean name was audited in its
        # place. That reports a correctly-tagged resident as missing its tags,
        # which is what Vigil then alarms on — every cycle, with no way to
        # tell a real gap from a shadowed one.
        best: dict[str, tuple] = {}
        for agent_id, meta in getattr(mcp_server_obj, "agent_metadata", {}).items():
            if getattr(meta, "status", None) != "active":
                continue
            label = getattr(meta, "label", None)
            if not label:
                continue
            claims = [(label, True)] if label in KNOWN_RESIDENT_LABELS else []
            renamed_from = _resident_label_claim(label, agent_id)
            if renamed_from and renamed_from in KNOWN_RESIDENT_LABELS:
                claims.append((renamed_from, False))
            for claim, exact in claims:
                key = _resident_meta_preference_key(meta, exact_label=exact)
                if claim not in best or key > best[claim][0]:
                    best[claim] = (key, meta)

        for label, (_key, meta) in best.items():
            checked.append(label)
            have = set(getattr(meta, "tags", None) or [])
            gap = sorted(RESIDENT_REQUIRED_TAGS - have)
            if gap:
                missing[label] = gap

        return JSONResponse({
            "success": True,
            "required_tags": sorted(RESIDENT_REQUIRED_TAGS),
            "checked": sorted(checked),
            "missing": missing,
            "ok_count": len(checked) - len(missing),
        })
    except Exception as exc:
        logger.error("http_resident_tag_audit error: %s", exc)
        return JSONResponse({
            "success": False,
            "error": str(exc),
        }, status_code=500)

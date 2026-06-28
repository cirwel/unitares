#!/usr/bin/env python3
"""
Sentinel — The Independent Observer

A continuous agent that monitors UNITARES governance in real-time via WebSocket,
detects fleet-wide anomalies, correlates incidents, and generates situation reports.

Unlike Vigil (cron, every 30 min, janitorial), Sentinel is:
- Continuous (WebSocket-connected, event-driven)
- Analytical (cross-agent correlation, fleet statistics)
- Interventional (can pause agents, escalate to human)

Usage:
    python3 agents/sentinel/agent.py                # Run continuously
    python3 agents/sentinel/agent.py --sitrep       # Generate situation report and exit
    python3 agents/sentinel/agent.py --once         # Run one analysis cycle and exit

Architecture:
    1. Resumes persistent "Sentinel" identity via SDK GovernanceAgent
    2. Connects to /ws/eisv WebSocket for real-time event stream
    3. Maintains rolling EISV windows per agent (fleet state)
    4. Every 5 minutes: analyzes fleet, detects anomalies, checks in to governance
    5. On anomaly: leaves KG notes, sends macOS notifications
    6. On --sitrep: queries audit trail, generates timeline report
"""

import asyncio
import json
import os
import signal
import sys
import time

from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.common.config import GOV_MCP_URL, GOV_WS_URL
from unitares_sdk.agent import CycleResult, GovernanceAgent
from unitares_sdk.client import GovernanceClient
from unitares_sdk.models import CheckinResult
from unitares_sdk.utils import notify
from agents.common.findings import post_finding, compute_fingerprint
from agents.sentinel.phase_b_promotion import (
    PhaseBEvaluatorError,
    detect_transitions as detect_phase_b_transitions,
)

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------

SESSION_FILE = Path.home() / ".unitares" / "anchors" / "sentinel.json"
LEGACY_SESSION_FILE = project_root / ".sentinel_session"
STATE_FILE = project_root / ".sentinel_state"
LOG_FILE = Path.home() / "Library" / "Logs" / "unitares-sentinel.log"
MAX_LOG_LINES = 1000

# Analysis cycle interval
ANALYSIS_INTERVAL = 300  # 5 minutes

# Hard upper bound on a single analysis cycle. Normal process_agent_update
# completes in <10s; 45s leaves comfortable slack while preventing a hung
# MCP call from blocking the main loop indefinitely (the anyio/asyncpg
# deadlock documented in unitares CLAUDE.md can hang call_tool
# without raising, which previously wedged Sentinel for ~30h until
# manual restart).
CYCLE_TIMEOUT = 45  # seconds

# Fleet anomaly thresholds
FLEET_COHERENCE_DROP_THRESHOLD = 0.15   # single-agent coherence drop to flag
FLEET_COORDINATED_WINDOW = 600          # 10 min window for coordinated detection
FLEET_COORDINATED_MIN_AGENTS = 2        # min agents degrading simultaneously
FLEET_ENTROPY_SIGMA = 2.0               # z-score for fleet entropy anomaly

# Rolling window sizes
EISV_WINDOW_SIZE = 72     # ~6h at 5-min intervals
EVENT_WINDOW_SIZE = 500   # recent events for correlation


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_interactive = sys.stdout.isatty()


def log(message: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    if _interactive:
        print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Fleet State — rolling EISV windows per agent
# ---------------------------------------------------------------------------

class AgentSnapshot:
    """Rolling window of EISV observations for one agent."""
    __slots__ = ("agent_id", "name", "eisv_history", "last_seen", "last_verdict",
                 "last_coherence", "coherence_history")

    def __init__(self, agent_id: str, name: str = ""):
        self.agent_id = agent_id
        self.name = name
        self.eisv_history: deque[Dict[str, Any]] = deque(maxlen=EISV_WINDOW_SIZE)
        self.coherence_history: deque[float] = deque(maxlen=EISV_WINDOW_SIZE)
        self.last_seen: float = 0.0
        self.last_verdict: str = ""
        self.last_coherence: float = 1.0

    def record(self, event: Dict[str, Any]):
        self.last_seen = time.time()
        self.name = event.get("agent_name", self.name)

        eisv = event.get("eisv", {})
        coherence = event.get("coherence", 0)
        decision = event.get("decision", {})
        verdict = decision.get("action", "") if isinstance(decision, dict) else ""

        self.eisv_history.append({
            "ts": self.last_seen,
            "E": eisv.get("E", 0),
            "I": eisv.get("I", 0),
            "S": eisv.get("S", 0),
            "V": eisv.get("V", 0),
            "coherence": coherence,
            "verdict": verdict,
        })
        self.coherence_history.append(coherence)
        self.last_verdict = verdict
        self.last_coherence = coherence

    def coherence_drop(self, window_seconds: float = 600) -> float:
        """Return coherence drop in the last window. Positive = degradation."""
        if len(self.coherence_history) < 2:
            return 0.0
        cutoff = time.time() - window_seconds
        recent = [h for h in self.eisv_history if h["ts"] >= cutoff]
        if len(recent) < 2:
            return 0.0
        return recent[0]["coherence"] - recent[-1]["coherence"]

    def mean_entropy(self, window_seconds: float = 3600) -> float:
        cutoff = time.time() - window_seconds
        recent = [h["S"] for h in self.eisv_history if h["ts"] >= cutoff]
        if not recent:
            return 0.0
        return sum(recent) / len(recent)


class FleetState:
    """Tracks all agents' EISV state for cross-agent analysis."""

    def __init__(self):
        self.agents: Dict[str, AgentSnapshot] = {}
        self.events: deque[Dict[str, Any]] = deque(maxlen=EVENT_WINDOW_SIZE)
        self.incidents: List[Dict[str, Any]] = []

    def ingest(self, event: Dict[str, Any]):
        """Process a WebSocket event."""
        self.events.append(event)

        event_type = event.get("type", "")
        agent_id = event.get("agent_id", "")

        if event_type == "eisv_update" and agent_id:
            if agent_id not in self.agents:
                self.agents[agent_id] = AgentSnapshot(agent_id, event.get("agent_name", ""))
            self.agents[agent_id].record(event)

    def analyze(self, self_agent_id: str = "") -> List[Dict[str, Any]]:
        """Run fleet-wide anomaly detection. Self-findings are tagged, not excluded."""
        findings: List[Dict[str, Any]] = []
        now = time.time()

        # --- 1. Coordinated coherence drop ---
        degraded = []
        for aid, snap in self.agents.items():
            if now - snap.last_seen > FLEET_COORDINATED_WINDOW * 2:
                continue  # stale agent, skip
            drop = snap.coherence_drop(FLEET_COORDINATED_WINDOW)
            if drop >= FLEET_COHERENCE_DROP_THRESHOLD:
                degraded.append((aid, snap.name, drop))

        if len(degraded) >= FLEET_COORDINATED_MIN_AGENTS:
            agents_str = ", ".join(f"{name or aid[:8]}(-{drop:.2f})" for aid, name, drop in degraded)
            findings.append({
                "type": "coordinated_degradation",
                "violation_class": "CON",
                "severity": "high",
                "summary": f"Coordinated coherence drop: {agents_str}",
                "agents": [aid for aid, _, _ in degraded],
                "details": {aid: round(drop, 3) for aid, _, drop in degraded},
            })

        # --- 2. Fleet entropy anomaly ---
        entropies = []
        for aid, snap in self.agents.items():
            if now - snap.last_seen > 3600:
                continue
            s = snap.mean_entropy(3600)
            if s > 0:
                entropies.append((aid, snap.name, s))

        if len(entropies) >= 3:
            values = [s for _, _, s in entropies]
            mean_s = sum(values) / len(values)
            if len(values) > 1:
                var = sum((x - mean_s) ** 2 for x in values) / (len(values) - 1)
                std_s = var ** 0.5
                if std_s > 0:
                    for aid, name, s in entropies:
                        z = (s - mean_s) / std_s
                        if z >= FLEET_ENTROPY_SIGMA:
                            is_self = (aid == self_agent_id)
                            findings.append({
                                "type": "entropy_outlier",
                                "violation_class": "ENT",
                                "severity": "info" if is_self else "medium",
                                "summary": f"{name or aid[:8]} entropy outlier (z={z:.1f}, S={s:.3f})",
                                "agents": [aid],
                                "self_observation": is_self,
                            })

        # --- 3. Verdict distribution shift ---
        recent_verdicts = []
        for aid, snap in self.agents.items():
            if now - snap.last_seen > FLEET_COORDINATED_WINDOW:
                continue
            for h in snap.eisv_history:
                if h["ts"] >= now - FLEET_COORDINATED_WINDOW:
                    recent_verdicts.append(h["verdict"])

        if len(recent_verdicts) >= 5:
            pause_count = sum(1 for v in recent_verdicts if v in ("pause", "reject"))
            pause_rate = pause_count / len(recent_verdicts)
            if pause_rate >= 0.20:
                findings.append({
                    "type": "verdict_shift",
                    "violation_class": "ENT",
                    "severity": "high",
                    "summary": f"Pause rate {pause_rate:.0%} in last {FLEET_COORDINATED_WINDOW // 60}min ({pause_count}/{len(recent_verdicts)})",
                    "details": {"pause_rate": round(pause_rate, 3), "pause_count": pause_count},
                })

        # --- 4. Incident correlation from typed events ---
        typed_events = [e for e in self.events
                        if e.get("type", "").startswith(("lifecycle_", "circuit_breaker_", "identity_", "knowledge_"))]
        recent_typed = [e for e in typed_events if self._event_age(e) < FLEET_COORDINATED_WINDOW]

        if len(recent_typed) >= 3:
            # Multiple event types in short window = potential incident
            event_types = set(e.get("type") for e in recent_typed)
            if len(event_types) >= 2:
                findings.append({
                    "type": "correlated_events",
                    "violation_class": "BEH",
                    "severity": "medium",
                    "summary": f"{len(recent_typed)} governance events in {FLEET_COORDINATED_WINDOW // 60}min: {', '.join(sorted(event_types))}",
                    "details": {"event_types": sorted(event_types), "count": len(recent_typed)},
                })

        return findings

    def _event_age(self, event: Dict[str, Any]) -> float:
        ts = event.get("timestamp", "")
        if ts:
            try:
                return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
            except (ValueError, TypeError):
                pass
        return float("inf")

    def fleet_summary(self) -> Dict[str, Any]:
        """Compact fleet state for check-in text."""
        now = time.time()
        active = [(aid, s) for aid, s in self.agents.items() if now - s.last_seen < 3600]
        return {
            "active_agents": len(active),
            "agents": {
                s.name or aid[:8]: {
                    "coherence": round(s.last_coherence, 3),
                    "verdict": s.last_verdict,
                    "age_min": round((now - s.last_seen) / 60, 1),
                }
                for aid, s in active
            },
        }


# ---------------------------------------------------------------------------
# Situation Report Generator
# ---------------------------------------------------------------------------

class SitrepGenerator:
    """Template-based situation report from audit trail and fleet state."""

    def __init__(self, fleet: FleetState):
        self.fleet = fleet

    async def generate(self, hours: float = 6.0) -> str:
        """Generate a situation report covering the last N hours."""
        lines: List[str] = []
        now = datetime.now(timezone.utc)
        since = now - timedelta(hours=hours)
        lines.append(f"# Sentinel Situation Report")
        lines.append(f"Period: {since.strftime('%Y-%m-%d %H:%M')} to {now.strftime('%H:%M')} UTC ({hours:.0f}h)")
        lines.append("")

        # Fleet status
        summary = self.fleet.fleet_summary()
        lines.append(f"## Fleet Status ({summary['active_agents']} active agents)")
        for name, info in summary.get("agents", {}).items():
            verdict_icon = {"proceed": "+", "guide": "~", "pause": "!", "reject": "X"}.get(info["verdict"], "?")
            lines.append(f"  [{verdict_icon}] {name}: coherence={info['coherence']}, last seen {info['age_min']}min ago")
        lines.append("")

        # Recent events from ring buffer
        typed_events = [e for e in self.fleet.events
                        if e.get("type", "").startswith(("lifecycle_", "circuit_breaker_", "identity_", "knowledge_"))]
        if typed_events:
            lines.append(f"## Events ({len(typed_events)} total)")
            # Group by type
            by_type: Dict[str, int] = {}
            for e in typed_events:
                t = e.get("type", "unknown")
                by_type[t] = by_type.get(t, 0) + 1
            for t, count in sorted(by_type.items(), key=lambda x: -x[1]):
                lines.append(f"  {t}: {count}")
            lines.append("")

            # Timeline of notable events
            notable = [e for e in typed_events if any(k in e.get("type", "")
                       for k in ("paused", "silent", "drift", "trip", "clamped"))]
            if notable:
                lines.append("## Timeline")
                for e in notable[-20:]:  # last 20
                    ts = e.get("timestamp", "?")
                    if isinstance(ts, str) and len(ts) > 16:
                        ts = ts[11:16]  # HH:MM
                    agent = e.get("agent_id", "?")[:8]
                    etype = e.get("type", "?")
                    reason = e.get("reason", e.get("payload", {}).get("reason", ""))
                    line = f"  {ts} [{agent}] {etype}"
                    if reason:
                        line += f" — {reason}"
                    lines.append(line)
                lines.append("")

        # Query audit DB for deeper history
        # TODO: replace direct src.audit_db import with an SDK/MCP tool
        # once an audit query endpoint exists (last direct src/ import in agents/)
        try:
            from src.audit_db import query_audit_events_async
            audit_events = await query_audit_events_async(
                start_time=since.isoformat(),
                limit=50,
                order="desc",
            )
            if audit_events:
                lifecycle_events = [e for e in audit_events if "lifecycle" in e.get("event_type", "")]
                if lifecycle_events:
                    lines.append(f"## Audit Trail ({len(lifecycle_events)} lifecycle events)")
                    for e in lifecycle_events[:15]:
                        ts = e.get("timestamp", "?")
                        if isinstance(ts, str) and len(ts) > 16:
                            ts = ts[11:16]
                        agent = (e.get("agent_id") or "?")[:8]
                        etype = e.get("event_type", "?")
                        details = e.get("details", {})
                        reason = details.get("reason", "")
                        line = f"  {ts} [{agent}] {etype}"
                        if reason:
                            line += f" — {reason}"
                        lines.append(line)
                    lines.append("")
        except Exception as e:
            lines.append(f"## Audit Trail (unavailable: {e})")
            lines.append("")

        # Findings
        findings = self.fleet.analyze()
        if findings:
            lines.append(f"## Findings ({len(findings)})")
            for f in findings:
                sev = f.get("severity", "?").upper()
                lines.append(f"  [{sev}] {f['summary']}")
            lines.append("")
        else:
            lines.append("## Findings: None")
            lines.append("")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# anyio-asyncio mitigation for forced-release alarm polling
# ---------------------------------------------------------------------------

_POLL_TIMEOUT_S = 30.0


def _poll_sync_forced_release(db_url: str, last_event_ts: "datetime | None"):
    """Sync wrapper for `poll_forced_release_alarms` to be called via
    `loop.run_in_executor` (CLAUDE.md anyio mitigation pattern 2).

    Sentinel's cycle runs inside the GovernanceAgent SDK's anyio scope
    (`unitares_sdk/client.py` wraps MCP calls in `anyio.fail_after`).
    Awaiting `asyncpg.connect()` directly inside that scope can deadlock —
    the prior 30h wedge cited at lines 63-69 was this exact pathology.
    This wrapper runs the asyncpg work on a thread executor with
    `asyncio.run()`, which creates a fresh inner event loop. Per memory
    `feedback_asyncpg-loop-binding.md` ("pools/Futures bound to whichever
    loop is running when awaitable is evaluated"), asyncpg binds to that
    inner loop, runs to completion, and tears down inside `asyncio.run()` —
    never crossing the outer anyio loop.

    The inner `asyncio.wait_for(..., timeout=_POLL_TIMEOUT_S)` is
    defense-in-depth (council CONCERN — architect + reviewer convergence):
    `loop.run_in_executor` cannot cancel a running thread, so a CYCLE_TIMEOUT
    cancellation in the outer loop frees the awaiting task but the executor
    thread keeps running until asyncpg returns. With sustained DB outage
    that's up to asyncpg's default 60s connect timeout — longer than
    CYCLE_TIMEOUT (45s), so threads accumulate ~1/cycle and the default
    ThreadPoolExecutor (~12-14 threads) eventually exhausts. The inner
    wait_for forces the executor thread itself to release at 30s, well
    inside CYCLE_TIMEOUT. asyncio.TimeoutError propagates through the
    caller's existing try/except.

    Returns the same `(alarms, new_cursor)` tuple as
    `poll_forced_release_alarms`. Exceptions propagate unchanged.
    """
    from agents.sentinel.forced_release_alarm import poll_forced_release_alarms

    return asyncio.run(
        asyncio.wait_for(
            poll_forced_release_alarms(db_url=db_url, last_event_ts=last_event_ts),
            timeout=_POLL_TIMEOUT_S,
        )
    )


# ---------------------------------------------------------------------------
# Sentinel Agent
# ---------------------------------------------------------------------------

class SentinelAgent(GovernanceAgent):
    def __init__(
        self,
        mcp_url: str = GOV_MCP_URL,
        ws_url: str = GOV_WS_URL,
        label: str = "Sentinel",
        analysis_interval: int = ANALYSIS_INTERVAL,
    ):
        super().__init__(
            name=label,
            mcp_url=mcp_url,
            session_file=SESSION_FILE,
            legacy_session_file=LEGACY_SESSION_FILE,
            state_dir=STATE_FILE.parent,
            timeout=30.0,
            persistent=True,
            refuse_fresh_onboard=True,
            cycle_timeout_seconds=CYCLE_TIMEOUT,
            log_file=LOG_FILE,
            max_log_lines=MAX_LOG_LINES,
        )
        self.ws_url = ws_url
        self.analysis_interval = analysis_interval
        self.fleet = FleetState()
        self.sitrep = SitrepGenerator(self.fleet)
        self._ws_connected = False
        self._cycle_count = 0
        self._findings_total = 0

    # --- State persistence (use .sentinel_state, not the SDK default) ---

    def load_state(self) -> dict:
        """Load Sentinel's cross-cycle state."""
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def save_state(self, state: dict) -> None:
        """Save Sentinel's cross-cycle state."""
        from unitares_sdk.utils import atomic_write
        try:
            atomic_write(STATE_FILE, json.dumps(state, default=str))
        except Exception:
            pass

    # --- WebSocket consumer (local — SDK doesn't own /ws/eisv) ---

    async def ws_consumer(self):
        """Connect to WebSocket and feed events into fleet state."""
        import websockets

        while self.running:
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=None,  # loopback — TCP detects drops
                ) as ws:
                    self._ws_connected = True
                    log(f"WebSocket connected to {self.ws_url}")
                    async for message in ws:
                        if not self.running:
                            break
                        try:
                            event = json.loads(message)
                            self.fleet.ingest(event)
                        except json.JSONDecodeError:
                            pass
            except Exception as e:
                self._ws_connected = False
                if self.running:
                    log(f"WebSocket disconnected: {e}")
                    await asyncio.sleep(10)  # reconnect delay

    # --- Analysis cycle (SDK pattern: return CycleResult) ---

    async def run_cycle(self, client: GovernanceClient = None) -> CycleResult | None:
        """Run one analysis cycle: detect anomalies, build check-in summary.

        Phase A advisory lease wraps the cycle. Sentinel runs continuously
        (analysis_interval=300s by default), so two concurrent Sentinel
        instances on the same Mac would surface here as held_by_other.
        Outcome does NOT gate execution per RFC v0.5 §6.1.
        """
        from unitares_sdk.lease_plane.advisory import lease_advisory_scope, new_holder_uuid

        # Migrated from "sentinel:cycle" → "resident:/sentinel_cycle" per RFC v0.8 §7.2.1.
        with lease_advisory_scope(
            surface_id="resident:/sentinel_cycle",
            holder_agent_uuid=new_holder_uuid(),
            ttl_s=300,
            intent="sentinel analysis cycle",
            audit_session=getattr(self, "client_session_id", None),
        ):
            return await self._run_cycle_inner()

    async def _run_cycle_inner(self) -> CycleResult | None:
        self._cycle_count += 1
        # Poll forced-release alarms (RFC v0.8 §7.10 + §7.11.5).
        # Wrapped in try/except so DB unreachable doesn't break the cycle.
        await self._emit_forced_release_alarms()
        findings = self.fleet.analyze(self_agent_id=self.agent_uuid or "")
        fleet = self.fleet.fleet_summary()

        # Separate self-observations from fleet findings
        fleet_findings = [f for f in findings if not f.get("self_observation")]
        self_findings = [f for f in findings if f.get("self_observation")]

        # Build check-in text
        parts = [f"Cycle {self._cycle_count}"]
        parts.append(f"Fleet: {fleet['active_agents']} agents")
        parts.append(f"WS: {'connected' if self._ws_connected else 'DISCONNECTED'}")

        if fleet_findings:
            self._findings_total += len(fleet_findings)
            for f in fleet_findings:
                vcls = f.get("violation_class", "")
                cls_tag = f"[{vcls}] " if vcls else ""
                parts.append(f"[{f['severity'].upper()}] {cls_tag}{f['summary']}")
                log(f"FINDING: [{f['severity']}] {cls_tag}{f['summary']}")
                if f["severity"] == "high":
                    notify("Sentinel", f["summary"])

                # Emit to governance event stream (Phase 1 of findings pipeline).
                # Fingerprint keys on finding type + violation class + agent so
                # the same fleet condition re-detected next cycle deduplicates.
                fp = compute_fingerprint([
                    "sentinel",
                    f.get("type", ""),
                    f.get("violation_class", ""),
                    self.agent_uuid or "",
                ])
                post_finding(
                    event_type="sentinel_finding",
                    severity=f["severity"],
                    message=f["summary"],
                    agent_id=self.agent_uuid or "sentinel",
                    agent_name="Sentinel",
                    fingerprint=fp,
                    extra={
                        "violation_class": vcls,
                        "finding_type": f.get("type", ""),
                    },
                )

        # Self-observations: log but don't count toward complexity
        for f in self_findings:
            parts.append(f"[SELF] {f['summary']}")
            log(f"SELF-OBS: {f['summary']}")

        issues = len([f for f in fleet_findings if f["severity"] == "high"])
        complexity = min(1.0, 0.2 + len(fleet_findings) * 0.15 + (0.1 if not self._ws_connected else 0))
        confidence = max(0.4, 0.85 - issues * 0.1 - (0.15 if not self._ws_connected else 0))

        summary = " | ".join(parts)

        # Findings already emit to the dashboard event stream via
        # post_finding() above (with fingerprint dedup). They previously also
        # wrote to the KG via leave_note — that path is removed because the
        # entries had no archival value (10-min fleet snapshots) and the
        # Vigil-Sentinel coordination contract that read them is broken at the
 # type-name level .
        return CycleResult(
            summary=f"Sentinel analysis: {summary}",
            complexity=complexity,
            confidence=confidence,
            response_mode="compact",
        )

    async def _emit_forced_release_alarms(self) -> None:
        """Poll lease_plane_events for forced-release alarms; emit findings.

        State: stores last_event_ts as ISO8601 string under
        `forced_release_alarm.last_event_ts` so successive cycles don't
        re-emit alarms for already-seen events.

        DB URL: GOVERNANCE_DATABASE_URL env var, default to localhost governance.
        Failures are logged and swallowed — alarm polling MUST NOT break the
        Sentinel cycle.

        anyio-asyncio mitigation (CLAUDE.md "Known Issue"): the
        `poll_forced_release_alarms` call does `asyncpg.connect()` + multiple
        `await conn.fetch(...)`. Sentinel's `run_cycle` runs inside the
        GovernanceAgent SDK's `run_once`, which is anyio-based. `await`-ing
        asyncpg directly from inside the anyio task group can deadlock — the
        prior wedge cited at line 67 (~30h) was this exact pathology.
        Mitigation: pattern 2 from CLAUDE.md — push the asyncpg call to a
        thread executor via `_poll_sync_forced_release`, which runs
        `asyncio.run(...)` in the executor thread. asyncpg binds to that
        fresh inner loop (per `feedback_asyncpg-loop-binding.md` —
        loop-binding happens at create time, so a new loop scoped to the
        executor call is safe), runs to completion, tears down. The outer
        anyio task group is unblocked throughout.
        """
        db_url = os.environ.get(
            "GOVERNANCE_DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/governance",
        )
        state = self.load_state()
        cursor_str = state.get("forced_release_alarm", {}).get("last_event_ts")
        cursor = datetime.fromisoformat(cursor_str) if cursor_str else None

        loop = asyncio.get_running_loop()
        try:
            alarms, new_cursor = await loop.run_in_executor(
                None, _poll_sync_forced_release, db_url, cursor,
            )
        except Exception as e:
            log(f"forced-release alarm poll failed: {e}")
            return

        conflict_surface_kinds: list[str] = []
        for alarm in alarms:
            severity_tag = alarm.severity.upper()
            log(f"FORCED-RELEASE ALARM: [{severity_tag}] {alarm.summary}")
            if alarm.severity == "high":
                notify("Sentinel forced-release", alarm.summary)
            post_finding(
                event_type="sentinel_alarm_finding",
                severity=alarm.severity,
                message=alarm.summary,
                agent_id=self.agent_uuid or "sentinel",
                agent_name="Sentinel",
                fingerprint=alarm.fingerprint,
                extra={"alarm_kind": alarm.kind, **alarm.extra},
            )
            if alarm.kind == "conflict_batch":
                kind = alarm.extra.get("surface_kind")
                if isinstance(kind, str):
                    conflict_surface_kinds.append(kind)

        if new_cursor is not None and new_cursor != cursor:
            state.setdefault("forced_release_alarm", {})["last_event_ts"] = (
                new_cursor.isoformat()
            )
            self.save_state(state)

        await self._emit_phase_b_transitions(conflict_surface_kinds, db_url)

    async def _emit_phase_b_transitions(
        self, surface_kinds: list[str], db_url: str
    ) -> None:
        """Run the §6.1 Phase B promotion evaluator for each surface_kind that
        had a conflict-batch alarm this cycle, and emit a transition finding
        only when one or more criteria flip status (or overall promotable
        flips) vs. the last recorded verdict.

        Failures are logged and swallowed — promotion-evaluator failure MUST
        NOT break the Sentinel cycle. Subprocess + sync psycopg2 inside the
        evaluator means no anyio loop binding hazard; we still push to a
        thread executor for consistency with the surrounding alarm path.
        """
        if not surface_kinds:
            return
        loop = asyncio.get_running_loop()
        try:
            transitions = await loop.run_in_executor(
                None, lambda: detect_phase_b_transitions(surface_kinds, db_url=db_url),
            )
        except PhaseBEvaluatorError as e:
            log(f"phase-B evaluator failed: {e}")
            return
        except Exception as e:
            log(f"phase-B transition detection failed: {e}")
            return

        for transition in transitions:
            severity = "high" if transition.promotable_now else "medium"
            log(f"PHASE-B TRANSITION: [{severity.upper()}] {transition.summary}")
            post_finding(
                event_type="lease_plane_phase_b_transition",
                severity=severity,
                message=transition.summary,
                agent_id=self.agent_uuid or "sentinel",
                agent_name="Sentinel",
                fingerprint=compute_fingerprint([
                    "phase_b",
                    transition.surface_kind,
                    transition.promotable_now,
                    *(f"{c.number}:{c.current_status}" for c in transition.criteria),
                ]),
                extra={
                    "surface_kind": transition.surface_kind,
                    "promotable_now": transition.promotable_now,
                    "promotable_before": transition.promotable_before,
                    "criteria_changed": [
                        {
                            "number": c.number,
                            "name": c.name,
                            "previous": c.previous_status,
                            "current": c.current_status,
                        }
                        for c in transition.criteria
                    ],
                },
            )

    async def on_after_checkin(
        self, client: GovernanceClient, checkin_result: CheckinResult, cycle_result: CycleResult,
    ) -> None:
        """Log one-line EISV summary after each check-in."""
        if not checkin_result.success:
            log(f"CHECK-IN FAILED | {cycle_result.summary}")
            return
        metrics = checkin_result.metrics or {}
        try:
            eisv = (
                f"E={float(metrics['E']):.3f} "
                f"I={float(metrics['I']):.3f} "
                f"S={float(metrics['S']):.3f} "
                f"V={float(metrics['V']):.3f}"
            )
        except (KeyError, TypeError, ValueError):
            eisv = "EISV=?"
        log(f"{checkin_result.verdict} | {eisv} | {cycle_result.summary}")

    # --- Main loops ---

    async def run_continuous(self):
        """Run Sentinel continuously: WebSocket consumer + periodic analysis."""
        log("=== Sentinel starting ===")

        def signal_handler(signum, frame):
            log(f"Signal {signum}, shutting down")
            self.running = False

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        # Start WebSocket consumer in background
        ws_task = asyncio.create_task(self.ws_consumer())

        try:
            # Initial check-in — catch exceptions so a transient governance
            # outage at startup doesn't kill the process and thrash launchd.
            # Matches the periodic loop's error handling below.
            await asyncio.sleep(5)  # let WS connect
            try:
                await self.run_once()
            except asyncio.TimeoutError:
                log(f"Analysis cycle exceeded {CYCLE_TIMEOUT}s — skipping")
            except Exception as e:
                log(f"Initial analysis cycle error: {e}")

            # Periodic analysis
            while self.running:
                await asyncio.sleep(self.analysis_interval)
                if self.running:
                    try:
                        await self.run_once()
                    except asyncio.TimeoutError:
                        log(f"Analysis cycle exceeded {CYCLE_TIMEOUT}s — skipping")
                    except Exception as e:
                        log(f"Analysis cycle error: {e}")
        finally:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass

        log("=== Sentinel stopped ===")

    async def run_once_mode(self):
        """Run one analysis cycle and exit."""
        log("--- Sentinel single cycle ---")
        # Brief WS connection to gather some state
        ws_task = asyncio.create_task(self.ws_consumer())
        try:
            await asyncio.sleep(10)  # collect events for 10s
            try:
                await self.run_once()
                result = f"cycle {self._cycle_count} complete"
            except asyncio.TimeoutError:
                log(f"Analysis cycle exceeded {CYCLE_TIMEOUT}s — skipping")
                result = f"TIMEOUT after {CYCLE_TIMEOUT}s"
            self.running = False
        finally:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
        log(f"Result: {result}")

    async def run_sitrep(self, hours: float = 6.0):
        """Generate and print a situation report."""
        # Brief WS connection
        ws_task = asyncio.create_task(self.ws_consumer())
        try:
            await asyncio.sleep(5)
            report = await self.sitrep.generate(hours)
            self.running = False
        finally:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass
        print(report)
        log(f"Sitrep generated ({hours}h window)")


# ---------------------------------------------------------------------------
# Finding adjudication → exogenous outcome (Stage-0 bridge half b, option A)
# ---------------------------------------------------------------------------

async def adjudicate_finding(
    agent: "SentinelAgent",
    client: GovernanceClient,
    status: str,
    fingerprint: str,
    reason: str | None = None,
) -> dict:
    """Record an operator's adjudication of a Sentinel finding as an external-truth
    ``outcome_event`` attributed to Sentinel's own (baselined) UUID.

    The operator verdict is ground truth from outside the loop, so the outcome is
    ``external_signal``; the handler auto-snapshots Sentinel's EISV by agent_id,
    giving the residual-vs-Φ falsifiability test a second baselined-resident
    channel beyond Watcher (docs/proposals/eisv-stage0-bridge-b-label-routing.md).
    Option A: the outcome_event is the durable adjudication record — backlog /
    audit.events status mutation is a deliberate follow-up.
    """
    from agents.common.resolution_outcome import build_resolution_outcome_args

    await agent._ensure_identity(client)
    if not agent.agent_uuid:
        raise RuntimeError("Sentinel identity unresolved — refusing to attribute outcome")
    args = build_resolution_outcome_args(
        "sentinel_finding", status, fingerprint, agent.agent_uuid, reason
    )
    await client.call_tool("outcome_event", args)
    log(f"recorded external-truth outcome ({status}) for sentinel finding {fingerprint}")
    return args


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Sentinel — The Independent Observer")
    parser.add_argument("--once", action="store_true", help="Run one analysis cycle and exit")
    parser.add_argument("--sitrep", action="store_true", help="Generate situation report and exit")
    parser.add_argument("--hours", type=float, default=6.0, help="Sitrep window (hours)")
    parser.add_argument("--resolve", metavar="FINGERPRINT", help="Adjudicate a finding as CONFIRMED (Sentinel was right) → external-truth outcome")
    parser.add_argument("--dismiss", metavar="FINGERPRINT", help="Adjudicate a finding as DISMISSED → external-truth outcome (use --reason fp for a false positive)")
    parser.add_argument("--reason", default=None, help="Dismissal reason; 'fp' marks a false positive (the only 'bad' outcome for Sentinel)")
    parser.add_argument("--url", default=GOV_MCP_URL, help="MCP URL")
    parser.add_argument("--ws-url", default=GOV_WS_URL, help="WebSocket URL")
    parser.add_argument("--interval", type=int, default=ANALYSIS_INTERVAL, help="Analysis interval (seconds)")
    args = parser.parse_args()

    agent = SentinelAgent(
        mcp_url=args.url,
        ws_url=args.ws_url,
        analysis_interval=args.interval,
    )

    if args.resolve or args.dismiss:
        status = "confirmed" if args.resolve else "dismissed"
        fingerprint = args.resolve or args.dismiss
        async with GovernanceClient(
            mcp_url=agent.mcp_url,
            timeout=agent.timeout,
            connect_timeout=agent.connect_timeout,
            connect_retries=agent.connect_retries,
        ) as client:
            await adjudicate_finding(agent, client, status, fingerprint, args.reason)
        return

    if args.sitrep:
        await agent.run_sitrep(args.hours)
    elif args.once:
        await agent.run_once_mode()
    else:
        await agent.run_continuous()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("Interrupted")
        sys.exit(0)

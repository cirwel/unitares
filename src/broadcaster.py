from starlette.websockets import WebSocket
import logging
import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Activity history entry: (timestamp_epoch, verdict_action)
ACTIVITY_HISTORY_MAX = 720  # ~1 hour at 5s intervals, generous buffer

# Event history for sentinel consumption (ring buffer, ~6h at moderate activity)
EVENT_HISTORY_MAX = 2000


# Verdicts that are neither a nudge nor a hard stop. Anything else recorded
# (pause, reject, risk_pause, cirs_block, a future hard-stop action) is a
# produced hard verdict — the open-ended rule governance.pause.7d uses, so a
# new action folds in rather than being counted as a proceed.
_PROCEED_ACTIONS = frozenset({"proceed", "approve", "continue"})


def verdict_of(event: dict) -> str:
    """The verdict an eisv_update carries: sub_action when present, else action."""
    decision = event.get("decision") if isinstance(event, dict) else None
    if not isinstance(decision, dict):
        return "proceed"
    return decision.get("sub_action") or decision.get("action") or "proceed"


def verdict_bucket(action: Optional[str]) -> str:
    if action == "guide":
        return "guide"
    if not action or action in _PROCEED_ACTIONS:
        return "proceed"
    return "pause"


class EISVBroadcaster:
    def __init__(self):
        self.connections: list[WebSocket] = []
        self.last_update: dict = None
        self._lock = asyncio.Lock()
        self.activity_history: deque = deque(maxlen=ACTIVITY_HISTORY_MAX)
        self.event_history: deque = deque(maxlen=EVENT_HISTORY_MAX)
        self.started_at: float = time.time()

    async def connect(self, websocket: WebSocket, subprotocol: str | None = None):
        await websocket.accept(subprotocol=subprotocol)
        async with self._lock:
            self.connections.append(websocket)
        logger.info(f"[WS] Dashboard client connected ({len(self.connections)} active)")

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self.connections:
                self.connections.remove(websocket)
        logger.info(f"[WS] Dashboard client disconnected")

    def activity_coverage_start(self, window_minutes=60) -> float:
        """Earliest time the activity buckets can be trusted to be complete.

        The history is in memory: it starts empty at process start and, once
        full, drops its oldest entries. A count over a window reaching past
        either point undercounts, so a reader that shows a total ("N check-ins
        in the last hour") needs to know how much of the window is covered.
        """
        start = self.started_at
        if len(self.activity_history) == self.activity_history.maxlen:
            start = max(start, self.activity_history[0][0])
        return max(start, time.time() - window_minutes * 60)

    def activity_totals(self, window_minutes=60) -> dict:
        """Exact verdict counts over [now - window, now].

        The sparkline buckets are aligned to bucket boundaries, so their span
        starts up to one bucket inside the window and summing them can drop
        the window's first few minutes. A total uses this instead.
        """
        cutoff = time.time() - window_minutes * 60
        totals = {"proceed": 0, "guide": 0, "pause": 0}
        for ts, action in self.activity_history:
            if ts >= cutoff:
                totals[verdict_bucket(action)] += 1
        return totals

    def get_activity_buckets(self, window_minutes=60, bucket_minutes=5):
        """Return check-in counts grouped by 5-min bucket + verdict for sparkline."""
        now = time.time()
        cutoff = now - (window_minutes * 60)
        bucket_size = bucket_minutes * 60

        # Initialize buckets covering the window
        num_buckets = window_minutes // bucket_minutes
        # Align to bucket boundaries
        current_bucket_start = int(now // bucket_size) * bucket_size
        bucket_starts = [current_bucket_start - (i * bucket_size) for i in range(num_buckets - 1, -1, -1)]

        buckets = []
        for bs in bucket_starts:
            buckets.append({
                "ts": bs,
                "proceed": 0,
                "guide": 0,
                "pause": 0,
            })

        # Fill from history
        for ts, action in self.activity_history:
            if ts < cutoff:
                continue
            bucket_idx = int((ts - bucket_starts[0]) // bucket_size)
            if 0 <= bucket_idx < len(buckets):
                buckets[bucket_idx][verdict_bucket(action)] += 1

        return buckets

    async def broadcast(self, data: dict):
        self.last_update = data

        # Track activity for sparkline. The verdict is `sub_action` when present
        # (a guided check-in is action="proceed", sub_action="guide"), else
        # `action` — the same rule record_agent_state persists by
        # (src/mcp_handlers/updates/phases.py). Reading `action` alone counted
        # every guide as a proceed.
        self.activity_history.append((time.time(), verdict_of(data)))

        # Store in event history for sentinel/query access
        self.event_history.append(data)

        await self._send_to_clients(data)

    async def broadcast_event(
        self,
        event_type: str,
        agent_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ):
        """Broadcast a typed governance event.

        Event types:
            lifecycle_paused, lifecycle_resumed, lifecycle_archived,
            lifecycle_created, lifecycle_loop_detected, lifecycle_stuck_detected,
            identity_drift, identity_assurance_change,
            knowledge_write, knowledge_read, knowledge_confidence_clamped,
            circuit_breaker_trip, circuit_breaker_reset
        """
        event = {
            "type": event_type,
            "agent_id": agent_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            **(payload or {}),
        }
        self.event_history.append(event)
        await self._send_to_clients(event)

        # Fire-and-forget persist to audit.events so dashboard survives restarts
        try:
            from src.background_tasks import create_tracked_task
            create_tracked_task(self._persist_event(event), name="persist_event")
        except RuntimeError:
            pass  # No event loop (tests, CLI)

    @staticmethod
    async def _persist_event(event: dict):
        """Write a governance event to audit.events (best-effort)."""
        try:
            from src.audit_db import append_audit_event_async
            await append_audit_event_async({
                "timestamp": event.get("timestamp"),
                "agent_id": event.get("agent_id"),
                "event_type": event.get("type", "governance_event"),
                "confidence": 1.0,
                "details": {k: v for k, v in event.items()
                            if k not in ("timestamp", "agent_id")},
            })
        except Exception as e:
            logger.debug(f"Governance event persist failed (non-fatal): {e}")

    def get_recent_events(
        self,
        event_type: Optional[str] = None,
        agent_id: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 100,
    ) -> list[dict]:
        """Query recent events from the ring buffer.

        Args:
            event_type: Filter by event type prefix (e.g. "lifecycle" matches all lifecycle_* events).
            agent_id: Filter by agent UUID.
            since: Unix timestamp — only return events after this time.
            limit: Max events to return.
        """
        results = []
        for event in reversed(self.event_history):
            if len(results) >= limit:
                break
            if event_type:
                evt = event.get("type", "")
                if not evt.startswith(event_type):
                    continue
            if agent_id and event.get("agent_id") != agent_id:
                continue
            if since:
                ts = event.get("timestamp", "")
                if ts:
                    try:
                        evt_time = datetime.fromisoformat(ts).timestamp()
                        if evt_time < since:
                            break  # ring buffer is append-order, so we can stop
                    except (ValueError, TypeError):
                        pass
            results.append(event)
        results.reverse()
        return results

    # Per-client send timeout. Without this, a single slow or hung
    # WebSocket client blocks broadcasts to *every* client, so live
    # dashboard tabs never see new EISV updates (chart stops rendering).
    _SEND_TIMEOUT_SECONDS = 2.0

    async def _send_to_clients(self, data: dict):
        """Send data to all connected WebSocket clients.

        Sends run in parallel, each bounded by a 2s timeout. Clients that
        error or stall past the timeout are culled so a stuck consumer
        can't hold up the broadcast for healthy ones.
        """
        async with self._lock:
            if not self.connections:
                return
            conns = list(self.connections)

        async def _send_one(ws):
            try:
                await asyncio.wait_for(
                    ws.send_json(data),
                    timeout=self._SEND_TIMEOUT_SECONDS,
                )
                return None
            except Exception as exc:
                return exc

        results = await asyncio.gather(*(_send_one(ws) for ws in conns))
        dead = [ws for ws, result in zip(conns, results) if result is not None]

        if dead:
            async with self._lock:
                for ws in dead:
                    if ws in self.connections:
                        self.connections.remove(ws)

            async def _close_one(ws):
                try:
                    await asyncio.wait_for(
                        ws.close(),
                        timeout=self._SEND_TIMEOUT_SECONDS,
                    )
                except Exception:
                    pass

            await asyncio.gather(*(_close_one(ws) for ws in dead))
            logger.info(f"[WS] Removed {len(dead)} dead/slow connections")

broadcaster_instance = EISVBroadcaster()

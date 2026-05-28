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


class EISVBroadcaster:
    def __init__(self):
        self.connections: list[WebSocket] = []
        self.last_update: dict = None
        self._lock = asyncio.Lock()
        self.activity_history: deque = deque(maxlen=ACTIVITY_HISTORY_MAX)
        self.event_history: deque = deque(maxlen=EVENT_HISTORY_MAX)

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        async with self._lock:
            self.connections.append(websocket)
        logger.info(f"[WS] Dashboard client connected ({len(self.connections)} active)")

    async def disconnect(self, websocket: WebSocket):
        async with self._lock:
            if websocket in self.connections:
                self.connections.remove(websocket)
        logger.info(f"[WS] Dashboard client disconnected")

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
                if action in ("guide",):
                    buckets[bucket_idx]["guide"] += 1
                elif action in ("pause", "reject"):
                    buckets[bucket_idx]["pause"] += 1
                else:
                    buckets[bucket_idx]["proceed"] += 1

        return buckets

    async def broadcast(self, data: dict):
        self.last_update = data

        # Track activity for sparkline
        decision = data.get("decision", {})
        action = decision.get("action", "proceed") if isinstance(decision, dict) else "proceed"
        self.activity_history.append((time.time(), action))

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

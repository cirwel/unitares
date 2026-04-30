"""HeartbeatEvaluator — thin wrapper over the existing silence/cadence
semantics. The probe uses this so its candidate gate is consistent with
how the rest of the server defines a resident as alive.

Critical-silence threshold is 3x expected cadence. Below that we consider
the resident alive even if the most recent update is slightly stale.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

# Match the existing convention: alive if last_update within 3x cadence.
ALIVE_CADENCE_MULTIPLIER = 3


class _MetadataStore(Protocol):
    async def get(self, agent_uuid: str) -> dict | None: ...


@dataclass
class HeartbeatStatus:
    alive: bool
    last_update: datetime | None
    expected_cadence_s: int | None
    in_critical_silence: bool
    eval_error: str | None = None

    def to_jsonable(self) -> dict[str, Any]:
        d = asdict(self)
        if d["last_update"] is not None:
            d["last_update"] = d["last_update"].isoformat()
        return d


class HeartbeatEvaluator:
    def __init__(
        self,
        store: _MetadataStore,
        _now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._store = store
        self._now = _now

    async def evaluate(
        self,
        agent_uuid: str,
        *,
        cadence_override_s: int | None = None,
    ) -> HeartbeatStatus:
        try:
            row = await self._store.get(agent_uuid)
        except Exception as e:
            return HeartbeatStatus(
                alive=False, last_update=None, expected_cadence_s=None,
                in_critical_silence=False, eval_error=f"{type(e).__name__}: {e}",
            )
        if row is None:
            return HeartbeatStatus(
                alive=False, last_update=None, expected_cadence_s=None,
                in_critical_silence=False,
            )
        last = row.get("last_update")
        cadence = (
            cadence_override_s
            or int(row.get("expected_cadence_s") or 0)
            or None
        )
        if last is None or cadence is None:
            return HeartbeatStatus(
                alive=False, last_update=last, expected_cadence_s=cadence,
                in_critical_silence=False,
            )
        elapsed = self._now() - last
        critical_threshold = timedelta(seconds=cadence * ALIVE_CADENCE_MULTIPLIER)
        in_critical = elapsed > critical_threshold
        return HeartbeatStatus(
            alive=not in_critical, last_update=last, expected_cadence_s=cadence,
            in_critical_silence=in_critical,
        )

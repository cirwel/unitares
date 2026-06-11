"""Resident-progress probe orchestrator. See plan task 8 + spec."""
from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.resident_progress.heartbeat import HeartbeatStatus
from src.resident_progress.registry import (
    RESIDENT_PROGRESS_REGISTRY,
    resolve_resident_uuid,
)
from src.resident_progress.snapshot_writer import SnapshotRow

logger = logging.getLogger(__name__)

STARTUP_GRACE_TICKS = 2


class ProgressFlatProbe:
    def __init__(self, sources_by_name, heartbeat_evaluator, writer,
                 audit_emitter, _now_tick: int = 0) -> None:
        self._sources = sources_by_name
        self._heartbeat = heartbeat_evaluator
        self._writer = writer
        self._audit = audit_emitter
        self._tick_count = _now_tick

    async def tick(self) -> None:
        self._tick_count += 1
        tick_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        # Step 2: resolve registry
        resolved: dict[str, str] = {}
        unresolved_rows: list[SnapshotRow] = []
        for label, cfg in RESIDENT_PROGRESS_REGISTRY.items():
            agent_uuid = resolve_resident_uuid(label)
            if agent_uuid is None:
                reason = ("startup_unresolved_label"
                          if self._tick_count <= STARTUP_GRACE_TICKS
                          else "unresolved_label")
                unresolved_rows.append(SnapshotRow(
                    probe_tick_id=tick_id, ticked_at=now,
                    resident_label=label, resident_uuid=None,
                    source=cfg.source, metric_value=None,
                    window_seconds=int(cfg.window.total_seconds()),
                    threshold=cfg.threshold, metric_below_threshold=None,
                    heartbeat_alive=False, candidate=False,
                    suppressed_reason=reason, error_details=None,
                    liveness_inputs=None, loop_detector_state=None,
                ))
            else:
                resolved[label] = agent_uuid

        # Step 3: group sources, fetch in parallel with isolated errors
        groups: dict[tuple[str, int], list[str]] = defaultdict(list)
        for label, agent_uuid in resolved.items():
            cfg = RESIDENT_PROGRESS_REGISTRY[label]
            groups[(cfg.source, int(cfg.window.total_seconds()))].append(agent_uuid)

        async def _call_source(name, window_s, uuids):
            try:
                out = await self._sources[name].fetch(uuids, timedelta(seconds=window_s))
                return name, out, None
            except Exception as e:
                return name, None, f"{type(e).__name__}: {e}"

        results = await asyncio.gather(*[
            _call_source(n, w, u) for (n, w), u in groups.items()
        ])
        source_outputs = {n: out for n, out, err in results if err is None}
        source_errors = {n: err for n, out, err in results if err is not None}

        # Step 4: heartbeat in parallel. Pass per-resident cadence from
        # the registry so non-continuous residents (Vigil 30min, Steward
        # 5min, Chronicler daily) aren't all judged against a 60s default.
        # Residents whose registry cadence is None are event-driven
        # (Watcher) — heartbeat-liveness is the wrong abstraction, so we
        # synthesize an "alive=True, event_driven" status and skip the
        # staleness gate entirely. Better than encoding "n/a" as "very
        # slow timeout".
        async def _hb(label, agent_uuid):
            cfg = RESIDENT_PROGRESS_REGISTRY[label]
            if cfg.expected_cadence_s is None:
                return agent_uuid, HeartbeatStatus(
                    alive=True, last_update=None, expected_cadence_s=None,
                    in_critical_silence=False,
                )
            return agent_uuid, await self._heartbeat.evaluate(
                agent_uuid, cadence_override_s=cfg.expected_cadence_s,
            )

        hb_pairs = await asyncio.gather(*[
            _hb(label, agent_uuid) for label, agent_uuid in resolved.items()
        ])
        hb_by_uuid = dict(hb_pairs)

        # Step 5: compose resident rows
        resident_rows: list[SnapshotRow] = []
        for label, agent_uuid in resolved.items():
            cfg = RESIDENT_PROGRESS_REGISTRY[label]
            window_s = int(cfg.window.total_seconds())
            hb = hb_by_uuid[agent_uuid]
            if cfg.source in source_errors:
                row = SnapshotRow(
                    probe_tick_id=tick_id, ticked_at=now,
                    resident_label=label, resident_uuid=agent_uuid,
                    source=cfg.source, metric_value=None,
                    window_seconds=window_s, threshold=cfg.threshold,
                    metric_below_threshold=None, heartbeat_alive=hb.alive,
                    candidate=False, suppressed_reason="source_error",
                    error_details={"source": cfg.source,
                                   "error": source_errors[cfg.source]},
                    liveness_inputs=hb.to_jsonable(),
                    loop_detector_state=None,
                )
            elif hb.eval_error is not None:
                row = SnapshotRow(
                    probe_tick_id=tick_id, ticked_at=now,
                    resident_label=label, resident_uuid=agent_uuid,
                    source=cfg.source, metric_value=None,
                    window_seconds=window_s, threshold=cfg.threshold,
                    metric_below_threshold=None, heartbeat_alive=False,
                    candidate=False, suppressed_reason="heartbeat_eval_error",
                    error_details={"heartbeat_error": hb.eval_error},
                    liveness_inputs=hb.to_jsonable(),
                    loop_detector_state=None,
                )
            else:
                metric = source_outputs[cfg.source].get(agent_uuid, 0)
                below = metric < cfg.threshold
                # Distinguish "never checked in" from "went silent". A fresh
                # resident with last_update=None looks identical to a long
                # outage on the dashboard otherwise; operators investigate
                # phantom failures of brand-new instances.
                never_seen = (
                    not hb.alive
                    and hb.last_update is None
                    and hb.eval_error is None
                    and cfg.expected_cadence_s is not None
                )
                if never_seen:
                    suppressed = "never_seen"
                    candidate = False
                elif not hb.alive and below:
                    suppressed = "heartbeat_not_alive"
                    candidate = False
                else:
                    suppressed = None
                    candidate = below and hb.alive
                row = SnapshotRow(
                    probe_tick_id=tick_id, ticked_at=now,
                    resident_label=label, resident_uuid=agent_uuid,
                    source=cfg.source, metric_value=metric,
                    window_seconds=window_s, threshold=cfg.threshold,
                    metric_below_threshold=below, heartbeat_alive=hb.alive,
                    candidate=candidate, suppressed_reason=suppressed,
                    error_details=None, liveness_inputs=hb.to_jsonable(),
                    loop_detector_state=None,
                )
            resident_rows.append(row)

        # Step 6: persist resident + unresolved rows
        all_rows = unresolved_rows + resident_rows
        try:
            await self._writer.write(all_rows)
        except Exception as e:
            logger.warning("[PROGRESS_FLAT] resident-row write failed: %s", e)
            return

        # Step 7: dogfood row (non-fatal write)
        dogfood = SnapshotRow(
            probe_tick_id=tick_id, ticked_at=datetime.now(timezone.utc),
            resident_label="progress_flat_probe", resident_uuid=None,
            source="probe_self", metric_value=len(all_rows),
            window_seconds=None, threshold=None,
            metric_below_threshold=False, heartbeat_alive=True,
            candidate=False, suppressed_reason=None,
            error_details=None, liveness_inputs=None,
            loop_detector_state=None,
        )
        try:
            await self._writer.write([dogfood])
        except Exception as e:
            logger.warning(
                "[PROGRESS_FLAT] dogfood-row write failed (non-fatal): %s", e,
            )

        # Step 8: emit candidate events
        for r in resident_rows:
            if r.candidate:
                try:
                    await self._audit.emit(
                        event_type="progress_flat_candidate", severity="low",
                        payload={
                            "resident_label": r.resident_label,
                            "resident_uuid": r.resident_uuid,
                            "source": r.source, "metric_value": r.metric_value,
                            "threshold": r.threshold,
                            "window_seconds": r.window_seconds,
                            "probe_tick_id": str(r.probe_tick_id),
                        },
                    )
                except Exception as e:
                    logger.warning(
                        "[PROGRESS_FLAT] candidate audit emit failed: %s", e,
                    )


def _verify_unique_source_names() -> None:
    seen = set()
    for cfg in RESIDENT_PROGRESS_REGISTRY.values():
        if cfg.source in seen:
            raise RuntimeError(
                f"resident-progress registry has duplicate source name "
                f"'{cfg.source}' — orchestrator's source_outputs dict cannot "
                f"distinguish them. If you intentionally share a source across "
                f"residents, change source_outputs to key on (name, window) tuple."
            )
        seen.add(cfg.source)


_verify_unique_source_names()

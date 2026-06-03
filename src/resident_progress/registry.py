"""Label-keyed config for the resident-progress probe.

Resident UUIDs are NOT stored here. They resolve at tick time from
filesystem anchors, so a resident that re-onboards or rotates UUID is
picked up automatically.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

ANCHOR_DIR = Path.home() / ".unitares" / "anchors"


@dataclass(frozen=True)
class ResidentConfig:
    source: str       # source.name as defined in sources.py
    metric: str       # human-readable metric label, recorded on snapshot row
    window: timedelta
    threshold: int    # candidate fires when measured metric is strictly less than threshold
    # Heartbeat cadence. Drives critical-silence detection
    # (alive iff last_update within 3x cadence). Per-resident because
    # residents have very different natural cadences: Sentinel is a
    # 60s loop, Steward syncs every 5min, Vigil cycles every 30min,
    # Chronicler runs daily. None is reserved for event-driven
    # residents (Watcher) where heartbeat-liveness is the wrong
    # abstraction — the probe skips the staleness gate for those.
    expected_cadence_s: int | None

    def __post_init__(self) -> None:
        if self.expected_cadence_s is not None and self.expected_cadence_s <= 0:
            raise ValueError(
                f"expected_cadence_s must be positive or None, got "
                f"{self.expected_cadence_s!r}"
            )


RESIDENT_PROGRESS_REGISTRY: dict[str, ResidentConfig] = {
    "vigil":      ResidentConfig("kg_writes",        "rows_written", timedelta(minutes=60),  1, expected_cadence_s=1800),
    "watcher":    ResidentConfig("watcher_findings", "rows_any",     timedelta(hours=6),     1, expected_cadence_s=None),
    "steward":    ResidentConfig("eisv_sync_rows",   "rows_written", timedelta(minutes=30),  1, expected_cadence_s=300),
    "chronicler": ResidentConfig("metrics_series",   "rows_written", timedelta(hours=26),    1, expected_cadence_s=86400),
    # Sentinel migrated to the Elixir/BEAM runtime (com.unitares.sentinel-beam),
    # which does not post record_progress_pulse — so the sentinel_pulse source
    # read a permanent 0 and the probe false-flagged sentinel as "flat-candidate"
    # against a 60s expected cadence. Retired from the progress probe: BEAM
    # sentinel liveness shows via the resident strip + its launchd KeepAlive, and
    # its findings still feed Vigil. To re-cover it here, re-add an entry and have
    # the BEAM sentinel emit record_progress_pulse (the source is still wired).
}


def is_event_driven_label(label: str | None) -> bool:
    """True iff `label` is a known resident registered with no scheduled cadence.

    Event-driven residents (Watcher) fire on external triggers — there is no
    heartbeat between events, so liveness/silence semantics don't apply. Surfaces
    that decide "is this agent inactive?" must consult this rather than infer
    from silence-since-last-update; otherwise a quiet-but-healthy event-driven
    resident gets badged as inactive between firings.
    """
    if not label:
        return False
    cfg = RESIDENT_PROGRESS_REGISTRY.get(label.lower())
    return cfg is not None and cfg.expected_cadence_s is None


def resolve_resident_uuid(label: str) -> str | None:
    """Read ~/.unitares/anchors/<label>.json and return the agent_uuid, or None."""
    path = ANCHOR_DIR / f"{label}.json"
    try:
        with path.open() as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("anchor %s unreadable: %s", path, e)
        return None
    uuid = doc.get("agent_uuid")
    return uuid if isinstance(uuid, str) and uuid else None

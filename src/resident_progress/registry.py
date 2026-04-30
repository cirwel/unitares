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
    # Heartbeat cadence override. Drives critical-silence detection
    # (alive iff last_update within 3x cadence). Per-resident because
    # residents have very different natural cadences: Sentinel is a
    # 60s loop, Steward syncs every 5min, Vigil cycles every 30min,
    # Chronicler runs daily, Watcher fires only on edits and has no
    # natural heartbeat cadence at all. A single global default
    # mislabels every non-continuous resident as "silent".
    expected_cadence_s: int


RESIDENT_PROGRESS_REGISTRY: dict[str, ResidentConfig] = {
    "vigil":      ResidentConfig("kg_writes",        "rows_written", timedelta(minutes=60),  1, expected_cadence_s=1800),
    "watcher":    ResidentConfig("watcher_findings", "rows_any",     timedelta(hours=6),     1, expected_cadence_s=21600),
    "steward":    ResidentConfig("eisv_sync_rows",   "rows_written", timedelta(minutes=30),  1, expected_cadence_s=300),
    "chronicler": ResidentConfig("metrics_series",   "rows_written", timedelta(hours=26),    1, expected_cadence_s=86400),
    "sentinel":   ResidentConfig("sentinel_pulse",   "latest_count", timedelta(minutes=30),  1, expected_cadence_s=60),
}


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

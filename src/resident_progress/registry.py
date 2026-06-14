"""Label-keyed config for the resident-progress probe.

Resident UUIDs are NOT stored here. They resolve at tick time from
filesystem anchors, so a resident that re-onboards or rotates UUID is
picked up automatically.
"""
from __future__ import annotations

import json
import logging
import os
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


# The resident-progress roster is deployment configuration, not a hardcoded
# fleet. It is loaded from a JSON manifest pointed to by the
# UNITARES_RESIDENT_PROGRESS_MANIFEST env var; unset/missing => no residents
# are probed (the user-agnostic default). The canonical fleet ships as
# config/resident_progress.example.json — a deployment points the env var at
# that file or its own copy. See docs/operations/resident-roster.md.
#
# Manifest shape (one entry per resident label, lowercase to match anchor
# filenames):
#   {
#     "vigil": {
#       "source": "kg_writes",          # must be a source registered in
#                                        # background_tasks.py's sources dict
#       "metric": "rows_written",        # human-readable label on the snapshot
#       "window_seconds": 3600,
#       "threshold": 1,                  # candidate fires when metric < threshold
#       "expected_cadence_s": 1800       # null for event-driven residents
#     }
#   }
RESIDENT_PROGRESS_MANIFEST_ENV = "UNITARES_RESIDENT_PROGRESS_MANIFEST"


def parse_resident_progress_manifest(doc: dict) -> dict[str, ResidentConfig]:
    """Build the label→ResidentConfig registry from a parsed manifest dict."""
    registry: dict[str, ResidentConfig] = {}
    for label, entry in doc.items():
        # Underscore-prefixed keys (e.g. "_comment") are manifest metadata.
        if label.startswith("_") or not isinstance(entry, dict):
            continue
        cadence = entry.get("expected_cadence_s")
        registry[label] = ResidentConfig(
            source=entry["source"],
            metric=entry["metric"],
            window=timedelta(seconds=int(entry["window_seconds"])),
            threshold=int(entry["threshold"]),
            expected_cadence_s=None if cadence is None else int(cadence),
        )
    return registry


def load_resident_progress_registry(
    path: str | Path | None = None,
) -> dict[str, ResidentConfig]:
    """Load the resident-progress registry from a JSON manifest.

    Reads ``path`` if given, else ``UNITARES_RESIDENT_PROGRESS_MANIFEST``.
    Unset, empty, missing, or unreadable => empty registry (no residents
    probed), the user-agnostic default. Read once at import; tests that vary
    the roster set the env/path and call this helper directly.
    """
    raw = str(path) if path is not None else os.environ.get(
        RESIDENT_PROGRESS_MANIFEST_ENV, ""
    )
    if not raw.strip():
        return {}
    manifest_path = Path(raw)
    try:
        doc = json.loads(manifest_path.read_text())
    except FileNotFoundError:
        logger.warning(
            "resident-progress manifest %s not found; probing no residents",
            manifest_path,
        )
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "resident-progress manifest %s unreadable (%s); probing no residents",
            manifest_path, e,
        )
        return {}
    return parse_resident_progress_manifest(doc)


RESIDENT_PROGRESS_REGISTRY: dict[str, ResidentConfig] = (
    load_resident_progress_registry()
)


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

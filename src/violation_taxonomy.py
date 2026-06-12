"""Violation taxonomy loader, validator, and reverse-lookup index.

Loads src/violation_taxonomy.yaml once, builds a reverse index from surface
IDs to class IDs. The taxonomy is server-owned fleet vocabulary — it maps
Watcher patterns, Sentinel finding types, and broadcast event types to
violation classes. In-repo residents and external consumers read it via the
``/v1/taxonomy`` endpoint rather than importing this module; cross-contract
tests (``tests/test_sentinel_taxonomy.py``) pin that resident-emitted names
stay mapped.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_TAXONOMY_FILE = Path(__file__).parent / "violation_taxonomy.yaml"
_cached: Optional[dict] = None
_reverse: Optional[dict[str, dict[str, str]]] = None  # kind -> {surface_id: class_id}


def load_taxonomy() -> dict:
    """Parse violation_taxonomy.yaml, return raw dict.

    Raises ValueError if any surface ID appears in more than one class.
    """
    with open(_TAXONOMY_FILE) as f:
        data = yaml.safe_load(f)

    # Build and validate reverse index on every fresh load
    reverse: dict[str, dict[str, str]] = {
        "watcher_patterns": {},
        "sentinel_findings": {},
        "broadcast_events": {},
    }
    for cls in data.get("classes", []):
        cid = cls["id"]
        for kind in reverse:
            for sid in cls.get("surfaces", {}).get(kind, []):
                if sid in reverse[kind]:
                    raise ValueError(
                        f"Surface '{sid}' in both {reverse[kind][sid]} and {cid}"
                    )
                reverse[kind][sid] = cid

    global _cached, _reverse
    _cached = data
    _reverse = reverse
    return data


def get_taxonomy() -> dict:
    """Cached access to loaded taxonomy."""
    if _cached is None:
        load_taxonomy()
    return _cached


def _get_reverse() -> dict[str, dict[str, str]]:
    if _reverse is None:
        load_taxonomy()
    return _reverse


def validate_class_id(class_id: str) -> bool:
    """True if class_id is a known class with status 'active'.

    Classes with any other status fail validation.
    Logs a warning for unknown or inactive classes.
    """
    tax = get_taxonomy()
    for cls in tax.get("classes", []):
        if cls["id"] == class_id and cls.get("status") == "active":
            return True
    if class_id:
        logger.warning("Unknown or inactive violation class: %s", class_id)
    return False


def validate_surface_mapping(surface_kind: str, surface_id: str) -> bool:
    """True if surface_id appears under surface_kind in any class."""
    rev = _get_reverse()
    return surface_id in rev.get(surface_kind, {})


def lookup_class_for_surface(
    surface_kind: str, surface_id: str
) -> Optional[str]:
    """Reverse lookup: given a surface kind and ID, return the class ID."""
    rev = _get_reverse()
    return rev.get(surface_kind, {}).get(surface_id)


def class_for_watcher_pattern(pattern_id: str) -> Optional[str]:
    """Return violation class for a Watcher pattern ID, or None."""
    return lookup_class_for_surface("watcher_patterns", pattern_id)


def class_for_sentinel_finding(finding_type: str) -> Optional[str]:
    """Return violation class for a Sentinel finding type, or None."""
    return lookup_class_for_surface("sentinel_findings", finding_type)


def class_for_broadcast_event(event_type: str) -> Optional[str]:
    """Return violation class for a broadcast event type, or None."""
    return lookup_class_for_surface("broadcast_events", event_type)

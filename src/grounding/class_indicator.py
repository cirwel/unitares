"""Map an agent (via its metadata) to a calibration class.

Spec §3 (paper §2 Heterogeneous Agent Fleets): class assignment uses the
identity tag and label fields already present on every agent. The class
indicator function k(a) here is keyed first on label (specific known resident
agent names like 'Lumen', 'Vigil') and then on tag set (embodied, persistent,
autonomous, ephemeral). Unrecognized agents fall through to the default
class, where fleet-wide constants apply.

Returns a class name string used to key into the class-conditional scale
maps in config/governance_config.py (S_SCALE_BY_CLASS, etc.).
"""
from typing import Any, Optional

# Specific labels that identify a unique resident agent. Each is its own
# calibration class because population N=1 means class==agent in practice.
KNOWN_RESIDENT_LABELS = frozenset(
    {"Lumen", "Vigil", "Sentinel", "Watcher", "Steward", "Chronicler"}
)

# Tag-derived class names.
CLASS_EMBODIED = "embodied"
CLASS_RESIDENT_PERSISTENT = "resident_persistent"
CLASS_EPHEMERAL = "ephemeral"
CLASS_ENGAGED_EPHEMERAL = "engaged_ephemeral"
CLASS_DEFAULT = "default"


def classify_agent(meta: Optional[Any]) -> str:
    """Return the calibration class name for an agent given its metadata.

    Resolution order:
      1. Known resident label (Lumen / Vigil / Sentinel / Watcher / Steward).
      2. Tag-derived: embodied → embodied; engaged_ephemeral → engaged_ephemeral;
         ephemeral → ephemeral; persistent + autonomous → resident_persistent.
      3. Default — used for session-bounded agents and anything unrecognized.

    Axis note (technical debt — see KG follow-up): ``embodied`` and
    ``ephemeral`` are *identity-class* claims; ``engaged_ephemeral`` is a
    *behavior-cohort* claim ("crossed activity threshold") encoded in the
    same single-tag namespace as a temporary measure. A future schema
    change will give behavior cohorts their own field; until then the
    resolution order treats engaged_ephemeral as more specific than the
    plain ephemeral fallback.

    ``engaged_ephemeral`` is checked before ``ephemeral`` so that during
    any transient state where both tags coexist (the promotion UPDATE
    strips ephemeral atomically, but the in-memory cache sync is best
    effort), the post-promotion class wins.

    Returns 'default' if meta is None or carries no class-relevant tags.
    """
    if meta is None:
        return CLASS_DEFAULT

    label = getattr(meta, "label", None)
    if label and label in KNOWN_RESIDENT_LABELS:
        return label

    tags_raw = getattr(meta, "tags", None) or []
    tags = set(tags_raw)

    if "embodied" in tags:
        return CLASS_EMBODIED
    if "engaged_ephemeral" in tags:
        return CLASS_ENGAGED_EPHEMERAL
    if "ephemeral" in tags:
        return CLASS_EPHEMERAL
    if "persistent" in tags and "autonomous" in tags:
        return CLASS_RESIDENT_PERSISTENT

    return CLASS_DEFAULT

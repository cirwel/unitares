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
import os
from typing import Any, Iterable, Optional

# The resident roster is *deployment configuration*, not a hardcoded fleet.
# A deployment declares its named residents via the UNITARES_RESIDENTS env
# var (comma-separated labels, e.g. "Vigil,Sentinel,Lumen"). Unset or empty
# means this install has no named residents — every agent then classifies by
# tag (embodied / persistent / ephemeral) or falls through to the default
# class, where fleet-wide constants apply. This is what makes UNITARES
# user-agnostic: a fresh install inherits no operator-specific identities.
#
# Each named resident becomes its own N=1 calibration class, so a deployment
# that names residents must also provide their class-conditional scale
# constants (config/governance_config.py) — guarded by
# tests/test_grounding_scale_constants.py.
#
# The SDK mirrors this contract by reading the same env var; see
# agents/sdk/src/unitares_sdk/_substrate.py. The env var NAME is the
# cross-package contract (the SDK cannot import from src/).
RESIDENT_ROSTER_ENV = "UNITARES_RESIDENTS"


def parse_resident_roster(raw: Optional[str]) -> frozenset[str]:
    """Parse a comma-separated resident roster string into a label set."""
    if not raw:
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def load_resident_labels() -> frozenset[str]:
    """Resident labels for this deployment, from ``UNITARES_RESIDENTS``.

    Read once at import into ``KNOWN_RESIDENT_LABELS`` (server processes read
    their roster at startup). Tests that vary the roster set the env var
    before import (see tests/conftest.py) or call this helper directly.
    """
    return parse_resident_roster(os.environ.get(RESIDENT_ROSTER_ENV))


# Specific labels that identify a unique resident agent. Each is its own
# calibration class because population N=1 means class==agent in practice.
# Empty by default; populated from UNITARES_RESIDENTS — see above.
KNOWN_RESIDENT_LABELS = load_resident_labels()

# Tag-derived class names.
CLASS_EMBODIED = "embodied"
CLASS_RESIDENT_PERSISTENT = "resident_persistent"
CLASS_EPHEMERAL = "ephemeral"
CLASS_ENGAGED_EPHEMERAL = "engaged_ephemeral"
CLASS_DEFAULT = "default"


def classify_by_label_and_tags(
    label: Optional[str],
    tags: Optional[Iterable[str]],
    known: Optional[frozenset[str]] = None,
) -> str:
    """Canonical fold: return the calibration class name from raw label + tags.

    Single source of truth for class assignment k(a). Both the runtime path
    (`classify_agent` below, given a meta object) and batch scripts that read
    directly from DB rows delegate here — do not duplicate the resolution
    logic. Drift between runtime and batch folds silently mis-classifies
    agents at fit time vs at gating time.

    Resolution order:
      1. Known resident label (Lumen / Vigil / Sentinel / Watcher / Steward /
         Chronicler) — each is its own class because N=1 means class==agent.
      2. Tag-derived: embodied → embodied; engaged_ephemeral → engaged_ephemeral;
         ephemeral → ephemeral; persistent + autonomous → resident_persistent.
      3. Default — session-bounded agents and anything unrecognized.

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

    ``known`` overrides the resident roster (defaults to the deployment's
    ``KNOWN_RESIDENT_LABELS``); pass an explicit set to classify against a
    specific roster without mutating module state.
    """
    roster = KNOWN_RESIDENT_LABELS if known is None else known
    if label and label in roster:
        return label

    tags_set = set(tags) if tags else set()

    if "embodied" in tags_set:
        return CLASS_EMBODIED
    if "engaged_ephemeral" in tags_set:
        return CLASS_ENGAGED_EPHEMERAL
    if "ephemeral" in tags_set:
        return CLASS_EPHEMERAL
    if "persistent" in tags_set and "autonomous" in tags_set:
        return CLASS_RESIDENT_PERSISTENT

    return CLASS_DEFAULT


def classify_agent(meta: Optional[Any]) -> str:
    """Return the calibration class name for an agent given its metadata.

    Thin adapter over `classify_by_label_and_tags`; accepts any object with
    ``label`` and ``tags`` attributes (the runtime metadata shape).

    Returns 'default' if meta is None or carries no class-relevant tags.
    """
    if meta is None:
        return CLASS_DEFAULT
    return classify_by_label_and_tags(
        getattr(meta, "label", None),
        getattr(meta, "tags", None),
    )

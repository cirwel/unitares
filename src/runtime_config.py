"""
Runtime Configuration Management

Allows runtime access and modification of governance thresholds
without requiring code changes or redeployment.
"""

from typing import Dict, Optional, Any

# Ensure project root is in path for imports
from src._imports import ensure_project_root
ensure_project_root()

from config import governance_config as config_module


# Runtime overrides (None = use class defaults)
_runtime_overrides: Dict[str, float] = {}


# The overridable thresholds: name -> (GovernanceConfig attribute, valid range).
#
# Single source for three things that must agree — the effective value returned
# by get_thresholds, the write gate in set_thresholds, and the `settable` /
# `class_default` fields in describe_thresholds. Held as one table because a
# second hand-maintained list of "which thresholds can be set" is exactly the
# drift that makes a config surface lie. Every other key in get_thresholds is
# structural: read from the config class, no override path, not settable.
OVERRIDABLE_THRESHOLDS: Dict[str, tuple] = {
    "risk_approve_threshold": ("RISK_APPROVE_THRESHOLD", (0.0, 1.0)),
    "risk_revise_threshold": ("RISK_REVISE_THRESHOLD", (0.0, 1.0)),
    # Note: reject threshold is implicit (risk > revise_threshold triggers reject)
    "coherence_critical_threshold": ("COHERENCE_CRITICAL_THRESHOLD", (0.0, 1.0)),
    "void_threshold_initial": ("VOID_THRESHOLD_INITIAL", (0.0, 1.0)),
    # Width of the "tight" band around a decision edge, as a fraction of that
    # edge's own attainable margin. Overridable BECAUSE it is a policy choice,
    # not a measured constant: the shipped value reproduces the historical fixed
    # risk band exactly, but nothing in the evidence fixes it for the void edge.
    # Surfacing it here means describe_thresholds reports it as `class_default`
    # until an operator sets it — an un-ratified value that says so.
    "tight_band_fraction": ("TIGHT_BAND_FRACTION", (0.0, 1.0)),
}


def _class_default(name: str) -> float:
    """The GovernanceConfig default for an overridable threshold."""
    attr, _range = OVERRIDABLE_THRESHOLDS[name]
    return getattr(config_module.GovernanceConfig, attr)


def get_thresholds() -> Dict[str, float]:
    """
    Get current threshold configuration (runtime overrides + defaults).

    Returns all decision thresholds for governance system.
    """
    config = config_module.GovernanceConfig

    return {
        **{
            name: _runtime_overrides.get(name, _class_default(name))
            for name in OVERRIDABLE_THRESHOLDS
        },
        "void_threshold_min": config.VOID_THRESHOLD_MIN,
        "void_threshold_max": config.VOID_THRESHOLD_MAX,
        "lambda1_min": config.LAMBDA1_MIN,
        "lambda1_max": config.LAMBDA1_MAX,
        "target_coherence": config.TARGET_COHERENCE,
        "target_void_freq": config.TARGET_VOID_FREQ,
        # Basin breakpoints along the I axis — the structural (non-overridable)
        # I-axis projection of classify_basin. Exposed so the /phase view and
        # other read clients render basin bands from the engine's own constants
        # instead of hardcoding a copy that silently drifts. classify_basin is
        # multi-dimensional (I, coherence, |V|, risk + the BASIN_HIGH box); these
        # two values are only its I-axis breakpoints, not the full classifier.
        "basin_low_i_ceil": config_module.BASIN_LOW_I_CEIL,
        "basin_high_i_min": config_module.BASIN_HIGH.I_min,
    }


def set_thresholds(thresholds: Dict[str, float], validate: bool = True) -> Dict[str, Any]:
    """
    Set runtime threshold overrides.
    
    Args:
        thresholds: Dict of threshold_name -> value
        validate: If True, validate values are in reasonable ranges
    
    Returns:
        {
            "success": bool,
            "updated": List[str],
            "errors": List[str]
        }
    """
    config = config_module.GovernanceConfig
    updated = []
    errors = []
    
    # Derived from the one table, so the write gate cannot drift from what
    # describe_thresholds reports as settable.
    valid_ranges = {
        name: rng for name, (_attr, rng) in OVERRIDABLE_THRESHOLDS.items()
    }

    for name, value in thresholds.items():
        if name not in valid_ranges:
            errors.append(f"Unknown threshold: {name}")
            continue
        
        if validate:
            min_val, max_val = valid_ranges[name]
            if not (min_val <= value <= max_val):
                errors.append(f"{name}={value} out of range [{min_val}, {max_val}]")
                continue
        
        # Additional logical validation: enforce APPROVE < REVISE < REJECT invariant.
        # Reject is not writable (see comment at line 38) — always the class default.
        if name in ("risk_approve_threshold", "risk_revise_threshold"):
            effective_approve = thresholds.get(
                "risk_approve_threshold",
                _runtime_overrides.get("risk_approve_threshold", config.RISK_APPROVE_THRESHOLD),
            )
            effective_revise = thresholds.get(
                "risk_revise_threshold",
                _runtime_overrides.get("risk_revise_threshold", config.RISK_REVISE_THRESHOLD),
            )
            effective_reject = config.RISK_REJECT_THRESHOLD

            if not (effective_approve < effective_revise < effective_reject):
                errors.append(
                    f"Ordering violated: APPROVE({effective_approve}) "
                    f"< REVISE({effective_revise}) "
                    f"< REJECT({effective_reject}) must hold"
                )
                continue
        
        _runtime_overrides[name] = float(value)
        updated.append(name)
    
    return {
        "success": len(errors) == 0,
        "updated": updated,
        "errors": errors
    }


def get_effective_threshold(threshold_name: str, default: Optional[float] = None) -> float:
    """
    Get effective threshold value (runtime override or default).
    
    Used internally by governance system.
    
    Args:
        threshold_name: Name of threshold to get
        default: Optional default value if threshold not found (for backward compatibility)
    
    Returns:
        Effective threshold value
    """
    config = config_module.GovernanceConfig
    
    if threshold_name == "risk_approve_threshold":
        return _runtime_overrides.get("risk_approve_threshold", config.RISK_APPROVE_THRESHOLD)
    elif threshold_name == "risk_revise_threshold":
        return _runtime_overrides.get("risk_revise_threshold", config.RISK_REVISE_THRESHOLD)
    elif threshold_name == "risk_reject_threshold":
        # Not writable via set_thresholds; class default unless caller passes an explicit default.
        return default if default is not None else config.RISK_REJECT_THRESHOLD
    elif threshold_name == "coherence_critical_threshold":
        return _runtime_overrides.get("coherence_critical_threshold", config.COHERENCE_CRITICAL_THRESHOLD)
    elif threshold_name == "void_threshold_initial":
        return _runtime_overrides.get("void_threshold_initial", config.VOID_THRESHOLD_INITIAL)
    elif threshold_name == "tight_band_fraction":
        return _runtime_overrides.get("tight_band_fraction", config.TIGHT_BAND_FRACTION)
    else:
        if default is not None:
            return default
        raise ValueError(f"Unknown threshold: {threshold_name}")


def describe_thresholds() -> Dict[str, Dict[str, Any]]:
    """Every threshold with the layer that supplied it.

    ``get_thresholds`` merges runtime overrides into class defaults and returns
    bare numbers, so a reader cannot tell an operator-set value from a shipped
    one — and the mixed set also hides that most keys cannot be set at all. This
    reports each value alongside where it came from.

    Per key:
      ``value``          effective value, identical to get_thresholds()[key]
      ``source``         ``runtime_override`` or ``class_default``
      ``settable``       whether set_thresholds accepts it
      ``class_default``  the shipped value — present only when overridden, so
                         the reader can see what was displaced

    ``source`` is the config *layer* a value came from. It is deliberately not
    the ``docs/trust-contract.md`` §1 provenance vocabulary
    (measured / derived / prior-default / unknown), which classifies epistemic
    status. Do not map one onto the other: a class default is not a "prior" in
    the §1 sense, and labelling it as one would assert something this function
    has no basis for.
    """
    return {
        name: {
            "value": value,
            "source": (
                "runtime_override" if name in _runtime_overrides else "class_default"
            ),
            "settable": name in OVERRIDABLE_THRESHOLDS,
            **(
                {"class_default": _class_default(name)}
                if name in _runtime_overrides
                else {}
            ),
        }
        for name, value in get_thresholds().items()
    }


def clear_overrides() -> None:
    """Clear all runtime overrides, revert to defaults"""
    _runtime_overrides.clear()


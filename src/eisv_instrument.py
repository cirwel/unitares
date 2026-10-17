"""Which EISV instrument produced a persisted row, and how long it has run.

Every ``core.agent_state`` row is stamped with ``state_json.eisv_instrument``
(schema ``eisv.instrument.v1``) so analyses can partition rows by the maths
that produced them instead of by deploy timestamps remembered in prose.

The stamp answers two questions:

* **Which instrument.** ``era`` is the latest splitting entry of the era
  registry (``config/eisv_instrument_eras.json``, read by ``src/eisv_eras.py``,
  which also holds the analysis-side guards), which a PR that changes the
  maths must extend (``tests/test_eisv_instrument_eras.py`` enforces this for
  every change the golden trajectories pin). ``config_hash`` digests the
  effective dynamics parameters, flags, class-calibration tables, resident
  roster and decision thresholds, so a plist or overlay change that alters
  values without a code change still yields a different ``id``.
* **How long under it.** ``updates_since_change`` counts this monitor's
  check-ins since its instrument ``id`` last changed. A monitor restored across
  a restart carries ODE state integrated under the previous instrument and
  takes several check-ins to relax (10-15 minutes after #2834), so rows with
  ``carried_from`` set and a small count are a transient, not the new level.
  ``carried_from`` is ``None`` for a monitor whose state began under this
  instrument, the previous ``id`` when it changed, ``"untracked"`` when the
  state predates stamping, and ``"db_hydrated"`` when it was rebuilt from
  database rows after a lost state file.

``build_sha`` is recorded for forensics only. It is deliberately not part of
``id``: most deploys do not change the maths, and an id that moved on every
deploy would fragment cohorts without marking anything.

The stamp is provenance. Nothing on the decision path reads it, and every
failure here is fail-open: a check-in is never lost to a stamp.
"""

from __future__ import annotations

import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

from src import eisv_eras

logger = logging.getLogger(__name__)

STAMP_SCHEMA = "eisv.instrument.v1"

UNTRACKED = "untracked"
HYDRATED = "db_hydrated"

# Monitor attribute holding {"id": str, "updates": int, "carried_from": str|None}.
TRACK_ATTR = "_eisv_instrument_track"
# Key under which the track is persisted in the monitor state file.
TRACK_STATE_KEY = "eisv_instrument_track"


def _jsonable(value: Any) -> Any:
    # ScaleConstant and similar carry provenance prose next to the number;
    # only the number moves the dynamics, so only the number is hashed.
    if hasattr(value, "value"):
        return float(value.value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, tuple):
        return list(value)
    return repr(value)


@lru_cache(maxsize=1)
def _static_config() -> Dict[str, Any]:
    """Configuration fixed for the life of the process (environment, overlay)."""
    from config import governance_config as gc
    from governance_core.parameters import (
        get_active_params,
        get_i_dynamics_mode,
        get_integrator_mode,
        sensor_coupling_mode,
    )
    from src.behavioral_assessment import floor_breach_caution_apply_enabled
    from src.grounding.class_indicator import load_resident_labels

    return {
        "params": dict(sorted(vars(get_active_params()).items())),
        "integrator": get_integrator_mode(),
        "i_dynamics": get_i_dynamics_mode(),
        "sensor_coupling": sensor_coupling_mode(),
        "s_setpoint": gc.s_setpoint_enabled(),
        "phi_telemetry_only": gc.phi_telemetry_only(),
        "grounding_apply": gc.grounding_apply_enabled(),
        "floor_breach_caution_apply": floor_breach_caution_apply_enabled(),
        "healthy_operating_point": dict(gc.HEALTHY_OPERATING_POINT_BY_CLASS),
        "delta_norm_max": dict(gc.DELTA_NORM_MAX_BY_CLASS),
        "void_threshold": dict(gc.GovernanceConfig.VOID_THRESHOLD_BY_CLASS),
        "residents": sorted(load_resident_labels()),
    }


def effective_config() -> Dict[str, Any]:
    """Everything that can change EISV values without a code change."""
    from src import runtime_config

    return {**_static_config(), "thresholds": runtime_config.get_thresholds()}


def config_hash(config: Optional[Dict[str, Any]] = None) -> str:
    payload = json.dumps(
        effective_config() if config is None else config,
        sort_keys=True,
        default=_jsonable,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@lru_cache(maxsize=1)
def _build_sha() -> str:
    from src.versioning import load_build_sha_from_repo

    return load_build_sha_from_repo(Path(__file__).resolve().parent.parent)


def reset_caches() -> None:
    """For tests: re-read the registry, environment and build sha."""
    eisv_eras.load_registry.cache_clear()
    _static_config.cache_clear()
    _build_sha.cache_clear()


def advance(monitor: Any) -> None:
    """Count one real check-in for ``monitor`` under the current instrument.

    Called on the live update path just before the monitor state is saved,
    so the count survives a restart. Never call it for a simulation.
    """
    try:
        instrument_id = f"{eisv_eras.current_era_id()}:{config_hash()}"
        track = getattr(monitor, TRACK_ATTR, None)
        if not isinstance(track, dict) or not track.get("id"):
            state = getattr(monitor, "state", None)
            updates_so_far = int(getattr(state, "update_count", 0) or 0)
            # update_count already includes this check-in, so more than one
            # means the monitor carries state from before stamping began.
            carried_from = UNTRACKED if updates_so_far > 1 else None
            track = {"id": instrument_id, "updates": 0, "carried_from": carried_from}
        elif track["id"] != instrument_id:
            track = {"id": instrument_id, "updates": 0, "carried_from": track["id"]}
        setattr(monitor, TRACK_ATTR, {**track, "updates": int(track["updates"]) + 1})
    except Exception:  # noqa: BLE001 — provenance must never cost a check-in
        logger.warning("EISV instrument track not advanced", exc_info=True)


def stamp(monitor: Any) -> Optional[Dict[str, Any]]:
    """The ``eisv_instrument`` block for the row being persisted, or ``None``.

    ``None`` when the track was never advanced (for example a path that
    persists a row without a live update); the row then carries no stamp
    rather than a guessed one.
    """
    track = getattr(monitor, TRACK_ATTR, None)
    if not isinstance(track, dict) or ":" not in str(track.get("id", "")):
        return None
    try:
        era, digest = track["id"].split(":", 1)
        return {
            "schema": STAMP_SCHEMA,
            "era": era,
            "config_hash": digest,
            "id": track["id"],
            "build_sha": _build_sha(),
            "updates_since_change": int(track["updates"]),
            "carried_from": track.get("carried_from"),
        }
    except Exception:  # noqa: BLE001
        logger.warning("EISV instrument stamp skipped", exc_info=True)
        return None


def mark_hydrated(monitor: Any) -> None:
    """Record that the monitor's state was rebuilt from database rows.

    Hydration seeds the ODE from persisted primary values, which is a state
    discontinuity of its own, so the next check-in starts a fresh count with
    ``carried_from="db_hydrated"``.
    """
    setattr(monitor, TRACK_ATTR, {"id": HYDRATED, "updates": 0, "carried_from": None})


def restore_track(monitor: Any, raw: Any) -> None:
    """Restore a persisted track onto ``monitor``; ignore anything malformed."""
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("id"), str)
        and isinstance(raw.get("updates"), int)
        and (raw.get("carried_from") is None or isinstance(raw.get("carried_from"), str))
    ):
        setattr(monitor, TRACK_ATTR, dict(raw))

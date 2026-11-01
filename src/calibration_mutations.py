"""Frozen calibration mutations, independent of the persistence adapter.

Ordinary observations add counters and advance observation timestamps. Persist
those effects against the current durable state rather than replacing it with a
writer's stale snapshot. Non-additive edits require an explicit version match.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any

STATE_KEYS = (
    "bins", "complexity_bins", "tactical_bins", "tactical_bins_by_channel",
    "bins_by_agent", "tactical_bins_by_agent",
)
_MISSING = object()


class CalibrationVersionConflict(ValueError):
    """A replacement was computed from a different durable state version."""


def projection_state(data: dict[str, Any]) -> dict[str, Any]:
    """Copy model state without persistence receipts or version metadata."""
    return {key: deepcopy(data.get(key, {})) for key in STATE_KEYS}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("calibration observation timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def make_mutation(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Freeze additive effects, or identify a versioned replacement.

    A negative correction, deletion or unsupported change is not silently
    rebased. Its caller must prove the version on which the replacement rests.
    """
    before, after = projection_state(before), projection_state(after)
    changes: list[dict[str, Any]] = []

    def visit(old: Any, new: Any, path: list[str]) -> bool:
        if (_number(new) and not math.isfinite(new)) or (
            _number(old) and not math.isfinite(old)
        ):
            raise ValueError("calibration counters must be finite")
        if old == new:
            return True
        if isinstance(new, dict) and (old is _MISSING or isinstance(old, dict)):
            old_map = {} if old is _MISSING else old
            if old_map.keys() - new.keys():
                return False
            return all(visit(old_map.get(key, _MISSING), value, [*path, key])
                       for key, value in new.items())
        if _number(new) and (old is _MISSING or _number(old)):
            if not math.isfinite(new) or (old is not _MISSING and not math.isfinite(old)):
                raise ValueError("calibration counters must be finite")
            increment = new - (0 if old is _MISSING else old)
            if increment < 0:
                return False
            if increment:
                changes.append({"path": path, "add": increment})
            elif old is _MISSING:
                changes.append({"path": path, "initialize": new})
            return True
        if path[-1] == "last_observed_at" and isinstance(new, str):
            new_time = _time(new)
            if old not in (_MISSING, None) and (
                not isinstance(old, str) or new_time < _time(old)
            ):
                return False
            changes.append({"path": path, "latest": new})
            return True
        if old is _MISSING and new is None:
            changes.append({"path": path, "initialize": None})
            return True
        return False

    if visit(before, after, []):
        return {"kind": "additive", "changes": changes}
    return {"kind": "replace", "state": after}


def apply_mutation(
    current: dict[str, Any], mutation: dict[str, Any], *,
    expected_version: int | None = None, actual_version: int | None = None,
) -> dict[str, Any]:
    """Apply one mutation to fresh state; receipt deduplication belongs to storage."""
    result = deepcopy(current)
    if mutation["kind"] == "replace":
        if expected_version is None or expected_version != actual_version:
            raise CalibrationVersionConflict("calibration replacement needs a current version")
        result.update(projection_state(mutation["state"]))
        return result
    if mutation["kind"] != "additive":
        raise ValueError("unknown calibration mutation kind")
    for change in mutation["changes"]:
        path = change["path"]
        if not path or path[0] not in STATE_KEYS:
            raise ValueError("invalid calibration mutation path")
        target = result
        for key in path[:-1]:
            target = target.setdefault(key, {})
            if not isinstance(target, dict):
                raise ValueError("calibration mutation conflicts with stored structure")
        key = path[-1]
        if "add" in change:
            value, increment = target.get(key, 0), change["add"]
            if not _number(value) or not _number(increment) or not math.isfinite(value + increment):
                raise ValueError("invalid calibration counter")
            if increment < 0:
                raise ValueError("additive calibration mutations cannot subtract")
            target[key] = value + increment
        elif "latest" in change:
            value = change["latest"]
            if target.get(key) is None or _time(value) > _time(target[key]):
                target[key] = value
        elif "initialize" in change:
            target.setdefault(key, change["initialize"])
        else:
            raise ValueError("unknown calibration mutation operation")
    return result


def mutation_digest(mutation: dict[str, Any], expected_version: int | None) -> str:
    encoded = json.dumps(
        {"mutation": mutation, "expected_version": expected_version},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()

"""Alias-layer parameter normalization for friendly check-in aliases.

Canonical tools stay strict: process_agent_update rejects complexity outside
0-1. The friendly aliases (checkin/log/update/sync_state) absorb common agent
vocabulary by normalizing it BEFORE schema validation, and the dispatch
envelope discloses every transform via ``normalized_parameters``.

Bare numerics above 1 are never silently rescaled — a bare 5 could mean 5/10
or 5/100, so the caller must declare the scale (``{"value": 5, "scale": 10}``)
or use a named level.
"""

from typing import Any, Callable, Dict

# Named levels accepted on friendly aliases. Synonyms map to the same anchor
# values so the disclosure record stays unambiguous.
NAMED_LEVELS: Dict[str, float] = {
    "trivial": 0.1,
    "minimal": 0.1,
    "low": 0.3,
    "simple": 0.3,
    "medium": 0.5,
    "moderate": 0.5,
    "high": 0.7,
    "complex": 0.7,
    "very_high": 0.9,
    "critical": 0.9,
}

_LEVELS_DISPLAY = "'trivial'|'low'|'medium'|'high'|'very_high'"

# Normalizer return type: {param: {"from": ..., "to": ..., "interpretation": ...}}
NormalizationRecords = Dict[str, Dict[str, Any]]
ParamNormalizer = Callable[[Dict[str, Any]], NormalizationRecords]


class ParamNormalizationError(ValueError):
    """A parameter on a friendly alias could not be normalized unambiguously."""

    def __init__(self, message: str, *, parameter: str, provided: Any):
        super().__init__(message)
        self.parameter = parameter
        self.provided = provided


def _accepted_forms(param: str) -> str:
    return (
        f"a 0-1 float ({param}=0.7), a named level ({_LEVELS_DISPLAY}), or an "
        f"explicit scale object ({param}={{'value': 5, 'scale': 10}})"
    )


def _real_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def normalize_unit_interval(param: str) -> ParamNormalizer:
    """Build an alias-layer normalizer for a 0-1 parameter.

    The returned callable mutates the arguments dict in place and returns
    disclosure records for the response envelope (empty dict when the value
    passed through untouched).
    """

    def _reject(provided: Any, message: str) -> None:
        raise ParamNormalizationError(message, parameter=param, provided=provided)

    def _reject_ambiguous(provided: Any) -> None:
        _reject(
            provided,
            f"{param}={provided!r} is ambiguous: bare numeric values must "
            f"already be on the 0-1 scale (a bare 5 could mean 5/10 or "
            f"5/100). Use {_accepted_forms(param)}.",
        )

    def _normalize_scale_object(value: Dict[str, Any]) -> float:
        unexpected = set(value) - {"value", "scale"}
        if unexpected or "value" not in value or "scale" not in value:
            _reject(
                value,
                f"{param} object must have exactly the keys 'value' and "
                f"'scale', e.g. {param}={{'value': 5, 'scale': 10}}. "
                f"Got keys: {sorted(value)}.",
            )
        raw_value, raw_scale = value["value"], value["scale"]
        if not _real_number(raw_value) or not _real_number(raw_scale):
            _reject(value, f"{param}: 'value' and 'scale' must both be numbers.")
        if raw_scale < 1:
            _reject(value, f"{param}: 'scale' must be >= 1, got {raw_scale}.")
        if not 0 <= raw_value <= raw_scale:
            _reject(
                value,
                f"{param}: 'value' must be between 0 and 'scale' "
                f"({raw_scale}), got {raw_value}.",
            )
        return round(raw_value / raw_scale, 4)

    def _normalize(arguments: Dict[str, Any]) -> NormalizationRecords:
        if param not in arguments:
            return {}
        value = arguments[param]
        if value is None:
            return {}

        if _real_number(value):
            if 0.0 <= float(value) <= 1.0:
                return {}
            _reject_ambiguous(value)

        if isinstance(value, str):
            level = value.strip().lower().replace("-", "_").replace(" ", "_")
            if level in NAMED_LEVELS:
                normalized = NAMED_LEVELS[level]
                arguments[param] = normalized
                return {
                    param: {
                        "from": value,
                        "to": normalized,
                        "interpretation": "named_level",
                    }
                }
            try:
                numeric = float(value)
            except ValueError:
                _reject(
                    value,
                    f"{param}={value!r} is not a named level. Use "
                    f"{_accepted_forms(param)}.",
                )
            if 0.0 <= numeric <= 1.0:
                # In-range numeric strings are valid canonical input; the
                # schema's own coercion owns them.
                return {}
            _reject_ambiguous(value)

        if isinstance(value, dict):
            normalized = _normalize_scale_object(value)
            arguments[param] = normalized
            return {
                param: {
                    "from": {"value": value["value"], "scale": value["scale"]},
                    "to": normalized,
                    "interpretation": "explicit_scale",
                }
            }

        _reject(
            value,
            f"{param} got unsupported type {type(value).__name__}. Use "
            f"{_accepted_forms(param)}.",
        )

    return _normalize

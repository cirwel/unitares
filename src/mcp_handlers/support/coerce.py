"""Shared type coercion utilities for MCP handlers."""

from typing import Any, Dict


def safe_float(val: Any, default: float = 0.0) -> float:
    """Safely convert a value to float, returning default on failure."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Coerce bool-ish values from tool arguments.

    Handles string representations commonly passed through MCP transport:
    true/false, 1/0, yes/no, on/off (case-insensitive).
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def resolve_agent_uuid(arguments: Dict[str, Any], agent_id: str) -> str:
    """Resolve authoritative agent UUID from arguments or fall back to agent_id."""
    return arguments.get("_agent_uuid") or agent_id


class LimitError(ValueError):
    """A caller-supplied row limit was not usable as given."""


def parse_limit(
    value: Any,
    *,
    default: int,
    maximum: int,
    minimum: int = 1,
    name: str = "limit",
) -> int:
    """Resolve a caller-supplied row limit, or raise LimitError.

    Absent/None returns `default`. Over-asking clamps down to `maximum` --
    callers have always been allowed to ask for more than we will return, and
    that behaviour is relied on.

    Non-numeric values and values below `minimum` raise instead of being
    substituted. There is no replacement number that answers the question the
    caller actually asked, and a substituted limit does real damage: a
    non-positive value reaches PostgreSQL as a malformed LIMIT, and a backend
    that catches the resulting error renders it as an empty result set. The
    caller then cannot tell "nothing matched" from "your argument was invalid".
    """
    if value is None:
        return default
    try:
        limit = int(value)
    except (TypeError, ValueError):
        raise LimitError(
            f"Invalid {name} {value!r}: must be an integer "
            f"between {minimum} and {maximum}."
        ) from None
    if limit < minimum:
        raise LimitError(
            f"Invalid {name} {limit}: must be at least {minimum}. "
            f"Use {name}={minimum} for a single row, or omit {name} "
            f"for the default of {default}."
        )
    return min(limit, maximum)

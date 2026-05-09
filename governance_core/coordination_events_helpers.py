"""Helpers that build canonical payloads for `coordination_failure.beam_python_boundary.*`
events. Direct dict construction at emission sites is prohibited — every emission
goes through this module so the documented payload contract (in
`src/coordination_events.py` Wave 2 schema extension comments) is enforced once,
not relitigated per call site.

The helper exists ahead of the wire-up call sites that come with Wave 3 (or
earlier if lease-integration boundary hardening produces them) so that when
emissions land, the payload shape is already pinned and lint-checkable.
"""

from __future__ import annotations

from typing import Final


VALID_ERROR_CLASSES: Final[frozenset[str]] = frozenset({
    "timeout",
    "connect_error",
    "non_200",
    "decode_error",
    "other",
})


def make_boundary_payload(
    *,
    endpoint: str,
    method: str,
    error_class: str,
    status_code: int | None,
    elapsed_ms: int | None,
) -> dict:
    """Build a canonical boundary-event payload.

    Mandatory keyword-only arguments. Returns a fresh dict in the order the
    payload contract documents — callers that re-order keys violate the
    contract; using this helper is the cheapest way to stay compliant.

    Raises:
        ValueError: if `endpoint` or `method` is empty/whitespace-only, if
            `error_class` is missing from the documented enum, or if
            `status_code` is None when `error_class == "non_200"` (the doc
            comment pins `status_code` as populated for that case).
        TypeError: if `status_code` or `elapsed_ms` is the wrong type when
            non-None — caught here rather than at INSERT time so emission-
            site bugs surface in tests, not in production audit gaps.
    """
    if not endpoint or not endpoint.strip():
        raise ValueError("endpoint must be a non-empty stable identifier")
    if not method or not method.strip():
        raise ValueError("method must be a non-empty HTTP method")
    if error_class not in VALID_ERROR_CLASSES:
        raise ValueError(
            f"error_class={error_class!r} is not in the documented enum "
            f"{sorted(VALID_ERROR_CLASSES)}; emission sites must declare a "
            f"specific class, not None or a free-form string"
        )
    if error_class == "non_200" and status_code is None:
        raise ValueError(
            "status_code is required when error_class == 'non_200'; the "
            "payload contract pins it for that case"
        )
    if status_code is not None and not isinstance(status_code, int):
        raise TypeError(f"status_code must be int or None, got {type(status_code).__name__}")
    if elapsed_ms is not None and not isinstance(elapsed_ms, int):
        raise TypeError(f"elapsed_ms must be int or None, got {type(elapsed_ms).__name__}")

    return {
        "endpoint": endpoint,
        "method": method,
        "error_class": error_class,
        "status_code": status_code,
        "elapsed_ms": elapsed_ms,
    }

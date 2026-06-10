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


VALID_SHADOW_TABLES: Final[frozenset[str]] = frozenset({"identities", "agents"})

VALID_DIVERGENCE_KINDS: Final[frozenset[str]] = frozenset({
    "canonical_missing",
    "shadow_missing",
    "column_mismatch",
})


def make_shadow_divergence_payload(
    *,
    table_name: str,
    agent_id: str,
    kind: str,
    divergent_columns: list[str],
) -> dict:
    """Build the Wave 3 §8.2 shadow-divergence payload.

    Contract (pinned in `src/coordination_events.py` §8.4 comments):
    `{table_name, agent_id, kind, divergent_columns}`. All
    `coordination_failure.beam_python_boundary.shadow_divergence` emissions go
    through this helper — same enforcement rationale as `make_boundary_payload`.

    Raises:
        ValueError: if `table_name` is not a shadowed table, `agent_id` is
            empty, `kind` is outside the documented enum, or the
            kind/column coherence rule is violated (`column_mismatch`
            requires a non-empty column list; the missing-row kinds require
            an empty one — the row diverged wholesale, not per-column).
        TypeError: if `divergent_columns` is not a list of strings.
    """
    if table_name not in VALID_SHADOW_TABLES:
        raise ValueError(
            f"table_name={table_name!r} is not a shadowed table "
            f"{sorted(VALID_SHADOW_TABLES)}"
        )
    if not agent_id or not agent_id.strip():
        raise ValueError("agent_id must be the non-empty join-key of the divergent row")
    if kind not in VALID_DIVERGENCE_KINDS:
        raise ValueError(
            f"kind={kind!r} is not in the documented enum "
            f"{sorted(VALID_DIVERGENCE_KINDS)}"
        )
    if not isinstance(divergent_columns, list) or any(
        not isinstance(c, str) for c in divergent_columns
    ):
        raise TypeError("divergent_columns must be a list of column-name strings")
    if kind == "column_mismatch" and not divergent_columns:
        raise ValueError(
            "kind='column_mismatch' requires at least one divergent column"
        )
    if kind != "column_mismatch" and divergent_columns:
        raise ValueError(
            f"kind={kind!r} means the row is missing wholesale; "
            "divergent_columns must be empty"
        )

    return {
        "table_name": table_name,
        "agent_id": agent_id,
        "kind": kind,
        "divergent_columns": list(divergent_columns),
    }


def make_measurement_payload(
    *,
    endpoint: str,
    method: str,
    status_code: int | None,
    elapsed_ms: int,
    payload_bytes: int | None,
) -> dict:
    """Build the canonical measurement-channel payload (Wave 3 RFC §6.4).

    Sibling of `make_boundary_payload` for the INFORMATIONAL channel
    (`audit.coordination_measurements`, migration 041): successful boundary
    crossings, lease RPC baselines, and the §3.2 cutover counters. The table
    columns `endpoint` / `elapsed_ms` / `payload_bytes` are lifted from this
    dict by the emitter; `method` and `status_code` ride in the table's
    `meta` JSONB. Same enforcement rationale as the failure-channel helpers:
    payload shape is pinned once, here, and lint-checked
    (scripts/dev/check-boundary-event-helpers.sh).

    Raises:
        ValueError: if `endpoint` or `method` is empty/whitespace-only, or
            if `elapsed_ms` / `payload_bytes` is negative.
        TypeError: if `elapsed_ms` is not an int, or `status_code` /
            `payload_bytes` is the wrong type when non-None. `status_code`
            may be None — transport-level failures measured before a status
            line exists (timeout, connect_error) still produce a measurement.
    """
    if not endpoint or not endpoint.strip():
        raise ValueError("endpoint must be a non-empty stable identifier")
    if not method or not method.strip():
        raise ValueError("method must be a non-empty HTTP method")
    if status_code is not None and not isinstance(status_code, int):
        raise TypeError(f"status_code must be int or None, got {type(status_code).__name__}")
    if not isinstance(elapsed_ms, int):
        raise TypeError(f"elapsed_ms must be int, got {type(elapsed_ms).__name__}")
    if elapsed_ms < 0:
        raise ValueError("elapsed_ms must be >= 0")
    if payload_bytes is not None:
        if not isinstance(payload_bytes, int):
            raise TypeError(
                f"payload_bytes must be int or None, got {type(payload_bytes).__name__}"
            )
        if payload_bytes < 0:
            raise ValueError("payload_bytes must be >= 0")

    return {
        "endpoint": endpoint,
        "method": method,
        "status_code": status_code,
        "elapsed_ms": elapsed_ms,
        "payload_bytes": payload_bytes,
    }

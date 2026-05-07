"""Wave 2 §"Lease-integration boundary hardening" — Phase B (error translation).

Pins the contract that every error discriminant the BEAM lease-plane router
emits has a Python-side parser entry. Pre-Phase-B the parsers were nested
if-else chains and an unknown discriminant silently degraded to
`service_unavailable` — operators had to dig through Elixir router logs to
find the actual discriminant. Post-Phase-B the parsers are declarative tables
keyed on `error`, with a fallback that names the unknown discriminant in the
result's `reason` field so drift is visible.

These tests assert:
1. The Python translation tables (`_ACQUIRE_ERROR_PARSERS`, etc.) are
   supersets of the documented BEAM-emitted error sets — drift surfaces here
   before it surfaces in production.
2. An unknown discriminant produces a `service_unavailable` result whose
   `reason` field names the unknown value (registry-drift visibility).
3. Each known discriminant still parses to the correct typed model
   (no regression from the refactor).

The BEAM-side authoritative emit set lives in
`elixir/lease_plane/lib/unitares_lease_plane/{http_router,http_auth}.ex`
and is mirrored as `BEAM_EMITTED_*_ERRORS` in `src/lease_plane/client.py`.
Any new BEAM error variant must update both sides in the same PR (Stability
discipline).
"""

from __future__ import annotations

import pytest

from src.lease_plane import (
    AcquireHeldByOther,
    AcquireOk,
    AcquirePermissionDenied,
    AcquireSchemaInvalid,
    AcquireServiceUnavailable,
    SimpleError,
    SimpleOk,
    StatusOk,
    StatusSchemaInvalid,
    StatusServiceUnavailable,
)
from src.lease_plane.client import (
    BEAM_EMITTED_ACQUIRE_ERRORS,
    BEAM_EMITTED_SIMPLE_ERRORS,
    BEAM_EMITTED_STATUS_ERRORS,
    _ACQUIRE_ERROR_PARSERS,
    _SIMPLE_ACCEPTED_ERRORS,
    _STATUS_ERROR_PARSERS,
    _parse_acquire,
    _parse_simple,
    _parse_status,
)


# ============================================================================
# Coverage: every BEAM-emitted discriminant has a Python parser
# ============================================================================


def test_acquire_parser_covers_every_beam_emitted_acquire_error():
    """Drift guard: if the BEAM router adds a new acquire-error discriminant
    (e.g., `rate_limited`, `quota_exceeded`), the Python parser table MUST be
    extended in the same PR. Without this assertion, the new discriminant
    would silently fall through to `service_unavailable` and the typed
    result loses fidelity."""
    missing = BEAM_EMITTED_ACQUIRE_ERRORS - set(_ACQUIRE_ERROR_PARSERS.keys())
    assert not missing, (
        f"BEAM emits acquire errors {sorted(missing)} that the Python "
        f"_ACQUIRE_ERROR_PARSERS table doesn't map. Add them or update "
        f"BEAM_EMITTED_ACQUIRE_ERRORS if those discriminants were retired."
    )


def test_status_parser_covers_every_beam_emitted_status_error():
    missing = BEAM_EMITTED_STATUS_ERRORS - set(_STATUS_ERROR_PARSERS.keys())
    assert not missing, (
        f"BEAM emits status errors {sorted(missing)} that the Python "
        f"_STATUS_ERROR_PARSERS table doesn't map."
    )


def test_simple_parser_covers_every_beam_emitted_simple_error():
    missing = BEAM_EMITTED_SIMPLE_ERRORS - _SIMPLE_ACCEPTED_ERRORS
    assert not missing, (
        f"BEAM emits simple-shape errors {sorted(missing)} that the Python "
        f"_SIMPLE_ACCEPTED_ERRORS set doesn't map."
    )


# ============================================================================
# Unknown-discriminant fallback (registry-drift visibility)
# ============================================================================


def test_acquire_unknown_discriminant_surfaces_in_reason():
    """An unknown discriminant from the BEAM (e.g., a future error variant
    that landed on the server before the client deploy) must NOT silently
    coerce to a flat service_unavailable. The result's `reason` field must
    name the unknown discriminant so the operator can grep for it."""
    result = _parse_acquire({"ok": False, "error": "some_future_variant"})
    assert isinstance(result, AcquireServiceUnavailable)
    assert result.reason is not None
    assert "some_future_variant" in result.reason
    assert "registry drift" in result.reason.lower() or "unrecognized" in result.reason.lower()


def test_status_unknown_discriminant_surfaces_in_reason():
    result = _parse_status({"ok": False, "error": "some_future_variant"})
    assert isinstance(result, StatusServiceUnavailable)
    assert result.reason is not None
    assert "some_future_variant" in result.reason


def test_simple_unknown_discriminant_surfaces_in_reason():
    result = _parse_simple({"ok": False, "error": "some_future_variant"})
    assert isinstance(result, SimpleError)
    assert result.error == "service_unavailable"
    assert result.reason is not None
    assert "some_future_variant" in result.reason


def test_missing_error_discriminant_surfaces_named_reason():
    """A response with `ok: false` but no `error` field at all (malformed
    BEAM output, schema bug) gets a named reason rather than silently
    swallowed."""
    result = _parse_acquire({"ok": False})
    assert isinstance(result, AcquireServiceUnavailable)
    assert result.reason is not None
    assert "missing" in result.reason.lower()


# ============================================================================
# Known-discriminant regression: each Phase-A behavior preserved post-refactor
# ============================================================================


def test_acquire_known_discriminants_still_parse_to_correct_models():
    """Spot-check each acquire-error variant routes to the right typed
    model post-refactor. The if-else → table swap must not silently
    change classifications."""
    samples = [
        (
            {
                "ok": False,
                "error": "held_by_other",
                "surface_id": "dialectic:/x",
                "blocking_lease_id": "11111111-1111-1111-1111-111111111111",
                "held_by_uuid": "22222222-2222-2222-2222-222222222222",
                "expires_at": "2026-05-08T00:00:00+00:00",
                "retry_after_hint_ms": 250,
            },
            AcquireHeldByOther,
        ),
        (
            {"ok": False, "error": "permission_denied", "reason": "role_holders_unsupported"},
            AcquirePermissionDenied,
        ),
        (
            {"ok": False, "error": "schema_invalid", "detail": "surface_id required"},
            AcquireSchemaInvalid,
        ),
        (
            {"ok": False, "error": "service_unavailable"},
            AcquireServiceUnavailable,
        ),
    ]
    for payload, expected_cls in samples:
        result = _parse_acquire(payload)
        assert isinstance(result, expected_cls), (
            f"discriminant {payload['error']!r} should parse to "
            f"{expected_cls.__name__}, got {type(result).__name__}"
        )


def test_acquire_ok_path_unchanged_by_refactor():
    """The success path stays an AcquireOk — refactor only touched the
    error branches."""
    payload = {
        "ok": True,
        "lease": {
            "lease_id": "00000000-0000-0000-0000-000000000001",
            "surface_id": "dialectic:/x",
            "surface_kind": "dialectic",
            "holder_agent_uuid": "11111111-1111-1111-1111-111111111111",
            "holder_class": "process_instance",
            "holder_kind": "local_beam",
            "heartbeat_required": False,
            "expires_at": "2026-05-08T00:00:00+00:00",
            "original_ttl_s": 60,
        },
        "idempotent": False,
        "drift_warning": [],
    }
    result = _parse_acquire(payload)
    assert isinstance(result, AcquireOk)


def test_status_ok_with_null_lease_unchanged_by_refactor():
    """The 404-as-typed-absence shape (200 + ok:true + lease:null) — must
    NOT be misclassified as an error post-refactor."""
    result = _parse_status({"ok": True, "lease": None})
    assert isinstance(result, StatusOk)
    assert result.lease is None


def test_simple_known_discriminants_still_parse():
    """Each currently-accepted simple-shape error parses to SimpleError
    with the right `error` discriminant preserved."""
    for discriminant in _SIMPLE_ACCEPTED_ERRORS:
        result = _parse_simple({"ok": False, "error": discriminant, "reason": "test"})
        assert isinstance(result, SimpleError)
        assert result.error == discriminant
        assert result.reason == "test"


def test_simple_ok_path_unchanged_by_refactor():
    result = _parse_simple({"ok": True})
    assert isinstance(result, SimpleOk)


# ============================================================================
# Validation-error fallback unchanged
# ============================================================================


def test_acquire_validation_error_falls_back_to_schema_invalid():
    """When a known discriminant is present but the rest of the payload
    fails the typed model's validation (e.g., held_by_other missing
    required fields), the parser falls back to AcquireSchemaInvalid with
    the Pydantic error detail. Pre-refactor behavior, pinned here."""
    # held_by_other missing all the required fields below the discriminant
    result = _parse_acquire({"ok": False, "error": "held_by_other"})
    assert isinstance(result, AcquireSchemaInvalid)
    assert result.detail is not None  # Pydantic error list


# ============================================================================
# Drift smoke check between docstrings and constants
# ============================================================================


def test_beam_emitted_acquire_errors_constant_is_nonempty_and_ascii():
    """Pin the literal contents so a future find/replace can't silently
    rename a discriminant. The Elixir router test pins the same strings
    on the other side."""
    assert BEAM_EMITTED_ACQUIRE_ERRORS == frozenset({
        "held_by_other",
        "permission_denied",
        "schema_invalid",
        "service_unavailable",
    })


def test_beam_emitted_status_errors_constant_is_documented_set():
    assert BEAM_EMITTED_STATUS_ERRORS == frozenset({
        "schema_invalid",
        "service_unavailable",
    })


def test_beam_emitted_simple_errors_constant_is_documented_set():
    assert BEAM_EMITTED_SIMPLE_ERRORS == frozenset({
        "not_found",
        "expired",
        "permission_denied",
        "schema_invalid",
        "service_unavailable",
    })

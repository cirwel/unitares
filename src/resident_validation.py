"""Compatibility shim for the resident-validation model API.

Canonical imports live in :mod:`src.evaluation.resident_validation.model`.
"""

from src.evaluation.resident_validation.model import (
    DEFAULT_ALLOWED_EFFECTS as DEFAULT_ALLOWED_EFFECTS,
    FORBIDDEN_EFFECTS as FORBIDDEN_EFFECTS,
    VALID_ROLES as VALID_ROLES,
    ResidentProfile as ResidentProfile,
    build_process_update_kwargs as build_process_update_kwargs,
    build_tick_envelope as build_tick_envelope,
    stable_tick_id as stable_tick_id,
)

__all__ = (
    "DEFAULT_ALLOWED_EFFECTS",
    "FORBIDDEN_EFFECTS",
    "ResidentProfile",
    "VALID_ROLES",
    "build_process_update_kwargs",
    "build_tick_envelope",
    "stable_tick_id",
)

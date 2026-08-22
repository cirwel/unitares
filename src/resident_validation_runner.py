"""Compatibility shim for the resident-validation runner API.

Canonical imports live in :mod:`src.evaluation.resident_validation.runner`.
"""

from src.evaluation.resident_validation.runner import (
    append_ticks as append_ticks,
    build_canary_ticks as build_canary_ticks,
    next_tick_index as next_tick_index,
)

__all__ = ("append_ticks", "build_canary_ticks", "next_tick_index")

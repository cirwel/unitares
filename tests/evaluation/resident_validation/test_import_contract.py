"""Compatibility contracts for the resident-validation package move."""

from __future__ import annotations

from types import ModuleType

import pytest

import src.evaluation.resident_validation.invocation as canonical_invocation
import src.evaluation.resident_validation.model as canonical_model
import src.evaluation.resident_validation.runner as canonical_runner
import src.resident_validation as legacy_model
import src.resident_validation_invocation as legacy_invocation
import src.resident_validation_runner as legacy_runner


@pytest.mark.parametrize(
    ("legacy", "canonical"),
    (
        (legacy_model, canonical_model),
        (legacy_runner, canonical_runner),
        (legacy_invocation, canonical_invocation),
    ),
)
def test_legacy_modules_reexport_canonical_public_api(
    legacy: ModuleType,
    canonical: ModuleType,
) -> None:
    """Every supported old-path object is the canonical object, not a copy."""
    assert legacy.__all__ == canonical.__all__
    assert canonical.__all__
    assert len(canonical.__all__) == len(set(canonical.__all__))
    for name in canonical.__all__:
        assert getattr(legacy, name) is getattr(canonical, name)

"""Descriptive provenance for identity-creation entry paths.

``onboard_origin`` records which client path initiated an onboard request.  It
is observability-only caller context: it is not identity proof and must never
influence assurance, authorization, lineage, or governance policy.
"""

from __future__ import annotations

from typing import Literal, cast


OnboardOrigin = Literal["agent", "harness_backstop", "orchestrated_resume"]
OnboardOriginBasis = Literal["explicit_argument", "default_unmarked_call"]
ONBOARD_ORIGINS = frozenset({"agent", "harness_backstop", "orchestrated_resume"})
ONBOARD_ORIGIN_BASES = frozenset({"explicit_argument", "default_unmarked_call"})
DEFAULT_ONBOARD_ORIGIN: OnboardOrigin = "agent"


def normalize_onboard_origin(value: object | None) -> OnboardOrigin:
    """Return a validated onboarding entry-path label.

    An omitted label is the ordinary explicit tool-call path (``agent``).
    Adapter-managed paths report one of the other values explicitly.  The
    value remains descriptive caller context, not authenticated authorship.
    """

    if value is None:
        return DEFAULT_ONBOARD_ORIGIN
    if not isinstance(value, str) or value not in ONBOARD_ORIGINS:
        allowed = ", ".join(sorted(ONBOARD_ORIGINS))
        raise ValueError(f"onboard_origin must be one of: {allowed}")
    return cast(OnboardOrigin, value)


def normalize_onboard_origin_basis(value: object) -> OnboardOriginBasis:
    """Return a validated server-derived basis for the origin label."""

    if not isinstance(value, str) or value not in ONBOARD_ORIGIN_BASES:
        allowed = ", ".join(sorted(ONBOARD_ORIGIN_BASES))
        raise ValueError(f"onboard_origin_basis must be one of: {allowed}")
    return cast(OnboardOriginBasis, value)

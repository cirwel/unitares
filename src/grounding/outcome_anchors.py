"""Stage 0 — exogenous anchor tiering for ``audit.outcome_events``.

The EISV maths roadmap (docs/proposals/eisv-maths-roadmap-v0.md) needs an
*exogenous* anchor: a signal that comes from outside the governance loop, so the
loop's references stay externally falsifiable. The substrate already exists —
``audit.outcome_events`` carries per-agent outcomes with an ``is_bad`` label, the
EISV state at the outcome moment, and a ``verification_source`` provenance field.

But (recon 2026-06-25) ~88% of those rows are **self-referential** — the
governance loop validating its own trajectories (``server_observation`` /
``trajectory_validated``). Roadmap **Invariant 4**: *a signal derived from the
loop cannot anchor the loop.* This module is the single place that maps a
``verification_source`` to a trust tier and exposes the canonical filter that the
outcome-gated baseline update (§4b) and B's falsifiability gate (§6) read from.
Centralising it prevents a future caller from anchoring on the echo by accident.

Tier mapping (from the measured provenance distribution):

    external_signal             -> TRUSTED_EXTERNAL  (task/test outcomes verified
                                                      outside the loop)
    agent_reported_tool_result  -> SOFT_SELF_ATTESTED (the agent attests its own
                                                       result — gameable)
    server_observation          -> EXCLUDED  (the loop observing itself)
    <null> / anything else      -> EXCLUDED  (unknown provenance can't anchor)

Only TRUSTED_EXTERNAL counts as an anchor by default. SOFT may be opted in for
analyses that tolerate self-attestation, but never silently.

Gold-vs-strong separation *within* ``external_signal`` (operator correction vs
CI vs verified tool failure) is a later refinement — it likely lives in the
``detail`` jsonb and is not yet distinguished here.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class AnchorTier(str, Enum):
    """Trust tier of an outcome's provenance (roadmap §7)."""

    TRUSTED_EXTERNAL = "trusted_external"      # external_signal — exogenous
    SOFT_SELF_ATTESTED = "soft_self_attested"  # agent_reported_tool_result — gameable
    EXCLUDED = "excluded"                       # self-referential / unknown provenance


# Verification-source string -> tier. Anything not listed (including NULL) is
# EXCLUDED: unknown provenance cannot anchor the loop (Invariant 4).
_TIER_BY_SOURCE = {
    "external_signal": AnchorTier.TRUSTED_EXTERNAL,
    "agent_reported_tool_result": AnchorTier.SOFT_SELF_ATTESTED,
    "server_observation": AnchorTier.EXCLUDED,  # explicit: the loop observing itself
}


def tier_for_source(verification_source: Optional[str]) -> AnchorTier:
    """Classify a ``verification_source`` into its trust tier.

    NULL/empty/unknown -> EXCLUDED (provenance we cannot vouch for is not an
    anchor). ``server_observation`` is mapped EXCLUDED explicitly because it is
    the loop's self-validation — the single most common value and the one that
    would silently build the echo chamber if treated as an outcome.
    """
    if not verification_source:
        return AnchorTier.EXCLUDED
    return _TIER_BY_SOURCE.get(verification_source, AnchorTier.EXCLUDED)


def is_exogenous_anchor(
    verification_source: Optional[str],
    *,
    include_soft: bool = False,
) -> bool:
    """True if this outcome may anchor the loop.

    Default = TRUSTED_EXTERNAL only. ``include_soft=True`` also admits
    agent-self-attested outcomes — never the default, and callers must opt in
    explicitly so self-attestation is a visible choice, not an accident.
    """
    tier = tier_for_source(verification_source)
    if tier is AnchorTier.TRUSTED_EXTERNAL:
        return True
    if include_soft and tier is AnchorTier.SOFT_SELF_ATTESTED:
        return True
    return False


# --- Canonical SQL predicates -------------------------------------------------
# The single source of truth for "which outcome_events rows may anchor". Use
# these in any query that feeds a baseline update or a falsifiability gate, so
# the Invariant-4 exclusion is applied uniformly and is greppable.

#: Externally-anchored outcomes only (default — the honest anchor set).
ANCHORED_OUTCOMES_SQL = "verification_source = 'external_signal'"

#: Externally-anchored + soft self-attested (opt-in; tolerate gameable signal).
ANCHORED_OUTCOMES_WITH_SOFT_SQL = (
    "verification_source IN ('external_signal', 'agent_reported_tool_result')"
)

#: Rows that must NEVER anchor — self-referential or unknown provenance. Useful
#: for an assertion / audit that nothing leaked the loop's self-validation in.
EXCLUDED_OUTCOMES_SQL = (
    "(verification_source IS NULL "
    "OR verification_source NOT IN ('external_signal', 'agent_reported_tool_result'))"
)


def anchored_outcomes_predicate(*, include_soft: bool = False) -> str:
    """Return the SQL predicate selecting anchorable outcome rows."""
    return ANCHORED_OUTCOMES_WITH_SOFT_SQL if include_soft else ANCHORED_OUTCOMES_SQL

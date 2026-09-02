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

from collections.abc import Mapping
from enum import Enum
import json
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

_CONTROLLED_FIXTURE_FLAGS = frozenset({
    "synthetic_calibration_fixture",
    "synthetic_negative_control",
    "do_not_use_for_live_validation",
    "do_not_persist",
    "calibration_excluded",
})
_CONTROLLED_FIXTURE_BINDINGS = frozenset({"synthetic_negative_control"})
_CONTROLLED_FIXTURE_TEST_NAMES = frozenset({"clean_control", "overconfidence_probe"})
# Every explicit fixture marker except ``calibration_excluded``, which the
# outcome writer also stamps for two non-fixture causes (see the rules below).
_EXPLICIT_FIXTURE_FLAGS = _CONTROLLED_FIXTURE_FLAGS - {"calibration_excluded"}

# Fixture rules.
#
# ``calibration_excluded`` is stamped by the outcome writer for three causes:
# the row declares itself a controlled fixture, it is a Phase-5 ``shadow_write``
# row, or the server had to scrape the confidence (the caller sent none and no
# registered prediction resolved). All three must keep calibration from
# training on the row. Only the first two are fixture traffic. Reading the flag
# as a fixture marker dropped every scraped-confidence row from the validation
# instruments (issue #1790; decision packet
# docs/proposals/outcome-fixture-conflation-decision-packet-v0.md).
#
# ``registered``: the flag is a fixture marker, whatever its cause. This is the
#   predicate the 2026-12-01 pre-registered read was registered with, and the
#   frozen 2026-08-09 read applied. Protocol-bound reads pin it.
# ``corrected``: the flag is a fixture marker unless the exclusion is solely a
#   scraped confidence. Rows recorded through the outcome_event recorder say
#   why they were excluded (``calibration_exclusion_reasons``); rows written
#   before that key existed are classified by ``prediction_source``. Rows the
#   Phase-5 writers insert through ``record_outcome_event`` directly carry
#   neither key: they are never excluded by the flag under either rule, and
#   their calibration path does not consult it, so this rule does not reach
#   them. For rows recorded through the outcome_event recorder, calibration
#   still never trains on an excluded row: this rule changes what the
#   validation instruments count, not what calibration learns from. Opt-in
#   (`--fixture-rule corrected`): the shared default stays `registered`, so no
#   cohort or statistic moves on a deploy (report headers and additive summary
#   keys do change) and the pre-registered read keeps the predicate it was
#   registered with; the packet's Selection block records why the default was
#   not flipped.
REGISTERED_FIXTURE_RULE = "registered"
CORRECTED_FIXTURE_RULE = "corrected"
FIXTURE_RULES = (REGISTERED_FIXTURE_RULE, CORRECTED_FIXTURE_RULE)
DEFAULT_FIXTURE_RULE = REGISTERED_FIXTURE_RULE

# Prediction sources where the SERVER supplied the confidence, not the caller.
# The writer keys the scraped-confidence exclusion on these (see
# outcome_events.py for the measurement behind the rule).
SCRAPED_PREDICTION_SOURCES = frozenset({
    "prev_confidence_fallback",
    "audit_trail_fallback",
})
EXCLUSION_REASONS_KEY = "calibration_exclusion_reasons"
EXCLUSION_REASON_FIXTURE = "controlled_fixture"
EXCLUSION_REASON_SHADOW = "shadow_write"
EXCLUSION_REASON_SCRAPED = "scraped_confidence"


def normalize_fixture_rule(rule: object) -> str:
    """Return a valid fixture rule or raise; an unknown rule never passes silently."""
    text = str(rule).strip().lower() if rule is not None else ""
    if text not in FIXTURE_RULES:
        raise ValueError(
            f"unknown fixture rule {rule!r}; expected one of {', '.join(FIXTURE_RULES)}"
        )
    return text


def _truthy_fixture_flag(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return False


def _parse_detail(detail: Mapping[str, object] | str | None) -> Mapping[str, object] | None:
    if isinstance(detail, str):
        try:
            parsed = json.loads(detail)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return detail if isinstance(detail, Mapping) else None


def has_explicit_fixture_marker(detail: Mapping[str, object] | str | None) -> bool:
    """True when the row carries a fixture marker other than ``calibration_excluded``."""
    parsed = _parse_detail(detail)
    if parsed is None:
        return False
    if any(_truthy_fixture_flag(parsed.get(flag)) for flag in _EXPLICIT_FIXTURE_FLAGS):
        return True
    if parsed.get("prediction_binding") in _CONTROLLED_FIXTURE_BINDINGS:
        return True
    return parsed.get("test_name") in _CONTROLLED_FIXTURE_TEST_NAMES


def is_scraped_only_exclusion(detail: Mapping[str, object] | str | None) -> bool:
    """True when the row IS excluded and the exclusion is attributable solely to a scraped confidence.

    A row that is not excluded at all (``calibration_excluded`` falsy) is never
    scraped-only, whatever its ``prediction_source`` says. Rows written since
    the writer records ``calibration_exclusion_reasons`` are judged by that
    list. Older rows are judged by ``prediction_source``: the writer only ever
    scraped from the two fallback sources. Any explicit fixture marker or a
    ``shadow_write`` flag wins over both, so a fixture that also happened to be
    scraped stays a fixture. One historical shape stays ambiguous: a
    caller-supplied bare ``calibration_excluded`` fixture that was also scraped
    before reasons existed reads as scraped-only here.
    """
    parsed = _parse_detail(detail)
    if parsed is None:
        return False
    if not _truthy_fixture_flag(parsed.get("calibration_excluded")):
        return False
    if has_explicit_fixture_marker(parsed):
        return False
    if _truthy_fixture_flag(parsed.get("shadow_write")):
        return False
    reasons = parsed.get(EXCLUSION_REASONS_KEY)
    if isinstance(reasons, (list, tuple)):
        return {str(reason) for reason in reasons} == {EXCLUSION_REASON_SCRAPED}
    if _truthy_fixture_flag(parsed.get("confidence_scraped")):
        return True
    return parsed.get("prediction_source") in SCRAPED_PREDICTION_SOURCES


def is_structurally_controlled_fixture(
    detail: Mapping[str, object] | str | None,
    *,
    rule: str = DEFAULT_FIXTURE_RULE,
) -> bool:
    """Return whether immutable outcome detail marks a controlled fixture.

    This deliberately ignores mutable identity metadata and free-form purpose.
    It is safe for prospective/frozen evidence consumers that need fixture
    attrition without letting the measured subject opt itself out later.

    ``rule`` selects how ``calibration_excluded`` is read (see the module
    comment): ``registered`` treats it as a fixture marker whatever its cause;
    ``corrected`` treats it as one unless the exclusion is solely a scraped
    confidence. An unknown rule raises rather than defaulting.
    """
    rule = normalize_fixture_rule(rule)
    parsed = _parse_detail(detail)
    if parsed is None:
        return False
    if has_explicit_fixture_marker(parsed):
        return True
    if _truthy_fixture_flag(parsed.get("calibration_excluded")):
        if rule == REGISTERED_FIXTURE_RULE:
            return True
        return not is_scraped_only_exclusion(parsed)
    return False


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


def is_anchorable(
    verification_source: Optional[str],
    *,
    eisv_present: bool,
    snapshot_missing: bool = False,
    include_soft: bool = False,
) -> bool:
    """True if an outcome row may anchor the residual/falsifiability test.

    Full anchorability = exogenous provenance (``is_exogenous_anchor``) AND a
    joinable EISV snapshot at outcome time. The row-level twin of
    ``ANCHORED_OUTCOMES_SQL``: a trusted-provenance row with no state has nothing
    to compute a residual against (roadmap §6.3), and snapshot-less synthetic
    harness traffic must never train the gate. ``eisv_present`` is whether the row
    carries an EISV vector (e.g. ``eisv_e is not None``); ``snapshot_missing``
    mirrors the ``detail.snapshot_missing`` flag.
    """
    if not is_exogenous_anchor(verification_source, include_soft=include_soft):
        return False
    return bool(eisv_present) and not snapshot_missing


# --- Canonical SQL predicates -------------------------------------------------
# The single source of truth for "which outcome_events rows may anchor". Use
# these in any query that feeds a baseline update or a falsifiability gate, so
# both the Invariant-4 exclusion AND the joinability requirement are applied
# uniformly and greppably.
#
# Anchorability has two parts, both required:
#   1. exogenous provenance  (tier, below) — Invariant 4;
#   2. a joinable EISV snapshot at outcome time — roadmap §6.3.
# A trusted-provenance row with no state at outcome time cannot anchor the
# residual test: there is nothing to compute `measurement − reference` against.
# This is not a cosmetic filter — it is the §6.3 precondition. It also removes
# synthetic harness traffic (BEAM wiring smoke tests emit external_signal with
# snapshot_missing=true and no eisv_*), which must never train the gate. The
# snapshot bridge (db/mixins/tool_usage.py) attaches state for genuinely
# instrumented agents, so real outcomes pass; non-instrumented/synthetic ones
# correctly do not.

#: A row carries the EISV state needed to compute a residual at outcome time.
#: Required for any anchor — see module note above (roadmap §6.3).
JOINABLE_SNAPSHOT_SQL = (
    "(eisv_e IS NOT NULL "
    "AND coalesce((detail->>'snapshot_missing')::boolean, false) = false)"
)

_TRUSTED_SOURCE_SQL = "verification_source = 'external_signal'"
_TRUSTED_OR_SOFT_SOURCE_SQL = (
    "verification_source IN ('external_signal', 'agent_reported_tool_result')"
)

#: Externally-anchored outcomes only (default — the honest anchor set):
#: exogenous provenance AND a joinable snapshot.
ANCHORED_OUTCOMES_SQL = f"({_TRUSTED_SOURCE_SQL}) AND {JOINABLE_SNAPSHOT_SQL}"

#: Externally-anchored + soft self-attested (opt-in; tolerate gameable signal).
#: Still requires a joinable snapshot.
ANCHORED_OUTCOMES_WITH_SOFT_SQL = (
    f"({_TRUSTED_OR_SOFT_SOURCE_SQL}) AND {JOINABLE_SNAPSHOT_SQL}"
)

#: Rows that must NEVER anchor on *provenance* grounds — self-referential or
#: unknown source. Useful for an assertion / audit that nothing leaked the loop's
#: self-validation in. NB: this is provenance-only and is deliberately NOT the
#: complement of ANCHORED_OUTCOMES_SQL — a trusted-provenance row that simply
#: lacks a snapshot is neither an anchor nor a provenance leak (it is unjoinable,
#: a coverage gap, not an Invariant-4 violation).
EXCLUDED_OUTCOMES_SQL = (
    "(verification_source IS NULL "
    "OR verification_source NOT IN ('external_signal', 'agent_reported_tool_result'))"
)

#: Exogenous provenance only, snapshot NOT required.
#:
#: Use this — not ANCHORED_OUTCOMES_SQL — for consumers that read outcomes as
#: *behavioural evidence* rather than as residual anchors. The joinable-snapshot
#: requirement (§6.3) exists so a residual has state to compute against; a
#: consumer that only asks "did this agent's outcomes go well" needs the
#: Invariant-4 provenance exclusion but has no residual to join, and applying
#: the snapshot clause there would silently discard genuine outcomes.
#:
#: Deliberately excludes SOFT_SELF_ATTESTED, matching ``is_exogenous_anchor``'s
#: default. Admitting the soft tier into a *verdict* input would relocate the
#: loop rather than close it: the live consumer (db/mixins/tool_usage.py
#: get_recent_outcomes) ignores ``evidence_weight`` and the sensor counts rows
#: equally, so self-attested rows would carry the same force as CI results.
#: A consumer that genuinely wants soft evidence must weight by corroboration
#: grade, not merely admit it — hence no ``include_soft`` switch here.
EXOGENOUS_OUTCOMES_SQL = f"({_TRUSTED_SOURCE_SQL})"


def anchored_outcomes_predicate(
    *, include_soft: bool = False, table_alias: Optional[str] = None
) -> str:
    """Return the SQL predicate selecting anchorable outcome rows.

    Selects rows with exogenous provenance AND a joinable EISV snapshot — both
    are required (see module note; roadmap §6.3).

    ``table_alias`` qualifies the column references (``verification_source``,
    ``eisv_e``, ``detail``) with ``<alias>.`` so the predicate can be AND-ed into
    a query that aliases ``audit.outcome_events`` (e.g. ``... o`` in the skeptic
    report). With no alias the bare-column constants are returned unchanged.
    """
    base = ANCHORED_OUTCOMES_WITH_SOFT_SQL if include_soft else ANCHORED_OUTCOMES_SQL
    if not table_alias:
        return base
    a = f"{table_alias}."
    # The three column tokens are distinct and do not appear as substrings of one
    # another in the predicate, so targeted replacement is safe here.
    return (
        base
        .replace("verification_source", f"{a}verification_source")
        .replace("eisv_e", f"{a}eisv_e")
        .replace("detail->>", f"{a}detail->>")
    )

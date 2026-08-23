#!/usr/bin/env python3
"""Describe condition 3 without inventing a historical accrual denominator.

The registered 2026-12-01 EISV outcome-grounding gate requires at least 150 bad
clusters on the trusted slice. The frozen 2026-08-09 artifact reports the stock
present inside trailing 30- and 90-day windows at one cutoff. It does not report
two comparable censuses, so it cannot identify a longitudinal accrual rate.

The default command renders only what that frozen artifact supports:

* the remaining arithmetic for each registered lead;
* the blocks contributed by widening the lookback from 30 to 90 days; and
* ``INSUFFICIENT_LONGITUDINAL_EVIDENCE`` for a rate comparison.

The module-level longitudinal count-change comparator is available only when the caller
supplies two dated, paired lead-0/lead-30 cumulative censuses produced from one
registered-window lower bound and one named protocol fingerprint. The script
cannot verify that provenance, so every such result remains a planning scenario
rather than a forecast or a structural reachability proof. The command-line
interface deliberately does not accept census overrides.

For this registered gate, do not obtain a second live census before the read.
The optional comparison exists for independently frozen, methodologically
comparable inputs; its presence does not authorize an interim look.

This script is database-free. It changes no threshold, date, PASS condition,
interpretation rule, or kill criterion.

Usage:
    python3 scripts/analysis/support_reachability.py
    python3 scripts/analysis/support_reachability.py --json
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
import json
import sys
from typing import Any, Mapping, Sequence


# Frozen trusted-anchor read. Source:
# docs/operations/eisv-ablation-frozen-2026-08-09.md, "Overall rows".
FROZEN_CUTOFF = date(2026, 8, 9)
FROZEN_WINDOW_COUNTS_BY_LEAD: dict[int, dict[int, int]] = {
    0: {30: 29, 90: 29},
    30: {30: 28, 90: 28},
}

# Condition 3 and the read date. Source:
# docs/proposals/eisv-outcome-grounding-stop-rule-v0.md, "Pre-registered gate".
TARGET_BAD_CLUSTERS = 150
READ_DATE = date(2026, 12, 1)
REGISTERED_WINDOW_DAYS = 365
# Date-resolution representation only. The cohort fingerprint must pin the
# registered command's exact UTC cutoff and all other query rules.
REGISTERED_COHORT_START = READ_DATE - timedelta(days=REGISTERED_WINDOW_DAYS)

DAYS_PER_MONTH = 30.4375  # mean Gregorian month
INSUFFICIENT_LONGITUDINAL_EVIDENCE = "INSUFFICIENT_LONGITUDINAL_EVIDENCE"


@dataclass(frozen=True)
class SupportRequirement:
    """Conditional arithmetic from a count observed at one cutoff."""

    observed_blocks: int
    observed_through: date
    interval_to_read_days: int
    target_blocks: int
    conditional_gap_if_no_older_blocks: int
    conditional_required_per_month: float | None


@dataclass(frozen=True)
class LeadCountChangeScenario:
    """Conditional net eligible-count arithmetic for one lead."""

    lead_minutes: int
    start_blocks: int
    end_blocks: int
    net_eligible_count_change: int
    observed_days: int
    remaining_days: int
    target_blocks: int
    blocks_still_needed: int
    net_change_per_month: float
    required_per_month: float | None
    acceleration_required: float | None
    verdict: str
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PairedLongitudinalScenario:
    """Two comparable cumulative censuses covering both registered leads."""

    cohort_fingerprint: str
    fixed_cohort_start: date
    start_date: date
    end_date: date
    count_change_by_lead_minutes: dict[int, LeadCountChangeScenario]
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LookbackComparison:
    """What a wider retrospective window added at one fixed cutoff."""

    counts_by_window_days: dict[int, int]
    narrow_window_days: int
    wide_window_days: int
    distinct_cluster_keys_added: int
    lookback_invariant_at_cutoff: bool
    verdict: str
    reading: str


def _require_nonnegative(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def support_requirement(
    observed_blocks: int,
    *,
    observed_through: date = FROZEN_CUTOFF,
) -> SupportRequirement:
    """Return the remaining support arithmetic without estimating accrual."""
    _require_nonnegative("observed_blocks", observed_blocks)
    if observed_through > READ_DATE:
        raise ValueError("observed_through must not be after the registered read date")
    remaining_days = (READ_DATE - observed_through).days
    still_needed = max(0, TARGET_BAD_CLUSTERS - observed_blocks)
    required_per_month = (
        still_needed / remaining_days * DAYS_PER_MONTH
        if still_needed > 0 and remaining_days > 0
        else 0.0
        if still_needed == 0
        else None
    )
    return SupportRequirement(
        observed_blocks=observed_blocks,
        observed_through=observed_through,
        interval_to_read_days=remaining_days,
        target_blocks=TARGET_BAD_CLUSTERS,
        conditional_gap_if_no_older_blocks=still_needed,
        conditional_required_per_month=(
            round(required_per_month, 2) if required_per_month is not None else None
        ),
    )


def _validated_paired_counts(
    name: str,
    counts_by_lead: Mapping[int, int],
) -> dict[int, int]:
    counts = dict(counts_by_lead)
    registered_leads = set(FROZEN_WINDOW_COUNTS_BY_LEAD)
    if len(counts) != len(registered_leads) or set(counts) != registered_leads:
        raise ValueError(f"{name} must contain exactly the registered leads 0 and 30")
    for lead, count in counts.items():
        if isinstance(lead, bool) or not isinstance(lead, int):
            raise ValueError(f"{name} lead keys must be integers")
        _require_nonnegative(f"{name}[{lead}]", count)
    return counts


def _lead_count_change_scenario(
    *,
    lead_minutes: int,
    start_blocks: int,
    end_blocks: int,
    observed_days: int,
    remaining_days: int,
) -> LeadCountChangeScenario:
    net_change = end_blocks - start_blocks
    still_needed = max(0, TARGET_BAD_CLUSTERS - end_blocks)
    net_change_per_month = net_change / observed_days * DAYS_PER_MONTH

    if still_needed == 0:
        return LeadCountChangeScenario(
            lead_minutes=lead_minutes,
            start_blocks=start_blocks,
            end_blocks=end_blocks,
            net_eligible_count_change=net_change,
            observed_days=observed_days,
            remaining_days=remaining_days,
            target_blocks=TARGET_BAD_CLUSTERS,
            blocks_still_needed=0,
            net_change_per_month=round(net_change_per_month, 2),
            required_per_month=0.0,
            acceleration_required=0.0,
            verdict="TARGET_ALREADY_MET_AT_SECOND_CENSUS",
            notes=["the supplied second census meets the support threshold"],
        )

    if remaining_days == 0:
        return LeadCountChangeScenario(
            lead_minutes=lead_minutes,
            start_blocks=start_blocks,
            end_blocks=end_blocks,
            net_eligible_count_change=net_change,
            observed_days=observed_days,
            remaining_days=remaining_days,
            target_blocks=TARGET_BAD_CLUSTERS,
            blocks_still_needed=still_needed,
            net_change_per_month=round(net_change_per_month, 2),
            required_per_month=None,
            acceleration_required=None,
            verdict="REGISTERED_READ_REACHED_BELOW_TARGET",
            notes=["the supplied second census is the registered read"],
        )

    required_per_month = still_needed / remaining_days * DAYS_PER_MONTH
    acceleration = (
        required_per_month / net_change_per_month if net_change_per_month > 0 else None
    )
    if acceleration is None:
        verdict = "NO_NET_ELIGIBLE_COUNT_CHANGE"
        rate_note = "the supplied censuses contain no net eligible-count change"
    elif acceleration <= 1.0:
        verdict = "SUPPLIED_NET_CHANGE_PACE_WOULD_SUFFICE"
        rate_note = "continuing the supplied net-change pace would meet the target"
    else:
        verdict = "SUPPLIED_NET_CHANGE_PACE_WOULD_NOT_SUFFICE"
        rate_note = (
            f"meeting the target would require about {acceleration:.1f}x the "
            "supplied net-change pace"
        )

    return LeadCountChangeScenario(
        lead_minutes=lead_minutes,
        start_blocks=start_blocks,
        end_blocks=end_blocks,
        net_eligible_count_change=net_change,
        observed_days=observed_days,
        remaining_days=remaining_days,
        target_blocks=TARGET_BAD_CLUSTERS,
        blocks_still_needed=still_needed,
        net_change_per_month=round(net_change_per_month, 2),
        required_per_month=round(required_per_month, 2),
        acceleration_required=(
            round(acceleration, 2) if acceleration is not None else None
        ),
        verdict=verdict,
        notes=[rate_note],
    )


def longitudinal_count_change_comparison(
    *,
    cohort_fingerprint: str,
    fixed_cohort_start: date,
    start_counts_by_lead: Mapping[int, int],
    start_date: date,
    end_counts_by_lead: Mapping[int, int],
    end_date: date,
) -> PairedLongitudinalScenario:
    """Compare two complete, cumulative censuses under one claimed protocol.

    The counts must cover both registered leads and be cumulative from the
    registered 365-day slice's lower bound. One fingerprint applies to the
    entire pair so a caller cannot silently use different cohort, scope,
    fixture, harness-lane, clustering, or joinability rules between dates or
    leads. This function can validate arithmetic consistency, but it cannot
    authenticate provenance or distinguish new events from late/backfilled
    eligibility changes.
    """
    if not isinstance(cohort_fingerprint, str) or not cohort_fingerprint.strip():
        raise ValueError("cohort_fingerprint must be non-empty")
    start_counts = _validated_paired_counts(
        "start_counts_by_lead", start_counts_by_lead
    )
    end_counts = _validated_paired_counts("end_counts_by_lead", end_counts_by_lead)
    if fixed_cohort_start != REGISTERED_COHORT_START:
        raise ValueError(
            "fixed_cohort_start must equal the registered 365-day slice start"
        )
    if start_date < fixed_cohort_start:
        raise ValueError("start_date must not be before fixed_cohort_start")
    if end_date <= start_date:
        raise ValueError("end_date must be after start_date")
    if end_date > READ_DATE:
        raise ValueError("end_date must not be after the registered read date")

    observed_days = (end_date - start_date).days
    remaining_days = (READ_DATE - end_date).days
    count_changes: dict[int, LeadCountChangeScenario] = {}
    for lead in sorted(start_counts):
        if end_counts[lead] < start_counts[lead]:
            raise ValueError(
                f"end_counts_by_lead[{lead}] must not be less than "
                f"start_counts_by_lead[{lead}]"
            )
        count_changes[lead] = _lead_count_change_scenario(
            lead_minutes=lead,
            start_blocks=start_counts[lead],
            end_blocks=end_counts[lead],
            observed_days=observed_days,
            remaining_days=remaining_days,
        )

    return PairedLongitudinalScenario(
        cohort_fingerprint=cohort_fingerprint,
        fixed_cohort_start=fixed_cohort_start,
        start_date=start_date,
        end_date=end_date,
        count_change_by_lead_minutes=count_changes,
        notes=[
            "valid only if the fingerprint identifies identical cohort, scope, "
            "fixture, harness-lane, clustering, and joinability rules",
            "counts must be cumulative from the registered 365-day lower bound; "
            "the fingerprint must pin its exact UTC timestamp",
            "net eligible-count change is not necessarily event accrual; late or "
            "backfilled anchors and prior state can add keys",
            "planning scenario only; not a forecast or reachability proof",
        ],
    )


def lookback_comparison(counts_by_window: Mapping[int, int]) -> LookbackComparison:
    """Describe the annulus added by widening one fixed-cutoff lookback."""
    counts = dict(counts_by_window)
    if len(counts) < 2:
        raise ValueError("need at least two windows to compare")
    if any(
        isinstance(window, bool) or not isinstance(window, int) or window <= 0
        for window in counts
    ):
        raise ValueError("window days must be positive integers")
    if any(
        isinstance(count, bool) or not isinstance(count, int) or count < 0
        for count in counts.values()
    ):
        raise ValueError("window counts must be nonnegative integers")

    windows = sorted(counts)
    for narrow, wide in zip(windows, windows[1:]):
        if counts[wide] < counts[narrow]:
            raise ValueError("counts must not decrease as the lookback widens")

    narrow, wide = windows[0], windows[-1]
    gained = counts[wide] - counts[narrow]
    invariant = gained == 0
    verdict = (
        "NO_ADDITIONAL_DISTINCT_CLUSTER_KEYS_WITH_WIDER_LOOKBACK"
        if invariant
        else "ADDITIONAL_DISTINCT_CLUSTER_KEYS_WITH_WIDER_LOOKBACK"
    )
    reading = (
        f"At this cutoff, widening the lookback from {narrow} to {wide} days "
        f"added {gained} distinct cluster keys to the slice. Equal key counts "
        "do not imply an empty older annulus: older rows can reuse keys already "
        "present. This does not estimate future accrual or the registered "
        "365-day count."
    )
    return LookbackComparison(
        counts_by_window_days=counts,
        narrow_window_days=narrow,
        wide_window_days=wide,
        distinct_cluster_keys_added=gained,
        lookback_invariant_at_cutoff=invariant,
        verdict=verdict,
        reading=reading,
    )


def frozen_diagnostic() -> dict[str, Any]:
    """Return only the claims supported by the one-cutoff frozen artifact."""
    requirements: dict[int, SupportRequirement] = {}
    lookbacks: dict[int, LookbackComparison] = {}
    for lead, counts in FROZEN_WINDOW_COUNTS_BY_LEAD.items():
        requirements[lead] = support_requirement(
            counts[max(counts)],
        )
        lookbacks[lead] = lookback_comparison(counts)
    return {
        "verdict": INSUFFICIENT_LONGITUDINAL_EVIDENCE,
        "frozen_cutoff": FROZEN_CUTOFF,
        "read_date": READ_DATE,
        "requirements_by_lead_minutes": requirements,
        "lookback_by_lead_minutes": lookbacks,
        "notes": [
            "one cutoff cannot identify an accrual rate",
            "additional blocks assume the unmeasured 365-day slice adds no older "
            "eligible blocks",
            "no threshold, date, PASS condition, or kill criterion changes",
        ],
    }


def _jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "__dataclass_fields__"):
        return {key: _jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def render_frozen(diagnostic: Mapping[str, Any]) -> str:
    """Render the frozen one-cutoff diagnostic without a rate projection."""
    lines = ["== stop-rule support condition — frozen arithmetic =="]
    lines.append(f"verdict: {diagnostic['verdict']}")
    requirements = diagnostic["requirements_by_lead_minutes"]
    for lead in sorted(requirements):
        requirement = requirements[lead]
        lines.append(
            f"lead {lead}m: {requirement.observed_blocks} bad clusters; "
            f"conditional gap {requirement.conditional_gap_if_no_older_blocks} "
            f"over the {requirement.interval_to_read_days}-day interval from "
            f"the frozen cutoff "
            f"({requirement.conditional_required_per_month}/month)"
        )
    lines.append("  - one frozen cutoff does not identify an accrual rate")
    lines.append(
        "  - remaining counts are conditional on the unmeasured 365-day slice "
        "adding no older eligible blocks"
    )
    lines.append("")
    lines.append("== fixed-cutoff lookback comparison ==")
    for lead in sorted(diagnostic["lookback_by_lead_minutes"]):
        lookback = diagnostic["lookback_by_lead_minutes"][lead]
        lines.append(f"lead {lead}m: {lookback.reading}")
    lines.append("")
    lines.append(
        "This changes no threshold, date, PASS condition, or kill criterion. "
        "The registered read remains in force."
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    diagnostic = frozen_diagnostic()
    if args.json:
        print(json.dumps(_jsonable(diagnostic), indent=2, allow_nan=False))
    else:
        print(render_frozen(diagnostic))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Is the stop rule's support condition reachable before the read date?

`docs/proposals/eisv-outcome-grounding-stop-rule-v0.md` makes PASS conditional on
four things at once. Three are about signal. The fourth, condition 3, is about
supply: `Bad clusters >= 150` on the trusted slice. A read that misses it closes
outcome-grounding for insufficient eligible evidence -- correctly labelled in the
stop rule as "not a measured null or as disproof".

Nothing currently establishes whether that threshold is attainable by the read
date. The `1/sqrt(K)` accrual projection that would have answered it was
withdrawn on 2026-08-17 along with the contaminated cohort it rested on, and was
never replaced. So the read is scheduled without anyone having checked that its
PASS branch is reachable.

This is the same question `k_reachability` answers for the coherence gate in
`src/coherence_gate_shadow.py`, one layer up: before trusting an instrument's
verdict, establish that each verdict it can return is attainable at all. There, a
positive control could not FAIL. Here, a gate may not be able to PASS. Both are
the same defect -- a decision procedure with an unreachable branch -- and both
are cheap to check in advance.

Two checks, both pure arithmetic on already-published counts:

  * ACCRUAL -- the observed rate of trusted bad clusters against the rate the
    remaining time would require. Reports the ratio between them.
  * WINDOW INVARIANCE -- whether widening the analysis window materially raises
    the count. The frozen 2026-08-09 read gives this directly: 30d and 90d
    windows returned the same 28-29 clusters, so the trusted-anchor population is
    supply-limited rather than window-limited. That matters because the December
    command widens to `--windows 365`; if the corpus is supply-limited, the wider
    window does not supply the missing blocks.

WHAT THIS DOES NOT DO. It changes no threshold, date, PASS condition, or kill
criterion, and it carries no authority to retire anything -- per the measurement
rules in CLAUDE.md, a count may retire an instrument and never a capability. It
does not query any database. A required-rate ratio is a projection from one
operator's historical rate, NOT a forecast: accrual can change, and a ratio above
1.0 establishes only that the past rate would not have sufficed. It cannot show
the target WILL be missed. Its intended use is disclosure before the read, so the
operator chooses knowingly between spending the interval, moving the checkpoint
earlier, or changing the premise now rather than in December.

Usage:
    python3 scripts/analysis/support_reachability.py
    python3 scripts/analysis/support_reachability.py --json
    python3 scripts/analysis/support_reachability.py --observed-blocks 60 \
        --observed-through 2026-10-01
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Sequence

# --- Published inputs -------------------------------------------------------
#
# Every default below is a figure already recorded in the repository, not a new
# measurement. Sources are named so a reader can check each one.

# First identity record; the deployment's start. Source: README status line.
OBSERVATION_START = date(2025, 11, 28)

# Frozen trusted-anchor read. Source:
# docs/operations/eisv-ablation-frozen-2026-08-09.md, "Overall rows".
FROZEN_CUTOFF = date(2026, 8, 9)
FROZEN_BAD_CLUSTERS = 28          # 28-29 across slices; 28 is the conservative read
FROZEN_BAD_ROWS = 53
FROZEN_AGENTS = 16

# Condition 3 and the read date. Source:
# docs/proposals/eisv-outcome-grounding-stop-rule-v0.md, "Pre-registered gate".
TARGET_BAD_CLUSTERS = 150
READ_DATE = date(2026, 12, 1)

# Window-invariance evidence, same frozen table: widening 30d -> 90d.
FROZEN_WINDOW_COUNTS = {30: 28, 90: 28}

DAYS_PER_MONTH = 30.4375  # mean Gregorian month


@dataclass
class Reachability:
    """Observed accrual against the rate the remaining interval would require."""

    observed_blocks: int
    observed_days: int
    remaining_days: int
    target_blocks: int
    blocks_still_needed: int
    observed_per_month: float
    required_per_month: float
    acceleration_required: float | None
    verdict: str
    notes: List[str] = field(default_factory=list)


def accrual_reachability(
    observed_blocks: int = FROZEN_BAD_CLUSTERS,
    observation_start: date = OBSERVATION_START,
    observed_through: date = FROZEN_CUTOFF,
    target_blocks: int = TARGET_BAD_CLUSTERS,
    read_date: date = READ_DATE,
) -> Reachability:
    """Compare the observed accrual rate with the rate the target would require.

    Returns the ratio between them. A ratio of 1.0 means the observed rate is
    exactly sufficient; above 1.0 means the past rate would not have got there.
    """
    observed_days = (observed_through - observation_start).days
    remaining_days = (read_date - observed_through).days
    if observed_days <= 0:
        raise ValueError("observation window must be positive")

    still_needed = max(0, target_blocks - observed_blocks)
    observed_per_month = observed_blocks / observed_days * DAYS_PER_MONTH

    notes: List[str] = []
    if still_needed == 0:
        return Reachability(
            observed_blocks=observed_blocks,
            observed_days=observed_days,
            remaining_days=remaining_days,
            target_blocks=target_blocks,
            blocks_still_needed=0,
            observed_per_month=round(observed_per_month, 2),
            required_per_month=0.0,
            acceleration_required=0.0,
            verdict="ALREADY_MET",
            notes=["the support condition is already satisfied at this count"],
        )

    if remaining_days <= 0:
        return Reachability(
            observed_blocks=observed_blocks,
            observed_days=observed_days,
            remaining_days=remaining_days,
            target_blocks=target_blocks,
            blocks_still_needed=still_needed,
            observed_per_month=round(observed_per_month, 2),
            required_per_month=float("inf"),
            acceleration_required=None,
            verdict="NO_TIME_REMAINING",
            notes=["the read date has arrived or passed; no interval remains"],
        )

    required_per_month = still_needed / remaining_days * DAYS_PER_MONTH
    acceleration = (
        required_per_month / observed_per_month if observed_per_month > 0 else None
    )

    if acceleration is None:
        verdict = "NO_OBSERVED_ACCRUAL"
        notes.append("no blocks observed, so no rate can be projected")
    elif acceleration <= 1.0:
        verdict = "ON_TRACK_AT_OBSERVED_RATE"
        notes.append("the observed rate alone would reach the target")
    else:
        verdict = "REQUIRES_ACCELERATION"
        notes.append(
            f"reaching the target needs about {acceleration:.1f}x the observed rate"
        )
    notes.append(
        "a projection from the historical rate, not a forecast; it cannot show "
        "the target will be missed, only what rate would be needed"
    )

    return Reachability(
        observed_blocks=observed_blocks,
        observed_days=observed_days,
        remaining_days=remaining_days,
        target_blocks=target_blocks,
        blocks_still_needed=still_needed,
        observed_per_month=round(observed_per_month, 2),
        required_per_month=round(required_per_month, 2),
        acceleration_required=(
            round(acceleration, 2) if acceleration is not None else None
        ),
        verdict=verdict,
        notes=notes,
    )


def window_invariance(
    counts_by_window: Dict[int, int] | None = None,
) -> Dict[str, Any]:
    """Does widening the analysis window materially raise the cluster count?

    If tripling the window adds essentially nothing, the population is limited by
    what has been adjudicated rather than by how far back the query reaches --
    and a still wider window at read time will not supply the missing blocks.
    """
    counts = dict(counts_by_window or FROZEN_WINDOW_COUNTS)
    windows = sorted(counts)
    if len(windows) < 2:
        raise ValueError("need at least two windows to compare")

    narrow, wide = windows[0], windows[-1]
    gained = counts[wide] - counts[narrow]
    window_ratio = wide / narrow
    count_ratio = counts[wide] / counts[narrow] if counts[narrow] else None
    supply_limited = gained == 0

    return {
        "counts_by_window_days": counts,
        "window_widened_by": round(window_ratio, 2),
        "clusters_gained": gained,
        "count_ratio": round(count_ratio, 3) if count_ratio is not None else None,
        "supply_limited": supply_limited,
        "reading": (
            f"widening the window {window_ratio:.0f}x added {gained} clusters — "
            + (
                "the population is supply-limited, so a wider window at read time "
                "does not supply the missing blocks"
                if supply_limited
                else "the window still admits new clusters, so a wider window may "
                "raise the count"
            )
        ),
    }


def render(reach: Reachability, window: Dict[str, Any]) -> str:
    lines = ["== stop-rule support condition — reachability =="]
    lines.append(
        f"observed: {reach.observed_blocks} bad clusters over {reach.observed_days} days "
        f"({reach.observed_per_month}/month)"
    )
    lines.append(
        f"target:   {reach.target_blocks} bad clusters, "
        f"{reach.blocks_still_needed} still needed in {reach.remaining_days} days "
        f"({reach.required_per_month}/month)"
    )
    if reach.acceleration_required:
        lines.append(f"required acceleration: {reach.acceleration_required}x observed")
    lines.append(f"verdict: {reach.verdict}")
    for note in reach.notes:
        lines.append(f"  - {note}")
    lines.append("")
    lines.append("== window invariance ==")
    lines.append(f"  counts by window (days): {window['counts_by_window_days']}")
    lines.append(f"  {window['reading']}")
    lines.append("")
    lines.append(
        "This changes no threshold, date, PASS condition, or kill criterion, and "
        "retires nothing. It is disclosure before the read."
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observed-blocks", type=int, default=FROZEN_BAD_CLUSTERS)
    parser.add_argument(
        "--observed-through",
        type=date.fromisoformat,
        default=FROZEN_CUTOFF,
        help="date the observed count was taken (ISO)",
    )
    parser.add_argument(
        "--observation-start", type=date.fromisoformat, default=OBSERVATION_START
    )
    parser.add_argument("--target-blocks", type=int, default=TARGET_BAD_CLUSTERS)
    parser.add_argument("--read-date", type=date.fromisoformat, default=READ_DATE)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    reach = accrual_reachability(
        observed_blocks=args.observed_blocks,
        observation_start=args.observation_start,
        observed_through=args.observed_through,
        target_blocks=args.target_blocks,
        read_date=args.read_date,
    )
    window = window_invariance()
    if args.json:
        print(json.dumps({"accrual": vars(reach), "window": window}, indent=2, default=str))
    else:
        print(render(reach, window))
    return 0


if __name__ == "__main__":
    sys.exit(main())

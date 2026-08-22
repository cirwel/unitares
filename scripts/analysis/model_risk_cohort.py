#!/usr/bin/env python3
"""Build a prospective exact-model/harness risk cohort report.

The capture boundary is mandatory.  Rows before it, legacy flat ``model``
fields, and display-name proxies are never attributed to an exact model.

Example:
    python3 scripts/analysis/model_risk_cohort.py \
      --capture-start 2026-08-23T00:00:00Z \
      --capture-end 2026-08-30T00:00:00Z \
      --output data/analysis/model-risk-2026-08-30.md
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.db import close_db  # noqa: E402
from src.identity.model_risk_cohort import (  # noqa: E402
    build_model_risk_cohort_report,
    collect_model_risk_observations,
)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "timestamp must be ISO-8601, for example 2026-08-23T00:00:00Z"
        ) from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def render_markdown(report: dict) -> str:
    coverage = report["coverage"]
    readiness = report["comparison_readiness"]
    window = report["capture_window"]
    lines = [
        "# Exact model/harness risk cohorts (descriptive)",
        "",
        f"- capture window: `{window['start']}` to `{window['end']}`",
        f"- measured state rows: **{coverage['state_rows']}**",
        f"- exact attributed rows with risk: **{coverage['exact_attributed_rows']}**",
        "- exact rows with harness version unavailable: "
        f"**{coverage['harness_version_unavailable_exact_rows']}**",
        f"- comparison readiness: **{readiness['status']}**",
        "- authority: descriptive measurement only; no causal, identity, verdict, or policy authority",
        "- historical nulls: never attributed; only `s22.runtime_provenance.v1` rows can enter cohorts",
        "",
        "## Attribution coverage",
        "",
        "| status | rows |",
        "|---|--:|",
    ]
    for status, count in coverage["attribution_status"].items():
        lines.append(f"| `{status}` | {count} |")

    lines.extend(
        [
            "",
            "## Stratified cohorts",
            "",
            "| model | provider | harness | version | readiness | updates | task | exposure | n | mean | median | >=0.5 | >=0.7 |",
            "|---|---|---|---|---|---|---|---|--:|--:|--:|--:|--:|",
        ]
    )
    for cohort in report["cohorts"]:
        mean_value = (
            f"{cohort['mean_risk']:.3f}"
            if cohort["mean_risk"] is not None
            else "—"
        )
        median_value = (
            f"{cohort['median_risk']:.3f}"
            if cohort["median_risk"] is not None
            else "—"
        )
        lines.append(
            f"| `{cohort['model']}` | `{cohort['model_provider']}` | "
            f"`{cohort['harness_type']}` | `{cohort['harness_version']}` | "
            f"{cohort['readiness']} | {cohort['update_bucket']} | "
            f"{cohort['task_type']} | {cohort['exposure_bucket']} | "
            f"{cohort['n']} | {mean_value} | {median_value} | "
            f"{cohort['risk_ge_0_5']} | {cohort['risk_ge_0_7']} |"
        )
    if not report["cohorts"]:
        lines.append("| _(no eligible exact rows)_ | — | — | — | — | — | — | — | 0 | — | — | 0 | 0 |")

    lines.extend(["", "## Like-for-like warm cells", ""])
    if report["like_for_like_warm_cells"]:
        for cell in report["like_for_like_warm_cells"]:
            lines.append(
                f"- updates `{cell['update_bucket']}`, task `{cell['task_type']}`, "
                f"exposure `{cell['exposure_bucket']}`: "
                f"{len(cell['cohorts'])} model/harness cohorts meet "
                f"n >= {readiness['min_cell_size']}"
            )
    else:
        lines.append(
            f"No warm comparison cell has at least two model/harness cohorts "
            f"with n >= {readiness['min_cell_size']}."
        )

    if readiness["reasons"]:
        lines.extend(["", "Readiness gaps:"])
        lines.extend(f"- `{reason}`" for reason in readiness["reasons"])
    lines.extend(["", readiness["next_step"], ""])
    return "\n".join(lines)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--capture-start",
        required=True,
        type=_timestamp,
        help="Mandatory prospective capture boundary (timezone-aware ISO-8601).",
    )
    parser.add_argument(
        "--capture-end",
        type=_timestamp,
        default=None,
        help="Exclusive window end; defaults to now (UTC).",
    )
    parser.add_argument("--min-cell-size", type=int, default=10)
    parser.add_argument("--row-limit", type=int, default=100_000)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args(argv)
    capture_end = args.capture_end or datetime.now(timezone.utc)

    try:
        rows = await collect_model_risk_observations(
            capture_start=args.capture_start,
            capture_end=capture_end,
            row_limit=args.row_limit,
        )
    finally:
        await close_db()

    report = build_model_risk_cohort_report(
        rows,
        capture_start=args.capture_start.isoformat(),
        capture_end=capture_end.isoformat(),
        min_cell_size=args.min_cell_size,
        row_limit=args.row_limit,
    )
    output = json.dumps(report, indent=2, sort_keys=True) if args.json else render_markdown(report)
    if args.output:
        path = Path(os.path.abspath(args.output))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output + ("" if output.endswith("\n") else "\n"), encoding="utf-8")
        print(f"Wrote {path}")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

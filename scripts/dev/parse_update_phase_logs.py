#!/usr/bin/env python3
"""Parse process_agent_update phase timing lines from a log slice."""

from __future__ import annotations

import argparse
import re
import statistics
import sys
from collections.abc import Iterable
from pathlib import Path


ENRICHERS = (
    "enrich_knowledge_surfacing",
    "enrich_temporal_context",
    "enrich_learning_context",
    "enrich_mirror_signals",
)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return ordered[index]


def _stats(values: list[float]) -> str:
    if not values:
        return "n=0"
    return (
        f"n={len(values)} avg={statistics.mean(values):.1f}ms "
        f"p50={_percentile(values, 0.50):.0f}ms "
        f"p90={_percentile(values, 0.90):.0f}ms "
        f"p99={_percentile(values, 0.99):.0f}ms "
        f"max={max(values):.0f}ms"
    )


def _read_selected_lines(path: Path, start_line: int, end_line: int | None) -> Iterable[str]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if line_number < start_line:
                continue
            if end_line is not None and line_number > end_line:
                break
            yield line


def _extract_ms(line: str, key: str) -> float | None:
    match = re.search(rf"{re.escape(key)}=(\d+(?:\.\d+)?)ms", line)
    if match:
        return float(match.group(1))
    return None


def parse_log(
    path: Path,
    start_line: int,
    end_line: int | None,
) -> tuple[list[float], list[float], dict[str, list[float]], int]:
    checkin_totals: list[float] = []
    enrichment_totals: list[float] = []
    per_enricher: dict[str, list[float]] = {name: [] for name in ENRICHERS}
    enrichment_line_count = 0

    for line in _read_selected_lines(path, start_line, end_line):
        if "[checkin_phases]" in line:
            total = _extract_ms(line, "total")
            enrichment = _extract_ms(line, "enrichment")
            if total is not None:
                checkin_totals.append(total)
            if enrichment is not None:
                enrichment_totals.append(enrichment)
        elif "[enrichment_phases]" in line:
            enrichment_line_count += 1
            for enricher in ENRICHERS:
                duration = _extract_ms(line, enricher)
                if duration is not None:
                    per_enricher[enricher].append(duration)

    return checkin_totals, enrichment_totals, per_enricher, enrichment_line_count


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--start-line", type=int, default=1)
    parser.add_argument("--end-line", type=int)
    parser.add_argument("--label", default="phase log")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    checkin_totals, enrichment_totals, per_enricher, enrichment_line_count = parse_log(
        args.path,
        args.start_line,
        args.end_line,
    )

    line_range = f"{args.start_line}..{args.end_line}" if args.end_line else f"{args.start_line}..EOF"
    print(f"=== {args.label} ({line_range}, enrichment_lines={enrichment_line_count}) ===")
    print(f"checkin total: {_stats(checkin_totals)}")
    print(f"enrichment phase: {_stats(enrichment_totals)}")
    for enricher in ENRICHERS:
        print(f"  {enricher}: {_stats(per_enricher[enricher])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

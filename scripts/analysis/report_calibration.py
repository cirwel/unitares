#!/usr/bin/env python3
"""
Report strategic and tactical calibration bins from local state or Postgres.

Read-only tool supporting:
  --from-db           Read persisted calibration JSONB from ``core.calibration``
  --state-file PATH   Read an explicit JSON calibration state snapshot
  (default)           Inspect the local ``CalibrationChecker`` state file

Exits cleanly with a clear message when no calibration samples exist rather
than printing empty bins. Does not query ``audit.outcome_events``.

Usage:
  python3 scripts/analysis/report_calibration.py --from-db
  python3 scripts/analysis/report_calibration.py --state-file path/to/state.json
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

# Add project root to path
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.calibration import CalibrationChecker  # noqa: E402

DEFAULT_DB_URL = os.environ.get(
    "GOVERNANCE_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/governance",
)
ERROR_THRESHOLD = 0.2


def _default_connect(db_url: str) -> Any:
    import psycopg2

    return psycopg2.connect(db_url)


def _total_bin_samples(checker: CalibrationChecker) -> int:
    strategic = sum(int(s.get("count", 0)) for s in checker.bin_stats.values())
    tactical = sum(
        int(s.get("count", 0)) for s in getattr(checker, "tactical_bin_stats", {}).values()
    )
    by_channel = sum(
        int(s.get("count", 0))
        for channel_bins in getattr(checker, "tactical_bin_stats_by_channel", {}).values()
        for s in channel_bins.values()
    )
    complexity = sum(
        int(s.get("count", 0)) for s in getattr(checker, "complexity_stats", {}).values()
    )
    return strategic + tactical + by_channel + complexity


def _summarize_accuracy_bins(bins_map: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for bin_key in sorted(bins_map.keys()):
        stats = bins_map[bin_key]
        count = int(stats.get("count", 0))
        if count <= 0:
            continue
        predicted_correct = int(stats.get("predicted_correct", 0))
        actual_correct = int(stats.get("actual_correct", 0))
        accuracy = actual_correct / count
        expected_conf = float(stats.get("confidence_sum", 0.0)) / count
        calibration_error = abs(accuracy - expected_conf)
        rows.append(
            {
                "bin": bin_key,
                "count": count,
                "predicted_correct": predicted_correct,
                "actual_correct": actual_correct,
                "accuracy": accuracy,
                "expected_confidence": expected_conf,
                "calibration_error": calibration_error,
            }
        )
    return rows


def load_checker_from_db(
    db_url: str,
    *,
    connect: Any = None,
) -> tuple[CalibrationChecker, dict[str, Any]]:
    """Load persisted calibration snapshot read-only from ``core.calibration``."""
    conn_fn = connect or _default_connect
    conn = conn_fn(db_url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data, updated_at, version FROM core.calibration WHERE id = TRUE"
            )
            row = cur.fetchone()
    finally:
        conn.close()

    checker = CalibrationChecker()
    if not row or row[0] is None:
        checker.reset()
        return checker, {"source": "postgres:core.calibration", "version": None, "updated_at": None}

    raw_data, updated_at, version = row
    data = json.loads(raw_data) if isinstance(raw_data, str) else dict(raw_data)
    checker._apply_state_data(data)
    updated_str = updated_at.isoformat() if hasattr(updated_at, "isoformat") else updated_at
    return checker, {
        "source": "postgres:core.calibration",
        "version": version,
        "updated_at": updated_str,
    }


def load_checker_from_file(state_file: Path) -> tuple[CalibrationChecker, dict[str, Any]]:
    """Load calibration snapshot from an explicit JSON file."""
    resolved = state_file.resolve()
    with open(resolved, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in calibration state file: {resolved}")
    checker = CalibrationChecker(state_file=resolved)
    checker._apply_state_data(data)
    return checker, {
        "source": f"file:{resolved}",
        "version": data.get("_version"),
        "updated_at": data.get("_updated_at"),
    }


def build_report_payload(
    checker: CalibrationChecker,
    source_meta: Mapping[str, Any],
    *,
    min_samples: int = 5,
) -> dict[str, Any]:
    """Build structured report payload; marks empty state cleanly."""
    total_samples = _total_bin_samples(checker)
    if total_samples == 0:
        return {
            "empty": True,
            "source": source_meta.get("source"),
            "version": source_meta.get("version"),
            "updated_at": source_meta.get("updated_at"),
            "total_samples": 0,
            "message": f"No calibration samples found in {source_meta.get('source')}.",
        }

    strategic = _summarize_accuracy_bins(checker.bin_stats)
    tactical = _summarize_accuracy_bins(getattr(checker, "tactical_bin_stats", {}))

    per_channel_raw = checker.compute_tactical_metrics_per_channel()
    tactical_by_channel: dict[str, list[dict[str, Any]]] = {}
    for channel in sorted(per_channel_raw.keys()):
        items = []
        for bin_key in sorted(per_channel_raw[channel].keys()):
            cb = per_channel_raw[channel][bin_key]
            items.append(
                {
                    "bin": bin_key,
                    "count": cb.count,
                    "predicted_correct": cb.predicted_correct,
                    "actual_correct": cb.actual_correct,
                    "accuracy": cb.accuracy,
                    "expected_confidence": cb.expected_accuracy,
                    "calibration_error": cb.calibration_error,
                }
            )
        if items:
            tactical_by_channel[channel] = items

    complexity_rows = []
    for bin_key in sorted(getattr(checker, "complexity_stats", {}).keys()):
        stats = checker.complexity_stats[bin_key]
        count = int(stats.get("count", 0))
        if count <= 0:
            continue
        complexity_rows.append(
            {
                "bin": bin_key,
                "count": count,
                "mean_discrepancy": float(stats.get("discrepancy_sum", 0.0)) / count,
                "high_discrepancy_count": int(stats.get("high_discrepancy_count", 0)),
            }
        )

    is_calibrated, check_result = checker.check_calibration(min_samples_per_bin=min_samples)
    characterization = checker.characterize_failure_modes(min_samples=3)

    return {
        "empty": False,
        "source": source_meta.get("source"),
        "version": source_meta.get("version"),
        "updated_at": source_meta.get("updated_at"),
        "total_samples": total_samples,
        "strategic_bins": strategic,
        "tactical_bins": tactical,
        "tactical_bins_by_channel": tactical_by_channel,
        "complexity_bins": complexity_rows,
        "is_calibrated": is_calibrated,
        "check_summary": check_result,
        "failure_characterization": characterization,
    }


def _print_accuracy_section(
    title: str, subtitle: str, rows: Sequence[Mapping[str, Any]], acc_label: str
) -> None:
    print(title)
    print(f"   {subtitle}")
    print()
    if not rows:
        print("   No non-empty bins recorded.")
        print()
        return
    for row in rows:
        indicator = "[!]" if row["calibration_error"] > ERROR_THRESHOLD else "[ok]"
        print(f"   {row['bin']}:")
        print(f"      Count: {row['count']}")
        print(f"      Predicted correct: {row['predicted_correct']}")
        print(f"      Actual correct: {row['actual_correct']}")
        print(f"      {acc_label}: {row['accuracy']:.2%}")
        print(f"      Expected confidence: {row['expected_confidence']:.3f}")
        print(f"      Calibration error: {row['calibration_error']:.3f} {indicator}")
        print()


def render_text(payload: Mapping[str, Any]) -> str:
    """Format calibration report text."""
    if payload.get("empty"):
        lines = [
            f"CALIBRATION REPORT: {payload['message']}",
            "Hint: pass --from-db to read core.calibration from Postgres or --state-file PATH for a JSON snapshot.",
        ]
        return "\n".join(lines) + "\n"

    buf = io.StringIO()
    with redirect_stdout(buf):
        print("=" * 70)
        print("CALIBRATION REPORT - STRATEGIC & TACTICAL")
        print("=" * 70)
        source_line = f"Source: {payload['source']}"
        if payload.get("version") is not None:
            source_line += f" (version={payload['version']}"
            if payload.get("updated_at"):
                source_line += f", updated_at={payload['updated_at']}"
            source_line += ")"
        print(source_line)
        print()

        _print_accuracy_section(
            "STRATEGIC CALIBRATION (Trajectory Health)",
            "Measures: Do agents with high confidence end up in healthy states? (retroactive: YES)",
            payload["strategic_bins"],
            "Accuracy",
        )
        _print_accuracy_section(
            "TACTICAL CALIBRATION (Per-Decision)",
            "Measures: Are individual decisions correct at decision time? (retroactive: NO)",
            payload["tactical_bins"],
            "Decision accuracy",
        )

        by_channel = payload.get("tactical_bins_by_channel") or {}
        if by_channel:
            print("TACTICAL CALIBRATION BY CHANNEL")
            print()
            for channel, rows in by_channel.items():
                total_ch = sum(r["count"] for r in rows)
                print(f"   Channel `{channel}` (n={total_ch}):")
                for r in rows:
                    indicator = "[!]" if r["calibration_error"] > ERROR_THRESHOLD else "[ok]"
                    print(
                        f"      {r['bin']}: n={r['count']}, acc={r['accuracy']:.2%}, "
                        f"exp={r['expected_confidence']:.3f}, err={r['calibration_error']:.3f} {indicator}"
                    )
                print()

        print("=" * 70)
        print(f"Overall Status: {'CALIBRATED' if payload['is_calibrated'] else 'NOT CALIBRATED'}")
        print("=" * 70)
        print()
        print("FAILURE CHARACTERIZATION")
        print("-" * 70)
        characterization = payload["failure_characterization"]
        for dimension in ("strategic", "tactical"):
            dim_data = characterization.get(dimension, {})
            dim_status = dim_data.get("status", "no_data")
            label = (
                "STRATEGIC (Trajectory Health)"
                if dimension == "strategic"
                else "TACTICAL (Per-Decision)"
            )
            print(f"   {label}:")
            if dim_status in ("no_data", "insufficient_samples"):
                print(f"      {dim_status}")
            else:
                print(f"      ECE (Expected Calibration Error): {dim_data['ece']:.4f}")
                print(f"      Curve inverted: {'YES' if dim_data['curve_inverted'] else 'no'}")
                wb = dim_data["worst_bin"]
                print(
                    f"      Worst bin: {wb['bin']} "
                    f"(error={wb['calibration_error']:.3f}, "
                    f"expected={wb['expected']:.3f}, actual={wb['actual']:.3f})"
                )
                for bin_key, bin_info in dim_data.get("bins", {}).items():
                    diag = bin_info["diagnosis"]
                    indicator = {
                        "overconfident": "!",
                        "underconfident": "?",
                        "well_calibrated": "+",
                    }.get(diag, ".")
                    print(
                        f"      [{indicator}] {bin_key}: {diag} "
                        f"(expected={bin_info['expected']:.3f}, actual={bin_info['actual']:.3f}, "
                        f"n={bin_info['count']})"
                    )
            print()

        warning = characterization.get("verdict_quality_warning")
        if warning:
            print(f"   VERDICT QUALITY: {warning}")
        else:
            print("   VERDICT QUALITY: No systemic issues detected")

    return buf.getvalue()


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Report strategic and tactical calibration bins from local JSON state "
            "or Postgres core.calibration."
        )
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--from-db",
        action="store_true",
        help="Load persisted calibration state read-only from Postgres core.calibration.",
    )
    source_group.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Path to a calibration JSON state file.",
    )
    parser.add_argument(
        "--db-url",
        default=DEFAULT_DB_URL,
        help="Postgres connection URI used with --from-db.",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=5,
        help="Minimum samples per bin passed to check_calibration (default: 5).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None, *, connect: Any = None) -> int:
    args = parse_args(argv)

    if args.from_db:
        try:
            checker, source_meta = load_checker_from_db(args.db_url, connect=connect)
        except Exception as exc:
            print(f"Error loading calibration from Postgres: {exc}", file=sys.stderr)
            return 2
    elif args.state_file is not None:
        if not args.state_file.exists():
            print(f"Calibration state file not found: {args.state_file}", file=sys.stderr)
            return 2
        try:
            checker, source_meta = load_checker_from_file(args.state_file)
        except Exception as exc:
            print(
                f"Error loading calibration state file {args.state_file}: {exc}",
                file=sys.stderr,
            )
            return 2
    else:
        checker = CalibrationChecker()
        source_meta = {
            "source": (
                f"file:{checker.state_file}"
                if checker.state_file.exists()
                else f"missing:{checker.state_file}"
            ),
            "version": getattr(checker, "_loaded_json_version", None),
            "updated_at": None,
        }

    payload = build_report_payload(checker, source_meta, min_samples=args.min_samples)
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(render_text(payload), end="")
    return 0


def report_calibration() -> int:
    """Compatibility entry point for direct callers."""
    return main([])


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Measure synthetic AuditLogger JSONL costs without operational data or PG.

The real _write_entry and session-reference redaction run. Files are created
only in a fresh temporary directory, PostgreSQL scheduling is a counted stub,
and the final report is written only to the explicitly supplied output path.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import random
import statistics
import sys
import tempfile
import time
import types

sys.dont_write_bytecode = True

REPO = Path(__file__).resolve().parents[2]
MODES = ("jsonl_disabled", "jsonl_fsync", "jsonl_noop_fsync_diagnostic")
PADDING_SIZES = (256, 2048)
EVENT_COUNTS = (1, 4)
SHUFFLE_SEED = 20261012
SYNTHETIC_SESSION = "agent-00000000-000-" + "a" * 20
SYNTHETIC_AGENT = "00000000-0000-0000-0000-000000000000"
SOURCE_FILES = (
    "src/audit_log.py",
    "src/logging_utils.py",
    "src/mcp_handlers/identity/stable_session.py",
)


def load_isolated_sources(scratch_log: Path):
    """Bypass src and identity initializers; never import DB/secret modules."""
    if "src" in sys.modules:
        raise RuntimeError("Run this benchmark in a fresh Python process")
    # audit_log constructs its default logger at import time. Redirect that
    # constructor too, even though measured loggers always receive a path.
    os.environ["UNITARES_AUDIT_LOG"] = str(scratch_log)
    for name, directory in (
        ("src", REPO / "src"),
        ("src.mcp_handlers", REPO / "src/mcp_handlers"),
        ("src.mcp_handlers.identity", REPO / "src/mcp_handlers/identity"),
    ):
        module = types.ModuleType(name)
        module.__path__ = [str(directory)]
        sys.modules[name] = module
    logging_utils = importlib.import_module("src.logging_utils")
    stable_session = importlib.import_module("src.mcp_handlers.identity.stable_session")
    audit_log = importlib.import_module("src.audit_log")
    for module, relative in zip(
        (audit_log, logging_utils, stable_session), SOURCE_FILES
    ):
        if Path(module.__file__).resolve() != (REPO / relative).resolve():
            raise RuntimeError(f"Unexpected import location: {relative}")
    return audit_log, stable_session


def require_equal(actual, expected, description):
    if actual != expected:
        raise RuntimeError(f"{description}: observed {actual!r}, expected {expected!r}")


@contextmanager
def counted_fsync(audit_log, mode, counters):
    original = audit_log.os.fsync

    def counted(fd):
        counters["fsync_calls"] += 1
        if mode != "jsonl_noop_fsync_diagnostic":
            original(fd)
            counters["os_fsync_completed"] += 1

    audit_log.os.fsync = counted
    try:
        yield
    finally:
        audit_log.os.fsync = original


def percentile(values, fraction):
    """Linear interpolation between ordered samples, including endpoints."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (position - lower) * (ordered[upper] - ordered[lower])


def latency_summary(values):
    return {
        "sample_count": len(values),
        "mean_ns": statistics.fmean(values),
        "p50_ns": percentile(values, 0.5),
        "p95_ns": percentile(values, 0.95),
    }


def cell_id(mode, padding_bytes, events):
    return f"{mode}__padding_{padding_bytes}__events_{events}"


def measure_block(
    audit_log, stable_session, directory, mode, padding_bytes, events, updates
):
    """Time groups of real writes, then verify the swallowed-error path worked."""
    path = directory / "events.jsonl"
    os.environ["UNITARES_AUDIT_WRITE_JSONL"] = "0" if mode == "jsonl_disabled" else "1"
    logger = audit_log.AuditLogger(log_file=path)
    expected_session = stable_session.audit_reference(SYNTHETIC_SESSION)
    entries = [
        audit_log.AuditEntry(
            timestamp="2026-10-12T00:00:00+00:00",
            agent_id=SYNTHETIC_AGENT,
            event_type="synthetic_base" if index == 0 else f"synthetic_shadow_{index}",
            confidence=0.0,
            details={"synthetic": True, "padding": "x" * padding_bytes},
            metadata={"purpose": "offline_audit_cost"},
            session_id=SYNTHETIC_SESSION,
        )
        for index in range(events)
    ]
    counters = {"pg_schedule_stub_calls": 0, "fsync_calls": 0, "os_fsync_completed": 0}

    def pg_stub(entry):
        counters["pg_schedule_stub_calls"] += 1
        require_equal(
            entry["session_id"], expected_session, "PG stub session redaction"
        )

    logger._schedule_postgres_write = pg_stub
    latencies = []
    with counted_fsync(audit_log, mode, counters):
        for _ in range(updates):
            start = time.perf_counter_ns()
            for entry in entries:
                logger._write_entry(entry)
            latencies.append(time.perf_counter_ns() - start)

    payload = path.read_bytes() if path.exists() else b""
    rows = [json.loads(line) for line in payload.splitlines()]
    expected_events = events * updates
    expected_rows = 0 if mode == "jsonl_disabled" else expected_events
    expected_os_fsync = expected_events if mode == "jsonl_fsync" else 0
    require_equal(len(rows), expected_rows, "JSONL row count")
    require_equal(
        counters["pg_schedule_stub_calls"], expected_events, "PG scheduling count"
    )
    require_equal(counters["fsync_calls"], expected_rows, "fsync invocation count")
    require_equal(
        counters["os_fsync_completed"], expected_os_fsync, "completed OS fsync count"
    )
    for index, row in enumerate(rows):
        require_equal(
            row["event_type"], entries[index % events].event_type, "event sequence"
        )
        require_equal(row["details"]["padding"], "x" * padding_bytes, "payload padding")
        require_equal(row["session_id"], expected_session, "JSONL session redaction")
        require_equal(row["agent_id"], SYNTHETIC_AGENT, "synthetic agent id")
    require_equal(bool(payload), bool(expected_rows), "JSONL byte presence")
    return {
        "logical_updates": updates,
        "event_writes": expected_events,
        "jsonl_rows": len(rows),
        "jsonl_bytes": len(payload),
        **counters,
        "latencies_ns_per_logical_update": latencies,
        "latency": latency_summary(latencies),
        "integrity_checks": "matched_expected_rows_bytes_presence_event_order_redaction_and_counts",
    }


def run_benchmark(iterations=200, rounds=3):
    if not 1 <= iterations <= 10000 or not 1 <= rounds <= 30:
        raise ValueError("iterations must be 1..10000 and rounds must be 1..30")
    warmup_updates = min(20, iterations)
    rng = random.Random(SHUFFLE_SEED)
    cells = [
        (mode, size, events)
        for mode in MODES
        for size in PADDING_SIZES
        for events in EVENT_COUNTS
    ]
    records = []
    with tempfile.TemporaryDirectory(
        prefix="unitares-synthetic-audit-cost-"
    ) as temporary:
        root = Path(temporary)
        audit_log, stable_session = load_isolated_sources(root / "unused-default.jsonl")
        for round_index in range(rounds):
            order = cells.copy()
            rng.shuffle(order)
            for block_index, (mode, size, events) in enumerate(order):
                identifier = cell_id(mode, size, events)
                block_root = root / f"round-{round_index}-block-{block_index}"
                warmup = measure_block(
                    audit_log,
                    stable_session,
                    block_root / "warmup",
                    mode,
                    size,
                    events,
                    warmup_updates,
                )
                measurement = measure_block(
                    audit_log,
                    stable_session,
                    block_root / "measurement",
                    mode,
                    size,
                    events,
                    iterations,
                )
                records.append(
                    {
                        "cell_id": identifier,
                        "round": round_index,
                        "block_order": block_index,
                        "mode": mode,
                        "padding_bytes": size,
                        "events_per_logical_update": events,
                        "warmup_excluded": {
                            key: value
                            for key, value in warmup.items()
                            if key not in ("latency", "latencies_ns_per_logical_update")
                        },
                        **measurement,
                    }
                )
    summaries = []
    count_fields = (
        "logical_updates",
        "event_writes",
        "jsonl_rows",
        "jsonl_bytes",
        "pg_schedule_stub_calls",
        "fsync_calls",
        "os_fsync_completed",
    )
    for mode, size, events in cells:
        identifier = cell_id(mode, size, events)
        selected = [block for block in records if block["cell_id"] == identifier]
        raw = [
            latency
            for block in selected
            for latency in block["latencies_ns_per_logical_update"]
        ]
        summaries.append(
            {
                "cell_id": identifier,
                "mode": mode,
                "padding_bytes": size,
                "events_per_logical_update": events,
                "latency": latency_summary(raw),
                **{
                    field: sum(block[field] for block in selected)
                    for field in count_fields
                },
            }
        )
    return {
        "schema": "audit.synthetic_cost.v1",
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "source_sha256": {
            name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES
        },
        "configuration": {
            "iterations_per_block": iterations,
            "rounds": rounds,
            "warmup_updates_per_block_excluded": warmup_updates,
            "shuffle_seed": SHUFFLE_SEED,
            "balanced_cells_per_round": len(cells),
            "padding_sizes_bytes": PADDING_SIZES,
            "event_counts_per_logical_update": EVENT_COUNTS,
            "percentile_method": "linear interpolation over ordered raw per-update samples",
        },
        "summaries": summaries,
        "blocks": records,
        "scope": [
            "Real AuditLogger._write_entry, dataclass conversion, audit_reference, JSON encoding, open/append, flock, flush and os.fsync; constructor and payload construction are outside timing.",
            "Fresh temporary synthetic JSONL files only; no existing audit files, secrets, study data, server or database are accessed.",
            "PostgreSQL scheduling is replaced by a counted validating stub in every mode; asynchronous scheduling and database persistence costs are excluded.",
            "Raw samples time one logical update of one base event or four events (one base plus three synthetic shadows); padding sizes are not total encoded row sizes.",
            "Warmup writes use separate files and are excluded from all measured summaries and raw samples.",
            "Normal mode invokes Python os.fsync, not macOS F_FULLFSYNC; durability behavior depends on the local filesystem and OS cache.",
            "No-op fsync is a diagnostic contrast only, not a recommendation for production durability.",
            "Sequential local microbenchmark, not full handler latency, PostgreSQL throughput, concurrent load, or a live shadow overhead estimate.",
            "No timing thresholds, empirical pass/fail gate or instrument/policy changes; count mismatches fail because AuditLogger can swallow write errors.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    report = run_benchmark(args.iterations, args.rounds)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    print(
        json.dumps(
            {
                "output": str(output),
                "measured_blocks": len(report["blocks"]),
                "measured_logical_updates": sum(
                    s["logical_updates"] for s in report["summaries"]
                ),
            }
        )
    )


if __name__ == "__main__":
    main()

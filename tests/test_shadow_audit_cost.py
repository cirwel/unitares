"""Tiny real-file runs verify measurement integrity, never timing thresholds."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/analysis/shadow_audit_cost.py"


@pytest.fixture(scope="module")
def tiny_report(tmp_path_factory):
    directory = tmp_path_factory.mktemp("synthetic-audit-cost")
    output = directory / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--iterations",
            "2",
            "--rounds",
            "2",
        ],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(directory)},
    )
    assert json.loads(result.stdout)["measured_blocks"] == 24
    return json.loads(output.read_text())


def test_balanced_blocks_and_warmup_exclusion(tiny_report):
    assert len(tiny_report["summaries"]) == 12
    assert len(tiny_report["blocks"]) == 24
    assert tiny_report["configuration"]["warmup_updates_per_block_excluded"] == 2
    for round_index in range(2):
        blocks = [b for b in tiny_report["blocks"] if b["round"] == round_index]
        assert len({b["cell_id"] for b in blocks}) == 12
    for summary in tiny_report["summaries"]:
        assert summary["logical_updates"] == 4
        assert summary["latency"]["sample_count"] == 4
    for block in tiny_report["blocks"]:
        assert len(block["latencies_ns_per_logical_update"]) == 2
        assert block["warmup_excluded"]["logical_updates"] == 2


def test_actual_jsonl_rows_fsync_and_pg_counts(tiny_report):
    for block in tiny_report["blocks"]:
        events = 2 * block["events_per_logical_update"]
        rows = 0 if block["mode"] == "jsonl_disabled" else events
        assert block["event_writes"] == events
        assert block["jsonl_rows"] == rows
        assert block["pg_schedule_stub_calls"] == events
        assert block["fsync_calls"] == rows
        assert block["os_fsync_completed"] == (
            events if block["mode"] == "jsonl_fsync" else 0
        )
        assert bool(block["jsonl_bytes"]) == bool(rows)
        if rows:
            assert block["jsonl_bytes"] > rows * block["padding_bytes"]
        assert block["integrity_checks"].startswith("matched_expected")


def test_provenance_and_scoped_claims(tiny_report):
    assert len(tiny_report["source_sha256"]) == 3
    assert all(len(digest) == 64 for digest in tiny_report["source_sha256"].values())
    assert tiny_report["runtime"]["python"]
    assert any(
        "not macOS F_FULLFSYNC" in statement for statement in tiny_report["scope"]
    )
    assert any(
        "not a recommendation" in statement for statement in tiny_report["scope"]
    )


def test_swallowed_jsonl_failure_invalidates_measurement(tmp_path):
    """A swallowed fsync error must fail the benchmark, not look exceptionally fast."""
    launcher = r"""
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("cost_probe", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
audit, stable = module.load_isolated_sources(Path(sys.argv[2]) / "unused-default.jsonl")
def fail_fsync(fd):
    raise OSError("synthetic fsync fault")
audit.os.fsync = fail_fsync
try:
    module.measure_block(audit, stable, Path(sys.argv[2]), "jsonl_fsync", 256, 1, 1)
except RuntimeError as exc:
    assert "completed OS fsync count" in str(exc), str(exc)
else:
    raise AssertionError("swallowed fsync failure was not detected")
"""
    subprocess.run(
        [sys.executable, "-c", launcher, str(SCRIPT), str(tmp_path / "fault")],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

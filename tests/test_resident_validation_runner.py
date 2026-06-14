"""Resident validation canary runner tests."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from src.resident_validation import ResidentProfile
from src.resident_validation_runner import build_canary_ticks, next_tick_index


NOW = datetime(2026, 6, 14, 20, 5, tzinfo=timezone.utc)


def test_next_tick_index_tracks_matching_cohort_and_resident_only(tmp_path: Path) -> None:
    state_path = tmp_path / "ticks.jsonl"
    state_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "cohort_id": "rv-2026-06",
                        "tick_index": 1,
                        "resident": {"id": "resident-dogfood-1"},
                    }
                ),
                json.dumps(
                    {
                        "cohort_id": "other-cohort",
                        "tick_index": 99,
                        "resident": {"id": "resident-dogfood-1"},
                    }
                ),
                json.dumps(
                    {
                        "cohort_id": "rv-2026-06",
                        "tick_index": 4,
                        "resident": {"id": "other-resident"},
                    }
                ),
                json.dumps(
                    {
                        "cohort_id": "rv-2026-06",
                        "tick_index": 2,
                        "resident": {"id": "resident-dogfood-1"},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert next_tick_index(state_path, "rv-2026-06", "resident-dogfood-1") == 3


def test_build_canary_ticks_appends_sequential_bounded_envelopes(tmp_path: Path) -> None:
    state_path = tmp_path / "ticks.jsonl"
    profile = ResidentProfile(
        cohort_id="rv-2026-06",
        resident_id="resident-dogfood-1",
        resident_name="Resident Dogfood Canary",
        role="dogfood_probe",
        cadence_seconds=600,
        observation_scope=("repo", "ci"),
    )

    ticks = build_canary_ticks(
        profile,
        state_path=state_path,
        count=2,
        observation="No actionable friction observed.",
        prediction="Next tick remains bounded.",
        confidence=0.72,
        now=NOW,
    )

    assert [tick["tick_index"] for tick in ticks] == [1, 2]
    assert all(tick["authority"]["deploy_authority"] is False for tick in ticks)
    assert state_path.exists()
    saved = [json.loads(line) for line in state_path.read_text(encoding="utf-8").splitlines()]
    assert [tick["tick_id"] for tick in saved] == [tick["tick_id"] for tick in ticks]


def test_cli_can_emit_two_canary_ticks_without_process_update(tmp_path: Path) -> None:
    state_path = tmp_path / "resident-canary.jsonl"
    script = Path("scripts/diagnostics/resident_validation_canary.py")

    completed = subprocess.run(
        [
            "python3",
            str(script),
            "--cohort-id",
            "rv-2026-06",
            "--resident-id",
            "resident-dogfood-1",
            "--resident-name",
            "Resident Dogfood Canary",
            "--role",
            "dogfood_probe",
            "--cadence-seconds",
            "600",
            "--observation",
            "No actionable friction observed.",
            "--prediction",
            "Next tick remains bounded.",
            "--confidence",
            "0.72",
            "--observed-at",
            "2026-06-14T20:05:00+00:00",
            "--state-path",
            str(state_path),
            "--count",
            "2",
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert [tick["tick_index"] for tick in payload["ticks"]] == [1, 2]
    assert payload["resident_id"] == "resident-dogfood-1"
    assert payload["state_path"] == str(state_path)
    assert len(state_path.read_text(encoding="utf-8").splitlines()) == 2

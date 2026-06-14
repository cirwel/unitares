"""Resident validation cohort protocol tests."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.resident_validation import (
    ResidentProfile,
    build_process_update_kwargs,
    build_tick_envelope,
    stable_tick_id,
)


NOW = datetime(2026, 6, 14, 19, 45, tzinfo=timezone.utc)


def test_resident_profile_rejects_direct_deploy_authority() -> None:
    """V0 validation residents are probes, not deploy actors."""
    with pytest.raises(ValueError, match="deploy authority"):
        ResidentProfile(
            cohort_id="rv-2026-06",
            resident_id="resident-builder-1",
            resident_name="Resident Builder Canary",
            role="builder",
            cadence_seconds=300,
            observation_scope=("repo", "ci"),
            allowed_effects=("emit_finding", "deploy"),
        )


def test_tick_envelope_carries_prediction_heartbeat_and_authority_boundary() -> None:
    profile = ResidentProfile(
        cohort_id="rv-2026-06",
        resident_id="resident-dogfood-1",
        resident_name="Resident Dogfood Canary",
        role="dogfood_probe",
        cadence_seconds=600,
        observation_scope=("hermes_cron", "unitares_mcp"),
    )

    envelope = build_tick_envelope(
        profile,
        tick_index=3,
        observation="No actionable friction observed in this tick.",
        prediction="Next tick should also complete without mutation.",
        confidence=0.74,
        now=NOW,
    )

    assert envelope["event_type"] == "resident_validation_tick"
    assert envelope["cohort_id"] == "rv-2026-06"
    assert envelope["resident"]["role"] == "dogfood_probe"
    assert envelope["heartbeat"] == {
        "cadence_seconds": 600,
        "observed_at": "2026-06-14T19:45:00+00:00",
        "next_due_at": "2026-06-14T19:55:00+00:00",
    }
    assert envelope["prediction"] == {
        "claim": "Next tick should also complete without mutation.",
        "confidence": 0.74,
        "horizon_seconds": 600,
        "decision_action": "observe",
    }
    assert envelope["authority"]["deploy_authority"] is False
    assert "merge" not in envelope["authority"]["allowed_effects"]
    assert "emit_finding" in envelope["authority"]["allowed_effects"]


def test_stable_tick_id_depends_on_resident_and_tick_not_wallclock() -> None:
    first = stable_tick_id("rv-2026-06", "resident-dogfood-1", 7)
    second = stable_tick_id("rv-2026-06", "resident-dogfood-1", 7)
    different_tick = stable_tick_id("rv-2026-06", "resident-dogfood-1", 8)

    assert first == second
    assert first != different_tick
    assert first.startswith("rv_")
    assert len(first) == len("rv_") + 16


def test_process_update_kwargs_preserve_prediction_and_bounded_authority() -> None:
    profile = ResidentProfile(
        cohort_id="rv-2026-06",
        resident_id="resident-steward-1",
        resident_name="Resident Steward Canary",
        role="steward",
        cadence_seconds=900,
        observation_scope=("findings", "kg", "dialectic"),
    )
    envelope = build_tick_envelope(
        profile,
        tick_index=1,
        observation="Finding queue inspected; no mutation required.",
        prediction="No dialectic escalation should be needed in the next horizon.",
        confidence=0.68,
        now=NOW,
    )

    kwargs = build_process_update_kwargs(envelope)

    assert kwargs["epistemic_class"] == "prediction"
    assert kwargs["task_type"] == "exploration"
    assert kwargs["confidence"] == 0.68
    assert kwargs["provenance_context"]["harness_type"] == "resident_validation_cohort"
    assert kwargs["provenance_context"]["cohort_id"] == "rv-2026-06"
    assert kwargs["recent_tool_results"] == [
        {
            "kind": "tool_call",
            "tool": "resident_validation_tick",
            "summary": "Resident Steward Canary tick 1 observed without governed mutation",
        }
    ]


def test_cli_emits_json_and_appends_state(tmp_path: Path) -> None:
    state_path = tmp_path / "resident_ticks.jsonl"
    script = Path("scripts/diagnostics/resident_validation_tick.py")

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
            "--tick-index",
            "2",
            "--observation",
            "No actionable friction observed.",
            "--prediction",
            "Next tick remains bounded.",
            "--confidence",
            "0.72",
            "--observed-at",
            "2026-06-14T19:45:00+00:00",
            "--state-path",
            str(state_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["event_type"] == "resident_validation_tick"
    assert payload["resident"]["name"] == "Resident Dogfood Canary"
    assert state_path.read_text(encoding="utf-8").strip() == json.dumps(payload, sort_keys=True)
